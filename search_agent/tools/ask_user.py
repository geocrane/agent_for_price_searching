# -*- coding: utf-8 -*-
"""Скил `ask_user` — задать уточняющий вопрос и приостановить цикл.

Действия не выполняет: цикл агента перехватывает его ДО реестра, отдаёт вопрос в чат и
останавливается, сохранив состояние. Следующее сообщение пользователя продолжает тот же ход —
поиск не начинается заново.

Зачем он в реестре, если не выполняется: чтобы описание и схема аргументов попадали в системный
промпт из одного источника (`registry.schemas()`), как у остальных скилов. Прямой вызов
возвращает вопрос как данные — это делает скил безопасным, если его когда-нибудь дёрнут вне чата.
"""
from .base import Tool, register
from ..obs.log import get_logger

log = get_logger("tools.ask_user")


@register
class AskUserTool(Tool):
    name = "ask_user"
    description = ("Задать пользователю УТОЧНЯЮЩИЙ вопрос и дождаться ответа. Вызывай только "
                   "когда вопрос допускает разные прочтения и от ответа зависит результат.")
    args_schema = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "короткий вопрос по существу"},
            "options": {"type": "array", "items": {"type": "string"},
                        "description": "варианты ответа, если уместны (опц.)"},
        },
        "required": ["question"],
    }

    async def run(self, ctx, question, options=None, **kwargs):
        q = (question or "").strip()
        opts = [str(o).strip() for o in (options or []) if str(o).strip()][:5]
        log.info("ask_user: %s", q[:160])
        return {"question": q, "options": opts, "paused": True}
