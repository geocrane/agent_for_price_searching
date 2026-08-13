# -*- coding: utf-8 -*-
"""Соблюдение лимитов API модели: контроль допуска ДО отправки + пауза вместо мгновенных повторов.

Зачем. Прогон 09.08 дал 1532 отказа 429 при том, что полезный трафик занимал 10% квоты токенов
и 23% квоты запросов в минуту. Разовая перегрузка модели на стороне платформы (ModelArts.81111
«The model TPM limit has been significantly exceeded») переросла в двухчасовой шторм: 429
возвращается за миллисекунды, слот освобождается мгновенно, туда встаёт следующая страница и
получает отказ снова. Отклонённые запросы сервер тоже считает, поэтому 161 отказ в минуту сам
держал лимит «4 запроса в секунду» пробитым.

Отсюда два правила, которые реализует модуль:

  1. `RateLimiter` — разрешение берётся ДО отправки, по трём скользящим окнам (запросы/с,
     запросы/мин, токены/мин). Токены РЕЗЕРВИРУЮТСЯ заранее по оценке и уточняются по факту:
     учёт задним числом не защищает. Отклонённый запрос списывает квоту наравне с успешным.
  2. `Availability` — на отказ отвечаем сном (Retry-After или экспонента), а не повтором в тот
     же миг; после серии отказов ЗАКРЫВАЕМ затвор: вся работа встаёт там, где стоит, пробник
     ждёт восстановления модели, и все продолжают с того же места.

Оба класса транспорт-агностичны и не знают про openai — классификация ошибки идёт по коду
статуса и тексту (см. `classify`). Все переходы видны в UI событием `llm_state` (правило
«логировать всё и видимо»).
"""
import asyncio
import random
import time
from collections import deque

from ..obs.log import get_logger
from ..obs.timing import note_wait          # ожидание квоты — очередь, а не работа над позицией

log = get_logger("llm.limits")

# Классы исхода вызова модели.
OVERLOAD = "overload"        # 429/5xx/обрыв связи — «сбавь скорость», лечится ожиданием
TIMEOUT = "timeout"          # не успел сгенерировать — лечится понижением параллельности
FATAL = "fatal"              # ключ/запрос неверны — повторять бессмысленно


class ModelUnavailable(RuntimeError):
    """Модель не отвечает дольше отведённого предела ожидания.

    Несёт ТОЧНУЮ причину (последний текст лимита от сервера и сколько ждали), потому что дальше
    она попадает в отчёт: пользователь должен видеть «лимит платформы TPM, ждали 15 мин»,
    а не безликое «модель недоступна».
    """
    def __init__(self, reason: str, waited_s: float = 0.0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.waited_s = waited_s


def classify(exc: BaseException) -> str:
    """Отнести исключение транспорта к OVERLOAD / TIMEOUT / FATAL.

    По коду статуса, если он есть, иначе по имени класса и тексту. Опираться на типы openai
    нельзя: транспорт может быть и SSH-мостом, и тестовой заглушкой.
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return TIMEOUT
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
    if isinstance(status, int):
        if status == 429 or 500 <= status <= 599:
            return OVERLOAD
        if status in (400, 401, 403, 404, 422):
            return FATAL
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return TIMEOUT
    if "ratelimit" in name or "connection" in name or "apiconnection" in name:
        return OVERLOAD
    text = str(exc).lower()
    if "429" in text or "too many requests" in text or "rate limit" in text or "tpm limit" in text:
        return OVERLOAD
    if "401" in text or "403" in text or "unauthorized" in text or "api key" in text:
        return FATAL
    # Неопознанное считаем перегрузкой: подождать и повторить дешевле, чем испортить данные
    # молчаливой деградацией на структурные метки.
    return OVERLOAD


def limit_text(exc: BaseException) -> str:
    """Короткое человеческое описание лимита из ответа сервера (для UI и отчёта)."""
    text = str(exc)
    for probe, said in (
            ("the rate limit is 4 per second", "не больше 4 запросов в секунду"),
            ("per second", "лимит запросов в секунду"),
            ("the rate limit is 100 per minute", "не больше 100 запросов в минуту"),
            ("tpm limit has been significantly exceeded", "перегрузка модели на стороне платформы (TPM)"),
            ("tokens per minute", "лимит токенов в минуту"),
            ("per minute", "лимит запросов в минуту")):
        if probe in text.lower():
            return said
    return text[:160]


def retry_after(exc: BaseException) -> float | None:
    """Сколько сервер просит подождать (заголовок Retry-After), если сказал."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    for key in ("retry-after", "Retry-After", "x-ratelimit-reset", "retry-after-ms"):
        raw = None
        try:
            raw = headers.get(key)
        except Exception:  # noqa: BLE001 — заголовки бывают любым отображением
            continue
        if raw in (None, ""):
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        return val / 1000.0 if key.endswith("-ms") else val
    return None


def estimate_tokens(messages, max_tokens: int) -> int:
    """Оценка расхода вызова: промпт + верхняя граница генерации.

    Резерв берётся ДО отправки, поэтому он обязан быть оценкой СВЕРХУ. Делитель 2.5 — про
    кириллицу (у неё символов на токен меньше, чем у латиницы); занизить его опаснее, чем
    завысить: заниженный резерв пропускает лишний запрос и приводит ровно к тому 429, от
    которого мы защищаемся.
    """
    chars = 0
    for m in messages or ():
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, str):
            chars += len(content)
        elif content:                                # мультимодальные части
            chars += len(str(content))
    return int(chars / 2.5) + max(0, int(max_tokens))


