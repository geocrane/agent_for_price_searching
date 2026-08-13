# -*- coding: utf-8 -*-
"""Гейт этапа с пробуждением по номеру строки: ограничивает число ПОЗИЦИЙ в работе.

Зачем не `asyncio.Semaphore`:
  • семафор считает ОПЕРАЦИИ, а нужно считать ПОЗИЦИИ: у одной позиции 10 кандидатов-URL, и они
    не должны занимать по слоту каждый (иначе «5 позиций на этапе» вырождается в «5 задач»);
  • семафор пробуждает ожидающих строго FIFO по времени прихода — из-за этого позиция №10
    получала цену, пока №2 и №3 стояли в очереди за вкладкой. Здесь очередь по НОМЕРУ СТРОКИ:
    меньший row — раньше, поэтому ранняя позиция никогда не стоит за поздней.

ИНВАРИАНТ ВЛОЖЕННОСТИ: гейт этапа берётся СНАРУЖИ, лимит ресурса (вкладки, квота поискового API,
исполнитель модели) — всегда ВНУТРИ. Обратный порядок запрещён: держатель ресурса не имеет права
ждать слот этапа, иначе дедлок. Держатели ресурсов делают только IO/CPU и гейтов не берут.
"""
import asyncio
import heapq
import itertools
import time
from collections import deque
from contextlib import asynccontextmanager

from ..obs.log import get_logger
from ..obs.timing import note_wait          # стояние в очереди вычитается из времени позиции

log = get_logger("agent.gates")

_seq = itertools.count(1)


class _Waiter:
    """Один ожидающий: строка, future (слот передаётся через set_result) и признак снятия."""

    __slots__ = ("row", "fut", "dead")

    def __init__(self, row, fut) -> None:
        self.row = row
        self.fut = fut
        self.dead = False


