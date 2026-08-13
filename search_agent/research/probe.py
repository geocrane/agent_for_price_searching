# -*- coding: utf-8 -*-
"""Аттестация источника данных: детекторы + досье.

Задача разведки — не «нашёл портал», а «убедился, что нужные данные там есть». Здесь живёт
детерминированная часть этой проверки: что за доступ (открыто/регистрация/платно/блок), какие
виды данных видны, есть ли выгрузка или API, какие годы встречаются. Решение о наличии данных
выносит модель (`research/verify.py`), но её ответ проверяется этими же фактами.

Никаких названий конкретных порталов и их структуры: только общие маркеры русско- и
англоязычных сайтов (правило universal-tool-any-file). Незнакомый источник должен обрабатываться
так же, как знакомый.
"""
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from .coverage import coverage, describe, observed_period, parse_period
from ..extract.focus import has_price
from ..obs.log import get_logger

log = get_logger("research.probe")

ACCESS_OPEN, ACCESS_REG, ACCESS_PAID, ACCESS_BLOCKED = "open", "registration", "paid", "blocked"

_REG_RE = re.compile(
    r"войти в личный кабинет|личный кабинет|зарегистрируйтесь|регистрац|авторизуйтесь|"
    r"требуется вход|sign in|log in|create an account", re.I | re.U)
_PAID_RE = re.compile(
    r"оформить подписку|стоимость доступа|тариф|платный доступ|купить доступ|прайс на доступ|"
    r"subscription|pricing plan|paywall", re.I | re.U)
_EXPORT_RE = {
    "csv": re.compile(r"\.csv\b|формат csv", re.I | re.U),
    "xlsx": re.compile(r"\.xlsx?\b|формат excel|в excel", re.I | re.U),
    "json": re.compile(r"\.json\b|формат json", re.I | re.U),
    "xml": re.compile(r"\.xml\b|формат xml|sdmx", re.I | re.U),
    "api": re.compile(r"\bapi\b|rest[- ]?api|открытые данные|opendata|open data", re.I | re.U),
}
_KIND_RE = {
    "цены": re.compile(r"\bцен[аыу]?\b|стоимость|прайс|тариф|руб\.?|₽", re.I | re.U),
    "закупки": re.compile(r"закупк|контракт|тендер|аукцион|котировк|поставщик|заказчик|44-фз|223-фз",
                          re.I | re.U),
    "статистика": re.compile(r"статистик|показател|динамик|индекс|росстат|отчётност|отчетност",
                             re.I | re.U),
    "реестр": re.compile(r"реестр|каталог|классификатор|перечень|справочник|база данных",
                         re.I | re.U),
    "нормативы": re.compile(r"норматив|смет|расцен|методик|регламент", re.I | re.U),
    "история": re.compile(r"истори[яию]|архив|за период|динамика цен|ретроспектив", re.I | re.U),
}
# Внутренние ссылки, похожие на вход к данным (для deep-разведки).
_DATA_LINK_RE = re.compile(
    r"поиск|найти|реестр|каталог|архив|выгруз|скача|загруз|открытые данные|opendata|open-data|"
    r"статистик|данны|отчёт|отчет|api|export|download|dataset|search|registry", re.I | re.U)
_DATA_EXT_RE = re.compile(r"\.(?:csv|xlsx?|json|xml|zip)(?:$|\?)", re.I)


def detect_access(text: str, blocked: str | None = None) -> tuple[str, list[str]]:
    """Уровень доступа + строки-обоснования. Блок антибота важнее всего остального."""
    if blocked:
        return ACCESS_BLOCKED, ["загрузка отбита источником: %s" % blocked]
    ev = []
    paid = [ln.strip()[:200] for ln in (text or "").splitlines() if _PAID_RE.search(ln)]
    if paid:
        return ACCESS_PAID, paid[:3]
    reg = [ln.strip()[:200] for ln in (text or "").splitlines() if _REG_RE.search(ln)]
    if reg:
        return ACCESS_REG, reg[:3]
    return ACCESS_OPEN, ev


def detect_kinds(text: str) -> list[str]:
    """Какие виды данных видны на странице (по общим маркерам)."""
    out = [k for k, rx in _KIND_RE.items() if rx.search(text or "")]
    if "цены" not in out and any(has_price(ln) for ln in (text or "").splitlines()):
        out.append("цены")
    return out


def detect_export(text: str, html: str = "") -> list[str]:
    """Форматы выгрузки и наличие API."""
    hay = (text or "") + "\n" + (html or "")
    return [k for k, rx in _EXPORT_RE.items() if rx.search(hay)]


