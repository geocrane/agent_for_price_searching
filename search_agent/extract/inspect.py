# -*- coding: utf-8 -*-
"""Триаж страницы (§8): что мы получили — карточка / листинг / блок / пусто / прочее.

Детерминированно, БЕЗ модели (тестируемо оффлайн). Опирается на уже собранные сигналы:
`fetch.blocked` (antibot), структурные данные (`structured`: JSON-LD/microdata) и плотность
ценовых меток в Markdown. Нужен, чтобы агент «перепроверял» результат и выбирал действие,
а не молча возвращал пустую цену (см. разбор Ozon-фикстур: 403-блок ≠ «нет цены»).

ВАЖНО (реальный кейс Ozon): страница-категория может нести `Product` + `AggregateOffer`
ради SEO — поэтому наличие `Product` само по себе НЕ означает карточку. Признаки листинга
(`AggregateOffer`/диапазон цен, URL категории/поиска, много ценовых меток) имеют приоритет.
"""
import re
from urllib.parse import urlparse

from .price import PRICE_RE               # единое определение цены (extract/price.py)
from .structured import collect_hints


# Служебная JSON-разметка: признак «оболочки» одностраничного приложения. Такая страница
# формально загрузилась, но товара в ней нет — только состояние виджетов и меню, а карточки
# дорисовываются скриптом уже в браузере.
_SERVICE_JSON = re.compile(r'\{"|":\s*\{|":\s*\[|"\w+":')
SHELL_RATIO = 0.7          # доля символов в служебных строках, выше которой это оболочка
SHELL_MIN_CHARS = 2000     # на коротких страницах доля неустойчива — не судим


def service_json_ratio(markdown: str) -> float:
    """Доля символов страницы, приходящаяся на строки со служебной JSON-разметкой.

    Замер на 178 сохранённых страницах: распределение резко двумодальное — 156 страниц ниже 0.2
    и 22 выше 0.96, между ними НИЧЕГО. Поэтому порог 0.7 безопасен и не требует подстройки.
    Признак структурный: он не привязан к конкретному сайту и сработает на любом SPA.
    """
    md = markdown or ""
    if not md:
        return 0.0
    lines = [l for l in md.splitlines() if l.strip()]
    service = sum(len(l) for l in lines if _SERVICE_JSON.search(l))
    return service / len(md)


# Порог «много цен» → страница со многими офферами (листинг), а не одна карточка.
LISTING_MIN_TOKENS = 5

_LISTING_TYPES = {"itemlist", "collectionpage", "searchresultspage", "offercatalog"}
_PRODUCT_TYPES = {"product", "offer"}
_URL_LISTING = ("/category/", "/catalog/", "/search", "/brand/", "/seller/", "/collection")
_URL_CARD = ("/product/", "/tovar/", "/item/", "/goods/", "/p/")


def _scan_jsonld(blocks) -> tuple[set[str], bool]:
    """Собрать все @type (в нижнем регистре) и признак агрегата (AggregateOffer/диапазон)."""
    types: set[str] = set()
    agg = False

    def walk(b):
        nonlocal agg
        if isinstance(b, list):
            for x in b:
                walk(x)
            return
        if not isinstance(b, dict):
            return
        t = b.get("@type")
        tvals = [t] if isinstance(t, str) else (t or [])
        for tv in tvals:
            if isinstance(tv, str):
                tl = tv.lower()
                types.add(tl)
                if tl == "aggregateoffer":
                    agg = True
        low = {str(k).lower() for k in b.keys()}
        if "lowprice" in low or "highprice" in low:
            agg = True
        for v in b.values():
            walk(v)

    for blk in blocks or []:
        walk(blk)
    return types, agg