class RowGate:
    """Лимит с очередью по номеру строки.

    per_row=True  — слот занимает ПОЗИЦИЯ: вторая задача той же строки проходит без нового слота
                    (лимит «N позиций на этапе»).
    per_row=False — обычный счётный лимит операций, но очередь всё равно по номеру строки
                    (вкладки браузера, квота поискового API).

    `release` СИНХРОННЫЙ: возврат слота не должен зависеть от возможности дождаться await —
    иначе отмена задачи в `finally` теряет слот и этап встаёт целиком.
    """

    def __init__(self, cap: int, *, per_row: bool = True, name: str = "", on_change=None) -> None:
        self.cap = max(1, int(cap))
        self.per_row = bool(per_row)
        self.name = name or ("rows" if per_row else "slots")
        self._on_change = on_change
        self._held: dict[object, int] = {}       # строка → сколько её задач внутри (refcount)
        self._slots = 0                          # занято слотов (при per_row == len(_held))
        self._q: list[tuple] = []                # min-heap (row, priority, seq); допускает протухшие
        self._by_row: dict[object, deque] = {}   # живые ожидающие по строке (допуск строки — O(k))
        self._saturated = False                  # для лога: пишем только смену насыщения

    # ---- срез состояния ------------------------------------------------------

    @property
    def free(self) -> int:
        return max(0, self.cap - self._slots)

    @property
    def waiting(self) -> int:
        return sum(len(dq) for dq in self._by_row.values())

    def snapshot(self) -> dict:
        """Состояние для события `stage_load` (правило проекта: нагрузка видна пользователю)."""
        return {"name": self.name, "cap": self.cap, "per_row": self.per_row,
                "slots": self._slots, "rows": len(self._held),
                "tasks": sum(self._held.values()), "waiting": self.waiting,
                "waiting_rows": len(self._by_row),
                "next_row": min(self._by_row) if self._by_row else None}

    # ---- вход ----------------------------------------------------------------

    def try_acquire(self, row) -> bool:
        """Занять слот без ожидания. True — можно работать (release обязателен)."""
        if self.per_row and self._held.get(row):
            self._held[row] += 1                 # строка уже внутри — своего слота достаточно
            return True
        if not self._by_row and self._slots < self.cap:
            self._take(row)                      # впереди никого — пролезть не грех
            self._changed()
            return True
        return False

    async def acquire(self, row, priority: int = 0) -> None:
        """Дождаться слота. Пробуждение по наименьшему номеру строки, затем по priority."""
        if self.try_acquire(row):
            return
        w = _Waiter(row, asyncio.get_running_loop().create_future())
        self._by_row.setdefault(row, deque()).append(w)
        heapq.heappush(self._q, (row, int(priority), next(_seq)))
        # Слот мог освободиться прямо сейчас, а в heap могли остаться только протухшие записи —
        # тогда будить нас было бы некому. Само-исцеление: выметаем их и забираем свободный слот.
        self._wake()
        self._changed()
        waiting_since = time.monotonic()         # стояние в очереди — не работа над позицией
        try:
            await w.fut                          # _wake() передаёт слот ВЛАДЕНИЕМ через set_result
            note_wait(waiting_since, time.monotonic())
        except asyncio.CancelledError:
            self._forget(w)
            # Отмена задачи, ждущей на УЖЕ завершённом future, не отменяет сам future: слот наш,
            # а работать мы не будем. Без возврата он теряется навсегда и этап встаёт.
            if w.fut.done() and not w.fut.cancelled():
                self.release(row)
            else:
                self._changed()
            raise

    @asynccontextmanager
    async def hold(self, row, priority: int = 0):
        """`async with gate.hold(row):` — слот держится на всё тело, возвращается всегда."""
        await self.acquire(row, priority)
        try:
            yield
        finally:
            self.release(row)

    # ---- выход ---------------------------------------------------------------

    def release(self, row) -> None:
        if self.per_row:
            n = self._held.get(row, 0)
            if n <= 0:
                log.warning("Гейт %s: release без acquire (строка %s) — учёт слотов разошёлся",
                            self.name, row)
                return
            if n > 1:
                self._held[row] = n - 1          # у строки остались задачи внутри — слот держим
                return
            self._held.pop(row, None)
            self._slots = len(self._held)
        else:
            if self._slots <= 0:
                log.warning("Гейт %s: release без acquire — учёт слотов разошёлся", self.name)
                return
            self._slots -= 1
            n = self._held.get(row, 0)           # держим и разбивку по строкам — для снапшота
            if n > 1:
                self._held[row] = n - 1
            else:
                self._held.pop(row, None)
        self._wake()
        self._changed()

    def cancel_all(self) -> None:
        """Глобальный стоп: снять всех ожидающих (их задачи всё равно отменяются)."""
        pending = [w for dq in self._by_row.values() for w in dq]
        self._by_row.clear()
        self._q.clear()
        for w in pending:
            w.dead = True
            if not w.fut.done():
                w.fut.cancel()
        if pending:
            log.info("Гейт %s: снято ожидающих задач %d (остановка прогона)", self.name, len(pending))
        self._changed()

    # ---- внутреннее ----------------------------------------------------------

    def _take(self, row) -> None:
        self._held[row] = self._held.get(row, 0) + 1
        self._slots = len(self._held) if self.per_row else self._slots + 1

    def _forget(self, w: _Waiter) -> None:
        """Убрать снятого ожидающего. Запись в heap остаётся протухшей — выметается лениво."""
        w.dead = True
        dq = self._by_row.get(w.row)
        if dq is None:
            return
        try:
            dq.remove(w)
        except ValueError:
            pass
        if not dq:
            self._by_row.pop(w.row, None)

    def _wake(self) -> None:
        """Отдать свободные слоты ожидающим с НАИМЕНЬШИМ номером строки."""
        while self._q and self._slots < self.cap:
            row, _prio, _s = heapq.heappop(self._q)
            dq = self._by_row.get(row)
            if not dq:
                continue                         # протухшая запись — слот НЕ тратим
            w = dq.popleft()
            if not dq:
                self._by_row.pop(row, None)
            if w.dead or w.fut.done():
                continue                         # ожидающего сняли — ищем следующего
            self._take(row)                      # слот занят ОТ ИМЕНИ w (передача владением)
            w.fut.set_result(True)
            if self.per_row:
                self._admit_same_row(row)

    def _admit_same_row(self, row) -> None:
        """Строку впустили — впускаем ВСЕ её ожидающие задачи, новых слотов не занимая.

        Иначе остальные задачи строки остались бы в очереди и допускались по одной по мере
        освобождения слотов — то есть «слот на задачу», от чего лимит по позициям и уходит.
        """
        dq = self._by_row.pop(row, None)
        if not dq:
            return
        for w in dq:
            if w.dead or w.fut.done():
                continue
            self._held[row] = self._held.get(row, 0) + 1
            w.fut.set_result(True)

    def _changed(self) -> None:
        """Сообщить о смене загрузки. Логируем ТОЛЬКО смену насыщения — иначе лог утонет."""
        sat = self._slots >= self.cap
        if sat != self._saturated:
            self._saturated = sat
            log.info("Гейт %s: %s (%d/%d, ждут %d)", self.name,
                     "заполнен" if sat else "есть свободный слот",
                     self._slots, self.cap, self.waiting)
        else:
            log.debug("Гейт %s: %d/%d, ждут %d", self.name, self._slots, self.cap, self.waiting)
        if self._on_change is not None:
            try:
                self._on_change(self)
            except Exception:  # noqa: BLE001 — уведомление не имеет права ломать прогон
                pass
