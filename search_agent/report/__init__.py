# -*- coding: utf-8 -*-
"""Итоговый Excel-отчёт (M7): своды по позициям → комментарии модели → .xlsx.

Колонки: Товар, Цена, Диапазон найденных цен, Комментарий модели по источникам.
`build_report` — единая точка: пишет комментарий по каждой позиции (модель или фолбэк,
параллельно под семафором), затем сериализует строки в файл. Эмитит события для UI.
"""
import asyncio

from . import checkpoint
from .rationale import write_rationale
from .xlsx import (COLUMNS, DETAIL_COLUMNS, SITE_COLUMNS, build_detail_rows, build_row,
                   build_rows, build_site_rows, money, norm_verdict, write_xlsx)
from ..obs.log import get_logger

log = get_logger("report")

__all__ = ["build_report", "build_rows", "build_row", "build_detail_rows", "build_site_rows",
           "write_xlsx", "write_rationale", "checkpoint", "COLUMNS", "DETAIL_COLUMNS",
           "SITE_COLUMNS", "money", "norm_verdict"]


async def _emit(emit, ev):
    if emit is None:
        return
    r = emit(ev)
    if asyncio.iscoroutine(r):
        await r


async def build_report(items: list[dict], path: str, *, llm_client=None, model=None,
                       cfg=None, emit=None, sheet_title: str = "Итоги",
                       use_model: bool = False) -> dict:
    """Собрать отчёт по списку товаров (со сводами) и записать .xlsx.

    Комментарии по позициям к этому моменту УЖЕ написаны — их пишет прогон сразу после свода
    (`agent/run.py:_write_rationale`). Поэтому сборка отчёта по умолчанию не зовёт модель вовсе:
    нажатие «Отчёт» обязано отдать файл сразу, а не запускать десятки платных запросов, каждый
    из которых способен подвесить выгрузку. Позиция без готового комментария получает
    детерминированную сводку по источникам — пустой ячейки не остаётся.

    `use_model=True` — явно попросить модель переписать ВСЕ комментарии заново (путь CLI).
    Если же модель передана, а `use_model=False`, она достаётся только позициям, у которых
    комментария нет вовсе: их прогон либо не успел написать, либо получил отказ по лимиту API.
    Пересобирать готовые при этом незачем — это и деньги, и минуты ожидания файла.

    Возвращает {path, total, with_price, not_found, sites}.
    """
    from ..config import ReportConfig
    cfg = cfg or ReportConfig()
    total = len(items)
    has_model = bool(cfg.use_model and llm_client is not None)
    need_model = bool(use_model and has_model)        # переписать ВСЕ комментарии заново
    log.info("Формирование отчёта: позиций=%d, комментарии=%s, файл=%s", total,
             "модель (по запросу)" if need_model else "готовые из прогона / сводка", path)
    sem = asyncio.Semaphore(max(1, int(cfg.concurrency)))
    done, lock = 0, asyncio.Lock()

    async def one(it: dict) -> None:
        nonlocal done
        row = it.get("row")
        if not need_model and (it.get("rationale") or "").strip():
            return                                    # комментарий уже написан прогоном
        await _emit(emit, {"type": "position", "row": row, "stage": "report", "state": "active"})
        async with sem:
            r = await write_rationale(it, llm_client=llm_client if has_model else None,
                                      model=model, cfg=cfg)
        it["rationale"] = r["text"]
        it["rationale_by"] = r.get("by") or ("сводка" if r.get("degraded") else "модель")
        if r.get("usage"):
            await _emit(emit, {"type": "usage_delta", "usage": r["usage"], "row": row})
        await _emit(emit, {"type": "rationale", "row": row, "text": r["text"],
                           "degraded": r["degraded"], "by": it["rationale_by"]})
        await _emit(emit, {"type": "position", "row": row, "stage": "report", "state": "done"})
        async with lock:
            done += 1
            await _emit(emit, {"type": "stage_progress", "stage": "report",
                               "item_done": done, "item_total": total, "row": row})

    await asyncio.gather(*[one(it) for it in items])

    rows = build_rows(items)
    detail_rows = build_detail_rows(items)
    site_rows = build_site_rows(items)
    write_xlsx(rows, path, sheet_title=sheet_title, detail_rows=detail_rows, site_rows=site_rows)
    with_price = sum(1 for r in rows if r["_found"])
    summary = {"path": path, "total": total, "with_price": with_price,
               "not_found": total - with_price, "sites": len(site_rows)}
    await _emit(emit, {"type": "report", **summary})
    log.info("Отчёт готов: %s (с ценой=%d/%d, строк цен=%d, сайтов=%d)", path,
             with_price, total, len(detail_rows), len(site_rows))
    return summary
