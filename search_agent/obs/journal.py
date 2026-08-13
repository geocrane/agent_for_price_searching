# -*- coding: utf-8 -*-
"""Журнал прогона: полная трасса одного поиска в `runs/logs/<run_id>.jsonl`.

Зачем. Консольный лог живёт до закрытия терминала, а разбирать вчерашний сбой нечем: почему
конкретный сайт дал пусто, сколько стоил вызов модели, где просела параллельность. Журнал —
машиночитаемая трасса прогона, по которой это восстанавливается постфактум.

Как устроено. Все события агента и так идут через колбэк `emit` (site_update / position /
verdict / usage_delta / llm_load / inspect / judge …), поэтому журнал просто ОБОРАЧИВАЕТ `emit`:
записал строку — передал дальше в UI. Отдельная проводка по коду не нужна, и ни одно событие не
может «забыть» попасть в журнал.

Формат — JSON Lines: первая строка `run.start` (метаданные прогона), далее события по одному в
строке, последняя `run.finish` (итог и метрики). Секреты не пишутся: события их не содержат, а
метаданные проходят через `redact`.
"""
import json
import os
import re
import threading
import time

from .log import get_logger, redact

log = get_logger("obs.journal")

_DEFAULT_LOGS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "runs", "logs")

# Каталог журналов переопределяется переменной окружения: тесты пишут во временный каталог и не
# засоряют боевой runs/, а на этапе «независимого хранилища» сюда же подставится каталог
# пользователя (~/.search_agent) без правок кода.
ENV_LOGS_DIR = "SEARCH_AGENT_LOGS_DIR"


def logs_dir() -> str:
    return os.environ.get(ENV_LOGS_DIR) or _DEFAULT_LOGS_DIR


# Имя файла прогона: по нему же работает ротация (чужие файлы в каталоге не трогаем).
_NAME_RE = re.compile(r"^run-[0-9a-zA-Z_-]{2,}-\d{8}-\d{6}\.jsonl$")

# Поток «мыслей» модели идёт сотнями кусочков на страницу — в журнал пишем только сводку по нему,
# иначе файл распухает на порядок и в нём невозможно ничего найти.
_STREAM_EVENT = "model_stream"

DEFAULT_KEEP = 100          # сколько последних прогонов держим на диске

# Ключи самой записи журнала — одноимённые поля события переименовываются в ev_*.
_RESERVED_KEYS = frozenset({"t", "kind"})


