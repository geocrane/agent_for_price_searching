# -*- coding: utf-8 -*-
"""Единый интерфейс LLM.

Транспорт один: прямой OpenAI-совместимый endpoint (Foundation Models, а для дева — локальный
LM Studio: отличается только base_url), lazy-import openai. Токен берётся из настроек
(llm.api_key ← .env FOUNDATION_KEY). Endpoint держит параллель — управление нагрузкой
в llm/orchestrator.py.

Примечание: реальные вызовы модели инициирует пользователь (правило проекта). Здесь —
абстракция, которую используют агент/скилы; doctor модель НЕ вызывает.
"""
import asyncio

from ..config import Settings
from ..obs.log import get_logger


class LLMClient:
    """Базовый интерфейс.

    `complete` — основной метод: возвращает {"content", "usage"} и умеет стримить дельты в
    on_chunk(delta) (для «живого мышления» в UI и учёта токенов). `chat` — тонкая обёртка,
    отдаёт только текст (обратная совместимость).

    timeout — предел ожидания ИМЕННО ЭТОГО вызова, в секундах (None — общий из настроек).
    Нужен там, где ответ заведомо длинный: разбор пакета из 40 позиций генерируется минутами,
    и общий llm.timeout=120 обрывал бы его раньше, чем вызывающий успеет что-то решить.
    """
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._gateway = None                         # см. gateway(): создаётся при первом вызове

    def gateway(self):
        """Единственная дверь к модели: лимитер + пауза на отказ (см. llm/limits.py).

        Один на клиента, а клиент — один на прогон, поэтому окна лимитов общие для всех
        вызовов: извлечения, предразбора наименований, поиска карточки, комментария отчёта.
        Считать надо ВЕСЬ трафик — иначе защита дырявая.
        """
        if self._gateway is None:
            from .limits import Availability, LLMGateway, RateLimiter
            cfg = self.settings.llm
            limiter = RateLimiter(rps=cfg.rps, rpm=cfg.rpm, tpm=cfg.tpm)
            avail = Availability(backoff=cfg.overload_backoff, open_after=cfg.open_after,
                                 probe_every=cfg.probe_every,
                                 degrade_after_s=cfg.degrade_after_s,
                                 up_after=cfg.limits_up_after,
                                 limiter=limiter, probe=self._probe, emit=self._emit)
            self._gateway = LLMGateway(limiter, avail, max_attempts=cfg.overload_attempts)
        return self._gateway

    def set_emit(self, emit) -> None:
        """Куда слать `llm_state` (пауза/повтор/возобновление). Ставит прогон при старте."""
        self._emit = emit
        if self._gateway is not None:
            self._gateway.availability.emit = emit

    _emit = None

    async def _probe(self) -> None:
        """Самый дешёвый запрос — проверить, ожила ли модель. Мимо лимитера и шлюза."""
        raise NotImplementedError

    async def complete(self, messages: list[dict], model: str | None = None,
                       on_chunk=None, timeout: float | None = None,
                       max_tokens: int | None = None) -> dict:
        raise NotImplementedError

    async def chat(self, messages: list[dict], model: str | None = None) -> str:
        res = await self.complete(messages, model=model)
        return res.get("content", "")

    async def aclose(self) -> None:
        """Освободить ресурсы транспорта. По умолчанию освобождать нечего."""
        return None


