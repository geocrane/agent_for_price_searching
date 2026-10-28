# -*- coding: utf-8 -*-
"""Профиль поиска: настройки пользователя под его тип товаров, переживающие перезапуск.

У каждого пользователя свой предмет закупки и свой формат поиска: один ищет стройматериалы в
своём городе, другой — серверные комплектующие оптом. Раньше это задавалось одной строкой-аффиксом
в поле интерфейса, которая терялась при перезагрузке страницы, а рейтинг источников был общий на
всех (`config/sources.yaml`).

Профиль хранится в той же SQLite, что история и кэш разбора: одно место состояния на пользователя,
которое переносится вместе с ним (Э6 — независимое хранилище). Профилей может быть несколько,
активен один.

Применение к прогону — `apply_to_settings`: домены пользователя подмешиваются в общий рейтинг
источников, не заменяя его. Приоритетные получают вес выше любого тира, запрещённые — ноль.
"""
import json
import sqlite3
import time
from pathlib import Path

from pydantic import BaseModel, Field

from .obs.log import get_logger

log = get_logger("profile")

DEFAULT_NAME = "по умолчанию"
PREFERRED_WEIGHT = 1.0          # приоритетный домен пользователя доверяем как официальному источнику

# Уровни домена в реестре сайтов (настройки → «Сайты»).
LEVEL_PREFERRED = "preferred"   # вес официального источника, идёт первым
LEVEL_NORMAL = "normal"         # как все: вес из общего рейтинга sources.yaml
LEVEL_LOWERED = "lowered"       # смотрим, но в последнюю очередь и с урезанным доверием
LEVEL_BANNED = "banned"         # не грузим вовсе (чёрный список)
LEVELS = (LEVEL_PREFERRED, LEVEL_NORMAL, LEVEL_LOWERED, LEVEL_BANNED)


def _norm_domain(raw: str) -> str:
    """Домен в сравнимом виде: нижний регистр, без схемы, без www, без пути и порта."""
    d = str(raw or "").strip().lower()
    d = d.split("//")[-1].split("/")[0].split(":")[0].removeprefix("www.")
    return d


def tld_forms(raw: str) -> set[str]:
    """Зона в обеих формах: юникод и punycode.

    Пользователь пишет «рф», а поисковая выдача отдаёт `xn--p1ai` (в реальной сессии домены
    именно такие: `xn--80adgebororwy.xn--p1ai`). Сравнивать надо и то, и другое, иначе правило
    молча не работает — худший вид неработающей настройки.
    """
    z = str(raw or "").strip().lower().lstrip(".")
    if not z:
        return set()
    out = {z}
    try:
        out.add(z.encode("idna").decode("ascii"))
    except (UnicodeError, ValueError):
        pass
    if z.startswith("xn--"):
        try:
            out.add(z.encode("ascii").decode("idna"))
        except (UnicodeError, ValueError):
            pass
    return out

