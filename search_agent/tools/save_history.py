# -*- coding: utf-8 -*-
"""Скил `save_history` — записать завершённый прогон в SQLite-историю (M5, БЕЗ модели).

Сохраняет ВСЕ найденные цены (офферы) + своды + dead-letter (не найдено/заблокировано) для
истории цен во времени и повторного прохода. Путь к БД — из настроек (ToolContext.settings).
"""
from .base import Tool, register
from ..obs.log import get_logger

log = get_logger("tools.save_history")


@register
class SaveHistoryTool(Tool):
    name = "save_history"
    description = ("Записать прогон в историю (SQLite): все найденные цены + своды + dead-letter "
                   "(позиции без надёжной цены — для повторного прохода). Детерминированно, без модели.")
    args_schema = {
        "type": "object",
        "properties": {
            "items": {"type": "array", "description": "товары со сводами и кандидатами/офферами",
                      "items": {"type": "object"}},
            "source": {"type": "string", "description": "метка источника прогона (имя файла/режим)"},
        },
        "required": ["items"],
    }

    async def run(self, ctx, items, source="", **kwargs):
        from ..storage import persist_run_async
        cfg = getattr(ctx.settings, "storage", None) if ctx.settings else None
        res = await persist_run_async(items, source=source, cfg=cfg)
        log.info("save_history: %s", res)
        return res
