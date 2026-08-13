# -*- coding: utf-8 -*-
"""Паттерн скилов (§5.0 ТЗ): единый интерфейс Tool + реестр.

Новый скилл = подкласс Tool с @register — и он сразу доступен агенту (для
tool-calling), пайплайну и песочнице webchat. Ядро при этом не меняется.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..obs.log import get_logger

log = get_logger("tools")


@dataclass
class ToolContext:
    """Ресурсы окружения, передаваемые скилу при ВЫЗОВЕ (не при регистрации).

    Пайплайн/агент держат один контекст и передают его скилам. Так скил остаётся stateless,
    а зависимости (модель, настройки, эмиссия событий, переиспускаемый браузер) — снаружи.
    """
    settings: Any = None            # search_agent.config.Settings
    llm_client: Any = None          # LLMClient (нужен только extract-скилу)
    emit: Any = None                # колбэк событий для UI (position/price/…)
    fetcher: Any = None             # переиспускаемый fetch.Fetcher (дорогой браузер) — опц.
    model: str | None = None        # имя модели для http/LM Studio (опц.)


class Tool(ABC):
    """Базовый интерфейс инструмента-скилла.

    Атрибуты класса:
      name        — уникальное имя (для tool-calling и реестра)
      description — краткое описание для модели
      args_schema — JSON-схема аргументов (parameters в tool-calling)

    Скилы асинхронны: `async def run(self, ctx, **kwargs)`. Ресурсы берут из ctx (ToolContext),
    аргументы (**kwargs) приходят от вызывающего/модели по args_schema.
    """
    name: str = ""
    description: str = ""
    args_schema: dict = {}

    @abstractmethod
    async def run(self, ctx: "ToolContext", **kwargs) -> Any:
        """Выполнить инструмент. Реализуется в подклассе."""
        raise NotImplementedError

    def schema(self) -> dict:
        """Схема для tool-calling (name/description/parameters)."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.args_schema or {"type": "object", "properties": {}},
        }


class ToolRegistry:
    """Реестр зарегистрированных скилов."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if not tool.name:
            raise ValueError("У скилла должно быть непустое имя (name).")
        if tool.name in self._tools:
            raise ValueError("Скилл с именем %r уже зарегистрирован." % tool.name)
        self._tools[tool.name] = tool
        log.debug("Зарегистрирован скилл: %s", tool.name)
        return tool

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self._tools.values()]


# Глобальный реестр.
registry = ToolRegistry()


def register(cls: type[Tool]) -> type[Tool]:
    """Декоратор класса-скилла: инстанцирует и регистрирует в глобальном реестре."""
    registry.register(cls())
    return cls
