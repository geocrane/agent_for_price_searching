# -*- coding: utf-8 -*-
"""Подготовка контента страницы для LLM: Markdown (сохраняет карточки) + структурная цена.

ВАЖНО (по итогам анализа): trafilatura (article-oriented) вырезал ценовые виджеты — цена
терялась. Здесь НЕ режем контент: убираем только явный мусор (script/style/svg/…), остальное
→ Markdown (заголовки/списки/таблицы/ссылки = границы карточек, цены рядом с товаром). Плюс
отдельно собираем структурные данные (JSON-LD/microdata/meta) — сильнейший сигнал товар→цена.
Саму цену НЕ извлекаем — это следующий шаг (LLM).
"""
import json
import re

from markdownify import markdownify as _md
from selectolax.parser import HTMLParser

from ..config import CleanConfig
from ..extract.price import PRICE_RE               # единое определение цены на весь проект

# Явный мусор — удаляем всегда. Контент/виджеты по умолчанию НЕ трогаем (там может быть цена).
# Критерий списка один и универсальный: элемент НИКОГДА не показывается пользователю как текст.
# `noframes` — легаси-элемент для браузеров без фреймов; современные SPA (напр. апиари-вёрстка)
# прячут в него служебное состояние страницы. Без его удаления Markdown забивался JSON-начинкой
# ({"widgets":…}) вместо товаров, и в модель уходила разметка вместо страницы.
_JUNK = ("script", "style", "noscript", "noframes", "svg", "iframe", "template", "link", "meta",
         "picture", "source")
_MD_LIMIT = 60000                                  # верхний предел размера Markdown (страховка)
_MULTINL = re.compile(r"\n{3,}")
_MICRO_PROPS = ("price", "pricecurrency", "name", "availability", "sku", "offers",
                "lowprice", "highprice", "pricespecification")
# Ценовой токен-страж: блок с таким признаком НЕ удаляем при условной очистке (защита цены).
# Само определение цены — общее (extract/price.py); здесь к нему добавлены слова-подсказки.
_PRICE_TOKEN = re.compile(r"цена|стоимост|%s" % PRICE_RE.pattern, re.I | re.U)
_STRUCTURAL = ("nav", "aside", "header", "footer")     # семантические блоки-обёртки
_COOKIE_SEL = ("[class*=cookie]", "[id*=cookie]", "[class*=consent]", "[id*=consent]",
               "[class*=gdpr]", "[id*=gdpr]")
_LINK_DENSITY_TAGS = ("div", "ul", "section")          # кандидаты на «меню» без семантики
_LINK_DENSITY_THRESHOLD = 0.8                          # доля текста внутри ссылок → это меню


def _has_price(node) -> bool:
    """Есть ли в поддереве узла ценовой токен (тогда блок трогать нельзя)."""
    return bool(_PRICE_TOKEN.search(node.text() or ""))


def _tree(html: str) -> HTMLParser | None:
    try:
        return HTMLParser(html)
    except Exception:  # noqa: BLE001
        return None


def extract_jsonld(html: str) -> list:
    tree = _tree(html)
    if tree is None:
        return []
    out = []
    for node in tree.css('script[type="application/ld+json"]'):
        raw = (node.text() or "").strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except Exception:  # noqa: BLE001
            continue
    return out


def extract_microdata(tree: HTMLParser) -> list:
    """Элементы с itemprop, относящиеся к товару/цене."""
    out = []
    for node in tree.css("[itemprop]"):
        prop = (node.attributes.get("itemprop") or "").lower()
        if prop not in _MICRO_PROPS:
            continue
        val = node.attributes.get("content") or node.attributes.get("value") or (node.text() or "").strip()
        if val:
            out.append({prop: val[:100]})
        if len(out) >= 40:
            break
    return out


def extract_meta(tree: HTMLParser) -> dict:
    """meta с ценой/валютой (og:price:amount, product:price:amount, itemprop=price…)."""
    out = {}
    for node in tree.css("meta"):
        key = (node.attributes.get("property") or node.attributes.get("itemprop")
               or node.attributes.get("name") or "").lower()
        content = node.attributes.get("content")
        if content and any(k in key for k in ("price", "currency")):
            out[key] = content[:80]
    return out


def _prune_dom(tree, cfg: CleanConfig) -> None:
    """Убрать из DOM мусор и (по сценарию) навигацию/баннеры, не задев блоки с ценой."""
    for node in tree.css(",".join(_JUNK)):             # явный мусор — всегда
        node.decompose()
    if cfg.remove_cookie_banner:                       # cookie/consent/gdpr-баннеры
        for node in tree.css(",".join(_COOKIE_SEL)):
            node.decompose()
    if cfg.drop_structural:                            # nav/aside/header/footer
        for node in tree.css(",".join(_STRUCTURAL)):
            if cfg.drop_structural_force or not _has_price(node):
                node.decompose()
    if cfg.link_density_prune:                         # div-«меню» без семантики <nav>
        for node in tree.css(",".join(_LINK_DENSITY_TAGS)):
            total = len((node.text() or "").strip())
            if total < 200:                            # мелкие блоки не трогаем
                continue
            link_len = sum(len((a.text() or "").strip()) for a in node.css("a"))
            if total and link_len / total > _LINK_DENSITY_THRESHOLD and not _has_price(node):
                node.decompose()
    if cfg.strip_link_urls:                            # <a> → его текст (URL убираем)
        for a in tree.css("a"):
            a.unwrap()


def to_markdown(html: str, cfg: CleanConfig | None = None) -> str:
    """HTML → Markdown с сохранением карточек/спеков; лишнее убираем по сценарию очистки."""
    cfg = cfg or CleanConfig()
    tree = _tree(html)
    if tree is None:
        return ""
    _prune_dom(tree, cfg)
    root = tree.body or tree.root
    src = root.html if root is not None else html
    try:
        md = _md(src, heading_style="ATX", strip=["img"])
    except Exception:  # noqa: BLE001
        md = root.text(separator="\n") if root is not None else ""
    md = _dedup_lines(md)
    md = _MULTINL.sub("\n\n", md).strip()
    return md[:cfg.max_chars]


def _dedup_lines(text: str) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if len(s) > 15 and s in seen:
            continue
        if len(s) > 15:
            seen.add(s)
        out.append(ln)
    return "\n".join(out)


def clean_page(html: str, cfg: CleanConfig | None = None) -> dict:
    """Итог: {markdown, structured{jsonld,microdata,meta}, chars}.

    cfg — сценарий очистки Markdown. Структурные сигналы (JSON-LD/microdata/meta) от сценария
    НЕ зависят — это сильнейший источник цены, извлекаем его всегда из исходного html.
    """
    if not html:
        return {"markdown": "", "structured": {"jsonld": [], "microdata": [], "meta": {}}, "chars": 0}
    cfg = cfg or CleanConfig()
    tree = _tree(html)
    md = to_markdown(html, cfg)
    structured = {
        "jsonld": extract_jsonld(html),
        "microdata": extract_microdata(tree) if tree is not None else [],
        "meta": extract_meta(tree) if tree is not None else {},
    }
    return {"markdown": md, "structured": structured, "chars": len(md)}
