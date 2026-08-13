# -*- coding: utf-8 -*-
"""Пример-заглушка скилла — демонстрирует и проверяет паттерн (§5.0 ТЗ).

Реальные скилы (discovery, fetch, extract, match, report…) добавляются так же:
подкласс Tool + @register + импорт в tools/__init__.py.
"""
from .base import Tool, register


@register
class EchoTool(Tool):
    name = "echo"
    description = "Тестовый скилл: возвращает переданный текст (проверка паттерна)."
    args_schema = {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "любой текст"}},
        "required": ["text"],
    }

    async def run(self, ctx, text: str = "", **kwargs) -> str:
        return text