def inspect_fetch(fetch: dict | None, url: str | None = None) -> dict:
    """Классифицировать результат загрузки страницы.

    Возвращает {kind, price_present, reason, signals}, где kind ∈
    {blocked, empty, card, listing, other}. Чистая функция — не зовёт модель и не ходит в сеть.
    """
    url = url or (fetch or {}).get("url") or ""
    if not fetch:
        return {"kind": "empty", "price_present": False,
                "reason": "нет контента (страница не загружена)", "signals": {}}

    blocked = fetch.get("blocked")
    if blocked:
        return {"kind": "blocked", "price_present": False,
                "reason": "антибот/блок: %s" % blocked, "signals": {"blocked": blocked}}

    # HTTP-статус: сайты отдают на несуществующий товар не пустую страницу, а полноценную
    # заглушку с меню и каталогом на десятки тысяч символов. Без этой проверки такая заглушка
    # шла в модель как обычная страница, а её честное «искомого тут нет» выглядело потом
    # необъяснимым пустым ответом. Настоящая причина — товара по адресу больше нет.
    # Исключение: кривые CMS отдают 404 вместе с настоящей карточкой. Признак настоящей карточки —
    # структурная ценовая метка; если она есть, статусу не верим и разбираем страницу как обычно.
    status = fetch.get("status")
    if isinstance(status, int) and status >= 400:
        if not collect_hints(fetch.get("structured") or {}):
            reason = ("страница не существует (HTTP %d) — сервер отдал заглушку" % status
                      if status in (404, 410) else
                      "сервер вернул ошибку HTTP %d — страница не получена" % status)
            return {"kind": "empty", "price_present": False, "reason": reason,
                    "signals": {"status": status, "chars": len(fetch.get("markdown") or "")}}

    md = fetch.get("markdown") or ""
    structured = fetch.get("structured") or {}
    has_structured = bool(structured.get("jsonld") or structured.get("microdata")
                          or structured.get("meta"))
    if not md.strip() and not has_structured:
        return {"kind": "empty", "price_present": False,
                "reason": "пустая страница (0 символов)", "signals": {}}

    # Оболочка SPA: страница «загрузилась», но товара в ней нет — только служебное состояние
    # виджетов. Отдавать такое модели бессмысленно и дорого (это десятки тысяч символов).
    shell = service_json_ratio(md)
    if len(md) >= SHELL_MIN_CHARS and shell >= SHELL_RATIO:
        return {"kind": "shell", "price_present": False,
                "reason": "оболочка сайта без товара (%d%% содержимого — служебная разметка, "
                          "карточки дорисовываются скриптом)" % round(shell * 100),
                "signals": {"service_json": round(shell, 3), "chars": len(md)}}

    hints = collect_hints(structured)
    tokens = len(PRICE_RE.findall(md))
    types, agg = _scan_jsonld(structured.get("jsonld"))
    micro = structured.get("microdata") or []
    path = urlparse(url).path.lower()
    url_listing = any(seg in path for seg in _URL_LISTING)
    url_card = any(seg in path for seg in _URL_CARD)
    listing_type = bool(types & _LISTING_TYPES)
    product_type = bool(types & _PRODUCT_TYPES) or bool(micro)
    price_present = bool(hints) or tokens > 0

    signals = {"tokens": tokens, "jsonld_types": sorted(types), "agg": agg,
               "url_card": url_card, "url_listing": url_listing, "hints": len(hints)}

    if listing_type or agg or url_listing or tokens >= LISTING_MIN_TOKENS:
        bits = []
        if agg:
            bits.append("AggregateOffer/диапазон")
        if listing_type:
            bits.append("schema:%s" % ",".join(sorted(types & _LISTING_TYPES)))
        if url_listing:
            bits.append("URL категории/поиска")
        if tokens >= LISTING_MIN_TOKENS:
            bits.append("%d ценовых меток" % tokens)
        return {"kind": "listing", "price_present": price_present,
                "reason": "листинг/категория (%s)" % ", ".join(bits), "signals": signals}

    if url_card or product_type or (1 <= tokens < LISTING_MIN_TOKENS):
        bits = []
        if product_type:
            bits.append("schema:Product/Offer")
        if url_card:
            bits.append("URL карточки")
        if tokens:
            bits.append("%d ценовых меток" % tokens)
        return {"kind": "card", "price_present": price_present,
                "reason": "карточка товара (%s)" % ", ".join(bits), "signals": signals}

    return {"kind": "other", "price_present": price_present,
            "reason": "тип страницы не распознан (нет ценовых сигналов)", "signals": signals}
