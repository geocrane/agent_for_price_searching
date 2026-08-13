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
    preferred_domains: list[str] = Field(default_factory=list)   # свои проверенные поставщики
    blocked_domains: list[str] = Field(default_factory=list)     # мусорные для этой отрасли
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
    """Подмешать домены профиля в рейтинг источников. Общий рейтинг НЕ заменяем, а дополняем.

    Приоритетные домены получают вес официального источника, запрещённые — попадают в чёрный
    список (в `discovery/rerank` он даёт вес 0 и кандидата отбрасывает).
    """
    preferred, blocked = profile.normalized_domains()
    sources = dict(settings.sources or {})
    if blocked:
        sources["blacklist"] = sorted(set(sources.get("blacklist") or []) | set(blocked))
    if preferred:
        tiers = {k: dict(v) for k, v in (sources.get("tiers") or {}).items()}
        tier = dict(tiers.get(1) or {"weight": PREFERRED_WEIGHT, "domains": []})
        tier["weight"] = max(float(tier.get("weight", PREFERRED_WEIGHT)), PREFERRED_WEIGHT)
        tier["domains"] = sorted(set(tier.get("domains") or []) | set(preferred))
        tiers[1] = tier
        sources["tiers"] = tiers
    settings.sources = sources
    settings.extract.strict_analog_judge = profile.strict_analog
    if preferred or blocked:
        log.info("Профиль %r: приоритетных доменов=%d, запрещённых=%d",
                 profile.name, len(preferred), len(blocked))
