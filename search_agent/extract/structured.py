# -*- coding: utf-8 -*-
"""Сбор ценовых подсказок из структурных данных страницы (JSON-LD/microdata/meta).

Это сильнейший сигнал «товар→цена» со страницы. Идёт в промпт модели как подсказка И
служит деградационным fallback'ом, если модель недоступна. БЕЗ вызова модели.
"""
from .price import normalize_currency, parse_price

_IN_STOCK_TOKENS = ("instock", "в наличии", "in_stock", "available")
_OUT_TOKENS = ("outofstock", "нет в наличии", "soldout", "unavailable")


def _availability(val) -> bool | None:
    if not val:
        return None
    low = str(val).lower()
    if any(t in low for t in _OUT_TOKENS):
        return False
    if any(t in low for t in _IN_STOCK_TOKENS):
        return True
    return None


def _iter_offers(block):
    """Рекурсивно обойти JSON-LD и выдать словари, похожие на Offer/Product с ценой."""
    if isinstance(block, list):
        for el in block:
            yield from _iter_offers(el)
        return
    if not isinstance(block, dict):
        return
    if "@graph" in block:
        yield from _iter_offers(block["@graph"])
    keys = {k.lower() for k in block.keys()}
    if keys & {"price", "lowprice", "highprice"}:
        yield block
    for key in ("offers", "hasOfferCatalog", "itemOffered"):
        if key in block:
            yield from _iter_offers(block[key])


def _lc(d: dict) -> dict:
    """Ключи объекта в нижнем регистре (schema.org встречается в разных регистрах)."""
    return {str(k).lower(): v for k, v in d.items()}


def collect_hints(structured: dict | None) -> list[dict]:
    """→ список {value, currency, in_stock?, source} из meta/microdata/jsonld (дедуплицированный)."""
    if not isinstance(structured, dict):
        return []
    hints: list[dict] = []

    # --- meta (og:price:amount / product:price:amount / itemprop=price) ---
    meta = structured.get("meta") or {}
    m_price, m_cur = None, None
    for k, v in meta.items():
        kl = str(k).lower()
        if "currency" in kl:
            m_cur = v
        elif "price" in kl and "valid" not in kl:      # pricevaliduntil — не цена
            m_price = v
    if m_price is not None:
        p = parse_price(m_price, currency_hint=m_cur)
        if p:
            p["source"] = "meta"
            hints.append(p)

    # --- microdata: список одиночных {prop: val}; собираем ВСЕ price + общую currency ---
    md_prices, md_cur, md_avail = [], None, None
    for entry in structured.get("microdata") or []:
        if not isinstance(entry, dict):
            continue
        for prop, val in entry.items():
            pl = str(prop).lower()
            if pl == "price":
                md_prices.append(val)
            elif pl == "pricecurrency" and md_cur is None:
                md_cur = val
            elif pl == "availability" and md_avail is None:
                md_avail = val
    for pv in md_prices:
        p = parse_price(pv, currency_hint=md_cur)
        if p:
            p["in_stock"] = _availability(md_avail)
            p["source"] = "microdata"
            hints.append(p)

    # --- JSON-LD schema.org Product/Offer ---
    for block in structured.get("jsonld") or []:
        for offer in _iter_offers(block):
            o = _lc(offer)
            raw = o.get("price") or o.get("lowprice") or o.get("highprice")
            if raw is None:
                spec = o.get("pricespecification")
                if isinstance(spec, dict):
                    raw = _lc(spec).get("price")
            if raw is None:
                continue
            cur = o.get("pricecurrency")
            if isinstance(o.get("pricespecification"), dict):
                cur = cur or _lc(o["pricespecification"]).get("pricecurrency")
            p = parse_price(raw, currency_hint=cur)
            if p:
                p["in_stock"] = _availability(o.get("availability"))
                p["source"] = "jsonld"
                hints.append(p)

    return _dedup(hints)


def _iter_named(block):
    """Рекурсивно обойти JSON-LD и выдать словари с именем товара (Product/ItemPage)."""
    if isinstance(block, list):
        for el in block:
            yield from _iter_named(el)
        return
    if not isinstance(block, dict):
        return
    if "@graph" in block:
        yield from _iter_named(block["@graph"])
    low = _lc(block)
    typ = str(low.get("@type") or low.get("type") or "").lower()
    if low.get("name") and ("product" in typ or "offer" in typ or not typ):
        yield low
    for key in ("itemoffered", "mainentity", "item"):
        if key in low:
            yield from _iter_named(low[key])


def product_name(structured: dict | None) -> str:
    """Название товара из разметки страницы: JSON-LD `name` → microdata `name` → og:title.

    Нужно там, где цену пришлось взять из разметки (модель была недоступна): без него колонка
    «Найдено» пустая, пользователю не с чем сверить цену, и код-судья не может отсеять явно
    другой товар. Это не замена проверке моделью — это минимум, позволяющий её отсутствие увидеть.
    """
    if not isinstance(structured, dict):
        return ""
    for block in structured.get("jsonld") or []:
        for named in _iter_named(block):
            name = named.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()[:300]
    for entry in structured.get("microdata") or []:
        if not isinstance(entry, dict):
            continue
        for prop, val in entry.items():
            if str(prop).lower() == "name" and isinstance(val, str) and val.strip():
                return val.strip()[:300]
    meta = structured.get("meta") or {}
    for key in ("og:title", "twitter:title", "title"):
        val = meta.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:300]
    return ""


def _dedup(hints: list[dict]) -> list[dict]:
    seen, out = set(), []
    for h in hints:
        key = (round(h["value"], 2), h["currency"])
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out
