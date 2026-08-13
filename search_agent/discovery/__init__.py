# -*- coding: utf-8 -*-
"""Слой обнаружения (M2): товар → ранжированные кандидаты (Serper + rerank).

Осторожно: ограниченная конкурентность по товарам, паузы в бэкенде, топ-N. Модель НЕ
зовём. Оркестратор эмитит события (position/candidates/progress) через колбэк `emit` —
UI показывает ход обнаружения live.
"""
import asyncio

import httpx

from .query_planner import plan_queries
from .rerank import rerank
from ..config import Settings, load_settings
from ..models import Candidate, Item
from ..obs.log import get_logger

log = get_logger("discovery")

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _build_backend(settings: Settings):
    """Собрать discovery-бэкенд. (backend, имя, конкурентность).

    Бэкенд один — Serper. Без ключа поиск невозможен, и молчать об этом нельзя: без явной
    ошибки прогон выглядел бы как «поиск ничего не нашёл» по всем позициям сразу.
    """
    if not settings.serper_api_key:
        raise RuntimeError(
            "Не задан ключ поиска SERPER_API_KEY. Задайте его в интерфейсе "
            "(кнопка «Модель» → «Токен поиска») или в .env — ключ берётся на serper.dev.")
    from .serper import SerperBackend
    return (SerperBackend(settings.serper_api_key, settings.serper_region, settings.serper_lang),
            "serper", max(1, settings.discovery.concurrency))


async def discover_for_item(item: Item, settings: Settings, affix: str | None,
                            client: httpx.AsyncClient, backend,
                            queries: list[str] | None = None,
                            name_clean: str | None = None, parsed: dict | None = None,
                            page: int = 1) -> tuple[list[str], list[Candidate]]:
    """Запросы + кандидаты по одному товару.

    parsed — разбор позиции моделью целиком (см. discovery/query_llm.py); из него берётся не
    только название, но и производитель с моделью — их `plan_queries` возвращает в запрос, если
    разбор их потерял. Запрос всё равно собирает `plan_queries`, поэтому аффикс пользователя
    применяется в любом случае. name_clean — то же самое одним полем, для вызовов без разбора.
    queries — полностью готовые строки (обход планировщика; используется в тестах и фикстурах).
    page — страница выдачи (1-based). Глубже первой ходим только когда по позиции не нашлось ни
    одной цены; решение об этом принимает движок (`agent/run.py`), а не этот слой.
    """
    if not queries:
        queries = plan_queries(item, affix=affix, max_queries=settings.discovery.queries_per_item,
                               name_clean=name_clean, parsed=parsed)
    if page > 1 and not getattr(backend, "supports_paging", False):
        log.info("Бэкенд %s не умеет страницы глубже первой — добор пропущен", backend.name)
        return queries, []
    cands: list[Candidate] = []
    for q in queries:                              # запросы по товару — последовательно
        cands.extend(await backend.discover(client, q, settings.discovery.max_results, page=page))
    top = rerank(item.name, item.part_number, cands, settings.sources or {},
                 top_n=settings.discovery.top_n)
    return queries, top


async def discover_items(items: list[Item], settings: Settings | None = None,
                         affix: str | None = None, emit=None) -> None:
    """Обнаружение по списку товаров с ограниченной конкурентностью и эмиссией событий."""
    settings = settings or load_settings()
    if affix is None:
        affix = settings.discovery.default_affix
    backend, backend_name, concurrency = _build_backend(settings)
    sem = asyncio.Semaphore(concurrency)
    total, done = len(items), 0
    log.info("Обнаружение: бэкенд=%s, товаров=%d, аффикс=%r, топ-N=%d, конкурентность=%d",
             backend_name, total, affix, settings.discovery.top_n, concurrency)

    async with httpx.AsyncClient(headers={"User-Agent": BROWSER_UA}) as client:
        async def one(it: Item):
            nonlocal done
            async with sem:
                await _emit(emit, {"type": "position", "row": it.row, "stage": "query", "state": "active"})
                try:
                    queries, cands = await discover_for_item(it, settings, affix, client, backend)
                except Exception as exc:  # noqa: BLE001
                    log.error("Обнаружение: ошибка row=%s: %s", it.row, exc, exc_info=True)
                    await _emit(emit, {"type": "position", "row": it.row, "stage": "query", "state": "error"})
                    cands, queries = [], []
                else:
                    await _emit(emit, {"type": "candidates", "row": it.row, "queries": queries,
                                       "list": [_cand_dict(c) for c in cands]})
                    await _emit(emit, {"type": "position", "row": it.row, "stage": "query",
                                       "state": "done" if cands else "error"})
                done += 1
                await _emit(emit, {"type": "progress", "done": done, "total": total})

        await asyncio.gather(*[one(it) for it in items])
    log.info("Обнаружение завершено: %d товаров", total)


async def discover_into(item_dicts: list[dict], settings: Settings | None = None,
                        affix: str | None = None, emit=None, only_missing: bool = True) -> list[dict]:
    """Обнаружение с привязкой кандидатов ПРЯМО к dict-товарам (для серверной сессии).

    В отличие от `discover_items` (только эмитит), кладёт `queries`/`candidates` в каждый
    dict. only_missing=True — ищем лишь по товарам без кандидатов (резюме: не переискиваем
    то, что уже найдено). Эмитит те же события (position/candidates/progress).
    """
    settings = settings or load_settings()
    if affix is None:
        affix = settings.discovery.default_affix
    targets = [d for d in item_dicts if not (only_missing and d.get("candidates"))]
    if not targets:
        return item_dicts
    backend, backend_name, concurrency = _build_backend(settings)
    sem = asyncio.Semaphore(concurrency)
    total, done = len(targets), 0
    log.info("Обнаружение (в сессию): бэкенд=%s, товаров=%d (из %d), аффикс=%r",
             backend_name, total, len(item_dicts), affix)

    async with httpx.AsyncClient(headers={"User-Agent": BROWSER_UA}) as client:
        async def one(d: dict):
            nonlocal done
            it = Item(row=d["row"], name=d.get("name") or "", part_number=d.get("part_number"))
            async with sem:
                await _emit(emit, {"type": "position", "row": it.row, "stage": "query", "state": "active"})
                try:
                    queries, cands = await discover_for_item(it, settings, affix, client, backend)
                except Exception as exc:  # noqa: BLE001
                    log.error("Обнаружение: ошибка row=%s: %s", it.row, exc, exc_info=True)
                    d.setdefault("candidates", [])
                    await _emit(emit, {"type": "position", "row": it.row, "stage": "query", "state": "error"})
                else:
                    d["queries"] = queries
                    d["candidates"] = [_cand_dict(c) for c in cands]
                    await _emit(emit, {"type": "candidates", "row": it.row, "queries": queries,
                                       "list": d["candidates"]})
                    await _emit(emit, {"type": "position", "row": it.row, "stage": "query",
                                       "state": "done" if cands else "error"})
                done += 1
                await _emit(emit, {"type": "progress", "done": done, "total": total})

        await asyncio.gather(*[one(d) for d in targets])
    return item_dicts


def _cand_dict(c: Candidate) -> dict:
    return {"url": c.url, "domain": c.domain, "title": c.title, "snippet": c.snippet,
            "engine": c.engine, "tier": c.tier, "weight": c.weight, "score": c.score,
            "is_file": c.is_file}


async def _emit(emit, event: dict) -> None:
    if emit is None:
        return
    res = emit(event)
    if asyncio.iscoroutine(res):
        await res
