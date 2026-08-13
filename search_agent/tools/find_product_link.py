# -*- coding: utf-8 -*-
"""Скил `find_product_link` (§8): на листинге найти карточку искомого товара.

Детерминированно, без модели. Берёт HTML листинга из кэша по URL, возвращает отранжированные
ссылки-карточки того же домена. Дальше агент/пайплайн грузит верхнюю карточку и извлекает цену.
"""
from .base import Tool, register
from ..obs.log import get_logger

log = get_logger("tools.find_product_link")


@register
class FindProductLinkTool(Tool):
    name = "find_product_link"
    description = ("На странице-листинге найти ссылки-карточки, подходящие искомому товару "
                   "(учитывает единицу измерения: 50 кг ≠ 10 кг). Возвращает топ URL с оценкой. "
                   "Страница берётся из кэша по URL. Без модели.")
    args_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "наименование искомого товара"},
            "url": {"type": "string", "description": "URL листинга (уже в кэше)"},
            "part_number": {"type": "string", "description": "парт-номер/артикул (опц.)"},
            "top_k": {"type": "integer", "description": "сколько ссылок вернуть (по умолчанию 5)"},
        },
        "required": ["name", "url"],
    }

    async def run(self, ctx, name, url, part_number=None, top_k=5, **kwargs) -> list[dict]:
        from ..extract.product_links import find_product_links
        from ..fetch import cache
        fetch = cache.get(url)
        html = (fetch or {}).get("html") or ""
        links = find_product_links(html, url, name, part_number=part_number, top_k=top_k)
        log.info("find_product_link '%s' @ %s → %d карточек%s", name, url, len(links),
                 (" (топ: %s)" % links[0]["url"]) if links else "")
        return links
