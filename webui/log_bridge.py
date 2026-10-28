# -*- coding: utf-8 -*-
"""Мост логов → WebSocket: стрим шагов агента в панель «Ход».

Кастомный logging.Handler кладёт каждую запись в набор asyncio-очередей (по одной на
подключённого WS-клиента) и в кольцевой буфер (для поздно подключившихся). Ставится на
корневой логгер пакета `search_agent` (и `webui`), поэтому UI видит реальные события
разбора/поиска с уровнем, именем слоя, run_id и временем.
"""
import asyncio
import logging
from collections import deque

MAX_BUFFER = 500

# Отставание интерфейса. События уходят клиенту по одному, а поток бывает чудовищным: на каждый
# кусочек текста модели — своё сообщение (`model_stream`), и таких потоков идёт до десяти разом.
# Браузер столько не успевает, очередь копится, и экран показывает картинку МНОГОЧАСОВОЙ давности:
# на переизвлечении 2532 страниц отставание дошло до 2,5 часов — «Очередь» выглядела застывшей,
# хотя прогон шёл полным ходом. Поэтому под завалом жертвуем подробностями, но не состоянием:
# статусы сайтов, цены, своды и загрузка этапов доходят всегда.
SOFT_BACKLOG = 300      # с этого хвоста опускаем «мышление» модели и отладочные строки
HARD_BACKLOG = 3000     # с этого — всё, кроме событий состояния

# Что можно опустить и в каком порядке. 0 — состояние (не опускаем никогда), 1 — массовая
# служебка, 2 — самое объёмное и наименее нужное.
_BULK_TYPES = ("task", "usage_delta", "fetch_load", "inspect")


def _priority(event: dict) -> int:
    kind = event.get("type")
    if kind == "model_stream":
        return 2
    if kind == "log":
        return 2 if event.get("level") == "DEBUG" else 1
    return 1 if kind in _BULK_TYPES else 0


