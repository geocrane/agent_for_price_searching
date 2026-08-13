# -*- coding: utf-8 -*-
"""Конвейер поиска цен: загрузка страниц и извлечение цены ОВЕРЛАПОМ.

Как только страницы очередного товара загружены — по нему СРАЗУ стартует извлечение цены
моделью, параллельно с загрузкой следующего товара (не ждём, пока распарсятся все). Разные
ресурсы (fetch = браузер/сеть, extract = модель) → оверлап безопасен и заметно сокращает
общее время. Это задел под будущий режим `run`/ReAct-агент — поверх тех же скилов
(`Fetcher.fetch_one`, `extract_prices`, `adjudicate`).
"""
import asyncio

from .config import load_settings
from .extract import (_ItemView, _emit, _extract_item_cands, infer_not_found_reason,
                      verdict_event)
from .extract.adjudicate import adjudicate
from .fetch.fetcher import Fetcher
from .models import PriceCandidate
from .obs.log import get_logger

log = get_logger("pipeline")


async def run_price_search(items_data: list[dict], *, fetch_cfg=None, clean_cfg=None,
                           extract_cfg=None, llm_client=None, model=None, emit=None,
                           source: str = "pipeline", persist: bool = True) -> list[dict]:
    """Загрузить страницы и извлечь цену по каждому товару с оверлапом этапов.

    items_data: [{row, name, part_number, candidates:[{url, domain, is_file, tier, weight}]}].
    Кладёт в кандидатов `fetch`/`prices`, в товар — `verdict`. Эмитит те же события, что fetch и
    extract (position/fetched/price/model_stream/usage_delta/verdict/stage_progress).
    """
    settings = load_settings()
    fetch_cfg = fetch_cfg or settings.fetch
    clean_cfg = clean_cfg or settings.clean
    extract_cfg = extract_cfg or settings.extract

    from .llm.orchestrator import build_executor
    fetcher = await Fetcher(fetch_cfg, clean_cfg).start()
    fetch_sem = asyncio.Semaphore(max(1, fetch_cfg.concurrency))
    # адаптивный лимит вызовов модели (8→4→1, таймаут+повтор) — один на весь конвейер
    model_executor = build_executor(extract_cfg, emit=emit, name="pipeline")
    total, done, lock = len(items_data), 0, asyncio.Lock()
    log.info("Конвейер: товаров=%d, fetch-паузы=%s, extract-конкурентность=%d",
             total, fetch_cfg.delay_range, extract_cfg.concurrency)

    async def fetch_item(it: dict) -> None:
        cands = [c for c in it.get("candidates", []) if not c.get("is_file")][:fetch_cfg.top_n_fetch]
        if not cands:
            await _emit(emit, {"type": "position", "row": it["row"], "stage": "fetch", "state": "skip"})
            return

        async def one(c):
            async with fetch_sem:
                await _emit(emit, {"type": "position", "row": it["row"], "stage": "fetch", "state": "active"})
                res = await fetcher.fetch_one(c["url"])
                c["fetch"] = res
                await _emit(emit, {"type": "fetched", "row": it["row"], "url": c["url"],
                                   "status": res["status"], "blocked": res["blocked"], "chars": res["chars"]})

        await asyncio.gather(*[one(c) for c in cands])
        ok = any(c.get("fetch") and not c["fetch"]["blocked"] for c in cands)
        await _emit(emit, {"type": "position", "row": it["row"], "stage": "fetch",
                           "state": "done" if ok else "error"})

    async def extract_item(it: dict) -> None:
        nonlocal done
        item = _ItemView(it)
        await _extract_item_cands(it, item, llm_client, extract_cfg, model, emit, model_executor)
        prices = [PriceCandidate(**p) for c in it.get("candidates", []) for p in c.get("prices", [])]
        v = adjudicate(item, prices)
        it["verdict"] = v.model_dump()
        reason = infer_not_found_reason(it.get("candidates", [])) if v.primary is None else None
        it["not_found_reason"] = reason.value if reason else None
        await _emit(emit, verdict_event(it.get("row"), v, it["not_found_reason"]))
        async with lock:
            done += 1
            await _emit(emit, {"type": "stage_progress", "stage": "price",
                               "item_done": done, "item_total": total, "row": it.get("row")})

    extract_tasks: list[asyncio.Task] = []
    try:
        # fetch по товарам последовательно (браузер 1-на-домен), extract каждого стартует сразу
        # и перекрывается с загрузкой следующего.
        for it in items_data:
            await fetch_item(it)
            extract_tasks.append(asyncio.create_task(extract_item(it)))
        if extract_tasks:
            await asyncio.gather(*extract_tasks)
    finally:
        await fetcher.close()
    log.info("Конвейер завершён")
    if persist:                                         # M5: сохранить прогон в историю (все цены + dead-letter)
        from .storage import persist_run_async
        res = await persist_run_async(items_data, source=source, cfg=settings.storage)
        if res.get("run_id"):
            await _emit(emit, {"type": "history_saved", **res})
    return items_data
