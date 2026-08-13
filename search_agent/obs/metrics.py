# -*- coding: utf-8 -*-
"""Метрики прогона: чем он кончился и где потери. Детерминированно, без модели и без сети.

Зачем. «Нашли 6 из 9» — не диагноз. Чтобы понимать, куда уходит результат, нужны доли по исходам
источников и, главное, оценка УПУЩЕННОГО: сколько страниц загрузилось, дало пусто, но цена на них
была. Это единственная метрика, которая отличает «на сайте правда нет цены» от «модель не увидела».

Оценка упущенного детерминированная и намеренно грубая: цена в отобранном для модели тексте есть
(`extract/focus`) и название искомого заметно пересекается с этим текстом. Она не заменяет ручную
проверку, но показывает направление и сравнима между прогонами.
"""
import re

from .. import session as sess
from ..extract.focus import has_price, select_pricing_blocks
from .log import get_logger

log = get_logger("obs.metrics")

# Доля токенов названия, при которой считаем, что на странице речь всё-таки об искомом.
MISS_OVERLAP = 0.4

_WORD = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9]+", re.U)


def _cand_page_text(cand: dict, item, max_chars: int) -> str:
    """Текст, который получила (или получила бы) модель: из кандидата либо из дискового кэша."""
    fetch = cand.get("fetch")
    if not fetch:
        from ..fetch import cache
        fetch = cache.get(cand.get("url", "")) or {}
    md = fetch.get("markdown") or ""
    if not md.strip():
        return ""
    return select_pricing_blocks(md, item, max_chars)[0]


def _overlap(name: str, text: str) -> float:
    """Доля значимых слов названия, встретившихся в тексте страницы."""
    toks = {t.lower() for t in _WORD.findall(name or "") if len(t) >= 3}
    if not toks:
        return 0.0
    hay = {t.lower() for t in _WORD.findall(text or "")}
    return sum(1 for t in toks if t in hay) / len(toks)


def suspected_misses(items: list[dict], *, max_chars: int = 24000) -> list[dict]:
    """Страницы со статусом «пусто», на которых цена, судя по тексту, всё-таки была.

    Возвращает список {row, name, domain, url, price_lines, overlap} — по нему видно, какие именно
    источники стоит пересмотреть. Пустой список означает, что «пусто» честное.
    """
    out: list[dict] = []
    for it in items:
        name = it.get("name") or ""

        class _Item:                                 # минимальный вид под extract/focus
            part_number = it.get("part_number")
        _Item.name = name

        for cand in it.get("candidates") or []:
            if sess.cand_status(cand) != sess.STATUS_EMPTY:
                continue
            text = _cand_page_text(cand, _Item, max_chars)
            if not text:
                continue
            lines = sum(1 for ln in text.splitlines() if has_price(ln))
            ov = _overlap(name, text)
            if lines and ov >= MISS_OVERLAP:
                out.append({"row": it.get("row"), "name": name[:60],
                            "domain": cand.get("domain"), "url": cand.get("url"),
                            "price_lines": lines, "overlap": round(ov, 2)})
    return out


def run_metrics(items: list[dict], *, usage: dict | None = None, duration_s: float | None = None,
                detect_misses: bool = True) -> dict:
    """Свести прогон к цифрам: исходы источников, итоги позиций, упущенное, расход."""
    statuses: dict[str, int] = {}
    sites = 0
    for it in items:
        for cand in it.get("candidates") or []:
            if cand.get("is_file"):
                continue
            sites += 1
            st = sess.cand_status(cand)
            statuses[st] = statuses.get(st, 0) + 1

    with_price = sum(1 for it in items if (it.get("verdict") or {}).get("primary") is not None)
    on_request = sum(1 for it in items if it.get("on_request"))
    total = len(items)

    misses = suspected_misses(items) if detect_misses else []
    empty = statuses.get(sess.STATUS_EMPTY, 0)

    def share(n: int) -> float:
        return round(100.0 * n / sites, 1) if sites else 0.0

    m = {
        "items_total": total,
        "items_with_price": with_price,
        "items_on_request": on_request,
        "items_not_found": total - with_price - on_request,
        "sites_total": sites,
        "sites_by_status": dict(sorted(statuses.items())),
        "share_done_pct": share(statuses.get(sess.STATUS_DONE, 0)),
        "share_blocked_pct": share(statuses.get(sess.STATUS_BLOCKED, 0)),
        "share_empty_pct": share(empty),
        "suspected_misses": len(misses),
        "suspected_misses_pct_of_empty": round(100.0 * len(misses) / empty, 1) if empty else 0.0,
        "missed_sites": misses[:20],                 # первые — чтобы было что открыть глазами
    }
    if usage:
        m["usage"] = dict(usage)
    if duration_s is not None:
        m["duration_s"] = round(duration_s, 1)
        m["sec_per_item"] = round(duration_s / total, 1) if total else 0.0
    return m


def format_metrics(m: dict) -> str:
    """Человекочитаемая сводка прогона — её видит пользователь в конце (правило «видимо»)."""
    lines = [
        "Позиций: %d · с ценой: %d · цена по запросу: %d · не найдено: %d" % (
            m["items_total"], m["items_with_price"], m["items_on_request"], m["items_not_found"]),
        "Источников: %d · цена извлечена %.1f%% · заблокировано %.1f%% · пусто %.1f%%" % (
            m["sites_total"], m["share_done_pct"], m["share_blocked_pct"], m["share_empty_pct"]),
    ]
    if m.get("suspected_misses"):
        lines.append("Похоже на упущенное: %d страниц из «пусто» (%.1f%%) содержали цену искомого"
                     % (m["suspected_misses"], m["suspected_misses_pct_of_empty"]))
    u = m.get("usage") or {}
    if u.get("total_tokens"):
        lines.append("Модель: запросов %d, токенов %d" % (u.get("requests", 0), u["total_tokens"]))
    if m.get("duration_s") is not None:
        lines.append("Время: %.0f с (%.1f с на позицию)" % (m["duration_s"], m["sec_per_item"]))
    return "\n".join(lines)