class LogBroadcaster(logging.Handler):
    """Раздаёт лог-записи подписчикам (WS) в виде dict-событий {type:'log', ...}."""

    def __init__(self, level=logging.DEBUG):
        super().__init__(level)
        self._subs: set[asyncio.Queue] = set()
        self._buffer: deque[dict] = deque(maxlen=MAX_BUFFER)
        # Последнее состояние «залипающих» событий (см. _STATEFUL): переподключившийся клиент
        # должен получить актуальную картинку, а не остаться с той, что была до обрыва.
        self._state: dict[str, dict] = {}
        self._meta: dict[int, dict] = {}          # по подписчику: сколько опущено и когда сказали
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Запомнить event loop сервера (handler вызывается из разных потоков/корутин)."""
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        self._meta[id(q)] = {"dropped": 0, "told": 0.0}
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)
        self._meta.pop(id(q), None)

    # События, состояние которых ЖИВЁТ между сообщениями: если такое потерялось при обрыве
    # WebSocket, интерфейс остаётся с неверной картинкой навсегда. Так и появилась строка «ждём
    # ответ модели по пакету 2 из 13 — 4:31:23»: терминальное событие по пакету не дошло.
    # Держим ПОСЛЕДНЕЕ состояние каждого и отдаём его при подключении.
    _STATEFUL = ("parse_wait", "parse_done", "llm_state", "refill", "manual")

    # Состояния ручного окна, после которых помнить нечего: вкладки больше нет. Без этого
    # снимок рос бы по числу когда-либо открытых вручную страниц и переигрывал бы клиенту
    # давно закрытые окна.
    _MANUAL_DONE = ("grabbed", "closed", "queued", "analyze_failed")

    def recent(self) -> list[dict]:
        """Буфер логов плюс последнее состояние «залипающих» событий (для реконнекта)."""
        return list(self._buffer) + list(self._state.values())

    def publish(self, event: dict) -> None:
        """Разослать произвольное событие (position/candidates/progress) клиентам WS."""
        kind = event.get("type")
        if kind == "manual":
            # У ручного окна состояние живёт по АДРЕСУ страницы: окон бывает несколько сразу.
            key = "manual:%s" % event.get("url", "")
            if event.get("state") in self._MANUAL_DONE:
                self._state.pop(key, None)
            else:
                self._state[key] = event
        elif kind in self._STATEFUL:
            # Ключ включает пакет: у разных пакетов разбора состояния независимы.
            self._state["%s:%s" % (kind, event.get("batch", ""))] = event
        self._fanout(event)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            event = {
                "type": "log",
                "ts": self.format_time(record),
                "level": record.levelname,
                "name": record.name,
                "run_id": getattr(record, "run_id", None) or _run_from_ctx(record),
                "msg": record.getMessage(),
            }
        except Exception:  # noqa: BLE001 — логирование не должно ронять приложение
            return
        self._buffer.append(event)
        self._fanout(event)

    def _fanout(self, event: dict) -> None:
        if self._loop is None:
            return
        prio = _priority(event)
        for q in list(self._subs):
            try:
                if prio and self._behind(q, prio):
                    continue                      # клиент не успевает — подробность опускаем
                self._loop.call_soon_threadsafe(q.put_nowait, event)
            except Exception:  # noqa: BLE001
                pass

    def _behind(self, q: asyncio.Queue, prio: int) -> bool:
        """Отстал ли этот клиент настолько, что событие приоритета `prio` пора опустить.

        Опущенное считаем и раз в 20 с говорим об этом в «Ход» — молча редеющий журнал выглядел бы
        как пропавшие события (правило «логировать всё и видимо»). Сообщение кладём в очередь
        НАПРЯМУЮ, а не через логгер: логгер вернулся бы сюда же и получилась бы рекурсия.
        """
        size = q.qsize()
        if size < SOFT_BACKLOG or (prio < 2 and size < HARD_BACKLOG):
            return False
        meta = self._meta.get(id(q))
        if meta is None:
            return True
        meta["dropped"] += 1
        import time
        now = time.monotonic()
        if now - meta["told"] > 20.0:
            meta["told"] = now
            note = {"type": "log", "ts": self.format_time_now(), "level": "WARNING",
                    "name": "webui", "run_id": None,
                    "msg": "Интерфейс не успевает за прогоном (в очереди %d событий): опущено "
                           "подробностей %d — статусы, цены и своды приходят полностью, "
                           "поток «мышления» модели прорежен" % (size, meta["dropped"])}
            try:
                self._loop.call_soon_threadsafe(q.put_nowait, note)
            except Exception:  # noqa: BLE001
                pass
        return True

    @staticmethod
    def format_time(record: logging.LogRecord) -> str:
        import time
        return time.strftime("%H:%M:%S", time.localtime(record.created))

    @staticmethod
    def format_time_now() -> str:
        import time
        return time.strftime("%H:%M:%S")


def _run_from_ctx(record: logging.LogRecord) -> str | None:
    # ContextFilter из obs.log добавляет строку record.ctx вида " [run=ab12 item=r9]".
    ctx = getattr(record, "ctx", "") or ""
    if "run=" in ctx:
        try:
            return ctx.split("run=", 1)[1].split()[0].rstrip("]")
        except Exception:  # noqa: BLE001
            return None
    return None


# Единый брокер на процесс.
broadcaster = LogBroadcaster()


def install() -> LogBroadcaster:
    """Поставить брокер на логгеры search_agent и webui (идемпотентно)."""
    from search_agent.obs.log import ROOT_LOGGER, _ContextFilter
    broadcaster.addFilter(_ContextFilter())   # чтобы был record.ctx с run_id
    # uvicorn.error — сюда попадают трейсбеки необработанных исключений эндпоинтов; без него
    # сбой сервера выглядел бы в UI как «ничего не произошло».
    for name in (ROOT_LOGGER, "webui", "uvicorn.error"):
        lg = logging.getLogger(name)
        if broadcaster not in lg.handlers:
            lg.addHandler(broadcaster)
        if name != "uvicorn.error":               # свой уровень uvicorn не трогаем (лишний шум)
            lg.setLevel(logging.DEBUG)
    return broadcaster
