# -*- coding: utf-8 -*-
"""Скил `probe_source` — аттестация источника данных: есть ли там нужное и за какой период.

Смысл разведки не в том, чтобы найти сайт, а в том, чтобы УБЕДИТЬСЯ, что нужные данные на нём
есть. Поэтому скил открывает источник (а при необходимости — его разделы «поиск/реестр/выгрузка»),
показывает текст модели и требует дословных доказательств, которые затем проверяет по странице.

Второй (после `extract_prices`) скил, который зовёт модель внутри себя: без модели разведка
выродилась бы в пересказ поисковых сниппетов. Вердикт при этом считает КОД (`probe.decide_verdict`)
из проверенных фактов — доступа, покрытия периода и выживших цитат.
"""
from .base import Tool, register
from ..obs.log import get_logger

log = get_logger("tools.probe_source")


@register
class ProbeSourceTool(Tool):
    name = "probe_source"
    description = ("Проверить источник данных: открыть его (и разделы с данными) и убедиться, "
                   "что нужные данные там ЕСТЬ и за нужный период. Возвращает досье: что лежит, "
                   "период, доступ, форматы выгрузки, как добраться, ограничения, цитаты-"
                   "доказательства. Зовёт модель и проверяет её выводы по тексту страницы.")
    args_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "адрес источника"},
            "need": {"type": "string",
                     "description": "какие данные нужны (напр. 'исторические цены закупок на X')"},
            "period": {"type": "string",
                       "description": "требуемый период: '2022-2025', 'с 2020', 'за последние 3 года'"},
            "deep": {"type": "boolean",
                     "description": "обойти разделы источника с данными (по умолчанию да)"},
        },
        "required": ["url", "need"],
    }

    async def run(self, ctx, url, need, period=None, deep=True, **kwargs):
        from ..config import load_settings
        from ..research.excerpt import focus_text
        from ..research.probe import (SourceDossier, build_period, data_links, decide_verdict,
                                      detect_access, detect_export, detect_kinds)
        from ..research.sources import domain_of
        from ..research.verify import verify_source

        settings = (ctx.settings if ctx and ctx.settings else load_settings())
        rcfg = getattr(settings, "research", None)
        max_chars = rcfg.excerpt_max_chars if rcfg else 3000
        max_links = rcfg.probe_links if rcfg else 3
        use_model = (rcfg.verify_with_model if rcfg else True)

        fetcher, own = ctx.fetcher if ctx else None, False
        if fetcher is None:
            from ..fetch.fetcher import Fetcher
            fetcher = await Fetcher(settings.fetch, settings.clean).start()
            own = True
        try:
            pages = await _collect(fetcher, url, deep=bool(deep), max_links=max_links)
        finally:
            if own:
                await fetcher.close()

        main = pages[0]
        blocked = main.get("blocked")
        text = "\n".join(p.get("markdown") or "" for p in pages)
        html = main.get("html") or ""
        checked = [p.get("url") or url for p in pages]

        dossier = SourceDossier(
            url=main.get("url") or url, domain=domain_of(main.get("url") or url),
            title=_title(main), checked_urls=checked,
            kinds=detect_kinds(text), export=detect_export(text, html),
            period=build_period(text, period))
        dossier.access, code_ev = detect_access(text, blocked=blocked)

        if blocked and not text.strip():
            # Ничего не прочитали — притворяться проверкой нельзя.
            dossier.verdict = "не годится"
            dossier.evidence = code_ev
            dossier.note = "источник не открылся: %s" % blocked
            log.info("probe_source: %s заблокирован (%s)", url, blocked)
            return dossier.to_dict()

        excerpt = focus_text(text, "%s %s" % (need or "", period or ""), max_chars)
        links = data_links(html, main.get("url") or url, limit=max_links)

        verdict = await verify_source(
            ctx.llm_client if (ctx and use_model) else None,
            need=need, period=period, url=dossier.url, title=dossier.title,
            excerpt=excerpt, page_text=text, links=links,
            model=(ctx.model if ctx else None), blocked=blocked)

        dossier.evidence = list(verdict.get("evidence") or []) or code_ev
        dossier.period = verdict.get("period") or dossier.period
        dossier.access = verdict.get("access") or dossier.access
        dossier.kinds = _merge(dossier.kinds, verdict.get("kinds"))
        dossier.export = _merge(dossier.export, verdict.get("export"))
        dossier.how_to = verdict.get("how_to") or ""
        dossier.limits = verdict.get("limits") or ""
        dossier.note = "; ".join(x for x in [verdict.get("note"),
                                             "; ".join(verdict.get("check_notes") or [])] if x)
        dossier.verdict = decide_verdict(
            access=dossier.access, has_data=verdict.get("has_data") or "неизвестно",
            period_status=(dossier.period or {}).get("status") or "unknown",
            evidence=dossier.evidence, kinds=dossier.kinds)

        if ctx and ctx.emit:
            ctx.emit({"type": "chat_dossier", **dossier.to_dict()})
        log.info("probe_source: %s → %s (данные=%s, период=%s, цитат=%d)",
                 dossier.domain, dossier.verdict, verdict.get("has_data"),
                 (dossier.period or {}).get("status"), len(dossier.evidence))
        return dossier.to_dict()


async def _collect(fetcher, url: str, *, deep: bool, max_links: int) -> list[dict]:
    """Главная страница источника + до max_links его разделов, похожих на вход к данным.

    Разделы нужны потому, что на титульной странице портала данных обычно нет — там навигация;
    данные лежат за «Реестр», «Поиск», «Открытые данные», «Выгрузка».
    """
    from ..research.probe import data_links
    main = await fetcher.fetch_one(url) or {}
    pages = [main]
    if not deep or max_links <= 0:
        return pages
    for link in data_links(main.get("html") or "", main.get("url") or url, limit=max_links):
        try:
            sub = await fetcher.fetch_one(link)
        except Exception as exc:  # noqa: BLE001 — раздел не открылся, это не провал разведки
            log.debug("probe_source: раздел %s не открылся: %s", link, exc)
            continue
        if sub and (sub.get("markdown") or "").strip():
            pages.append(sub)
    return pages


def _title(page: dict) -> str:
    meta = ((page.get("structured") or {}).get("meta") or {})
    for key in ("og:title", "title"):
        if meta.get(key):
            return str(meta[key]).strip()[:200]
    for line in (page.get("markdown") or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()[:200]
    return ""


def _merge(base: list, extra) -> list:
    """Объединить признаки кода и модели, сохранив порядок и без дублей."""
    out = list(base or [])
    for x in (extra or []):
        s = str(x).strip()
        if s and s not in out:
            out.append(s)
    return out[:10]
