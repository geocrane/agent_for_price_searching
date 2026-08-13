# -*- coding: utf-8 -*-
"""Логирование каждого шага агента (по best practices).

Возможности:
  * уровни DEBUG/INFO/WARNING/ERROR/CRITICAL, консоль (stderr) + опционально файл;
  * сквозной контекст (run_id, позиция, домен…) — инъекция во все записи для корреляции;
  * `timed(...)` — тайминг шага/вызова (start→done с латентностью, ошибка с трейсом);
  * `log_event(...)` — структурное событие `event key=val`;
  * `redact(...)` — маскирование секретов (пароль/passphrase/TOKEN/api-key НЕ логируем).

Соглашение (применяем на каждом этапе M1–M8):
  - исходящий вызов (LLM/HTTP/поиск/скрапинг/файл): оборачивать в `timed(log, "<target>.<action>", ...)`;
  - решения (матч/adjudication/не найдено), ретраи, антибот-блоки, кэш, бюджет — логировать явно;
  - контекст позиции: `bind_context(item=row)` на время обработки позиции.
"""
import logging
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

ROOT_LOGGER = "search_agent"
_CONSOLE_FMT = "%(asctime)s %(levelname)-7s %(name)s%(ctx)s: %(message)s"
_FILE_FMT = "%(asctime)s %(levelname)-7s %(name)s%(ctx)s [%(filename)s:%(lineno)d]: %(message)s"
_DATEFMT = "%H:%M:%S"

# Ключи, значения которых НИКОГДА не попадают в лог (маскируются).
SECRET_KEYS = {"password", "passphrase", "token", "api_key", "apikey", "secret",
               "authorization", "auth", "key_path_passphrase"}

_context: ContextVar[dict] = ContextVar("log_context", default={})


class _ContextFilter(logging.Filter):
    """Добавляет к записи строку контекста ' [run=… item=…]'."""
    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _context.get()
        record.ctx = (" [" + " ".join("%s=%s" % (k, v) for k, v in ctx.items()) + "]") if ctx else ""
        return True


def setup_logging(console_level: str = "INFO",
                  log_file: str | Path | None = None,
                  file_level: str = "DEBUG") -> None:
    """Настроить корневой логгер пакета. Идемпотентно (пересоздаёт хендлеры)."""
    logger = logging.getLogger(ROOT_LOGGER)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)

    cfilter = _ContextFilter()

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(_lvl(console_level))
    console.setFormatter(logging.Formatter(_CONSOLE_FMT, _DATEFMT))
    console.addFilter(cfilter)
    logger.addHandler(console)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setLevel(_lvl(file_level))
        fh.setFormatter(logging.Formatter(_FILE_FMT, _DATEFMT))
        fh.addFilter(cfilter)
        logger.addHandler(fh)


def get_logger(name: str) -> logging.Logger:
    """Логгер слоя: get_logger('input.excel_reader')."""
    return logging.getLogger("%s.%s" % (ROOT_LOGGER, name) if name else ROOT_LOGGER)


# ---- сквозной контекст ------------------------------------------------------

def new_run_id() -> str:
    """Сгенерировать и привязать короткий run_id (корреляция всех записей прогона)."""
    rid = uuid.uuid4().hex[:8]
    bind_context(run=rid)
    return rid


def current_run_id() -> str | None:
    """Текущий run_id из контекста (для журнала прогона и корреляции)."""
    return _context.get().get("run")


def bind_context(**fields) -> None:
    """Добавить/обновить поля контекста (run, item, domain…)."""
    ctx = dict(_context.get())
    ctx.update({k: v for k, v in fields.items() if v is not None})
    _context.set(ctx)


def unbind_context(*keys) -> None:
    ctx = dict(_context.get())
    for k in keys:
        ctx.pop(k, None)
    _context.set(ctx)


@contextmanager
def context(**fields):
    """Временный контекст (например, на обработку одной позиции)."""
    token = _context.set({**_context.get(), **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _context.reset(token)


# ---- события и тайминг ------------------------------------------------------

def log_event(logger: logging.Logger, event: str, level: int = logging.INFO,
              _stacklevel: int = 2, **fields) -> None:
    """Структурное событие: 'event key=val …' (секреты маскируются).

    `_stacklevel` — чтобы filename:line в логе указывали на вызывающий код, а не на log.py.
    """
    safe = redact(fields)
    parts = " ".join("%s=%s" % (k, _short(v)) for k, v in safe.items())
    logger.log(level, "%s %s", event, parts, stacklevel=_stacklevel)


@contextmanager
def timed(logger: logging.Logger, step: str, level: int = logging.INFO, *, trace: bool = True,
          **fields):
    """Тайминг шага/вызова: start(DEBUG) → done(level, dur_ms) или error(ERROR, трейс).

    `trace=False` — писать причину одной строкой, без трейсбека. Для ОЖИДАЕМЫХ сбоев (сайт не
    ответил, оборвал соединение): там трейс не несёт информации, зато на десятке недоступных
    сайтов забивает консоль полусотней строк каждый — и настоящие ошибки в ней тонут.
    """
    safe = redact(fields)
    log_event(logger, step + ".start", logging.DEBUG, _stacklevel=3, **safe)
    t0 = time.perf_counter()
    try:
        yield
    except Exception as exc:  # noqa: BLE001 — логируем и пробрасываем
        dt = (time.perf_counter() - t0) * 1000
        logger.error("%s.error dur_ms=%.0f error=%s: %s", step, dt, type(exc).__name__, exc,
                     exc_info=trace)
        raise
    else:
        dt = (time.perf_counter() - t0) * 1000
        log_event(logger, step + ".done", level, _stacklevel=3, dur_ms=round(dt), **safe)


# ---- безопасность / утилиты -------------------------------------------------

def redact(fields: dict) -> dict:
    """Замаскировать значения секретных ключей."""
    out = {}
    for k, v in fields.items():
        out[k] = "***" if k.lower() in SECRET_KEYS else v
    return out


def _short(v, limit: int = 200) -> str:
    s = str(v)
    return s if len(s) <= limit else s[:limit] + "…"


def _lvl(name: str) -> int:
    return getattr(logging, str(name).upper(), logging.INFO)
