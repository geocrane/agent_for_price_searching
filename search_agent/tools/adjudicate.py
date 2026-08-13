# -*- coding: utf-8 -*-
"""Скил `adjudicate_prices` — свод цен товара к надёжному итогу + диапазону (обёртка, БЕЗ модели)."""
from .base import Tool, register
from ..obs.log import get_logger

log = get_logger("tools.adjudicate")


@register
class AdjudicatePricesTool(Tool):
    name = "adjudicate_prices"
    description = ("Свести все найденные цены товара к наиболее достоверной итоговой цене + диапазон "
                   "(min/max), отсеяв фейки/приманки и выбросы. Детерминированно, без модели.")
    args_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "наименование товара"},
            "prices": {"type": "array", "description": "список найденных цен (PriceCandidate)",
                       "items": {"type": "object"}},
        },
        "required": ["name", "prices"],
    }

    async def run(self, ctx, name, prices, **kwargs):
        from ..extract.adjudicate import adjudicate
        from ..models import PriceCandidate
        item = type("_It", (), {"row": 0, "name": name})()
        pcs = [p if isinstance(p, PriceCandidate) else PriceCandidate(**p) for p in prices]
        verdict = adjudicate(item, pcs)
        log.info("adjudicate_prices: '%s' → primary=%s, диапазон %s–%s",
                 name, verdict.primary.value if verdict.primary else None,
                 verdict.price_min, verdict.price_max)
        return verdict.model_dump()