class RunJournal:
    """Журнал одного прогона. Потокобезопасен, ошибки записи никогда не роняют прогон."""

    def __init__(self, run_id: str, *, dir_path: str | None = None, keep: int = DEFAULT_KEEP):
        self.run_id = run_id
        self.dir = dir_path or logs_dir()
        self.path = os.path.join(self.dir, "run-%s-%s.jsonl" % (
            run_id, time.strftime("%Y%m%d-%H%M%S")))
        self.started = time.time()
        self._lock = threading.Lock()
        self._fh = None
        self._stream_chars: dict[str, int] = {}     # домен → сколько символов настримила модель
        self._counts: dict[str, int] = {}           # тип события → сколько раз
        self._keep = keep
        # Расход модели копим по ходу: к концу прогона нужна цена вопроса, а не только токены.
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "requests": 0}

    # ---- жизненный цикл -----------------------------------------------------

    def start(self, **meta) -> "RunJournal":
        try:
            os.makedirs(self.dir, exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8")
        except OSError as exc:
            log.warning("Журнал прогона недоступен (%s): %s — продолжаю без него", self.path, exc)
            self._fh = None
            return self
        self._rotate()
        self.write("run.start", **redact(meta))
        log.info("Журнал прогона: %s", self.path)
        return self

    def finish(self, **summary) -> None:
        self._flush_streams()
        self.write("run.finish", duration_s=round(time.time() - self.started, 1),
                   events=dict(sorted(self._counts.items())), **summary)
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None

    # ---- запись -------------------------------------------------------------

    def write(self, kind: str, **fields) -> None:
        """Записать строку журнала. Любой сбой записи гасится: журнал не важнее прогона."""
        self.write_record(kind, fields)

    def write_record(self, kind: str, fields: dict) -> None:
        """То же, но поля приходят СЛОВАРЁМ.

        Через `**fields` нельзя: у событий есть собственное поле `kind` (тип страницы в триаже),
        и вызов падал с «multiple values for argument 'kind'» — событие просто не записывалось.
        Свои ключи записи защищены: пришедшие с тем же именем переименовываются.
        """
        if self._fh is None:
            return
        safe = {("ev_" + k if k in _RESERVED_KEYS else k): v for k, v in (fields or {}).items()}
        rec = {"t": round(time.time() - self.started, 3), "kind": kind, **safe}
        try:
            line = json.dumps(rec, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as exc:      # неожиданный объект в событии
            line = json.dumps({"t": rec["t"], "kind": kind, "journal_error": str(exc)},
                              ensure_ascii=False)
        with self._lock:
            if self._fh is None:
                return
            try:
                self._fh.write(line + "\n")
                self._fh.flush()                    # прогон может быть убит — не теряем хвост
            except OSError as exc:
                log.warning("Запись в журнал не удалась: %s", exc)

    def wrap(self, emit):
        """Обернуть колбэк событий: записать в журнал → передать дальше (в UI/лог).

        Возвращает колбэк с той же сигнатурой. Если исходного `emit` нет, журналирование всё равно
        работает — прогон из CLI пишет такую же трассу, что и из интерфейса.
        """
        def journalled(ev):
            try:
                self._on_event(ev)
            except Exception as exc:  # noqa: BLE001 — журнал не имеет права ломать прогон
                log.warning("Событие не записано в журнал: %s", exc)
            return emit(ev) if emit is not None else None
        return journalled

    # ---- внутреннее ---------------------------------------------------------

    def _on_event(self, ev) -> None:
        if not isinstance(ev, dict):
            return
        kind = str(ev.get("type") or "event")
        self._counts[kind] = self._counts.get(kind, 0) + 1
        if kind == _STREAM_EVENT:                   # копим объём, сами куски не пишем
            key = str(ev.get("domain") or "?")
            self._stream_chars[key] = self._stream_chars.get(key, 0) + len(ev.get("delta") or "")
            return
        if kind == "usage_delta":
            self._add_usage(ev.get("usage"))
        self.write_record(kind, {k: v for k, v in ev.items() if k != "type"})

    def _add_usage(self, usage) -> None:
        if not isinstance(usage, dict):
            return
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            try:
                self.usage[key] += int(usage.get(key) or 0)
            except (TypeError, ValueError):
                continue
        self.usage["requests"] += 1

    def _flush_streams(self) -> None:
        if self._stream_chars:
            self.write("model_stream.summary", by_domain=dict(self._stream_chars),
                       total_chars=sum(self._stream_chars.values()))
            self._stream_chars.clear()

    def _rotate(self) -> None:
        """Оставить только `keep` последних журналов. Трогаем ТОЛЬКО файлы своего формата."""
        if self._keep <= 0:
            return
        try:
            names = [n for n in os.listdir(self.dir) if _NAME_RE.match(n)]
        except OSError:
            return
        if len(names) <= self._keep:
            return
        names.sort()                                # имя содержит дату — сортировка = хронология
        for name in names[:len(names) - self._keep]:
            try:
                os.remove(os.path.join(self.dir, name))
            except OSError as exc:
                log.warning("Старый журнал не удалён (%s): %s", name, exc)


def read_journal(path: str) -> list[dict]:
    """Прочитать журнал в список записей (для разбора прогона и скрипта замера)."""
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:                  # оборванная последняя строка при падении
                    continue
    except OSError as exc:
        log.warning("Журнал не прочитан (%s): %s", path, exc)
    return out


def latest_journal(dir_path: str | None = None) -> str | None:
    """Путь к последнему журналу или None."""
    d = dir_path or logs_dir()
    try:
        names = sorted(n for n in os.listdir(d) if _NAME_RE.match(n))
    except OSError:
        return None
    return os.path.join(d, names[-1]) if names else None
