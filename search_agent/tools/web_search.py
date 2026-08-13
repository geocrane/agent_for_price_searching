# -*- coding: utf-8 -*-
"""Скил `web_search` — ОБЩИЙ веб-поиск по произвольному запросу (не товарный).

Отличие от `find_sources`: тот собирает запрос через `query_planner` (подмешивает товарный
аффикс «цена купить», чистит операторы) и ранжирует выдачу по тирам доверия магазинов — это
правильно для поиска цены и неправильно для «отзывы канистра металлическая против пластиковой»
или «реестр контрактов историческая цена». Здесь запрос модели уходит в бэкенд КАК ЕСТЬ.

Бэкенд берём из общей фабрики (`discovery._build_backend`) — скил не привязан к Serper
(правило search-backend-agnostic). Пагинация ленивая: вторую страницу агент запрашивает сам,
если на первой не нашлось нужного; глубже второй не ходим никогда — каждая страница это
отдельный оплачиваемый запрос.
"""
from .base import Tool, register
from ..obs.log import get_logger

log = get_logger("tools.web_search")

MAX_PAGE = 2                       # жёсткий потолок глубины выдачи (см. модуль-док)


@register
class WebSearchTool(Tool):
    name = "web_search"
    description = ("Общий поиск в интернете по произвольному запросу (обзоры, отзывы, реестры, "
                   "базы данных, статьи). Возвращает ссылки с заголовками и сниппетами. "
                   "Страницы не открывает, модель внутри не зовёт.")
    args_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "поисковый запрос как есть"},
            "site": {"type": "string",
                     "description": "искать только внутри домена (напр. 'example.gov.ru')"},
            "limit": {"type": "integer", "description": "сколько результатов вернуть (опц.)"},
            "page": {"type": "integer",
                     "description": "страница выдачи, 1 или 2; вторую — только если на первой "
                                    "не нашлось нужного"},
        },
        "required": ["query"],
    }

    async def run(self, ctx, query, site=None, limit=None, page=1, **kwargs):
        import httpx
        from ..config import load_settings
        from ..discovery import BROWSER_UA, _build_backend, _cand_dict
        settings = (ctx.settings if ctx and ctx.settings else load_settings())
        q = (query or "").strip()
        if not q:
            return {"query": "", "results": [], "note": "пустой запрос"}
        if site:
            dom = str(site).strip().lower().split("//")[-1].split("/")[0]
            if dom and ("site:" + dom) not in q:
                q = "site:%s %s" % (dom, q)
        try:
            page = max(1, min(MAX_PAGE, int(page or 1)))
        except (TypeError, ValueError):
            page = 1
        top = int(limit or settings.discovery.max_results or 10)

        backend, backend_name, _ = _build_backend(settings)
        if page > 1 and not getattr(backend, "supports_paging", False):
            log.info("web_search: бэкенд %s не умеет страницу %d — пропуск", backend_name, page)
            return {"query": q, "page": page, "results": [],
                    "note": "бэкенд не поддерживает страницы глубже первой"}
        async with httpx.AsyncClient(headers={"User-Agent": BROWSER_UA}) as client:
            cands = await backend.discover(client, q, top, page=page)
        results = [{k: v for k, v in _cand_dict(c).items()
                    if k in ("url", "domain", "title", "snippet", "is_file")} for c in cands[:top]]
        log.info("web_search: %r (стр. %d, %s) → %d результатов", q, page, backend_name, len(results))
        return {"query": q, "page": page, "results": results}