class _Window:
    """Скользящее окно: сколько единиц потрачено за последние `span` секунд.

    События держим списками, а не кортежами: резерв токенов уточняется по факту ответа, и
    поправку надо внести в ТУ ЖЕ запись, иначе окно будет считать по завышенной оценке до
    самого своего истечения.
    """
    __slots__ = ("span", "limit", "events", "used")

    def __init__(self, span: float, limit: float) -> None:
        self.span = float(span)
        self.limit = float(limit)
        self.events: deque[list] = deque()
        self.used = 0.0

    def prune(self, now: float) -> None:
        while self.events and self.events[0][0] <= now - self.span:
            self.used -= self.events.popleft()[1]
        if not self.events:
            self.used = 0.0                          # копеечные погрешности не копим

    def wait_for(self, now: float, amount: float) -> float:
        """Через сколько секунд запрос на `amount` поместится в окно (0 — прямо сейчас)."""
        self.prune(now)
        if self.limit <= 0 or self.used + amount <= self.limit:
            return 0.0
        need = self.used + amount - self.limit
        freed = 0.0
        for ev in self.events:
            freed += ev[1]
            if freed >= need:
                return max(0.0, ev[0] + self.span - now)
        # Один запрос больше всего окна: ждать бесполезно, пропускаем и полагаемся на backoff.
        return 0.0

    def add(self, now: float, amount: float) -> list:
        ev = [now, float(amount)]
        self.events.append(ev)
        self.used += float(amount)
        return ev

    def amend(self, ev: list, amount: float) -> None:
        self.used += float(amount) - ev[1]
        ev[1] = float(amount)


class Reservation:
    """Занятая квота одного вызова. `settle` уточняет расход по факту ответа."""
    __slots__ = ("_limiter", "_tok_ev", "estimated")

    def __init__(self, limiter: "RateLimiter", tok_ev: list, estimated: int) -> None:
        self._limiter = limiter
        self._tok_ev = tok_ev
        self.estimated = estimated

    def settle(self, actual_tokens: int | None) -> None:
        if actual_tokens is None or self._tok_ev is None:
            return
        self._limiter._tpm.amend(self._tok_ev, max(0, int(actual_tokens)))


