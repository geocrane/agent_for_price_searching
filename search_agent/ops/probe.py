# -*- coding: utf-8 -*-
"""probe — health-check источника: доступен ли, не блокирует ли (M2, лёгкая версия).

Проверяет URL/домен обычным httpx-GET с реалистичными заголовками. Полный браузерный
probe и проверка извлечения цены — на M3/M4.
"""
import re
import time

import httpx

from ..obs.log import get_logger, log_event

log = get_logger("ops.probe")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


async def probe(url: str) -> dict:
    """Проверить доступность источника. Возвращает dict со статусом."""
    if not re.match(r"^https?://", url):
        url = "https://" + url
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as c:
            r = await c.get(url)
        dur = (time.perf_counter() - t0) * 1000
        body = r.text[:3000].lower()
        blocked = r.status_code in (403, 429) or any(m in body for m in ("captcha", "antirobot", "доступ ограничен"))
        res = {"url": url, "reachable": True, "status": r.status_code, "blocked": blocked,
               "content_type": r.headers.get("content-type", ""), "bytes": len(r.content),
               "dur_ms": round(dur)}
    except Exception as exc:  # noqa: BLE001
        res = {"url": url, "reachable": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    log_event(log, "probe", level=(30 if (not res.get("reachable") or res.get("blocked")) else 20), **res)
    return res
