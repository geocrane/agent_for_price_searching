# -*- coding: utf-8 -*-
"""Скил `read_page` — открыть страницу и вернуть КОМПАКТНУЮ выжимку под вопрос.

Почему не `fetch_page`: тот отдаёт весь markdown (20–40 тыс. символов). В пайплайне это нормально
(страница уходит в extract и не возвращается в диалог), а в чате такой ответ переполнит контекст
за два-три шага. Здесь модель получает только релевантные вопросу куски — остальное остаётся в
`runs/fetch_cache/` и доступно другим скилам по URL.

Заголовок и тип страницы (карточка/листинг/блок/пусто) отдаём отдельно: агенту важно понимать,
почему выжимка пустая — страница отбита антиботом или на ней действительно нет ответа.
"""
from .base import Tool, register
from ..obs.log import get_logger

log = get_logger("tools.read_page")


@register
class ReadPageTool(Tool):
    name = "read_page"
    description = ("Открыть страницу по URL и прочитать её под конкретный вопрос: возвращает "
                   "выжимку релевантных мест, заголовок и признак блокировки. Модель не зовёт.")
    args_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "адрес страницы"},
            "focus": {"type": "string",
                      "description": "что именно ищем на странице (вопрос/ключевые слова) — "
                                     "по нему отбираются фрагменты"},
            "max_chars": {"type": "integer", "description": "предел размера выжимки (опц.)"},
        },
        "required": ["url"],
    }

    async def run(self, ctx, url, focus=None, max_chars=None, **kwargs):
        from ..config import load_settings
        from ..extract.inspect import inspect_fetch
        from ..research.excerpt import focus_text
        from ..research.sources import domain_of
        settings = (ctx.settings if ctx and ctx.settings else load_settings())
        rcfg = getattr(settings, "research", None)
        limit = int(max_chars or (rcfg.excerpt_max_chars if rcfg else 3000))

        if ctx and ctx.fetcher is not None:                  # переиспускаемый браузер агента
            res = await ctx.fetcher.fetch_one(url)
        else:                                                # разовый вызов (CLI/тест)
            from ..fetch.fetcher import Fetcher
            fetcher = await Fetcher(settings.fetch, settings.clean).start()
            try:
                res = await fetcher.fetch_one(url)
            finally:
                await fetcher.close()

        res = res or {}
        markdown = res.get("markdown") or ""
        insp = inspect_fetch(res, url=url) or {}
        excerpt = focus_text(markdown, focus or "", limit) if markdown else ""
        out = {"url": res.get("url") or url, "domain": domain_of(res.get("url") or url),
               "title": _title(res, markdown), "status": res.get("status"),
               "blocked": res.get("blocked"), "chars": res.get("chars", 0),
               "kind": insp.get("kind"), "reason": insp.get("reason"), "excerpt": excerpt}
        log.info("read_page: %s → тип=%s, блок=%s, символов=%s, выжимка=%d",
                 url, out["kind"], out["blocked"], out["chars"], len(excerpt))
        return out


def _title(res: dict, markdown: str) -> str:
    """Заголовок страницы: из meta, иначе первый markdown-заголовок."""
    meta = ((res.get("structured") or {}).get("meta") or {})
    for key in ("og:title", "title"):
        val = meta.get(key)
        if val:
            return str(val).strip()[:200]
    for line in (markdown or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()[:200]
    return ""
