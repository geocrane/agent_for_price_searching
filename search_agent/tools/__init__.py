# -*- coding: utf-8 -*-
"""Реестр скилов агента. Импорт модулей-скилов регистрирует их в общем реестре."""
from .base import Tool, ToolContext, ToolRegistry, registry, register

# Регистрируем встроенные скилы (импорт модуля-скила = регистрация в общем реестре).
from . import example         # noqa: F401  — пример-заглушка, демонстрирует паттерн
from . import find_sources    # noqa: F401  — M2: товар → источники
from . import fetch_page      # noqa: F401  — M3: URL → контент
from . import inspect_page      # noqa: F401  — §8: триаж страницы (карточка/листинг/блок/пусто)
from . import find_product_link  # noqa: F401  — §8: листинг → ссылки-карточки
from . import escalate_fetch      # noqa: F401  — §8: усиление загрузки (спец-эндпоинт/антибот)
from . import extract_prices  # noqa: F401  — M4: страница → цены+match (модель)
from . import adjudicate      # noqa: F401  — M4: цены → надёжный итог + диапазон
from . import write_rationale  # noqa: F401  — M7: свод позиции → комментарий по источникам (модель)
from . import save_history    # noqa: F401  — M5: прогон → SQLite-история (все цены + dead-letter)
from . import web_search      # noqa: F401  — M9: общий веб-поиск (обзоры/реестры, не только товар)
from . import read_page       # noqa: F401  — M9: страница → выжимка под вопрос
from . import probe_source    # noqa: F401  — M9: аттестация источника данных (модель + проверка)
from . import ask_user        # noqa: F401  — M9: уточняющий вопрос (пауза цикла чата)

__all__ = ["Tool", "ToolContext", "ToolRegistry", "registry", "register"]
