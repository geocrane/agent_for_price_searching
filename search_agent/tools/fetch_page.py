# -*- coding: utf-8 -*-
"""Скил `fetch_page` — загрузить и очистить страницу по URL (обёртка над fetch.Fetcher)."""
from .base import Tool, register
from ..obs.log import get_logger

log = get_logger("tools.fetch_page")


@register
class FetchPageTool(Tool):
    name = "fetch_page"
    description = ("Загрузить страницу по URL (браузер-first, антибот, паузы) и очистить контент "
                   "в Markdown + структурные данные. Цену не извлекает. Модель не зовётся.")
    args_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "адрес страницы"},
            "level": {"type": "string", "enum": ["fast", "basic", "strong", "max"],
                      "description": "режим открытия (антиблок): fast=httpx … max=браузер+профиль"},
        },
        "required": ["url"],
    }

    async def run(self, ctx, url, level=None, **kwargs):
        from ..config import load_settings, resolve_fetch
        settings = (ctx.settings if ctx and ctx.settings else load_settings())
        if ctx and ctx.fetcher is not None:                 # переиспускаемый браузер (агент/пайплайн)
            res = await ctx.fetcher.fetch_one(url)
        else:                                               # разовый вызов — поднять/закрыть временный
            from ..fetch.fetcher import Fetcher
            cfg = resolve_fetch(settings.fetch, {"preset": level} if level else None)
            fetcher = await Fetcher(cfg, settings.clean).start()
            try:
                res = await fetcher.fetch_one(url)
            finally:
                await fetcher.close()
        log.info("fetch_page: %s → http=%s blocked=%s chars=%s",
                 url, res.get("status"), res.get("blocked"), res.get("chars"))
        return {"url": res.get("url"), "status": res.get("status"), "blocked": res.get("blocked"),
                "markdown": res.get("markdown", ""), "structured": res.get("structured", {}),
                "chars": res.get("chars", 0)}