class RateLimiter:
    """Контроль допуска к модели: запросы/с, запросы/мин, токены/мин.

    Разрешение выдаётся строго по очереди (общий замок): так несколько корутин не могут
    одновременно увидеть свободную квоту и уйти в отправку вдвоём. Ожидание идёт ПОД замком —
    это и есть честная очередь FIFO вместо толпы у двери.

    Лимиты умеют сужаться сами (`shrink`) — паспортные значения провайдера могут не совпадать
    с реальными для конкретного аккаунта, и подбирать их вручную мы не хотим.
    """

    def __init__(self, rps: float = 3.0, rpm: float = 80.0, tpm: float = 700_000.0,
                 *, floor_rps: float = 1.0, clock=time.monotonic) -> None:
        self.clock = clock
        self.base = {"rps": float(rps), "rpm": float(rpm), "tpm": float(tpm)}
        self.floor_rps = float(floor_rps)
        self._rps = _Window(1.0, rps)
        self._rpm = _Window(60.0, rpm)
        self._tpm = _Window(60.0, tpm)
        self._lock = asyncio.Lock()

    # --- допуск ----------------------------------------------------------------
    async def acquire(self, est_tokens: int) -> Reservation:
        """Дождаться квоты и занять её. Возвращает резерв для уточнения по факту."""
        waiting_since = None
        async with self._lock:
            while True:
                now = self.clock()
                wait = max(self._rps.wait_for(now, 1),
                           self._rpm.wait_for(now, 1),
                           self._tpm.wait_for(now, est_tokens))
                if wait <= 0:
                    break
                if waiting_since is None:
                    waiting_since = time.monotonic()
                # Спим ПОД замком: очередь к модели должна быть одна и по порядку.
                await asyncio.sleep(min(wait, 1.0))
            now = self.clock()
            self._rps.add(now, 1)
            self._rpm.add(now, 1)
            tok_ev = self._tpm.add(now, est_tokens)
        if waiting_since is not None:
            note_wait(waiting_since, time.monotonic())
        return Reservation(self, tok_ev, est_tokens)

    def charge_rejected(self) -> None:
        """Отклонённый сервером запрос — тоже запрос.

        Сервер посчитал его в своих окнах; если мы не посчитаем, отказ вернётся мгновенно,
        освободит слот и уйдёт на повтор — тот самый шторм, из-за которого 429 не гаснет.
        Занимаем квоту запросов повторно, БЕЗ токенов (генерации не было).
        """
        now = self.clock()
        self._rps.add(now, 1)
        self._rpm.add(now, 1)

    # --- самонастройка ---------------------------------------------------------
    def shrink(self, factor: float = 0.7) -> dict:
        """Сузить окна после отказа: реальные лимиты аккаунта могут быть ниже паспортных."""
        self._rps.limit = max(self.floor_rps, self._rps.limit * factor)
        self._rpm.limit = max(self.floor_rps * 60.0, self._rpm.limit * factor)
        self._tpm.limit = max(1000.0, self._tpm.limit * factor)
        return self.snapshot()

    def grow(self, factor: float = 1.15) -> dict:
        """Вернуть окна вверх после серии успехов, но не выше паспортных значений."""
        self._rps.limit = min(self.base["rps"], self._rps.limit * factor)
        self._rpm.limit = min(self.base["rpm"], self._rpm.limit * factor)
        self._tpm.limit = min(self.base["tpm"], self._tpm.limit * factor)
        return self.snapshot()

    def snapshot(self) -> dict:
        return {"rps": round(self._rps.limit, 2), "rpm": round(self._rpm.limit),
                "tpm": round(self._tpm.limit)}

    def at_base(self) -> bool:
        return (self._rps.limit >= self.base["rps"] and self._rpm.limit >= self.base["rpm"]
                and self._tpm.limit >= self.base["tpm"])


