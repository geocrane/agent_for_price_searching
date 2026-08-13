# -*- coding: utf-8 -*-
"""Интерфейс слоя обнаружения (discovery): один контракт на любую поисковую систему.

Бэкенды взаимозаменяемы (сейчас — Serper, дальше могут быть другие). Всё, что знает о конкретной системе,
живёт в её модуле; вызывающий код работает только с этим интерфейсом, поэтому смена поисковика не
требует правок в движке.

`page` — номер страницы выдачи, 1-based. Это часть ОБЩЕГО контракта, а не особенность конкретного
поставщика: вторая страница добирается лениво, только когда по позиции не нашлось ни одной цены
(см. `agent/run.py`). Бэкенд, который пагинацию не поддерживает, на page > 1 возвращает пустой
список — вызывающий код это переживает.
"""
from abc import ABC, abstractmethod

import httpx

from ..models import Candidate


class DiscoveryBackend(ABC):
    name: str = ""
    supports_paging: bool = False       # умеет ли бэкенд отдавать страницы глубже первой

    @abstractmethod
    async def discover(self, client: httpx.AsyncClient, query: str, limit: int,
                       page: int = 1) -> list[Candidate]:
        """Вернуть кандидатов по одному запросу. page — 1-based номер страницы выдачи."""
        raise NotImplementedError
