# -*- coding: utf-8 -*-
"""Оркестратор (§9/M7): очередь задач, бюджеты, глобальный стоп.

Задача, поставленная в `submit`, запускается СРАЗУ и работает параллельно с остальными.
Собственного лимита у оркестратора нет — и это осознанно: считать работу надо по позициям и по
ресурсам, а не по числу корутин, чем и занимаются гейты `agent/gates.py` (позиции на этапе,
вкладки браузера, квота поискового API) и адаптивный исполнитель модели (`llm/executor.py`).

Отдельной «полосы модели» здесь больше нет. Она существовала (приоритетная очередь + N
потребителей, разбирающих задачи с `uses_model`), но флаг `uses_model` не выставлял НИ ОДИН
вызов: все задачи шли мимо очереди, полоса стояла пустой, а настройка `model_lanes` в
config/agent.yaml не делала ничего. Сериализацию модели на деле обеспечивает исполнитель
(8→4→1 по таймаутам). Читать `submit` как «поставил — побежало» и видеть рядом мёртвый
механизм, обещающий обратное, — ровно та неоднозначность, из-за которой поведение системы
переставало быть предсказуемым.

Обработчик задачи регистрируется по kind: `async def handler(task, orch) -> result`. Внутри он
может докидывать follow-up задачи (`orch.submit(...)`) — так триаж порождает escalate/find_link.
События `task` эмитятся на каждом переходе состояния (для панели очереди и таймлайна в UI).

ЛИМИТ ПОЗИЦИЙ НА ЭТАПЕ живёт не здесь, а в обработчике: он берёт слот `RowGate` (agent/gates.py)
через `OrchestratedRun._stage(...)` и на время ожидания помечает задачу `BLOCKED` (`orch.mark`).
Так учёт задач позиции (`_after`/`_pending` в run.py) остаётся в `finally` обработчика: если ждать
слот ЗДЕСЬ, до вызова обработчика, то отмена во время ожидания не даст этому `finally` сработать и
строка навсегда останется без свода. Инвариант вложенности: гейт этапа — снаружи, лимит ресурса
(вкладки/квота поиска/исполнитель модели) — всегда внутри.
"""
import asyncio
import time
from contextlib import asynccontextmanager

from .tasks import AgentTask, TaskState
from ..obs.log import get_logger

log = get_logger("agent.orchestrator")


