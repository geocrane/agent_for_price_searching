# -*- coding: utf-8 -*-
"""Реестр источников и проверка цитат: «каждое утверждение — со ссылкой».

Требование пользователя: любое утверждение о цене, преимуществе, недостатке, сравнении или
наличии данных должно опираться на конкретную ссылку, из которой сделан вывод. Просить об этом
модель в промпте — необходимо, но недостаточно: она забывает и выдумывает. Поэтому механика
двойная:

  1) КАЖДЫЙ прочитанный источник получает короткий идентификатор `[S1]`, `[S2]`… и агент видит
     его прямо в наблюдении — цитировать становится проще, чем не цитировать;
  2) готовый ответ ПРОВЕРЯЕТСЯ кодом: ссылки на несуществующие идентификаторы (`unknown`) —
     галлюцинация; утверждения без ссылок (`uncited`) — повод переспросить модель.

Эвристика «утверждения» намеренно общая: цена/год/оценочное слово. Никакой привязки к товарной
категории или конкретному порталу (правило universal-tool-any-file).
"""
import re
import time
from dataclasses import dataclass, field

from ..obs.log import get_logger

log = get_logger("research.sources")

_SID_RE = re.compile(r"\[S(\d+)\]")

# Признаки утверждения, которое обязано быть подкреплено ссылкой.
from ..extract.price import PRICE_RE as _PRICE_RE   # единое определение цены
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_CLAIM_WORDS = (
    "лучше", "хуже", "дешевле", "дороже", "выгоднее", "надёжнее", "надежнее", "качественнее",
    "громче", "тише", "мощнее", "легче", "тяжелее", "удобнее", "точнее", "быстрее",
    "есть данные", "данные есть", "покрывает", "доступн", "содержит", "хранит", "публикует",
    "позволяет выгрузить", "требует регистрац", "платн", "бесплатн", "отзыв", "обзор",
    "рекоменд", "жалуются", "хвалят",
)
_CLAIM_RE = re.compile("|".join(re.escape(w) for w in _CLAIM_WORDS), re.I | re.U)

# Строки, к которым требование ссылки неприменимо: заголовки, разделители, служебные блоки,
# сам список источников и строки-вопросы к пользователю.
_SKIP_RE = re.compile(r"^\s*(?:#{1,6}\s|\|[\s:-]+\||[-*_]{3,}\s*$|источник|sources?\b)", re.I | re.U)


def domain_of(url: str) -> str:
    """Домен без схемы и www (в проекте это каноничная форма домена)."""
    d = (url or "").split("//")[-1].split("/")[0].split("?")[0].lower()
    return d.removeprefix("www.")


@dataclass
class Source:
    """Прочитанный источник: то, на что агент имеет право ссылаться."""
    sid: str
    url: str
    domain: str = ""
    title: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"sid": self.sid, "url": self.url, "domain": self.domain,
                "title": self.title, "ts": self.ts}


class SourceRegistry:
    """Источники одного диалога. Идентификаторы сквозные и не переиспользуются.

    Стабильность важна: ссылка `[S3]`, поставленная три хода назад, должна указывать на тот же
    источник и сегодня — иначе история диалога врёт задним числом.
    """

    def __init__(self, sources: list[Source] | None = None) -> None:
        self._by_sid: dict[str, Source] = {}
        self._by_url: dict[str, str] = {}
        self._n = 0
        for s in sources or []:
            self._by_sid[s.sid] = s
            self._by_url[s.url] = s.sid
            try:
                self._n = max(self._n, int(s.sid.lstrip("S")))
            except ValueError:
                pass

    def add(self, url: str, title: str = "", domain: str = "") -> str:
        """Зарегистрировать источник и получить его `Sn`. Повторный url → прежний идентификатор."""
        url = (url or "").strip()
        if not url:
            return ""
        known = self._by_url.get(url)
        if known:
            src = self._by_sid[known]
            if title and not src.title:                # уточнили заголовок позже — дополняем
                src.title = title
            return known
        self._n += 1
        sid = "S%d" % self._n
        src = Source(sid=sid, url=url, domain=domain or domain_of(url), title=title)
        self._by_sid[sid] = src
        self._by_url[url] = sid
        log.debug("Источник %s: %s", sid, url)
        return sid

    def get(self, sid: str) -> Source | None:
        return self._by_sid.get((sid or "").strip().upper())

    def known(self) -> list[Source]:
        return sorted(self._by_sid.values(), key=lambda s: int(s.sid.lstrip("S")))

    def __len__(self) -> int:
        return len(self._by_sid)

    def label(self, url: str) -> str:
        """`[Sn]` для уже известного url (или пусто) — для вставки в наблюдения."""
        sid = self._by_url.get((url or "").strip())
        return "[%s]" % sid if sid else ""

    def render(self, only: list[str] | None = None) -> str:
        """Блок «Источники» для ответа. Пишет КОД, не модель — так он всегда верен."""
        items = [s for s in self.known() if not only or s.sid in set(only)]
        if not items:
            return ""
        lines = ["**Источники**"]
        for s in items:
            title = (s.title or s.domain or s.url).strip()
            lines.append("- [%s] %s — %s" % (s.sid, title[:120], s.url))
        return "\n".join(lines)


def cited_ids(text: str) -> list[str]:
    """Все `[Sn]`, встреченные в тексте (в порядке появления, без дублей)."""
    out, seen = [], set()
    for m in _SID_RE.finditer(text or ""):
        sid = "S" + m.group(1)
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def is_claim(line: str) -> bool:
    """Требует ли строка ссылки: цена, год или оценочное/фактическое утверждение."""
    s = (line or "").strip()
    if len(s) < 12 or _SKIP_RE.match(s):
        return False
    return bool(_PRICE_RE.search(s) or _YEAR_RE.search(s) or _CLAIM_RE.search(s))


def validate_citations(text: str, registry: SourceRegistry) -> dict:
    """Проверить ответ: что процитировано, что выдумано, что осталось без ссылки.

    Возвращает {"used": [...], "unknown": [...], "uncited": [строки], "ok": bool}.
    Список источников в конце ответа сам по себе утверждением не считается.
    """
    used = cited_ids(text)
    unknown = [sid for sid in used if registry.get(sid) is None]
    uncited = []
    in_sources_block = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if re.match(r"^\**\s*(источники|sources)\b", line, re.I):
            in_sources_block = True
            continue
        if in_sources_block and (not line or line.startswith(("-", "*", "["))):
            continue
        in_sources_block = False
        if is_claim(line) and not _SID_RE.search(line):
            uncited.append(line[:200])
    res = {"used": used, "unknown": unknown, "uncited": uncited,
           "ok": not unknown and not uncited}
    if not res["ok"]:
        log.info("Цитаты: выдуманных ссылок=%d, утверждений без ссылки=%d",
                 len(unknown), len(uncited))
    return res
