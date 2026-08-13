# -*- coding: utf-8 -*-
"""Скил `write_rationale` — комментарий по источникам для позиции отчёта (модель + фолбэк).

Единственный скил отчёта, что зовёт модель (как extract). Оркестратор-агент вызывает его по
каждой позиции перед сборкой файла; ресурсы (llm_client/model) берутся из ToolContext.
"""
from .base import Tool, register
from ..obs.log import get_logger

log = get_logger("tools.write_rationale")


@register
class WriteRationaleTool(Tool):
    name = "write_rationale"
    description = ("Написать краткий комментарий-обоснование итоговой цены товара по найденным "
                   "источникам (для отчёта): на чём основана цена, разброс, оговорки (аналог, "
                   "единица измерения, отсеянные приманки). Использует модель; без модели — "
                   "детерминированная сводка по источникам.")
    args_schema = {
        "type": "object",
        "properties": {
            "item": {
                "type": "object",
                "description": ("товар со сводом: {name, part_number, verdict, "
                                "candidates|offers, not_found_reason}"),
            },
        },
        "required": ["item"],
    }

    async def run(self, ctx, item, **kwargs):
        from ..report.rationale import write_rationale
        cfg = getattr(ctx.settings, "report", None) if ctx.settings else None
        res = await write_rationale(item, llm_client=ctx.llm_client, model=ctx.model, cfg=cfg)
        log.info("write_rationale: r%s → %d симв. (%s)", item.get("row"), len(res["text"]),
                 "сводка" if res["degraded"] else "модель")
        return res
