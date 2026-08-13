# -*- coding: utf-8 -*-
"""Оркестрированный прогон поиска цены (§9/M7-wiring): реальные обработчики на оркестраторе.

Собирает конвейер как ЗАДАЧИ поверх Orchestrator: fetch → inspect → (escalate | find_link →
fetch карточки | extract) → adjudicate. Модельные задачи (extract) идут одной полосой; fetch/
инспекция/поиск карточки — параллельно. Триаж порождает follow-up задания под БЮДЖЕТОМ на
позицию (защита от runaway-эскалаций). Свод позиции считается, когда по ней не осталось задач.

ПАРАЛЛЕЛЬНОСТЬ ПО ПОЗИЦИЯМ. На каждом этапе одновременно не более `agent.stage_positions` (5)
ПОЗИЦИЙ, а очередь на вход — по номеру строки (`RowGate`, agent/gates.py). Задачи одной позиции
делят один слот: у позиции бывает десяток кандидатов-URL, и они не должны занимать по слоту каждый.
Смысл в порядке: раньше лимиты стояли только на ресурсах (вкладки, квота поиска, вызовы модели),
все позиции сабмитились сразу, а `asyncio.Semaphore` пробуждает FIFO по времени — поэтому позиция
№10 получала цену, пока №2 и №3 стояли в очереди. Лимиты ресурсов остались ВНУТРИ гейтов этапов
(вкладки — `tab_gate`, квота поискового API — `_disc["gate"]`, модель — `AdaptiveExecutor`).

Отличие от `pipeline.run_price_search`: здесь есть очередь/полоса/бюджеты/`task`-события и
авто-политика триажа (листинг→карточка, блок→эскалация). Зависимости (fetcher, llm) внедряются.
"""
import asyncio
import time
from contextlib import asynccontextmanager

import httpx

from .gates import RowGate
from .orchestrator import Orchestrator
from .policy import plan_after_inspect
from .tasks import AgentTask, Priority, TaskState
from ..extract import (_ItemView, extract_prices, infer_not_found_reason, pick_on_request,
                       verdict_event)
from ..extract.adjudicate import adjudicate
from ..extract.inspect import inspect_fetch
from ..discovery.query_planner import DEFAULT_AFFIX
from ..discovery.rerank import name_tokens, relevance
from ..extract.product_links import find_product_links
from ..fetch.escalate import marketplace_endpoint
from ..llm.orchestrator import build_executor
from ..models import NotFoundReason, PriceCandidate, Verdict
from ..obs.journal import RunJournal
from ..obs.log import current_run_id, get_logger
from ..obs.timing import RunTimer
from .. import session as sess

log = get_logger("agent.run")


async def _emit(emit, ev):
    if emit is None:
        return
    r = emit(ev)
    if asyncio.iscoroutine(r):
        await r


