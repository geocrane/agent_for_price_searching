# -*- coding: utf-8 -*-
"""Хранилище: фикстуры выдачи discovery + SQLite-история прогонов (M5).

`persist_run` — записать завершённый прогон в историю (все офферы + своды + dead-letter).
Никогда не роняет прогон: любые ошибки БД логируются и глотаются.
"""
import asyncio

from . import discovery_cache, extract_cache, parse_cache
from .history import HistoryStore, item_key
from ..obs.log import get_logger

log = get_logger("storage")

__all__ = ["HistoryStore", "item_key", "persist_run", "persist_run_async",
           "discovery_cache", "extract_cache", "parse_cache"]


def persist_run(items: list[dict], *, source: str = "", db_path: str | None = None,
                cfg=None) -> dict:
    """Записать прогон в SQLite-историю. Возвращает {run_id, total, with_price, not_found}.

    cfg — StorageConfig (если не задан, грузим из настроек). Отключается cfg.enabled=False.
    """
    from ..config import StorageConfig, load_settings
    if cfg is None:
        cfg = load_settings().storage
    if not getattr(cfg, "enabled", True):
        log.info("История отключена (storage.enabled=False) — прогон не сохранён")
        return {"skipped": True}

    from ..report.xlsx import norm_verdict
    store = HistoryStore(db_path or cfg.db_path)
    try:
        run_id = store.start_run(source=source)
        with_price = offers_total = dead = 0
        for it in items:
            res = store.record_item(run_id, it)
            offers_total += res["offers"]
            with_price += 1 if res["has_price"] else 0
            dead += 1 if res["dead_letter"] else 0
        total = len(items)
        store.finish_run(run_id, total, with_price, total - with_price)
        log.info("История: прогон #%d сохранён — позиций=%d, с ценой=%d, офферов=%d, dead-letter=%d",
                 run_id, total, with_price, offers_total, dead)
        return {"run_id": run_id, "total": total, "with_price": with_price,
                "not_found": total - with_price, "offers": offers_total, "dead_letter": dead}
    except Exception as exc:  # noqa: BLE001 — история не должна ронять прогон
        log.warning("Не удалось сохранить историю прогона: %s", exc)
        return {"error": str(exc)}
    finally:
        store.close()


async def persist_run_async(items: list[dict], *, source: str = "", db_path: str | None = None,
                            cfg=None) -> dict:
    """Асинхронная обёртка: пишем SQLite в отдельном потоке (не блокируем event-loop)."""
    return await asyncio.to_thread(persist_run, items, source=source, db_path=db_path, cfg=cfg)
