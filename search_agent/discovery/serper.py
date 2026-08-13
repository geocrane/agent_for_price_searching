# -*- coding: utf-8 -*-
"""Бэкенд обнаружения через Serper.dev — реальная выдача Google (в т.ч. РФ).

Официальный search-API (не скрейпинг): надёжно, без капчи. Регион/язык — gl/hl (ru).
Ключ (X-API-KEY) НЕ логируется. Файловые ссылки (xlsx/csv/pdf) помечаются is_file.
"""
import asyncio
import os
import time
from urllib.parse import urlparse

import httpx

from .base import DiscoveryBackend
from ..models import Candidate
from ..obs.log import get_logger, log_event

log = get_logger("discovery.serper")

API_URL = "https://google.serper.dev/search"
FILE_EXT = (".xlsx", ".xls", ".csv", ".pdf")


class SerperBackend(DiscoveryBackend):
    name = "serper"
    supports_paging = True              # проверено: page=2,3 отдают полностью новые домены

    def __init__(self, api_key: str, region: str = "ru", lang: str = "ru", delay: float = 0.3):
        self.api_key = api_key
        self.region = region
        self.lang = lang
        self.delay = delay

    @property
    def key(self) -> str:
        """Ключ на МОМЕНТ запроса, а не на момент сборки бэкенда.

        Бесплатный тариф — 2500 запросов, и кончаются они посреди прогона. Бэкенд же создаётся
        один раз на весь прогон (`agent/run.py` → `_build_backend`), поэтому сохранённый в
        `__init__` ключ означал бы «новый токен подействует только со следующего запуска».
        UI кладёт новый ключ в `os.environ` сразу при сохранении — читаем его отсюда, и
        прогон продолжается с новым ключом без перезапуска сервера.
        """
        return os.environ.get("SERPER_API_KEY") or self.api_key

    async def discover(self, client: httpx.AsyncClient, query: str, limit: int,
                       page: int = 1) -> list[Candidate]:
        await asyncio.sleep(self.delay)     # лёгкая пауза (щадим лимиты free-tier)
        # `num` сервис игнорирует (замер: при 10/30/100 всё равно ~9 результатов), глубину даёт
        # только `page`. Каждая страница — отдельный оплачиваемый запрос, поэтому вторая
        # запрашивается лишь когда по позиции не нашлось ни одной цены.
        payload = {"q": query, "gl": self.region, "hl": self.lang, "num": min(max(limit, 10), 40)}
        if page > 1:
            payload["page"] = int(page)
        headers = {"X-API-KEY": self.key, "Content-Type": "application/json"}
        t0 = time.perf_counter()
        try:
            r = await client.post(API_URL, json=payload, headers=headers, timeout=20)
        except Exception as exc:  # noqa: BLE001
            log.warning("Serper недоступен (q=%r): %s", query, exc)
            return []
        dur = (time.perf_counter() - t0) * 1000
        if r.status_code in (401, 403, 429):
            # Единственная причина, по которой поиск встаёт у пользователя: кончились запросы
            # тарифа или ключ отозван. Техническое «HTTP 403 {...}» об этом не говорит, а
            # действие требуется конкретное — вставить новый токен. Строка статуса в UI
            # показывает последнее сообщение лога, так что этого канала достаточно.
            log.warning("Serper: запросы по ключу закончились или ключ недействителен "
                        "(HTTP %s) — вставьте новый токен в «Модель и поиск». Запрос: %r",
                        r.status_code, query)
            return []
        if r.status_code != 200:
            # тело может содержать причину (напр. лимит/невалидный ключ) — ключ НЕ логируем
            log.warning("Serper HTTP %s (q=%r): %s", r.status_code, query, r.text[:160])
            return []
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            log.warning("Serper вернул не JSON (q=%r)", query)
            return []

        cands: list[Candidate] = []
        for it in (data.get("organic") or [])[:limit]:
            u = it.get("link") or ""
            if not u:
                continue
            cands.append(Candidate(
                url=u, title=it.get("title") or "", snippet=it.get("snippet") or "",
                engine="google/serper", domain=urlparse(u).netloc.lower(),
                is_file=u.lower().split("?")[0].endswith(FILE_EXT),
            ))
        log_event(log, "serper.query", level=(20 if cands else 30), q=query,
                  results=len(cands), dur_ms=round(dur))
        return cands
