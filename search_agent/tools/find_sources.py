# -*- coding: utf-8 -*-
"""Скил `find_sources` — товар → ранжированные источники (обёртка над discovery)."""
from .base import Tool, register
from ..obs.log import get_logger

log = get_logger("tools.find_sources")


@register
class FindSourcesTool(Tool):
    name = "find_sources"
    description = ("Найти источники с ценой на товар: поисковая выдача → ранжированные кандидаты "
                   "(магазины/каталоги/маркетплейсы) с тиром доверия. Модель внутри не зовётся.")
    args_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "наименование товара"},
            "part_number": {"type": "string", "description": "парт-номер/артикул (опц.)"},
            "affix": {"type": "string",
                      "description": "фраза-суффикс к наименованию (напр. 'купить цена'); пусто — дефолт"},
            "top_n": {"type": "integer", "description": "сколько кандидатов вернуть (опц.)"},
        },
        "required": ["name"],
    }

    async def run(self, ctx, name, part_number=None, affix=None, top_n=None, **kwargs):
        import httpx
        from ..config import load_settings
        from ..discovery import BROWSER_UA, _build_backend, _cand_dict, discover_for_item
        from ..models import Item
        settings = (ctx.settings if ctx and ctx.settings else load_settings())
        if top_n:
            settings.discovery.top_n = top_n
        item = Item(row=0, name=name, part_number=part_number)
        backend, backend_name, _ = _build_backend(settings)
        async with httpx.AsyncClient(headers={"User-Agent": BROWSER_UA}) as client:
            queries, cands = await discover_for_item(item, settings, affix, client, backend)
        log.info("find_sources: '%s' → %d кандидатов (бэкенд %s)", name, len(cands), backend_name)
        return {"queries": queries, "candidates": [_cand_dict(c) for c in cands]}
