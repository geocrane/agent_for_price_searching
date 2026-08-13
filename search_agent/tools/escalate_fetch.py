# -*- coding: utf-8 -*-
"""Скил `escalate_fetch` (§8): усилить загрузку проблемного URL (блок/пустой JS).

Порядок (дёшево → дорого), с жёстким бюджетом попыток:
  1) спец-эндпоинт маркетплейса (Ozon composer-api / WB card.wb.ru) — JSON без браузера;
  2) лестница пресетов «режима открытия» basic→strong→max (усиление антидетекта, рендер JS).
Останавливаемся, как только триаж перестаёт видеть блок/пустоту. Сам забор — через Fetcher
(с кэшем/антиботом/очисткой). Логику выбора см. в fetch/escalate.py (там же тесты).
"""
from .base import Tool, register
from ..obs.log import get_logger

log = get_logger("tools.escalate_fetch")


@register
class EscalateFetchTool(Tool):
    name = "escalate_fetch"
    description = ("Повторно загрузить проблемный URL сильнее: спец-эндпоинт маркетплейса "
                   "(Ozon/WB JSON), затем усиление антибота (basic→strong→max). Возвращает итог "
                   "триажа лучшей попытки. Есть бюджет попыток. Без модели.")
    args_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "проблемный URL"},
            "reason": {"type": "string", "description": "причина (blocked/empty), опц."},
            "max_attempts": {"type": "integer", "description": "бюджет попыток браузера (по умолчанию 2)"},
        },
        "required": ["url"],
    }

    async def run(self, ctx, url, reason=None, max_attempts=2, **kwargs) -> dict:
        from ..config import load_settings, resolve_fetch
        from ..extract.inspect import inspect_fetch
        from ..fetch.escalate import escalation_ladder, marketplace_endpoint
        from ..fetch.fetcher import Fetcher

        settings = (ctx.settings if ctx else None) or load_settings()
        emit = ctx.emit if ctx else None
        attempts: list[dict] = []

        async def _emit(ev):
            if emit is None:
                return
            r = emit(ev)
            if hasattr(r, "__await__"):
                await r

        def _record(strategy, res):
            insp = inspect_fetch(res, url=res.get("url", url))
            row = {"strategy": strategy, "url": res.get("url", url), "kind": insp["kind"],
                   "chars": res.get("chars", 0), "blocked": res.get("blocked")}
            attempts.append(row)
            log.info("escalate_fetch [%s] %s → %s (chars=%s)", strategy, row["url"], row["kind"], row["chars"])
            return insp

        def _ok(insp):
            return insp["kind"] not in ("blocked", "empty")

        await _emit({"type": "escalate", "url": url, "state": "start", "reason": reason})

        # 1) Спец-эндпоинт маркетплейса — дёшево, без браузера.
        ep = marketplace_endpoint(url)
        if ep:
            fetcher = await Fetcher(resolve_fetch(settings.fetch, {"preset": "fast"}), settings.clean).start()
            try:
                res = await fetcher.fetch_one(ep, use_cache=False)
            finally:
                await fetcher.close()
            insp = _record("marketplace_json", res)
            if _ok(insp):
                await _emit({"type": "escalate", "url": url, "state": "done", "via": "marketplace_json"})
                return {"resolved": True, "via": "marketplace_json", "url": ep, "attempts": attempts}

        # 2) Лестница пресетов (браузер, усиление антидетекта) — с бюджетом.
        for preset in escalation_ladder(reason)[:max(0, int(max_attempts))]:
            fetcher = await Fetcher(resolve_fetch(settings.fetch, {"preset": preset}), settings.clean).start()
            try:
                res = await fetcher.fetch_one(url, use_cache=False)
            except Exception as exc:  # noqa: BLE001 — браузерные режимы капризны, не роняем прогон
                log.warning("escalate_fetch preset=%s не удался: %s", preset, exc)
                continue
            finally:
                await fetcher.close()
            insp = _record("preset:%s" % preset, res)
            if _ok(insp):
                await _emit({"type": "escalate", "url": url, "state": "done", "via": preset})
                return {"resolved": True, "via": preset, "url": url, "attempts": attempts}

        await _emit({"type": "escalate", "url": url, "state": "failed"})
        return {"resolved": False, "via": None, "url": url, "attempts": attempts}