_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_profile (
    name       TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    is_active  INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


class SearchProfile(BaseModel):
    """Что пользователь настраивает под свой тип товаров."""
    name: str = DEFAULT_NAME
    affix: str = "цена купить"          # фраза-уточнение к каждому запросу (город, «оптом», «прайс»)
    # Модель переписывает наименования в «магазинные» запросы. По умолчанию ВЫКЛЮЧЕНО: это
    # отдельный пакет платных вызовов до начала поиска, и включает его пользователь осознанно.
    use_llm_queries: bool = False
    # Устаревшие плоские списки: остаются ради совместимости с сохранёнными профилями и с
    # полями «через запятую» в интерфейсе. Источник правды — `domain_ratings` (см. `ratings`).
    preferred_domains: list[str] = Field(default_factory=list)   # свои проверенные поставщики
    blocked_domains: list[str] = Field(default_factory=list)     # мусорные для этой отрасли
    # Домен → уровень: preferred | normal | lowered | banned. «Пониженный» — не то же самое, что
    # «запрещённый»: сайт остаётся в работе, но с урезанным весом и в конце очереди загрузки.
    # Раньше градации не было вовсе — домен либо приоритетный, либо выброшенный.
    domain_ratings: dict[str, str] = Field(default_factory=dict)
    # Зоны. Пустой список разрешённых = ограничения нет (не «запрещено всё»). Нужны, потому что
    # перечислять поштучно все домены чужой страны бессмысленно: их всегда находится ещё один.
    allowed_tlds: list[str] = Field(default_factory=list)        # напр. ru, рф, com
    banned_tlds: list[str] = Field(default_factory=list)         # напр. kz, ua, by
    # Вторая страница выдачи — не «глубина поиска», а реакция на пустой результат: она
    # запрашивается ТОЛЬКО по позициям, где на первой странице не нашлось ни одной цены.
    # Нашли цену — глубже не идём: каждая страница это отдельный оплачиваемый запрос к поиску.
    second_page_if_empty: bool = True
    strict_analog: bool = True          # отсеивать аналоги с доказанным расхождением характеристик
    # Сколько РАЗНЫХ доменов с ценой достаточно, чтобы по позиции остановиться и не трогать
    # остальные найденные сайты. 0 — обрабатывать всё (по умолчанию): разброс цен и коридор рынка
    # считаются по всей выдаче. Значение 2–3 экономит бОльшую часть загрузок и вызовов модели, но
    # сужает основу для разброса, поэтому включает это пользователь, а не мы за него.
    enough_confirmations: int = 0

    @property
    def max_pages(self) -> int:
        """Предел глубины выдачи для движка. Дальше второй страницы не ходим никогда."""
        return 2 if self.second_page_if_empty else 1

    def ratings(self) -> dict[str, str]:
        """Эффективные уровни доменов: старые плоские списки + явные `domain_ratings`.

        Явный уровень важнее унаследованного списка: пользователь только что нажал его руками.
        """
        out: dict[str, str] = {}
        pref, blocked = self.normalized_domains()
        for d in pref:
            out[d] = LEVEL_PREFERRED
        for d in blocked:
            out[d] = LEVEL_BANNED
        for raw, level in (self.domain_ratings or {}).items():
            d = _norm_domain(raw)
            if d and level in LEVELS:
                out[d] = level
        return {d: lv for d, lv in out.items() if lv != LEVEL_NORMAL}

    def normalized_domains(self) -> tuple[list[str], list[str]]:
        """Домены в сравнимом виде: нижний регистр, без схемы, без www и пути."""
        def norm(items):
            out = []
            for raw in items or []:
                d = str(raw).strip().lower()
                d = d.split("//")[-1].split("/")[0].removeprefix("www.")
                if d:
                    out.append(d)
            return out
        return norm(self.preferred_domains), norm(self.blocked_domains)


def _db(cfg=None):
    """Соединение с хранилищем профилей или None, если оно отключено/недоступно."""
    path = getattr(cfg, "db_path", None) if cfg is not None else None
    if cfg is not None and not getattr(cfg, "enabled", True):
        return None
    if not path:
        from .config import load_settings
        st = load_settings().storage
        if not st.enabled:
            return None
        path = st.db_path
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.executescript(_SCHEMA)
        return conn
    except (sqlite3.Error, OSError) as exc:
        log.warning("Хранилище профилей недоступно: %s — работаю на профиле по умолчанию", exc)
        return None


def load_active(cfg=None) -> SearchProfile:
    """Активный профиль. Если его нет или база недоступна — профиль по умолчанию."""
    conn = _db(cfg)
    if conn is None:
        return SearchProfile()
    try:
        row = conn.execute(
            "SELECT data FROM search_profile WHERE is_active=1 ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return SearchProfile()
        return SearchProfile(**json.loads(row[0]))
    except (sqlite3.Error, ValueError, TypeError) as exc:
        log.warning("Профиль не прочитан: %s — беру профиль по умолчанию", exc)
        return SearchProfile()
    finally:
        conn.close()


def save(profile: SearchProfile, *, make_active: bool = True, cfg=None) -> bool:
    """Сохранить профиль (и сделать активным). Возвращает True, если записан."""
    conn = _db(cfg)
    if conn is None:
        return False
    try:
        with conn:
            if make_active:
                conn.execute("UPDATE search_profile SET is_active=0")
            conn.execute(
                "INSERT INTO search_profile(name, data, is_active, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET data=excluded.data, "
                "is_active=excluded.is_active, updated_at=excluded.updated_at",
                (profile.name, profile.model_dump_json(), 1 if make_active else 0,
                 time.strftime("%Y-%m-%dT%H:%M:%S")))
        log.info("Профиль поиска сохранён: %r (активен=%s)", profile.name, make_active)
        return True
    except sqlite3.Error as exc:
        log.warning("Профиль не сохранён: %s", exc)
        return False
    finally:
        conn.close()


def list_names(cfg=None) -> list[str]:
    conn = _db(cfg)
    if conn is None:
        return []
    try:
        return [r[0] for r in conn.execute(
            "SELECT name FROM search_profile ORDER BY updated_at DESC")]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def apply_to_settings(profile: SearchProfile, settings) -> None:
    """Подмешать домены и зоны профиля в рейтинг источников. Общий рейтинг НЕ заменяем, а дополняем.

    Приоритетные домены получают вес официального источника, пониженные — множитель к весу и
    последнее место в очереди загрузки, запрещённые — чёрный список (в `discovery/rerank` он даёт
    вес 0 и кандидата отбрасывает). Зоны работают тем же механизмом веса 0.
    """
    levels = profile.ratings()
    preferred = sorted(d for d, lv in levels.items() if lv == LEVEL_PREFERRED)
    lowered = sorted(d for d, lv in levels.items() if lv == LEVEL_LOWERED)
    banned = sorted(d for d, lv in levels.items() if lv == LEVEL_BANNED)
    allowed_z = {f for z in (profile.allowed_tlds or []) for f in tld_forms(z)}
    banned_z = {f for z in (profile.banned_tlds or []) for f in tld_forms(z)}

    sources = dict(settings.sources or {})
    if banned:
        sources["blacklist"] = sorted(set(sources.get("blacklist") or []) | set(banned))
    if lowered:
        sources["lowered"] = sorted(set(sources.get("lowered") or []) | set(lowered))
    if allowed_z:
        sources["allowed_tlds"] = sorted(allowed_z)
    if banned_z:
        sources["banned_tlds"] = sorted(banned_z)
    if preferred:
        tiers = {k: dict(v) for k, v in (sources.get("tiers") or {}).items()}
        tier = dict(tiers.get(1) or {"weight": PREFERRED_WEIGHT, "domains": []})
        tier["weight"] = max(float(tier.get("weight", PREFERRED_WEIGHT)), PREFERRED_WEIGHT)
        tier["domains"] = sorted(set(tier.get("domains") or []) | set(preferred))
        tiers[1] = tier
        sources["tiers"] = tiers
    settings.sources = sources
    settings.extract.strict_analog_judge = profile.strict_analog
    if preferred or lowered or banned or allowed_z or banned_z:
        log.info("Профиль %r: приоритетных=%d, пониженных=%d, запрещённых=%d, "
                 "разрешённые зоны=%s, запрещённые зоны=%s", profile.name,
                 len(preferred), len(lowered), len(banned),
                 ", ".join(sorted(profile.allowed_tlds or [])) or "—",
                 ", ".join(sorted(profile.banned_tlds or [])) or "—")
