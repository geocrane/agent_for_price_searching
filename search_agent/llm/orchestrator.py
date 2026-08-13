# -*- coding: utf-8 -*-
"""Адаптивный оркестратор параллельности вызовов модели.

Прямой API держит параллель, но сбрасывает запрос при генерации > 180 с. Мы не знаем,
сколько модель тянет одновременно при данном размере контекста, поэтому управляем нагрузкой
адаптивно, по типу TCP congestion control:

  * запросы уходят параллельно под общим лимитом (cap), стартовый уровень — максимум (8);
  * при таймауте запрос ПОВТОРЯЕТСЯ, а общий лимит СТУПЕНЧАТО понижается (8 → 4 → 1),
    чтобы модель успевала генерировать; на уровне 1 повторы сериализуются (ждут слот →
    «повтор синхронно, дождавшись текущих»);
  * после серии успехов подряд лимит АВТО-ВОССТАНАВЛИВАЕТСЯ вверх (1 → 4 → 8).

Каждый переход уровня/таймаут виден в UI: событие `llm_load` через emit (правило «логировать
всё и видимо»). Оркестратор транспорт-агностичен — оборачивает любой корутин-фабрику вызова
модели (см. AdaptiveExecutor.run).
"""
import asyncio
import time

from ..obs.log import get_logger
from ..obs.timing import note_wait          # ожидание слота вычитается из времени позиции

log = get_logger("llm.orch")

DEFAULT_LEVELS = (8, 4, 1)      # ступени параллельности: макс → пониженная → синхронно
DEFAULT_TIMEOUT = 175.0         # с; сервер сбрасывает при > 180 с — берём с запасом
DEFAULT_UP_AFTER = 8            # успехов подряд до повышения уровня вверх


class AdaptiveExecutor:
    """Общий на прогон исполнитель вызовов модели с адаптивным лимитом параллельности."""

    def __init__(self, levels=DEFAULT_LEVELS, timeout: float = DEFAULT_TIMEOUT,
                 up_after: int = DEFAULT_UP_AFTER, *, emit=None, name: str = "llm") -> None:
        # уровни по убыванию (макс → 1); отфильтруем некорректные, гарантируем непустоту
        lv = sorted({int(x) for x in levels if int(x) >= 1}, reverse=True) or [1]
        self.levels = tuple(lv)
        self.timeout = float(timeout)
        self.up_after = max(1, int(up_after))
        self.emit = emit
        self.name = name
        self._li = 0                    # индекс текущего уровня (0 = максимум)
        self._active = 0                # сколько запросов сейчас в модели
        self._streak = 0                # успехов подряд на текущем уровне
        self._cond = asyncio.Condition()

    @property
    def cap(self) -> int:
        return self.levels[self._li]

    # --- гейт с динамическим лимитом (semaphore нельзя ресайзить → Condition) ---
    async def _acquire(self) -> None:
        async with self._cond:
            waiting_since = None
            while self._active >= self.cap:
                # Ожидание свободного слота модели — очередь, а не работа над позицией.
                # Сам вызов модели (ниже, в run) активным временем остаётся.
                waiting_since = waiting_since if waiting_since is not None else time.monotonic()
                await self._cond.wait()
            if waiting_since is not None:
                note_wait(waiting_since, time.monotonic())
            self._active += 1

    async def _release(self) -> None:
        async with self._cond:
            self._active -= 1
            self._cond.notify_all()

    async def _downgrade(self) -> bool:
        async with self._cond:
            if self._li < len(self.levels) - 1:
                self._li += 1
                self._streak = 0
                self._cond.notify_all()
                changed = True
            else:
                changed = False
            cap, active = self.cap, self._active
        if changed:
            await self._emit_load("downgrade", cap, active)
        return changed

    async def _on_success(self) -> None:
        async with self._cond:
            self._streak += 1
            if self._li > 0 and self._streak >= self.up_after:
                self._li -= 1
                self._streak = 0
                self._cond.notify_all()
                up, cap, active = True, self.cap, self._active
            else:
                up, cap, active = False, self.cap, self._active
        if up:
            await self._emit_load("upgrade", cap, active)

    async def run(self, coro_factory):
        """Выполнить вызов модели с таймаутом, понижением лимита и повтором.

        coro_factory — функция БЕЗ аргументов, возвращающая СВЕЖИЙ корутин (нужен для повтора).
        Возвращает результат вызова. При исчерпании ступеней (даже синхронно упёрлись в таймаут)
        пробрасывает TimeoutError — вызывающий решает, что делать (в extract — деградация).
        """
        last_exc: BaseException | None = None
        for _ in range(len(self.levels)):        # максимум попыток = число ступеней
            await self._acquire()
            try:
                res = await asyncio.wait_for(coro_factory(), timeout=self.timeout)
            except (asyncio.TimeoutError, TimeoutError) as exc:
                last_exc = exc
                await self._release()
                await self._emit_load("timeout", self.cap, self._active)
                await self._downgrade()          # понижаем общий лимит и повторяем
                continue
            except BaseException:                # прочие ошибки — слот вернуть и пробросить
                await self._release()
                raise
            else:
                await self._release()
                await self._on_success()
                return res
        assert last_exc is not None
        raise last_exc

    async def _emit_load(self, event: str, cap: int, active: int) -> None:
        log.warning("нагрузка модели: %s → лимит=%d, в работе=%d (уровень %d/%d)",
                    event, cap, active, self._li + 1, len(self.levels))
        if self.emit is None:
            return
        r = self.emit({"type": "llm_load", "name": self.name, "event": event,
                       "cap": cap, "active": active,
                       "level": self._li + 1, "levels": len(self.levels)})
        if asyncio.iscoroutine(r):
            await r


def build_executor(cfg=None, *, emit=None, name: str = "llm") -> AdaptiveExecutor:
    """Собрать исполнитель из конфига (extract/report): верхний уровень = concurrency, далее /2, 1."""
    top = int(getattr(cfg, "concurrency", DEFAULT_LEVELS[0]) or DEFAULT_LEVELS[0])
    top = max(1, top)
    levels = sorted({top, max(1, top // 2), 1}, reverse=True)
    timeout = float(getattr(cfg, "orch_timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
    up_after = int(getattr(cfg, "orch_up_after", DEFAULT_UP_AFTER) or DEFAULT_UP_AFTER)
    return AdaptiveExecutor(levels, timeout=timeout, up_after=up_after, emit=emit, name=name)