class OrchestratedRun:
    """Конвейер поиска цены на оркестраторе. Один экземпляр — один прогон по списку позиций."""

    # Этапы под лимитом позиций. `adjudicate` намеренно БЕЗ гейта: это терминальный CPU-шаг, он не
    # потребляет ресурсов, а его задержка только оттягивала бы итог по строке; плюс
    # `_sweep_unfinished` зовёт его напрямую, минуя оркестратор.
    GATED_STAGES = ("discover", "fetch", "inspect", "find_link", "escalate", "extract")

    def __init__(self, items, *, settings, fetcher, llm_client=None, model=None, emit=None,
                 model_lanes: int = 1, budgets: dict | None = None,
                 fetch_concurrency: int | None = None, affix: str | None = None,
                 use_llm_queries: bool = False, journal=None, max_pages: int = 1,
                 stage_positions: int | None = None, enough_confirmations: int = 0) -> None:
        self.items = items
        self.settings = settings
        self.fetcher = fetcher
        self.llm = llm_client
        self.model = model
        self.affix = affix                           # фраза-суффикс к поисковому запросу (для discovery-стадии)
        self.use_llm_queries = use_llm_queries       # запросы формирует модель (иначе — правила)
        self._parsed: dict[int, dict] = {}           # разбор позиций моделью (предпроход)
        # Глубина выдачи. Вторая страница — не «настройка полноты», а реакция на пустой результат:
        # запрашивается лениво и только по тем позициям, где цены не нашлось совсем.
        # Потолок — вторая страница: третья и дальше не окупаются (дальше выдача уходит в
        # нерелевантное), поэтому лимит жёсткий и не настраивается наружу.
        self.max_pages = min(2, max(1, int(max_pages)))
        self._page: dict[int, int] = {}              # до какой страницы дошли по позиции
        # Домены, показавшие антибот в ЭТОМ прогоне. Ограничение только для них: домен, который
        # открывается, грузится без квоты — у крупного магазина много нужных карточек.
        self._blocked_domains: set[str] = set()
        # Гейт релевантности перед загрузкой: 0 общих слов — не грузим, ниже порога — в конец
        # очереди. Порог осознанно низкий: полезные источники ниже 0,2 в замерах не встречались.
        self.relevance_gate = bool(getattr(settings.discovery, "relevance_gate", True))
        self.relevance_low = float(getattr(settings.discovery, "relevance_low", 0.2))
        self._disc = None                            # ресурсы обнаружения (backend/client/семафор) — на время run()
        self.by_row = {it["row"]: it for it in items}

        # Журнал прогона оборачивает emit: каждое событие сначала ложится в
        # runs/logs/<run_id>.jsonl, потом уходит в UI. Так трасса полна по построению —
        # ни одна стадия не может «забыть» записать себя (см. obs/journal.py).
        self.journal = journal if journal is not None else RunJournal(current_run_id() or "cli")
        self.emit = self.journal.wrap(emit)

        # Пауза модели и повторы по лимитам API (llm/limits.py) должны быть ВИДНЫ: без этого
        # «ждём восстановления модели» и «зависли» на экране неразличимы. Клиент один на
        # прогон, поэтому и адресат событий у него один.
        if self.llm is not None and hasattr(self.llm, "set_emit"):
            self.llm.set_emit(self.emit)

        # Секундомер прогона: общее время и активное время каждой позиции (см. obs/timing.py).
        # Отсчёт от создания прогона — то есть от нажатия «Найти», а не от первого запроса.
        self.timer = RunTimer(started=self.journal.started)

        self.orch = Orchestrator(emit=self.emit, model_lanes=model_lanes)

        # Ровно N живых вкладок: единый лимит на всю загрузку (fetch всех товаров идёт
        # параллельно под ним → предсказуемое число одновременных доменов). human_input ⇒ 1.
        conc = fetch_concurrency if fetch_concurrency is not None else settings.fetch.concurrency
        if getattr(settings.fetch, "human_input", False):
            conc = 1            # страховка инварианта: штатно это делает config.normalize_fetch
        self.fetch_cap = max(1, int(conc))
        # Не Semaphore: вкладки — самое узкое место (по умолчанию их 2 против 5 позиций в этапе),
        # и FIFO-семафор отдавал бы их по времени прихода, обнуляя порядок по строкам.
        self.tab_gate = RowGate(self.fetch_cap, per_row=False, name="вкладки",
                                on_change=self._gate_changed)
        # Модель — адаптивно параллельно (8→4→1, таймаут+повтор), общий на прогон исполнитель.
        self.model_executor = build_executor(settings.extract, emit=self.emit, name="find")

        # Лимит ПОЗИЦИЙ на каждом этапе (см. модульный docstring и agent/gates.py).
        acfg = getattr(settings, "agent", None)
        base_cap = stage_positions if stage_positions is not None else \
            getattr(acfg, "stage_positions", 5)
        by_kind = dict(getattr(acfg, "stage_positions_by_kind", None) or {})
        self.stage_caps = {k: max(1, int(by_kind.get(k, base_cap))) for k in self.GATED_STAGES}
        self.gates = {k: RowGate(cap, per_row=True, name=k, on_change=self._gate_changed)
                      for k, cap in self.stage_caps.items()}
        self._load_dirty = asyncio.Event()        # «загрузка изменилась» → фоновый эмит stage_load
        self._load_task = None

        b = budgets or {}
        self.escalate_budget = int(b.get("escalate_per_row", 1))
        self.find_link_budget = int(b.get("find_link_per_row", 1))
        self.card_fetch_budget = int(b.get("extra_pages_per_row", 2))

        # Ранняя остановка позиции: сколько РАЗНЫХ доменов с ценой считать достаточным (0 — выкл).
        # Настройка пользователя (профиль поиска): она экономит бОльшую часть работы, но сужает
        # основу для разброса цен, поэтому решение не наше.
        self.enough_confirmations = max(0, int(enough_confirmations or 0))
        self._satisfied: set[int] = set()           # позиции, по которым цены уже достаточно

        self._pending: dict[int, int] = {}          # сколько задач по позиции ещё в работе
        self._adjudicated: set = set()
        self._esc_used: dict[int, int] = {}
        self._find_used: dict[int, int] = {}
        self._page_used: dict[int, int] = {}
        self._register()

    # ---- статус сайта в UI (событие site_update) -----------------------------

    async def _site(self, row, cand, status, *, note=None) -> None:
        """Проставить статус кандидата и отправить компактный апдейт в UI-таблицу.

        Прогресс («загрузка страницы») и итог («модель не нашла товар») пишутся в РАЗНЫЕ поля:
        иначе прогресс затирает причину отказа и в таблице у всех пустых сайтов оказывается
        «извлечение цены». Что показать — решает `session.visible_note` по статусу.
        """
        cand["status"] = status
        if note is not None:
            cand["result_note" if status in sess.TERMINAL else "progress_note"] = note
        s = sess.summarize_cand(cand)
        await _emit(self.emit, {"type": "site_update", "row": row, "url": cand.get("url"),
                                "domain": cand.get("domain", ""), "status": status,
                                "chars": s["chars"], "found": s["found"], "match": s["match"],
                                "price": s["price"], "comment": s["comment"],
                                "note": s["note"]})

    async def _fetch_load(self) -> None:
        """Видимая асинхронность: сколько вкладок сейчас открыто из скольких доступно."""
        await _emit(self.emit, {"type": "fetch_load", "active": self.tab_gate.snapshot()["slots"],
                                "cap": self.fetch_cap})

    # ---- слот этапа и видимость загрузки -------------------------------------

    @asynccontextmanager
    async def _stage(self, task, kind: str):
        """Слот этапа: внутри не более `stage_caps[kind]` ПОЗИЦИЙ, очередь по номеру строки.

        Гейт берётся ЗДЕСЬ, а не в `Orchestrator._run`, намеренно: обработчик считает задачи
        позиции в своём `finally: await self._after(row)`. Если ждать слот до вызова обработчика,
        отмена во время ожидания не даст этому `finally` сработать — `_pending[row]` останется
        больше нуля и строка НИКОГДА не получит свод.
        """
        gate = self.gates[kind]
        row, prio = task.row, getattr(task, "priority", Priority.NORMAL)
        if gate.try_acquire(row):
            entered_blocked = False
        else:
            # Ждём слот — и это видно в панели очереди, а не выглядит как «ничего не происходит».
            await self.orch.mark(task, TaskState.BLOCKED)
            await gate.acquire(row, prio)
            entered_blocked = True
        try:
            if entered_blocked:
                await self.orch.mark(task, TaskState.RUNNING)
            # Слот получен — с этой секунды идёт РАБОТА над позицией; всё, что было до, —
            # стояние в очереди и во время позиции не попадает (см. obs/timing.py).
            async with self.timer.work(row):
                yield
        finally:
            gate.release(row)                     # синхронный: отмена не может потерять слот

    def _gate_changed(self, _gate) -> None:
        """Синхронное уведомление от гейта: пометить, что срез загрузки пора отправить в UI."""
        try:
            self._load_dirty.set()
        except Exception:  # noqa: BLE001 — событие не важнее прогона
            pass

    async def _stage_load_loop(self) -> None:
        """Фоновый эмит `stage_load` со склейкой всплесков.

        Не эмитим на каждый acquire/release: `emit` обёрнут журналом (каждое событие — строка в
        runs/logs/<run_id>.jsonl), а операций на гейтах тысячи. Склейка: событие не чаще ~3 раз в
        секунду, зато загрузка этапов всегда видна пользователю.
        """
        try:
            while True:
                await self._load_dirty.wait()
                self._load_dirty.clear()
                await _emit(self.emit, {
                    "type": "stage_load", "positions": self.stage_caps,
                    "stages": [self.gates[k].snapshot() for k in self.GATED_STAGES],
                    "tabs": self.tab_gate.snapshot(),
                    "search_api": self._disc["gate"].snapshot() if self._disc else None})
                await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — телеметрия не имеет права ронять прогон
            log.warning("Срез загрузки этапов не отправлен: %s", exc, exc_info=True)

    def _register(self) -> None:
        self.orch.register("discover", self._h_discover)
        self.orch.register("fetch", self._h_fetch)
        self.orch.register("inspect", self._h_inspect)
        self.orch.register("find_link", self._h_find_link)
        self.orch.register("escalate", self._h_escalate)
        self.orch.register("extract", self._h_extract)
        self.orch.register("adjudicate", self._h_adjudicate)

    # ---- бюджеты на позицию --------------------------------------------------

    @staticmethod
    def _has(d: dict, row: int, limit: int) -> bool:
        return d.get(row, 0) < limit

    @staticmethod
    def _use(d: dict, row: int) -> None:
        d[row] = d.get(row, 0) + 1

    # ---- релевантность кандидата до загрузки ---------------------------------

    async def _submit_fetch(self, row: int, cand: dict, item_name: str) -> None:
        """Поставить загрузку кандидата, отсеяв заведомо чужую страницу и понизив сомнительную.

        Гейт детерминированный и НАМЕРЕННО осторожный (замер на реальной выдаче: ни один источник,
        давший цену, не имел перекрытия ниже 0,2):
          • перекрытие ровно 0 — ни одного общего значимого слова — не грузим совсем
            (реальный случай: «Колодки Chery» по запросу о маршрутизаторе Huawei);
          • ниже порога — НЕ выбрасываем, а ставим в конец очереди: ничего не теряем,
            тратится только позиция в очереди.
        """
        if row in self._satisfied:               # цены по позиции уже достаточно — новых не ставим
            await self._skip_rest(row, cand)
            return
        title, snippet = cand.get("title") or "", cand.get("snippet") or ""
        # Судить не по чему (карточка, найденная по ссылке с листинга; кандидат из фикстуры) —
        # значит, доказательств против нет. Отсутствие данных не равно нерелевантности.
        judgeable = bool(name_tokens(item_name)) and bool((title + snippet).strip())
        rel = relevance(item_name, title, snippet, cand.get("url") or "")
        cand["relevance"] = round(rel, 2)
        if self.relevance_gate and judgeable and rel <= 0.0:
            await self._site(row, cand, sess.STATUS_EMPTY,
                             note="не грузили: страница не о том товаре (нет общих слов с названием)")
            log.info("Пропуск нерелевантного источника %s (перекрытие 0): %s",
                     cand.get("domain", ""), (cand.get("title") or "")[:60])
            return
        low = self.relevance_gate and judgeable and rel < self.relevance_low
        self._submit("fetch", row, {"cand": cand},
                     priority=Priority.LOW if low else Priority.NORMAL)

    # ---- глубина выдачи (ленивая: только когда цены нет) ---------------------

    def _want_next_page(self, row: int) -> bool:
        """Можно ли заглянуть на следующую страницу выдачи по этой позиции.

        Условия: пользователь разрешил добор, у позиции уже была выдача (иначе проблема не в
        глубине, а в том, что поиск вообще ничего не дал), и лимит страниц не исчерпан.
        """
        if self.max_pages <= 1 or self._disc is None:
            return False
        if self._page.get(row, 1) >= self.max_pages:
            return False
        return bool(self.by_row[row].get("candidates"))

    def _use_next_page(self, row: int) -> None:
        self._page[row] = self._page.get(row, 1) + 1

    # ---- ранняя остановка позиции (порог из профиля) -------------------------

    def _priced_domains(self, row: int) -> set:
        """Домены, на которых по этой позиции УЖЕ найдена цена (считаем именно домены, не сайты).

        Две страницы одного магазина — одно подтверждение: цена там из одного прайса, и брать её
        за независимое совпадение нельзя.
        """
        return {(c.get("domain") or "").lower()
                for c in self.by_row[row].get("candidates", []) or []
                if c.get("prices")}

    async def _check_enough(self, row: int) -> None:
        """Набрала ли позиция нужное число подтверждений — тогда остальные сайты не трогаем."""
        if not self.enough_confirmations or row in self._satisfied:
            return
        doms = self._priced_domains(row)
        if len(doms) < self.enough_confirmations:
            return
        self._satisfied.add(row)
        log.info("Позиция r%s: цена подтверждена доменами (%d) — остальные сайты не трогаю: %s",
                 row, len(doms), ", ".join(sorted(d for d in doms if d)))
        await _emit(self.emit, {"type": "enough_prices", "row": row, "domains": sorted(doms),
                                "need": self.enough_confirmations})

    def _needs_work(self, cand: dict) -> bool:
        """Требует ли кандидат работы при ЭТИХ настройках.

        Сайт, пропущенный по достатку подтверждений, терминален только пока порог включён: снял
        пользователь порог — причина пропуска отпала, и при следующем запуске сайт надо смотреть.
        Иначе настройка не давала бы эффекта до ручного сброса прогресса, что необъяснимо.
        """
        if cand.get("status") == sess.STATUS_SKIPPED and not self.enough_confirmations:
            return True
        return not sess.is_terminal(cand)

    def _item_done(self, it: dict) -> bool:
        """Позиция закрыта: есть свод и ни один её сайт не требует работы (см. `_needs_work`)."""
        if not it.get("verdict"):
            return False
        cands = [c for c in it.get("candidates", []) or [] if not c.get("is_file")]
        return bool(cands) and not any(self._needs_work(c) for c in cands)

    async def _skip_rest(self, row: int, cand: dict) -> None:
        """Пометить кандидата пропущенным по достижении порога — молчаливой отмены быть не должно."""
        await self._site(row, cand, sess.STATUS_SKIPPED,
                         note="не понадобился: цена уже подтверждена (%d источников)"
                              % len(self._priced_domains(row)))

    # ---- постановка задач и учёт по позиции ----------------------------------

    def _submit(self, kind, row, payload, uses_model=False, priority=Priority.NORMAL):
        self._pending[row] = self._pending.get(row, 0) + 1
        self.orch.submit(AgentTask(kind=kind, row=row, payload=payload,
                                   uses_model=uses_model, priority=priority))

    async def _after(self, row) -> None:
        """Задача по позиции завершилась. Когда их не осталось — считаем свод (adjudicate)."""
        self._pending[row] = self._pending.get(row, 1) - 1
        if self._pending[row] <= 0 and row not in self._adjudicated:
            self._adjudicated.add(row)
            self.orch.submit(AgentTask(kind="adjudicate", row=row, payload={}, priority=Priority.HIGH))

    # ---- обработчики ---------------------------------------------------------

    async def _h_discover(self, task, orch):
        """Обнаружение источников по ОДНОМУ товару; сразу ставит загрузку найденных (пайплайн).

        Не ждём обнаружения остальных товаров: как только по этому товару пришла выдача —
        по нему тут же стартует загрузка/извлечение параллельно с поиском по другим (проблема
        асинхронности на этапе поисковых запросов).
        """
        row = task.row
        it = self.by_row[row]
        page = int(task.payload.get("page", 1) or 1)
        try:
            async with self._stage(task, "discover"):
                from ..discovery import _cand_dict, discover_for_item
                from ..models import Item
                item = Item(row=row, name=it.get("name") or "", part_number=it.get("part_number"))
                await _emit(self.emit, {"type": "position", "row": row, "stage": "query", "state": "active"})
                async with self._disc["gate"].hold(row):      # квота поискового API — ВНУТРИ гейта
                    try:
                        queries, cands = await discover_for_item(
                            item, self.settings, self.affix, self._disc["client"], self._disc["backend"],
                            parsed=self._parsed.get(row), page=page)
                    except Exception as exc:  # noqa: BLE001 — поиск капризен, не роняем прогон
                        log.error("Обнаружение row=%s (страница %d): %s", row, page, exc, exc_info=True)
                        queries, cands = [], []
                it["queries"] = queries
                # Реальный запрос и аффикс храним ПРИ позиции: отчёт и разбор потом должны
                # показывать не то, что мы собирались искать, а то, что действительно ушло в
                # поиск. Добор второй страницы запрос не меняет — перезаписи не боимся.
                if queries:
                    it["query"] = queries[0]
                it["affix"] = self.affix if self.affix is not None else DEFAULT_AFFIX
                # Добор второй страницы ДОПОЛНЯЕТ выдачу, а не заменяет её: уже проанализированные
                # сайты должны остаться в таблице и в своде.
                known = {c.get("url") for c in it.get("candidates") or []}
                fresh = [_cand_dict(c) for c in cands if c.url not in known]
                it.setdefault("candidates", []).extend(fresh)
                await _emit(self.emit, {"type": "candidates", "row": row, "queries": queries,
                                        "page": page, "added": len(fresh), "list": it["candidates"]})
                await _emit(self.emit, {"type": "position", "row": row, "stage": "query",
                                        "state": "done" if it["candidates"] else "error"})
                if page > 1:
                    log.info("Добор страницы %d по позиции r%s: новых источников %d", page, row, len(fresh))
                for c in fresh:                      # сразу грузим найденные (оверлап с поиском других)
                    if c.get("is_file") or sess.is_terminal(c):
                        continue
                    await self._submit_fetch(row, c, it.get("name") or "")
        finally:
            await self._after(row)

    async def _h_fetch(self, task, orch):
        row, cand = task.row, task.payload["cand"]
        try:
            # Порог мог быть достигнут, пока задача стояла в очереди: грузить эту страницу уже
            # незачем. Задачу не отменяем, а завершаем штатно — иначе не отработает учёт
            # `_after(row)` в finally и позиция никогда не получит свод.
            if row in self._satisfied:
                await self._skip_rest(row, cand)
                return
            async with self._stage(task, "fetch"):
                # Домен уже показал антибот в этом прогоне — остальные его страницы не трогаем.
                # Ограничение действует ТОЛЬКО на заблокированные: домен, который открывается,
                # грузится целиком, сколько бы у него ни было карточек.
                dom = (cand.get("domain") or "").lower()
                if dom and dom in self._blocked_domains:
                    await self._site(row, cand, sess.STATUS_BLOCKED,
                                     note="пропущено: домен уже дал блок в этом прогоне")
                    log.info("Пропуск %s — домен %s уже заблокирован в этом прогоне",
                             cand.get("url", ""), dom)
                    return
                await self._site(row, cand, sess.STATUS_FETCHING, note="загрузка страницы")
                await _emit(self.emit, {"type": "position", "row": row, "stage": "fetch", "state": "active"})
                async with self.tab_gate.hold(row):   # ровно N вкладок одновременно (предсказуемо)
                    # Ожидание вкладки — самое долгое место очереди, и порог мог быть достигнут
                    # именно за это время. Проверяем ещё раз, уже со слотом на руках: иначе
                    # страница грузится, хотя цена по позиции давно подтверждена.
                    if row in self._satisfied:
                        await self._skip_rest(row, cand)
                        return
                    await self._fetch_load()          # «открыто вкладок N/M» — видно в UI
                    res = await self.fetcher.fetch_one(cand["url"])
                await self._fetch_load()
                cand["fetch"] = res
                cand["chars"] = res.get("chars")
                await _emit(self.emit, {"type": "fetched", "row": row, "url": cand["url"],
                                        "status": res.get("status"), "blocked": res.get("blocked"),
                                        "chars": res.get("chars"), "via": res.get("via")})
                if res.get("blocked"):
                    # Запоминаем домен: остальные его страницы в этом прогоне не пробуем. Ошибка
                    # загрузки (таймаут, сеть) домен НЕ блокирует — это разовый сбой, а не антибот.
                    if dom:
                        self._blocked_domains.add(dom)
                    await self._site(row, cand, sess.STATUS_BLOCKED, note="блок: %s" % res.get("blocked"))
                elif res.get("error"):
                    await self._site(row, cand, sess.STATUS_ERROR, note="ошибка загрузки")
                else:
                    via = {"http": " (без браузера)", "browser": " (браузером)"}.get(res.get("via"), "")
                    await self._site(row, cand, sess.STATUS_FETCHED,
                                     note="страница загружена%s" % via)
                self._submit("inspect", row, {"cand": cand})
        finally:
            await self._after(row)

    async def _h_inspect(self, task, orch):
        row, cand = task.row, task.payload["cand"]
        try:
            async with self._stage(task, "inspect"):
                insp = inspect_fetch(cand.get("fetch"), url=cand["url"])
                cand["inspect"] = insp
                await _emit(self.emit, {"type": "inspect", "row": row, "url": cand["url"],
                                        "domain": cand.get("domain", ""), "kind": insp["kind"],
                                        "price_present": insp["price_present"], "reason": insp["reason"]})
                if insp["kind"] in ("blocked", "empty", "shell"):
                    await self._site(row, cand, sess.STATUS_EMPTY if insp["kind"] == "shell"
                                     else sess.STATUS_BLOCKED, note=str(insp.get("reason")))
                plan = plan_after_inspect(
                    insp, can_escalate=self._has(self._esc_used, row, self.escalate_budget),
                    can_find_link=self._has(self._find_used, row, self.find_link_budget))
                for step in plan:
                    if step == "escalate":
                        self._use(self._esc_used, row)
                        self._submit("escalate", row, {"cand": cand})
                    elif step == "find_link":
                        self._use(self._find_used, row)
                        self._submit("find_link", row, {"cand": cand})
                    elif step == "extract":
                        self._submit("extract", row, {"cand": cand}, uses_model=False)  # модель — через AdaptiveExecutor
        finally:
            await self._after(row)

    async def _h_find_link(self, task, orch):
        row, cand = task.row, task.payload["cand"]
        try:
            async with self._stage(task, "find_link"):
                it = self.by_row[row]
                html = (cand.get("fetch") or {}).get("html") or ""
                links = find_product_links(html, cand["url"], it.get("name", ""),
                                           part_number=it.get("part_number"), top_k=3)
                await _emit(self.emit, {"type": "find_link", "row": row, "url": cand["url"],
                                        "found": len(links), "top": links[0]["url"] if links else None})
                if links and self._has(self._page_used, row, self.card_fetch_budget):
                    self._use(self._page_used, row)
                    card = {"url": links[0]["url"], "domain": cand.get("domain", ""),
                            "tier": cand.get("tier"), "weight": cand.get("weight"),
                            "from_listing": cand["url"]}
                    it.setdefault("candidates", []).append(card)   # чтобы свод увидел её цены
                    self._submit("fetch", row, {"cand": card})
                else:                                              # карточки нет — пробуем листинг
                    self._submit("extract", row, {"cand": cand}, uses_model=False)  # модель — через AdaptiveExecutor
        finally:
            await self._after(row)

    async def _h_escalate(self, task, orch):
        row, cand = task.row, task.payload["cand"]
        try:
            async with self._stage(task, "escalate"):
                ep = marketplace_endpoint(cand["url"])
                if ep:
                    async with self.tab_gate.hold(row):   # под тем же лимитом вкладок
                        await self._fetch_load()
                        res = await self.fetcher.fetch_one(ep)
                    await self._fetch_load()
                    insp = inspect_fetch(res, url=ep)
                    await _emit(self.emit, {"type": "escalate", "row": row, "url": cand["url"],
                                            "via": "marketplace_json", "kind": insp["kind"]})
                    if insp["kind"] not in ("blocked", "empty"):
                        cand["fetch"], cand["url_effective"] = res, ep
                        self._submit("extract", row, {"cand": cand}, uses_model=False)  # модель — через AdaptiveExecutor
                # иначе кандидат остаётся заблокированным → учтётся в причине при своде
        finally:
            await self._after(row)

    async def _h_extract(self, task, orch):
        row, cand = task.row, task.payload["cand"]
        try:
            if row in self._satisfied:            # цены по позиции уже достаточно (см. _h_fetch)
                await self._skip_rest(row, cand)
                return
            async with self._stage(task, "extract"):
                await self._site(row, cand, sess.STATUS_EXTRACTING, note="извлечение цены")
                await _emit(self.emit, {"type": "position", "row": row, "stage": "fetch", "state": "done"})
                await _emit(self.emit, {"type": "position", "row": row, "stage": "extract", "state": "active"})
                item = _ItemView(self.by_row[row])
                timed_out = False
                try:
                    # модель — через адаптивный исполнитель (параллельно, 8→4→1 при таймаутах)
                    prices = await self.model_executor.run(lambda: extract_prices(
                        item, cand, llm_client=self.llm, cfg=self.settings.extract,
                        model=self.model, emit=self.emit))
                except (asyncio.TimeoutError, TimeoutError):
                    log.warning("Извлечение отменено по таймауту модели: %s", cand.get("url", ""))
                    prices, timed_out = [], True
                except Exception as exc:  # noqa: BLE001 — сбой модели/транспорта на одном сайте
                    # Без этого сайт навсегда оставался в статусе «извлечение…»: причина не видна,
                    # позиция выглядит незавершённой. Помечаем ошибкой и идём к своду дальше.
                    log.error("Извлечение упало на %s: %s", cand.get("url", ""), exc, exc_info=True)
                    cand["prices"] = []
                    await self._site(row, cand, sess.STATUS_ERROR,
                                     note="ошибка извлечения: %s" % exc)
                    return
                cand["prices"] = [p.model_dump() for p in prices]
                # Итог по сайту всегда объясняется словами: «пусто» без причины пользователю
                # ничего не сообщает, а причина у пустого результата всегда есть.
                if prices:
                    status, note = sess.STATUS_DONE, "найдено предложений: %d" % len(prices)
                elif cand.get("on_request"):      # товар на странице есть, цена — по запросу
                    status, note = sess.STATUS_ON_REQUEST, "товар найден, цена только по запросу"
                elif timed_out:
                    status, note = sess.STATUS_ERROR, "таймаут модели — страница не проанализирована"
                elif cand.get("extract_failed"):
                    # Технический сбой (пустое тело ответа, неразбираемый JSON) — это НЕ «цены
                    # здесь нет»: страница не проанализирована. Смешивать их нельзя ни в UI, ни в
                    # отчёте, иначе сбой выглядит результатом работы.
                    status = sess.STATUS_ERROR
                    note = cand.get("extract_reason") or "сбой модели при извлечении"
                else:
                    status = sess.STATUS_EMPTY
                    note = cand.get("extract_reason") or "модель не нашла на странице искомый товар"
                await self._site(row, cand, status, note=note)
                await self._check_enough(row)     # набрали подтверждений — дальше не идём
        finally:
            await self._after(row)

    async def _h_adjudicate(self, task, orch):
        """Свод по позиции. Гейта у этого этапа нет (см. GATED_STAGES), очереди тут не бывает.

        Время закрывается ЗДЕСЬ: свод — последняя работа по позиции, и только после выхода из
        отрезка можно сложить активное время (внутри отрезок ещё открыт и в сумму не входит).
        """
        row = task.row
        async with self.timer.work(row):
            deeper = await self._adjudicate_row(row)
        if deeper:                                   # ушли добирать вторую страницу — не итог
            return
        it = self.by_row[row]
        it["active_s"] = round(self.timer.active_seconds(row), 1)
        await _emit(self.emit, {"type": "position_time", "row": row, "active_s": it["active_s"]})

    async def _adjudicate_row(self, row) -> bool:
        """Свести цены позиции. True — цены нет и мы ушли за следующей страницей выдачи."""
        it = self.by_row[row]
        try:
            prices = [PriceCandidate(**p) for c in it.get("candidates", []) for p in c.get("prices", [])]
            v = adjudicate(_ItemView(it), prices)
            # Цены-цифры нет, но товар найден и продавец даёт цену по запросу — это не «не найдено».
            it["on_request"] = pick_on_request(it.get("candidates", [])) if v.primary is None else None
            reason = (infer_not_found_reason(it.get("candidates", []))
                      if v.primary is None and not it["on_request"] else None)
            it["not_found_reason"] = reason.value if reason else None
        except Exception as exc:  # noqa: BLE001 — свод не имеет права «промолчать»
            # Позиция обязана получить терминальное состояние, иначе строка в UI навсегда
            # остаётся без итога, а причина видна только в логах.
            log.error("Свод по позиции row=%s упал: %s", row, exc, exc_info=True)
            v = Verdict()
            it["on_request"] = None
            it["not_found_reason"] = "ошибка свода: %s" % exc
        # Цены нет ни на одном источнике первой страницы — единственный случай, когда есть смысл
        # заглянуть глубже в выдачу. Если цена найдена, вторая страница не нужна и не запрашивается:
        # каждая страница — отдельный оплачиваемый запрос к поиску.
        if v.primary is None and not it.get("on_request") and self._want_next_page(row):
            self._use_next_page(row)
            self._adjudicated.discard(row)           # свод пересчитается после новых источников
            log.info("По позиции r%s цен нет — добираю страницу %d выдачи", row, self._page[row])
            await _emit(self.emit, {"type": "search_deeper", "row": row, "page": self._page[row]})
            self._submit("discover", row, {"page": self._page[row]})
            return True

        it["verdict"] = v.model_dump()
        resolved = v.primary is not None or bool(it["on_request"])
        # Чипы стадий товара: извлечение закрыто, итог получен (или «не найдено»).
        await _emit(self.emit, {"type": "position", "row": row, "stage": "extract", "state": "done"})
        await _emit(self.emit, {"type": "position", "row": row, "stage": "match",
                                "state": "done" if resolved else "error"})
        await _emit(self.emit, verdict_event(row, v, it["not_found_reason"], it["on_request"]))
        await self._write_rationale(it)
        return False

    async def _write_rationale(self, it: dict) -> None:
        """Комментарий-обоснование по позиции — СРАЗУ после свода, пока идёт прогон.

        Так кнопка «Отчёт» не запускает новую работу модели: к моменту выгрузки комментарии уже
        написаны и лежат в сессии. Модель здесь и так занята прогоном, лишнего ожидания не
        добавляется, а расход токенов виден там же, где остальной (`usage_delta`).

        Комментарий — не цена: залипший или упавший вызов не имеет права задерживать позицию,
        поэтому есть таймаут, а при любом отказе берётся детерминированная сводка по источникам
        (пустой ячейки в отчёте быть не должно).
        """
        from ..report.rationale import write_rationale
        row = it.get("row")
        cfg = self.settings.report
        llm = None if (self.llm is None or self.orch.stopped or not cfg.use_model) else self.llm
        try:
            async with asyncio.timeout(float(getattr(cfg, "rationale_timeout", 60.0))):
                r = await write_rationale(it, llm_client=llm, model=self.model, cfg=cfg)
        except TimeoutError:          # отмену прогона НЕ перехватываем — она должна пройти дальше
            from ..report.rationale import fallback_rationale, offers_from_item
            from ..report.xlsx import norm_verdict
            nv = norm_verdict(it.get("verdict"))
            log.warning("Комментарий по позиции r%s не дождались (%.0fс) — беру сводку по источникам",
                        row, float(getattr(cfg, "rationale_timeout", 60.0)))
            r = {"text": fallback_rationale(it, nv, offers_from_item(it, limit=cfg.max_offers_in_comment),
                                            include_excluded=cfg.include_excluded),
                 "usage": None, "degraded": True,
                 "by": "сводка (таймаут %.0f с)" % float(getattr(cfg, "rationale_timeout", 60.0))}
        it["rationale"] = r["text"]
        it["rationale_by"] = r.get("by") or ("сводка" if r.get("degraded") else "модель")
        if r.get("usage"):
            await _emit(self.emit, {"type": "usage_delta", "usage": r["usage"], "row": row})
        await _emit(self.emit, {"type": "rationale", "row": row, "text": r["text"],
                                "degraded": r["degraded"], "by": it["rationale_by"]})

    async def _sweep_unfinished(self):
        """Досвести позиции, оставшиеся без свода: у строки ВСЕГДА есть терминальное состояние.

        Штатно свод ставится по обнулению счётчика задач позиции (`_after`). Это страховка на
        случай, если какая-то задача не дошла до учёта (упала не там, отменена, будущая правка
        сломала баланс): молча незавершённых строк в таблице быть не должно.

        Обработчик зовётся НАПРЯМУЮ, поэтому гейтов этапов не касается (они внутри `_stage`) —
        это осознанно: досвод идёт после прогона, ограничивать его нечем и незачем. Ветка добора
        второй страницы внутри свода здесь мертва, потому что `_disc` уже закрыт (см. `run()`),
        и задача в остановленный оркестратор не улетит.
        """
        if self.orch.stopped or self._disc is not None:   # остановлено или прогон ещё не свёрнут
            return
        stuck = [it["row"] for it in self.items if not it.get("verdict")]
        if not stuck:
            return
        log.warning("Позиции без свода после прогона: %s — досвожу принудительно", stuck)
        for row in stuck:
            await self._h_adjudicate(AgentTask(kind="adjudicate", row=row, payload={}), self.orch)

    async def _plan_queries(self, need_disc: list[dict]) -> None:
        """Предпроход: модель разбирает позиции пакетами (см. discovery/query_llm.py).

        Проходит ЦЕЛИКОМ до начала обработки: тексты запросов к поисковику известны заранее,
        и дальше работа идёт по понятному, неизменному списку. Пакеты — по одному за раз.

        Модель отдаёт ЧИСТОЕ НАЗВАНИЕ, а запрос собирает `plan_queries` — так пользовательский
        аффикс применяется всегда. Позиции, по которым модель не ответила или чей разбор не прошёл
        проверку на подмену, работают по правилам: потерять позицию нельзя.

        Разбор кэшируется по позиции (`storage.parse_cache`) и переживает перезапуск: повторный
        прогон того же файла модель не переспрашивает, а результат стабилен между прогонами.
        """
        if not self.use_llm_queries or self.llm is None:
            return
        from ..discovery.query_llm import parse_items_llm
        from ..storage import parse_cache

        rows = [{"row": it["row"], "name": it.get("name"), "part_number": it.get("part_number")}
                for it in need_disc]
        cached = parse_cache.load_many(rows, cfg=getattr(self.settings, "storage", None))
        self._parsed.update(cached)
        self._store_parsed(cached)
        todo = [r for r in rows if r["row"] not in cached]
        if not todo:
            log.info("Разбор позиций взят из кэша целиком (%d) — модель не зовём", len(cached))
        else:
            try:
                fresh = await parse_items_llm(
                    todo, llm_client=self.llm, model=self.model,
                    batch_size=self.settings.discovery.llm_queries_batch,
                    # БЕЗ адаптивного исполнителя: параллельности здесь нет, а его предел в 175 с
                    # оборвал бы крупный пакет на середине генерации. Своё ожидание — в query_llm.
                    emit=self.emit,                # расход токенов виден наравне с остальными
                    on_ready=self._on_batch_parsed,
                    should_stop=lambda: self.orch.stopped)   # «Стоп» не ждёт конца пакета
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — без разбора просто работаем правилами
                log.error("Позиции моделью не разобраны: %s — работаю по правилам", exc, exc_info=True)
                fresh = {}
            self._parsed.update(fresh)
            self._store_parsed(fresh)
        await _emit(self.emit, {"type": "queries_planned", "by_model": len(self._parsed),
                                "from_cache": len(cached), "total": len(need_disc)})

    def _on_batch_parsed(self, parsed: dict, rows=None) -> None:
        """Готовый пакет разбора — сразу в сами товары И СРАЗУ В КЭШ.

        В поиск позиции при этом НЕ уходят: обработка начинается, когда разобран весь файл.
        Класть разбор по мере готовности нужно потому, что предразбор прерывают: «Стоп»
        посреди пакета бросает CancelledError, и сохранение «по завершении всего цикла» до
        кэша просто не доходило — 40 разобранных позиций (265 с и 13 260 токенов) уходили
        в мусор, а следующий прогон переспрашивал их заново.
        """
        parsed = parsed or {}
        self._parsed.update(parsed)
        self._store_parsed(parsed)
        if not rows:
            return
        try:
            from ..storage import parse_cache
            batch = [{"row": r, "name": (self.by_row.get(r) or {}).get("name"),
                      "part_number": (self.by_row.get(r) or {}).get("part_number")}
                     for r in rows]
            parse_cache.save_many(batch, parsed, cfg=getattr(self.settings, "storage", None))
        except Exception as exc:  # noqa: BLE001 — кэш не важнее самого разбора
            log.warning("Разбор пакета не сохранён в кэш: %s", exc)

    def _store_parsed(self, parsed: dict) -> None:
        """Положить разбор в сам товар: свод берёт из него бренд и признак родовой позиции."""
        for row, data in (parsed or {}).items():
            it = self.by_row.get(row)
            if it is not None and data:
                it["parsed"] = data

    # ---- запуск --------------------------------------------------------------

    async def run(self):
        self.journal.start(items=len(self.items), model=self.model, affix=self.affix,
                           fetch_backend=self.settings.fetch.backend,
                           fetch_concurrency=self.fetch_cap,
                           anti_detect=self.settings.fetch.anti_detect,
                           extract_concurrency=self.settings.extract.concurrency,
                           stage_positions=self.stage_caps,
                           use_llm_queries=self.use_llm_queries)
        log.info("Лимит позиций на этапах: %s; вкладок одновременно: %d",
                 ", ".join("%s=%d" % (k, v) for k, v in self.stage_caps.items()), self.fetch_cap)
        # Проблема №4: проверяем ВСЕ не-файловые кандидаты выдачи (без обрезки top_n).
        # Резюмирование: завершённые сайты/товары пропускаем; незакрытые — догружаем
        # (fetch кэширован по URL, повторный заход мгновенный и заново страницу не тянет).
        # Асинхронность: товары без выдачи идут через стадию «discover» — по мере нахождения
        # источников по товару СРАЗУ стартует его загрузка, параллельно с поиском по другим.
        need_disc = [it for it in self.items
                     if not self._item_done(it)
                     and not [c for c in it.get("candidates", []) if not c.get("is_file")]]
        if need_disc:
            from ..discovery import BROWSER_UA, _build_backend
            backend, backend_name, dconc = _build_backend(self.settings)
            # Квота поискового API — лимит РЕСУРСА внутри гейта этапа «discover». Очередь по
            # номеру строки и здесь: иначе ранняя позиция ждала бы запрос поздней.
            self._disc = {"backend": backend,
                          "gate": RowGate(max(1, dconc), per_row=False, name="поиск",
                                          on_change=self._gate_changed),
                          "client": httpx.AsyncClient(headers={"User-Agent": BROWSER_UA})}
            log.info("Обнаружение в движке: бэкенд=%s, товаров=%d, конкурентность=%d",
                     backend_name, len(need_disc), dconc)
        self._load_task = asyncio.create_task(self._stage_load_loop())
        # ТЕКСТЫ ЗАПРОСОВ ГОТОВЯТСЯ ДО НАЧАЛА ОБРАБОТКИ. Разбор идёт пакетами по одному и
        # целиком; только после него ставятся задачи. Так порядок работы предсказуем, разбор не
        # конкурирует за модель с извлечением цен, а пользователь видит один понятный этап
        # «разбор наименований» вместо чересполосицы. Плата за это — ожидание перед первой
        # ценой на большом файле; ход разбора виден в событиях parse_wait и во вкладке «Модель».
        if need_disc:
            await self._plan_queries(need_disc)
        try:
            for it in self.items:
                if self.orch.stopped:              # «Стоп» во время разбора — работу не начинаем
                    log.info("Прогон остановлен на разборе наименований — задачи не ставлю")
                    break
                row = it["row"]
                if self._item_done(it):            # товар полностью закрыт в прошлой сессии
                    self._adjudicated.add(row)
                    continue
                cands = [c for c in it.get("candidates", []) if not c.get("is_file")]
                if not cands:                      # нет выдачи → сначала обнаружение (пайплайн)
                    self._submit("discover", row, {})
                    continue
                submitted = 0
                for c in cands:
                    if not self._needs_work(c):    # сайт уже проанализирован/заблокирован
                        continue
                    await self._submit_fetch(row, c, it.get("name") or "")
                    submitted += 1
                if submitted == 0:                 # частичный товар без новых задач — свести сразу
                    self._adjudicated.add(row)
                    self.orch.submit(AgentTask(kind="adjudicate", row=row, payload={},
                                               priority=Priority.HIGH))
            await self.orch.run()
        finally:
            if self._load_task is not None:        # фоновый эмит загрузки не должен переживать прогон
                self._load_task.cancel()
                await asyncio.gather(self._load_task, return_exceptions=True)
                self._load_task = None
            if self._disc is not None:
                await self._disc["client"].aclose()
                self._disc = None
        await self._sweep_unfinished()
        await self._refill_unverified()
        log.info("Оркестрированный прогон завершён: позиций=%d", len(self.items))
        if getattr(self.settings, "storage", None) and self.settings.storage.enabled:
            from ..storage import persist_run_async     # M5: история (все цены + dead-letter)
            res = await persist_run_async(self.items, source="orchestrated", cfg=self.settings.storage)
            if res.get("run_id"):
                await _emit(self.emit, {"type": "history_saved", **res})
        await self._report_metrics()
        return self.items

    def _unverified_cands(self) -> list[tuple[dict, dict]]:
        """Страницы, цена с которых не проверена моделью: [(товар, кандидат)]."""
        out = []
        for it in self.items:
            for cand in it.get("candidates", []):
                if cand.get("is_file") or not cand.get("prices"):
                    continue
                if all(p.get("verified", True) for p in cand["prices"]):
                    continue
                out.append((it, cand))
        return out

    async def _refill_unverified(self):
        """Добор: переспросить модель по страницам, где цену пришлось взять из разметки.

        Страховка на случай, когда шлюз модели (llm/limits.py) всё же исчерпал ожидание —
        например, платформа была перегружена дольше `llm.degrade_after_s`. Страницы уже лежат
        в кэше загрузки, сеть не трогаем: платим только за вызовы модели, и только по тем
        позициям, где иначе в отчёт уедет непроверенное число.

        Ошибка здесь ничего не рушит: непроверенная цена и так помечена, добор её лишь уточняет.
        """
        if self.llm is None or self.orch.stopped:
            return
        todo = self._unverified_cands()
        if not todo:
            return
        rows = {it["row"] for it, _ in todo}
        log.info("Добор непроверенных: страниц=%d по %d позициям (страницы из кэша, платим "
                 "только за модель)", len(todo), len(rows))
        await _emit(self.emit, {"type": "refill", "state": "start",
                                "pages": len(todo), "rows": len(rows)})
        fixed = 0
        for idx, (it, cand) in enumerate(todo, 1):
            if self.orch.stopped:
                break
            try:
                prices = await extract_prices(
                    _ItemView(it), cand, llm_client=self.llm, cfg=self.settings.extract,
                    model=self.model, emit=self.emit, idx=idx, count=len(todo))
            except Exception as exc:  # noqa: BLE001 — добор не имеет права уронить готовый прогон
                log.warning("Добор не удался (%s): %s", cand.get("url", ""), exc)
                continue
            if prices and all(p.verified for p in prices):
                cand["prices"] = [p.model_dump() for p in prices]
                fixed += 1
            await _emit(self.emit, {"type": "refill", "state": "progress",
                                    "done": idx, "pages": len(todo), "fixed": fixed})
        if fixed:                                    # цены изменились — своды по этим позициям тоже
            for row in sorted(rows):
                await self._h_adjudicate(AgentTask(kind="adjudicate", row=row, payload={}),
                                         self.orch)
        log.info("Добор непроверенных: уточнено %d страниц из %d", fixed, len(todo))
        await _emit(self.emit, {"type": "refill", "state": "done",
                                "pages": len(todo), "fixed": fixed, "rows": len(rows)})

    async def _report_metrics(self) -> None:
        """Итог прогона в цифрах: в журнал, в лог и в UI. Считается ПОСЛЕ всех сводов.

        Расчёт не имеет права уронить уже сделанную работу, поэтому любая ошибка здесь только
        логируется: прогон к этому моменту завершён и результат у пользователя на руках.
        """
        try:
            from ..obs.metrics import format_metrics, run_metrics
            m = run_metrics(self.items, usage=self.journal.usage,
                            duration_s=self.timer.finish())
            for line in format_metrics(m).splitlines():
                log.info("%s", line)
            await _emit(self.emit, {"type": "run_metrics", **m})
            self.journal.finish(metrics=m)
        except Exception as exc:  # noqa: BLE001 — метрики не важнее результата прогона
            log.warning("Метрики прогона не посчитаны: %s", exc, exc_info=True)
            self.journal.finish(metrics_error=str(exc))

    async def stop(self):
        await self.orch.stop()
        # Ожидающих на гейтах уже нет (их задачи отменены), но снимаем и сами гейты: висящий
        # waiter не даст следующему acquire получить слот, а прогон уже свёрнут.
        for gate in list(self.gates.values()) + [self.tab_gate]:
            gate.cancel_all()
        if self._disc is not None:
            self._disc["gate"].cancel_all()


async def run_orchestrated(items, *, settings, fetcher, llm_client=None, model=None, emit=None,
                           model_lanes: int = 1, budgets: dict | None = None,
                           fetch_concurrency: int | None = None, affix: str | None = None,
                           max_pages: int = 1, stage_positions: int | None = None,
                           enough_confirmations: int = 0):
    """Удобная обёртка: собрать OrchestratedRun и выполнить. Возвращает items со сводами."""
    run = OrchestratedRun(items, settings=settings, fetcher=fetcher, llm_client=llm_client,
                          model=model, emit=emit, model_lanes=model_lanes, budgets=budgets,
                          fetch_concurrency=fetch_concurrency, affix=affix, max_pages=max_pages,
                          stage_positions=stage_positions,
                          enough_confirmations=enough_confirmations)
    return await run.run()