class Availability:
    """Затвор «модель доступна»: пауза вместо мгновенного повтора и ожидание восстановления.

    Пока затвор открыт, `wait()` не задерживает никого. После `open_after` отказов подряд
    затвор закрывается — все вызывающие встают в ожидание ТАМ, ГДЕ СТОЯТ, ничего не теряя, а
    один фоновый пробник раз в `probe_every` секунд проверяет модель самым дешёвым запросом.
    Успех пробника открывает затвор, и работа продолжается с того же места.

    `degrade_after_s` — предел терпения. Ноль означает «ждать сколько угодно» (ночной прогон).
    Только по его исчерпании наверх летит `ModelUnavailable`, и лишь тогда вызывающий имеет
    право на деградацию.
    """

    def __init__(self, *, backoff=(2.0, 4.0, 8.0, 16.0, 32.0), open_after: int = 3,
                 probe_every: float = 15.0, degrade_after_s: float = 900.0,
                 limiter: RateLimiter | None = None, probe=None, emit=None,
                 up_after: int = 20) -> None:
        self.backoff = tuple(float(x) for x in backoff) or (2.0,)
        self.open_after = max(1, int(open_after))
        self.probe_every = float(probe_every)
        self.degrade_after_s = float(degrade_after_s)
        self.limiter = limiter
        self.probe = probe
        self.emit = emit
        self.up_after = max(1, int(up_after))
        self._open = asyncio.Event()
        self._open.set()
        self._fails = 0                  # отказов подряд
        self._streak = 0                 # успехов подряд (для возврата лимитов вверх)
        self._closed_at = 0.0
        self._reason = ""
        self._probe_task = None

    @property
    def paused(self) -> bool:
        return not self._open.is_set()

    async def wait(self) -> None:
        """Пропустить дальше, когда модель доступна. Иначе ждать (с пределом терпения)."""
        if self._open.is_set():
            return
        waiting_since = time.monotonic()
        try:
            if self.degrade_after_s > 0:
                left = self.degrade_after_s - (time.monotonic() - self._closed_at)
                if left <= 0:
                    raise ModelUnavailable(self._reason or "модель недоступна",
                                           time.monotonic() - self._closed_at)
                await asyncio.wait_for(self._open.wait(), timeout=left)
            else:
                await self._open.wait()
        except (asyncio.TimeoutError, TimeoutError):
            raise ModelUnavailable(self._reason or "модель недоступна",
                                   time.monotonic() - self._closed_at) from None
        finally:
            note_wait(waiting_since, time.monotonic())

    def on_success(self) -> None:
        self._fails = 0
        self._streak += 1
        if self.limiter is not None and self._streak >= self.up_after and not self.limiter.at_base():
            self._streak = 0
            limits = self.limiter.grow()
            log.info("Лимиты возвращаю вверх после %d успехов подряд: %s", self.up_after, limits)
            self._fire({"type": "llm_state", "event": "limits_up", "limits": limits})

    async def on_overload(self, exc: BaseException, attempt: int) -> bool:
        """Отреагировать на отказ сервера: сузить окна, поспать, при серии — закрыть затвор.

        Возвращает True, если была полная пауза с восстановлением: такую попытку вызывающий
        не засчитывает — модель к этому моменту снова жива, и лимит повторов тратить не на что.
        """
        self._streak = 0
        self._fails += 1
        self._reason = limit_text(exc)
        if self.limiter is not None:
            limits = self.limiter.shrink()
            log.warning("Отказ модели (%s) — сужаю лимиты до %s", self._reason, limits)
            self._fire({"type": "llm_state", "event": "limits_down",
                        "limits": limits, "reason": self._reason})
        if self._fails >= self.open_after:
            self._close()
            await self.wait()                        # встаём вместе со всеми до восстановления
            return True
        delay = retry_after(exc)
        if delay is None:
            idx = min(attempt, len(self.backoff) - 1)
            delay = self.backoff[idx]
        delay = max(0.5, delay * (0.8 + 0.4 * random.random()))   # джиттер против синхронного залпа
        log.warning("Отказ модели (%s) — жду %.1f с и повторяю (попытка %d)",
                    self._reason, delay, attempt + 1)
        self._fire({"type": "llm_state", "event": "backoff", "delay": round(delay, 1),
                    "reason": self._reason, "attempt": attempt + 1})
        await asyncio.sleep(delay)
        return False

    # --- пауза и пробник -------------------------------------------------------
    def _close(self) -> None:
        if not self._open.is_set():
            return
        self._open.clear()
        self._closed_at = time.monotonic()
        log.warning("Модель недоступна (%s) — ставлю всю работу на паузу, жду восстановления",
                    self._reason)
        self._fire({"type": "llm_state", "event": "paused", "reason": self._reason,
                    "probe_every": self.probe_every, "degrade_after_s": self.degrade_after_s})
        if self._probe_task is None or self._probe_task.done():
            self._probe_task = asyncio.ensure_future(self._probe_loop())

    async def _probe_loop(self) -> None:
        """Раз в probe_every секунд щупать модель самым дешёвым запросом, пока не оживёт."""
        while not self._open.is_set():
            await asyncio.sleep(self.probe_every)
            if self._open.is_set():
                return
            waited = time.monotonic() - self._closed_at
            if self.probe is None:                   # некому щупать — открываем по таймеру
                self._reopen(waited, "проверка недоступна, продолжаю по таймеру")
                return
            self._fire({"type": "llm_state", "event": "probe", "waited_s": round(waited)})
            try:
                await self.probe()
            except Exception as exc:  # noqa: BLE001 — пробник на то и пробник
                self._reason = limit_text(exc)
                log.warning("Модель всё ещё недоступна (%s), ждём %.0f с",
                            self._reason, waited)
                self._fire({"type": "llm_state", "event": "still_paused",
                            "reason": self._reason, "waited_s": round(waited)})
                continue
            self._reopen(waited, "модель ответила")
            return

    def shutdown(self) -> None:
        """Снять паузу и погасить пробник: прогон окончен, ждать больше нечего и некому.

        Затвор открываем принудительно — иначе ожидающие корутины (если такие остались при
        отмене) висели бы до предела терпения уже после конца работы.
        """
        task, self._probe_task = self._probe_task, None
        if task is not None and not task.done():
            task.cancel()
        self._open.set()

    def _reopen(self, waited: float, why: str) -> None:
        self._fails = 0
        self._streak = 0
        self._open.set()
        log.info("Модель снова доступна (%s) после %.0f с паузы — продолжаю с того же места",
                 why, waited)
        self._fire({"type": "llm_state", "event": "resumed", "waited_s": round(waited), "why": why})

    def _fire(self, ev: dict) -> None:
        if self.emit is None:
            return
        try:
            r = self.emit(ev)
            if asyncio.iscoroutine(r):
                asyncio.ensure_future(r)
        except Exception as exc:  # noqa: BLE001 — телеметрия не имеет права ронять вызов
            log.debug("Событие llm_state не отправлено: %s", exc)