def data_links(html: str, base_url: str, limit: int = 3) -> list[str]:
    """Внутренние ссылки, похожие на вход к данным (поиск/реестр/выгрузка/API).

    Только тот же домен: разведка проверяет ИСТОЧНИК, а не уходит гулять по интернету.
    """
    if not html:
        return []
    from ..extract.product_links import _anchors     # общий разбор якорей, не дублируем
    base_dom = urlparse(base_url).netloc.lower()
    scored: dict[str, int] = {}
    for href, text in _anchors(html):
        try:
            absu = urljoin(base_url, href)
        except ValueError:
            continue
        pu = urlparse(absu)
        if pu.scheme not in ("http", "https") or pu.netloc.lower() != base_dom:
            continue
        url = absu.split("#")[0]
        if url.rstrip("/") == (base_url or "").rstrip("/"):
            continue
        score = 0
        if _DATA_EXT_RE.search(url):
            score += 3
        if _DATA_LINK_RE.search(text or ""):
            score += 2
        if _DATA_LINK_RE.search(pu.path):
            score += 1
        if score:
            scored[url] = max(scored.get(url, 0), score)
    top = sorted(scored, key=lambda u: scored[u], reverse=True)[:limit]
    if top:
        log.debug("data_links %s → %s", base_url, top)
    return top


@dataclass
class SourceDossier:
    """Досье источника: что там есть, за какой период, как достать, что мешает."""
    url: str
    domain: str = ""
    title: str = ""
    sid: str = ""
    access: str = ACCESS_OPEN
    kinds: list[str] = field(default_factory=list)
    period: dict = field(default_factory=dict)
    export: list[str] = field(default_factory=list)
    how_to: str = ""
    limits: str = ""
    verdict: str = "не проверено"
    evidence: list[str] = field(default_factory=list)
    note: str = ""
    checked_urls: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"url": self.url, "domain": self.domain, "title": self.title, "sid": self.sid,
                "access": self.access, "kinds": list(self.kinds), "period": dict(self.period),
                "export": list(self.export), "how_to": self.how_to, "limits": self.limits,
                "verdict": self.verdict, "evidence": list(self.evidence), "note": self.note,
                "checked_urls": list(self.checked_urls)}

    def summary(self) -> str:
        """Однострочная сводка для наблюдения агенту (компактно, с фактами)."""
        parts = ["%s — %s" % (self.domain or self.url, self.verdict)]
        if self.kinds:
            parts.append("данные: " + ", ".join(self.kinds))
        parts.append(describe(self.period))
        parts.append("доступ: " + ACCESS_RU.get(self.access, self.access))
        if self.export:
            parts.append("выгрузка: " + ", ".join(self.export))
        if self.limits:
            parts.append("ограничения: " + self.limits[:120])
        return "; ".join(parts)


ACCESS_RU = {ACCESS_OPEN: "открытый", ACCESS_REG: "нужна регистрация",
             ACCESS_PAID: "платный", ACCESS_BLOCKED: "заблокирован"}

_VERDICT_FIT, _VERDICT_PART, _VERDICT_NO, _VERDICT_UNKNOWN = (
    "годится", "частично", "не годится", "не проверено")


def decide_verdict(*, access: str, has_data: str, period_status: str, evidence: list[str],
                   kinds: list[str]) -> str:
    """Итоговый вердикт по источнику — считает КОД, не модель.

    Логика простая и честная: без подтверждённых доказательств вердикта не бывает; блок и
    платный доступ понижают даже при найденных данных (пользователь до них не доберётся).
    """
    if not evidence:
        return _VERDICT_UNKNOWN
    if access == ACCESS_BLOCKED:
        return _VERDICT_NO
    if has_data == "нет":
        return _VERDICT_NO
    if has_data == "неизвестно":
        return _VERDICT_UNKNOWN
    partial = (has_data == "частично" or period_status in ("partial", "unknown")
               or access in (ACCESS_REG, ACCESS_PAID) or not kinds)
    if period_status == "absent":
        return _VERDICT_NO
    return _VERDICT_PART if partial else _VERDICT_FIT


def build_period(text: str, period_request: str | None) -> dict:
    """Покрытие периода по тексту источника (наблюдаемые годы против запрошенных)."""
    return coverage(observed_period(text), parse_period(period_request or ""))


def render_catalog(dossiers: list) -> str:
    """Каталог источников таблицей — пишет КОД, поэтому цифры и ссылки всегда верны."""
    rows = [d if isinstance(d, dict) else d.to_dict() for d in (dossiers or [])]
    if not rows:
        return ""
    out = ["**Каталог источников**", "",
           "| Источник | Годится | Что есть | Период | Доступ | Выгрузка |",
           "| --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        sid = ("[%s] " % r["sid"]) if r.get("sid") else ""
        period = (r.get("period") or {})
        out.append("| %s%s | %s | %s | %s | %s | %s |" % (
            sid, r.get("domain") or r.get("url", ""),
            r.get("verdict") or "—",
            ", ".join(r.get("kinds") or []) or "—",
            period.get("seen") or "—",
            ACCESS_RU.get(r.get("access"), r.get("access") or "—"),
            ", ".join(r.get("export") or []) or "—"))
    notes = [r for r in rows if r.get("how_to") or r.get("limits")]
    if notes:
        out.append("")
        for r in notes:
            sid = ("[%s] " % r["sid"]) if r.get("sid") else ""
            tail = " Ограничения: %s" % r["limits"] if r.get("limits") else ""
            out.append("- %s%s — %s%s" % (sid, r.get("domain") or r.get("url", ""),
                                          r.get("how_to") or "—", tail))
    return "\n".join(out)
