# -*- coding: utf-8 -*-
"""Лёгкий httpx-претрай (реалистичные заголовки). Опционально, для «лёгких» страниц.

По умолчанию основной путь — браузер (надёжность); httpx-претрай можно включить, чтобы
не гонять Chromium там, где хватает простого GET. Блок определяется в fetcher/antibot.
"""
import httpx

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


async def fetch(url: str, timeout: float = 20.0) -> dict:
    """GET через httpx → {status, html}. Ошибки пробрасываются вызывающему.

    БЕЗ http2: httpx включает его только при установленном пакете `h2`, а иначе кидает ImportError
    прямо на создании клиента. В заявленных зависимостях `h2` нет, поэтому с `http2=True` лёгкий
    путь падал на КАЖДОЙ странице — и весь прогон выглядел как «все сайты заблокированы».
    HTTP/1.1 для нашей задачи (один GET страницы) ничем не хуже.
    """
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=timeout) as c:
        r = await c.get(url)
        return {"status": r.status_code, "html": r.text}