class LLMGateway:
    """Единственная дверь к модели: квота → вызов → классификация → пауза/повтор.

    Через неё обязаны идти ВСЕ вызовы (извлечение, предразбор наименований, поиск карточки,
    комментарий отчёта, чат). Иначе лимитер считает не весь трафик и защита дырявая.
    """

    def __init__(self, limiter: RateLimiter, availability: Availability,
                 *, max_attempts: int = 6) -> None:
        self.limiter = limiter
        self.availability = availability
        self.max_attempts = max(1, int(max_attempts))

    async def run(self, call, *, est_tokens: int, usage_of=None):
        """Выполнить вызов модели с соблюдением лимитов.

        call — фабрика корутины (нужна свежая на каждый повтор).
        usage_of(result) → фактическое число токенов, чтобы уточнить резерв.
        """
        last: BaseException | None = None
        attempt = 0
        while attempt < self.max_attempts:
            await self.availability.wait()
            res = await self.limiter.acquire(est_tokens)
            try:
                out = await call()
            except asyncio.CancelledError:
                res.settle(0)
                raise
            except Exception as exc:                          # noqa: BLE001 — разбираем сами
                kind = classify(exc)
                if kind != OVERLOAD:
                    res.settle(0)
                    raise
                # Сервер запрос ПОСЧИТАЛ, хотя и отклонил: списываем квоту, иначе повтор
                # уедет мимо лимитера и шторм повторится.
                self.limiter.charge_rejected()
                res.settle(0)
                last = exc
                resumed = await self.availability.on_overload(exc, attempt)
                if not resumed:      # пауза с восстановлением попытку не тратит: модель уже жива
                    attempt += 1
                continue
            else:
                res.settle(usage_of(out) if usage_of else None)
                self.availability.on_success()
                return out
        assert last is not None
        raise ModelUnavailable(
            "%s: %d повторов подряд не прошли" % (limit_text(last), self.max_attempts))
