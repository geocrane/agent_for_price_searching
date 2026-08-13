# -*- coding: utf-8 -*-
"""Скил `inspect_page` (§8): триаж загруженной страницы — карточка/листинг/блок/пусто/прочее.

Детерминированный, БЕЗ модели. Берёт результат загрузки из кэша по URL (или переданный fetch)
и классифицирует его. Нужен агенту, чтобы «перепроверить, что получил», и выбрать следующее
действие (усилить антибот, зайти в карточку с листинга, извлекать цену) вместо тихого пропуска.
"""
from .base import Tool, register
from ..obs.log import get_logger

log = get_logger("tools.inspect_page")


@register
class InspectPageTool(Tool):
    name = "inspect_page"
    description = ("Перепроверить загруженную страницу и определить её тип: card (карточка), "
                   "listing (листинг/категория), blocked (антибот), empty (пусто) или other. "
                   "Детерминированно, без модели. Страница берётся из кэша по URL.")
    args_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL уже загруженной (в кэше) страницы"},
        },
        "required": ["url"],
    }

    async def run(self, ctx, url, **kwargs) -> dict:
        from ..extract.inspect import inspect_fetch
        from ..fetch import cache
        fetch = cache.get(url)
        insp = inspect_fetch(fetch, url=url)
        log.info("inspect_page %s → %s (%s)", url, insp["kind"], insp["reason"])
        return insp
