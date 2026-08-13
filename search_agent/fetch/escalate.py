# -*- coding: utf-8 -*-
"""Усиление загрузки проблемного URL (§8: escalate_fetch) — чистые хелперы.

Две дешёвые/надёжные стратегии перед «тяжёлым» браузером:
  * спец-эндпоинт маркетплейса — структурированный JSON вместо HTML за антиботом
    (Ozon composer-api; WB card.wb.ru) — в разы дешевле и часто без блокировки;
  * лестница пресетов «режима открытия» (basic→strong→max) — усиливаем антидетект.
Сам сетевой забор делает Tool через Fetcher; здесь — только детерминированная логика.
"""
import re
from urllib.parse import quote, urlparse

# Лестница пресетов от лёгкого к максимальному (имена из config.FETCH_PRESETS).
_LADDER = ["basic", "strong", "max"]


def marketplace_endpoint(url: str) -> str | None:
    """Спец-URL со структурированными данными для известного маркетплейса или None.

    Ozon: страница → composer-api JSON. WB: карточка `/catalog/<nm>/…` → card.wb.ru.
    """
    p = urlparse(url or "")
    host = p.netloc.lower()
    if host.endswith("ozon.ru"):
        if any(p.path.startswith(s) for s in ("/product/", "/category/", "/search")):
            full = p.path + (("?" + p.query) if p.query else "")
            return "https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=" + quote(full, safe="")
        return None
    if "wildberries.ru" in host:
        m = re.search(r"/catalog/(\d+)/", p.path)
        if m:
            nm = m.group(1)
            return ("https://card.wb.ru/cards/detail?appType=1&curr=rub&dest=-1257786&nm=" + nm)
    return None


def escalation_ladder(reason: str | None = None, tried: list[str] | None = None) -> list[str]:
    """Оставшиеся пресеты «режима открытия» для повторной загрузки (сильнее → сильнее).

    `reason` (blocked/empty/…) пока не меняет лестницу: и антибот, и пустой JS лечит
    переход на браузер с усилением антидетекта. `tried` — уже испробованные пресеты.
    """
    done = set(tried or [])
    return [p for p in _LADDER if p not in done]
