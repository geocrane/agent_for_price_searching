# -*- coding: utf-8 -*-
"""Контекст диалога — точка расширения под работу чата с результатами поиска по таблице.

Сейчас чат самостоятелен: пользователь спрашивает про товар или источник данных, агент ищет
с нуля. Следующим шагом появится другое: «перепроверь позицию 7 из таблицы», «прокомментируй,
почему по этой строке цена такая». Чтобы это не потребовало переписывать цикл агента, точка
подключения задаётся сразу:

  * `summary()` — что агенту знать о сессии (короткий блок в системный промпт);
  * `extra_tools()` — какие дополнительные скилы доступны в этом контексте;
  * `snippet(ref)` — выдать данные по ссылке из диалога (позиция таблицы, источник).

В этой итерации `TableContext` умеет только рассказать о загруженной таблице; скилов не
добавляет и данные позиций в модель не отдаёт. Это сознательно: расширять контракт легко,
а откатывать поведение, к которому пользователь привык, — нет.
"""
from ..obs.log import get_logger

log = get_logger("research.context")


class ChatContext:
    """Базовый контракт контекста. Наследники переопределяют то, что умеют."""

    kind = "none"

    def summary(self) -> str:
        return ""

    def extra_tools(self) -> list[str]:
        return []

    def snippet(self, ref: str) -> str:
        return ""


class NullContext(ChatContext):
    """Контекста нет — чат работает сам по себе (режим по умолчанию)."""
    kind = "none"


class TableContext(ChatContext):
    """Загруженная таблица номенклатуры текущей сессии.

    ЗАДЕЛ: агент знает, что таблица есть, и может честно сказать пользователю, что работа с её
    результатами появится отдельно. Содержимое позиций в модель НЕ уходит — это следующий этап
    (скилы `table_item` / `recheck_item`), и делать его молча, без явного решения, неправильно.
    """
    kind = "table"

    def __init__(self, items=None, filename: str = "") -> None:
        self.items = list(items or [])
        self.filename = filename or ""

    def summary(self) -> str:
        if not self.items:
            return ""
        done = sum(1 for it in self.items if (it.get("verdict") or {}).get("primary"))
        src = (" из файла «%s»" % self.filename) if self.filename else ""
        return ("В рабочей сессии загружена таблица%s: позиций %d, с найденной ценой %d. "
                "Работа с её результатами (перепроверка отдельных позиций, комментарии к строкам) "
                "в этом режиме пока недоступна — если пользователь просит именно это, скажи "
                "прямо и предложи задать вопрос по конкретному товару." % (src, len(self.items), done))

    def snippet(self, ref: str) -> str:
        """Заготовка: строка таблицы по номеру. Пока не подключено к промпту."""
        try:
            row = int(str(ref).strip())
        except (TypeError, ValueError):
            return ""
        for it in self.items:
            if it.get("row") == row:
                return ("%s %s" % (it.get("name") or "", it.get("part_number") or "")).strip()
        return ""


def build_context(items=None, filename: str = "") -> ChatContext:
    """Выбрать контекст по состоянию сессии (нет таблицы — NullContext)."""
    return TableContext(items, filename) if items else NullContext()
