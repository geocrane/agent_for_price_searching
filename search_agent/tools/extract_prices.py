# -*- coding: utf-8 -*-
"""Скил `extract_prices` — цены искомого товара со страницы + match (обёртка над extract).

Единственный скил, который ВНУТРИ зовёт модель (экстрактор) — через ctx.llm_client.
"""
from .base import Tool, register
from ..obs.log import get_logger

log = get_logger("tools.extract_prices")


@register
class ExtractPricesTool(Tool):
    name = "extract_prices"
    description = ("Извлечь цены искомого товара с загруженной страницы и определить соответствие "
                   "(точное/аналог по характеристикам). Использует модель. Страница берётся из кэша по URL.")
    args_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "наименование искомого товара"},
            "url": {"type": "string", "description": "URL уже загруженной (в кэше) страницы"},
            "part_number": {"type": "string", "description": "парт-номер/артикул (опц.)"},
            "domain": {"type": "string", "description": "домен источника (опц.)"},
            "tier": {"type": "integer", "description": "тир источника (опц.)"},
            "weight": {"type": "number", "description": "вес доверия источника (опц.)"},
        },
        "required": ["name", "url"],
    }

    async def run(self, ctx, name, url, part_number=None, domain=None, tier=None, weight=None, **kwargs):
        from ..extract import extract_prices
        from ..models import Item
        item = Item(row=0, name=name, part_number=part_number)
        cand = {"url": url, "domain": domain or "", "tier": tier, "weight": weight}
        cfg = ctx.settings.extract if (ctx and ctx.settings) else None
        prices = await extract_prices(item, cand, llm_client=(ctx.llm_client if ctx else None),
                                      cfg=cfg, model=(ctx.model if ctx else None),
                                      emit=(ctx.emit if ctx else None))
        log.info("extract_prices: '%s' @ %s → %d цен", name, domain or url, len(prices))
        return [p.model_dump() for p in prices]
