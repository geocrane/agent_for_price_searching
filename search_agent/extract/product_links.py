# -*- coding: utf-8 -*-
"""Поиск карточки товара на странице-листинге (§8: листинг → карточка).

Детерминированно, БЕЗ модели. Разбирает якоря из HTML, оставляет ссылки-карточки того же
типа (`/product/…` и пр.), ранжирует по совпадению с искомым товаром. Важная особенность
рунет-маркетплейсов (Ozon): текст якоря — часто бейдж («Распродажа», «Осталось 9 шт»), а имя
товара — в slug URL латиницей (`tsement-m500-akkermann`). Поэтому запрос сопоставляем и как
есть (кириллица ↔ кириллица), и через транслитерацию (кириллица → латиница ↔ slug).
"""
import re
from urllib.parse import urljoin, urlparse

# Признак URL-карточки (а не категории/листинга).
_CARD_PATH = ("/product/", "/tovar/", "/item/", "/goods/", "/p/")

# Транслитерация кириллицы в латиницу «как у маркетплейсов» (ц→ts, ч→ch, ш→sh…).
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

# Единицы измерения → канон (для сверки «50 кг» ≠ «10 кг»). Латинские варианты из slug тоже.
_UNIT_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(кг|kg|мл|ml|мм|mm|см|cm|м2|m2|м3|m3|г|g|т|t|л|l|м|m|шт|pcs)", re.I)
_UNIT_CANON = {"kg": "кг", "g": "г", "t": "т", "l": "л", "ml": "мл", "mm": "мм",
               "cm": "см", "m": "м", "pcs": "шт", "m2": "м2", "m3": "м3"}
_TOKEN_RE = re.compile(r"[a-zа-я0-9]+", re.I)


def translit(s: str) -> str:
    return "".join(_TRANSLIT.get(ch, ch) for ch in (s or "").lower())


def tokens(s: str) -> set[str]:
    """Значимые токены (буквы/цифры), длиной ≥2 либо содержащие цифру (напр. «м500», «d0»)."""
    return {t for t in _TOKEN_RE.findall((s or "").lower())
            if len(t) >= 2 or any(c.isdigit() for c in t)}


def extract_units(text: str) -> set[str]:
    """Множество нормализованных единиц из текста: «50 кг»/«50kg»/«25кг» → {'50кг'}."""
    out: set[str] = set()
    for num, unit in _UNIT_RE.findall(text or ""):
        u = _UNIT_CANON.get(unit.lower(), unit.lower())
        n = num.replace(",", ".")
        if "." in n:
            n = n.rstrip("0").rstrip(".")
        out.add(n + u)
    return out


def _slug_tokens(path: str) -> set[str]:
    """Токены из последнего сегмента пути карточки (без хвостового числового id)."""
    seg = [s for s in path.split("/") if s]
    if not seg:
        return set()
    last = seg[-1]
    parts = [p for p in last.split("-") if p and not p.isdigit()]   # отбрасываем id вида -2916783171
    return tokens(" ".join(parts))


def _anchors(html: str):
    """Список (href, text) из HTML. Пусто, если selectolax недоступен/не распарсил."""
    try:
        from selectolax.parser import HTMLParser
    except Exception:  # noqa: BLE001
        return []
    try:
        tree = HTMLParser(html)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for a in tree.css("a"):
        href = a.attributes.get("href")
        if href:
            out.append((href, (a.text() or "").strip()))
    return out


def find_product_links(html: str, base_url: str, name: str, part_number: str | None = None,
                       top_k: int = 5) -> list[dict]:
    """Найти на листинге ссылки-карточки, подходящие искомому товару, отранжированные по score.

    Возвращает [{url, title, score, unit_ok}] (топ-K, score убыв.). Чистая функция.
    """
    if not html:
        return []
    base_dom = urlparse(base_url).netloc.lower()
    name_terms = tokens(name)
    name_units = extract_units(name)
    pn = (part_number or "").lower().strip()

    # href → лучший (самый длинный) текст якоря
    best_text: dict[str, str] = {}
    for href, text in _anchors(html):
        absu = urljoin(base_url, href)
        pu = urlparse(absu)
        if pu.netloc.lower() != base_dom:
            continue
        if not any(seg in pu.path.lower() for seg in _CARD_PATH):
            continue
        url = absu.split("?")[0].split("#")[0]
        if len(text) > len(best_text.get(url, "")):
            best_text[url] = text

    scored = []
    for url, text in best_text.items():
        path = urlparse(url).path.lower()
        title_terms = tokens(text) | _slug_tokens(path)
        title_blob = text + " " + path.replace("-", " ")
        # доля токенов запроса, найденных в заголовке/slug (напрямую или через транслит)
        matched = sum(1 for t in name_terms
                      if t in title_terms or translit(t) in title_terms)
        overlap = matched / len(name_terms) if name_terms else 0.0
        # единицы измерения: совпали → бонус, конфликтуют → штраф, нет данных → нейтрально
        cand_units = extract_units(title_blob)
        unit_ok = None
        unit_adj = 0.0
        if name_units and cand_units:
            if name_units & cand_units:
                unit_ok, unit_adj = True, 0.3
            else:
                unit_ok, unit_adj = False, -0.3
        pn_bonus = 0.5 if pn and (pn in title_blob) else 0.0
        score = round(max(0.0, overlap + unit_adj + pn_bonus), 3)
        scored.append({"url": url, "title": text, "score": score, "unit_ok": unit_ok})

    scored.sort(key=lambda d: d["score"], reverse=True)
    return scored[:top_k]