class Orchestrator:
    def __init__(self, *, emit=None) -> None:
        self._emit = emit
        self.tasks: dict[str, AgentTask] = {}
        self._handlers: dict[str, "callable"] = {}
        self._free: set[asyncio.Task] = set()
        self._active = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._stopped = False
        self._counters: dict[str, int] = {}

    @property
    def stopped(self) -> bool:
        """Цепочку остановил пользователь (кнопка «Стоп»), а не естественное завершение."""
        return self._stopped

    # ---- регистрация обработчиков -------------------------------------------

    def register(self, kind: str, handler) -> None:
        self._handlers[kind] = handler

    # ---- бюджеты (защита от runaway-эскалаций) -------------------------------

    def allow(self, key: str, limit: int) -> bool:
        """Разрешить очередное действие под бюджетом key (True) либо исчерпан (False)."""
        n = self._counters.get(key, 0)
        if n >= limit:
            return False
        self._counters[key] = n + 1
        return True

    # ---- события -------------------------------------------------------------

    def _emit_soon(self, ev: dict) -> None:
        if self._emit is None:
            return
        try:
            r = self._emit(ev)
            if asyncio.iscoroutine(r):
                asyncio.create_task(r)
        except Exception:  # noqa: BLE001 — эмиссия не должна ронять прогон
            pass

    async def _publish(self, ev: dict) -> None:
        if self._emit is None:
            return
        r = self._emit(ev)
        if asyncio.iscoroutine(r):
            await r

    async def mark(self, task: AgentTask, state: TaskState) -> None:
        """Сменить состояние задачи и показать это в UI (нужно обработчику: BLOCKED на гейте)."""
        task.state = state
        await self._publish(task.event())

    # ---- учёт активности (для детекта завершения) ----------------------------

    def _inc(self) -> None:
        self._active += 1
        self._idle.clear()

    def _dec(self) -> None:
        self._active -= 1
        if self._active <= 0:
            self._active = 0
            self._idle.set()

    def hold_busy(self) -> None:
        """Считать оркестратор занятым, пока идёт фоновая работа, которая ЕЩЁ поставит задачи.

        `run()` завершается, когда активных задач не осталось. Предразбор позиций отдаёт их
        пакетами, и между пакетами очередь может опустеть — без этого якоря прогон закончился бы,
        не дождавшись оставшихся позиций.

        Метод СИНХРОННЫЙ намеренно: якорь надо поставить до `create_task`, иначе фоновая задача
        не успеет начаться, `run()` увидит пустую очередь и завершится, отменив её.
        """
        self._inc()

    def release_busy(self) -> None:
        """Снять якорь фоновой работы (см. hold_busy)."""
        self._dec()

    @asynccontextmanager
    async def keep_busy(self):
        """`async with orch.keep_busy():` — якорь на время блока (см. hold_busy)."""
        self.hold_busy()
        try:
            yield
        finally:
            self.release_busy()

    # ---- постановка задачи ---------------------------------------------------

    def submit(self, task: AgentTask) -> AgentTask:
        """Поставить задачу — она начинает выполняться сразу. Лимиты держат гейты (см. модуль)."""
        self.tasks[task.id] = task
        if self._stopped:
            task.state = TaskState.CANCELLED
            self._emit_soon(task.event())
            return task
        self._inc()
        self._emit_soon(task.event())                    # queued (видно в панели очереди)
        t = asyncio.create_task(self._run(task))
        self._free.add(t)
        t.add_done_callback(self._free.discard)
        return task

    # ---- исполнение ----------------------------------------------------------

    async def _run(self, task: AgentTask) -> None:
        try:
            if self._stopped:
                task.state = TaskState.CANCELLED
                await self._publish(task.event())
                return
            task.state = TaskState.RUNNING
            task.started = time.time()
            await self._publish(task.event())
            handler = self._handlers.get(task.kind)
            if handler is None:
                raise RuntimeError("нет обработчика для kind=%r" % task.kind)
            task.result = await handler(task, self)
            task.state = TaskState.DONE
        except asyncio.CancelledError:
            task.state = TaskState.CANCELLED
            task.finished = time.time()
            await self._publish(task.event())
            raise
        except Exception as exc:  # noqa: BLE001 — падение одной задачи не роняет прогон
            task.state = TaskState.FAILED
            task.error = str(exc)
            log.warning("Задача %s (%s) упала: %s", task.id, task.kind, exc)
        finally:
            if task.finished is None:
                task.finished = time.time()
            if task.state in (TaskState.DONE, TaskState.FAILED):
                await self._publish(task.event())
            self._dec()

    # ---- жизненный цикл ------------------------------------------------------

    async def run(self) -> None:
        """Ждать, пока все задачи (включая порождённые обработчиками) завершатся."""
        await self._idle.wait()

    async def stop(self) -> None:
        """Остановить всю цепочку: отменить активные, очистить очередь, пометить cancelled."""
        self._stopped = True
        log.info("Оркестратор: остановка всей цепочки пользователем")
        # 1) отменить бегущие задачи
        for t in list(self._free):
            t.cancel()
        # 2) дождаться отмен; _idle взведётся, когда _active дойдёт до 0 → run() разблокируется
        await asyncio.gather(*list(self._free), return_exceptions=True)
        # 3) досчитать учёт принудительно. Задача, отменённая ДО первого шага корутины (а мы
        # только что сделали ровно это со всем _free), не исполняет свой finally — значит _dec()
        # не вызывается и _active залипает > 0. Тогда `await self._idle.wait()` в run() висит
        # вечно: в webui не закрывается fetcher, session.orchestrated остаётся не-None и кнопка
        # «Найти» до перезапуска процесса отвечает «Прогон уже идёт». Стоп означает, что
        # продолжать нечего, поэтому обнулить счётчик здесь корректно.
        for task in self.tasks.values():
            if task.state in (TaskState.QUEUED, TaskState.RUNNING, TaskState.BLOCKED):
                task.state = TaskState.CANCELLED
                if task.finished is None:
                    task.finished = time.time()
                await self._publish(task.event())
        if self._active:
            log.info("Оркестратор: после остановки осталось незакрытых задач %d — обнуляю учёт",
                     self._active)
        self._active = 0
        self._idle.set()

    # ---- срез для UI ---------------------------------------------------------

    def snapshot(self) -> dict:
        """Текущее состояние очереди для панели «сейчас/в очереди/готово»."""
        by = {"running": [], "queued": [], "done": [], "failed": [], "cancelled": [], "blocked": []}
        for t in self.tasks.values():
            by.get(t.state.value, by["queued"]).append(t.event())
        return by