class HTTPLLMClient(LLMClient):
    """Прямой OpenAI-совместимый вызов (LM Studio/LibreChat).

    Используем АСИНХРОННЫЙ клиент (AsyncOpenAI): синхронный блокировал бы event loop, и несколько
    параллельных вызовов шли бы по очереди. С async несколько complete() реально идут одновременно
    (LM Studio держит параллель). Шлюз-сериализацию НЕ применяем — она только для SSH-сервера.

    Клиент ОДИН на весь прогон. Раньше он создавался внутри каждого `complete`, то есть на сотни
    вызовов приходились сотни пулов соединений и TLS-рукопожатий, а keep-alive не работал вовсе.
    """
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._client = None
        self._client_loop = None          # клиент привязан к пулу своего event loop

    def _api_key(self) -> str:
        """Ключ: настройки (FOUNDATION_KEY) → окружение → «not-needed».

        Локальный LM Studio ключ не проверяет, но SDK требует непустую строку.
        """
        import os
        return (getattr(self.settings.llm, "api_key", "") or os.environ.get("FOUNDATION_KEY")
                or os.environ.get("OPENAI_API_KEY") or os.environ.get("TOKEN") or "not-needed")

    def _openai(self):
        """Общий AsyncOpenAI. Пересоздаётся, если сменился event loop (новый прогон в webui)."""
        from openai import AsyncOpenAI  # lazy: локально может быть не установлен
        loop = asyncio.get_running_loop()
        if self._client is None or self._client_loop is not loop:
            # Таймаут и число ретраев задаём ЯВНО: дефолт SDK (600 с × 3 попытки) означает, что
            # один залипший вызов держит операцию до получаса, и снаружи это выглядит как
            # «программа зависла» — без строчки в логе о том, чего мы ждём.
            self._client = AsyncOpenAI(base_url=self.settings.llm.base_url, api_key=self._api_key(),
                                       timeout=float(getattr(self.settings.llm, "timeout", 120.0)),
                                       max_retries=int(getattr(self.settings.llm, "max_retries", 1)))
            self._client_loop = loop
        return self._client

    async def aclose(self) -> None:
        """Закрыть пул соединений. Зовётся при завершении прогона рядом с закрытием fetcher."""
        if self._gateway is not None:                # пробник доступности не должен пережить прогон
            self._gateway.availability.shutdown()
            self._gateway = None
        client, self._client, self._client_loop = self._client, None, None
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 — закрытие пула не важнее результата прогона
                pass

    async def complete(self, messages: list[dict], model: str | None = None,
                       on_chunk=None, timeout: float | None = None,
                       max_tokens: int | None = None) -> dict:
        """Вызов модели ЧЕРЕЗ ШЛЮЗ: сначала квота, потом отправка, на отказ — пауза.

        Прямых обращений к SDK мимо этого метода быть не должно (кроме пробника): лимитер
        обязан видеть весь трафик, включая отклонённые запросы.
        """
        from .limits import estimate_tokens
        cap = int(max_tokens if max_tokens is not None else self.settings.llm.max_tokens)
        est = estimate_tokens(messages, cap)
        return await self.gateway().run(
            lambda: self._raw_complete(messages, model=model, on_chunk=on_chunk,
                                       timeout=timeout, max_tokens=cap),
            est_tokens=est,
            usage_of=lambda out: ((out.get("usage") or {}).get("total_tokens")
                                  if isinstance(out, dict) else None))

    async def _probe(self) -> None:
        """Один самый дешёвый запрос: жива ли модель. Мимо лимитера — это и есть проверка."""
        client = self._openai().with_options(timeout=30.0)
        await client.chat.completions.create(
            model=self.settings.llm.model, max_tokens=1,
            messages=[{"role": "user", "content": "ping"}])

    async def _raw_complete(self, messages: list[dict], model: str | None = None,
                            on_chunk=None, timeout: float | None = None,
                            max_tokens: int | None = None) -> dict:
        client = self._openai()
        if timeout is not None:                          # предел этого вызова, а не общий
            client = client.with_options(timeout=float(timeout))
        mdl = model or self.settings.llm.model
        # Верхняя граница генерации нужна не только против обрыва по времени: без неё нельзя
        # зарезервировать токены ДО вызова, а резерв — единственное, что защищает от 429 по TPM.
        cap = int(max_tokens or self.settings.llm.max_tokens)
        if on_chunk is not None:                         # стрим для «живого мышления»
            stream = await client.chat.completions.create(
                model=mdl, messages=messages, stream=True, max_tokens=cap,
                stream_options={"include_usage": True})
            parts, usage, finish = [], None, None
            async for chunk in stream:
                usage = getattr(chunk, "usage", None) or usage
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                finish = getattr(choices[0], "finish_reason", None) or finish
                delta = getattr(choices[0], "delta", None)
                piece = getattr(delta, "content", None) if delta else None
                if piece:
                    parts.append(piece)
                    on_chunk(piece)
            return {"content": "".join(parts), "usage": _usage_dict(usage), "finish": finish}
        resp = await client.chat.completions.create(model=mdl, messages=messages, max_tokens=cap)
        # finish_reason нужен наверху: "length" означает, что ответ ОБОРВАН лимитом токенов, и
        # неразобранный JSON тогда — не каприз модели, а слишком длинный ответ. Без этого
        # признака обе беды выглядят одинаково, и чинить будут не то.
        return {"content": resp.choices[0].message.content or "",
                "usage": _usage_dict(getattr(resp, "usage", None)),
                "finish": getattr(resp.choices[0], "finish_reason", None)}


def _usage_dict(usage):
    if usage is None:
        return None
    for attr in ("model_dump", "dict"):
        fn = getattr(usage, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                pass
    try:
        return dict(usage)
    except Exception:  # noqa: BLE001
        return None


async def close_client(llm) -> None:
    """Отпустить транспорт модели по окончании прогона (HTTP-пул).

    Ошибка закрытия только логируется: прогон к этому моменту уже отдал результат, и ронять его
    из-за неотданного сокета нельзя.
    """
    if llm is None:
        return
    try:
        await llm.aclose()
    except Exception as exc:  # noqa: BLE001
        get_logger("llm").warning("Транспорт модели не закрыт: %s", exc)


def build_client(settings: Settings) -> LLMClient:
    """Фабрика клиента модели (прямой HTTP API)."""
    return HTTPLLMClient(settings)
