# -*- coding: utf-8 -*-
"""Учёт времени прогона: общее и АКТИВНОЕ время работы по каждой позиции.

Две величины, обе видны пользователю (правило «логировать всё и видимо»):

* общее время задачи — от запуска прогона до его конца, обычный wall clock;
* активное время позиции — сколько по ней реально шла работа этапов.

Что считается активной работой. Этап начинается в тот момент, когда задача ПОЛУЧИЛА слот и
приступила к делу (начало загрузки сайтов — уже работа). Внутри этапа активно всё: запросы,
ожидание ответов, паузы вежливости между обращениями к сайту, cooldown после блокировки,
генерация модели, разбор. Вычитается только СТОЯНИЕ В ОЧЕРЕДИ агента — когда слоты заняты
другими задачами, а наша просто ждёт.

Параллельность не удваивает время. По одной позиции одновременно обрабатывается несколько
сайтов; если три из них грузились одни и те же 10 секунд, позиция потратила 10 секунд, а не 30.
Поэтому отрезки работы ОБЪЕДИНЯЮТСЯ, а не складываются, и сумма времён всех позиций не может
превысить время прогона — по этому признаку и проверяется, что счёт верен.

Ожидание вырезается из СВОЕГО отрезка, а не из общего объединения: пока сайт A стоит в очереди,
сайт B той же позиции может работать — позиция в это время активна, и вычитать её простой
целиком было бы неправдой.

Связь «ожидание внутри работающего этапа» передаётся через contextvars: очереди живут в
agent/gates.py и llm/orchestrator.py, далеко от места, где известен номер позиции, и тащить его
через все слои значило бы менять полдюжины сигнатур ради одной цифры.
"""
import contextvars
import time
from contextlib import asynccontextmanager

# Текущий открытый отрезок работы (см. RunTimer.work). None — вне работы этапа: такие ожидания
# никому не принадлежат и просто игнорируются.
_CURRENT: contextvars.ContextVar["_Span | None"] = contextvars.ContextVar(
    "search_agent_timing_span", default=None)


class _Span:
    """Отрезок работы одной задачи: начало, конец и собственные ожидания очереди внутри."""

    __slots__ = ("row", "started", "waits")

    def __init__(self, row, started: float) -> None:
        self.row = row
        self.started = started
        self.waits: list[tuple[float, float]] = []

    def note_wait(self, started: float, finished: float) -> None:
        if finished > started:
            self.waits.append((started, finished))

    def pieces(self, finished: float) -> list[tuple[float, float]]:
        """Чистые подотрезки: [начало, конец] минус собственные ожидания."""
        return subtract((self.started, finished), self.waits)


def merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Слить пересекающиеся отрезки. Основа счёта «параллельное не удваивается»."""
    out: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def subtract(span: tuple[float, float], holes: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Отрезок минус дырки (ожидания). Возвращает то, что осталось."""
    start, end = span
    pieces = [(start, end)]
    for hole_start, hole_end in merge(holes):
        nxt: list[tuple[float, float]] = []
        for piece_start, piece_end in pieces:
            if hole_end <= piece_start or hole_start >= piece_end:
                nxt.append((piece_start, piece_end))
                continue
            if hole_start > piece_start:
                nxt.append((piece_start, hole_start))
            if hole_end < piece_end:
                nxt.append((hole_end, piece_end))
        pieces = nxt
    return [(s, e) for s, e in pieces if e > s]


class RunTimer:
    """Секундомер прогона: общее время и активное время по каждой позиции."""

    def __init__(self, started: float | None = None) -> None:
        self.started = time.time() if started is None else started
        self.finished: float | None = None
        self._spans: dict[object, list[tuple[float, float]]] = {}

    # ---- общее время ---------------------------------------------------------

    def finish(self) -> float:
        """Отметить конец прогона и вернуть его длительность в секундах."""
        if self.finished is None:
            self.finished = time.time()
        return self.elapsed()

    def elapsed(self) -> float:
        """Сколько идёт (или шёл) прогон — секунд."""
        return (self.finished if self.finished is not None else time.time()) - self.started

    # ---- время позиции -------------------------------------------------------

    @asynccontextmanager
    async def work(self, row):
        """Отрезок работы этапа по позиции `row`. Внутри вложенные ожидания вычитаются."""
        span = _Span(row, time.monotonic())
        token = _CURRENT.set(span)
        try:
            yield span
        finally:
            _CURRENT.reset(token)
            self._spans.setdefault(row, []).extend(span.pieces(time.monotonic()))

    def note_wait(self, started: float, finished: float) -> None:
        """Отметить ожидание очереди. Вне открытого отрезка вызов игнорируется."""
        span = _CURRENT.get()
        if span is not None:
            span.note_wait(started, finished)

    def active_seconds(self, row) -> float:
        """Активное время позиции: объединение отрезков работы без ожиданий очереди."""
        return sum(end - start for start, end in merge(self._spans.get(row, [])))

    def rows(self) -> list:
        return list(self._spans)


def note_wait(started: float, finished: float) -> None:
    """Отметить ожидание в текущем отрезке (см. RunTimer.note_wait).

    Свободная функция, чтобы очереди в agent/gates.py и llm/orchestrator.py не зависели от
    того, кто и как создал таймер прогона.
    """
    span = _CURRENT.get()
    if span is not None:
        span.note_wait(started, finished)


def format_hms(seconds: float | None) -> str:
    """Секунды → «0:07» / «4:12» / «1:02:05». Пустое значение — прочерк."""
    if seconds is None:
        return "—"
    total = int(round(max(0.0, float(seconds))))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%d:%02d" % (minutes, secs)
