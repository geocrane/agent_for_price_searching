# -*- coding: utf-8 -*-
"""Явная проверка системы: поиск, модель, действующие настройки скорости.

Зачем. `doctor` отвечает на вопрос «всё ли настроено» и модель принципиально не зовёт.
Но когда прогон идёт медленно, вопрос другой: «что именно работает и с какой скоростью».
Отвечает на него только реальный запрос — в поисковый бэкенд и в модель, с замером времени.

Проверки независимы: падение одной не отменяет остальные, каждая возвращает
{"ok": bool, "error": str|None, ...}. Тест платный (два коротких вызова модели), поэтому
инициирует его пользователь кнопкой, а расход токенов уходит в общий счётчик (usage_delta).
"""
import time

from ..config import Settings, load_settings
from ..obs.log import get_logger

log = get_logger("ops.selftest")

# Нейтральный товарный запрос: похож на рабочий (та же длина и вид), но ни к какому файлу
# пользователя не привязан.
SEARCH_QUERY = "аккумулятор 12В 12Ач цена купить"
# Короткий вопрос: меряет отклик, а не генерацию — ответ в одно слово.
PING_PROMPT = "Ответь одним словом: работаешь?"
# Заведомо предсказуемая по объёму генерация (~150–250 токенов) для замера скорости.
GEN_PROMPT = ("Перечисли по-русски числа от 1 до 60 словами, через запятую, "
              "без пояснений и без переводов строки.")


async def _emit(emit, ev) -> None:
    if emit is None:
        return
    r = emit(ev)
    if hasattr(r, "__await__"):
        await r


async def check_search(settings: Settings | None = None, emit=None) -> dict:
    """Один реальный запрос в тот же поисковый бэкенд, что и в прогоне."""
    import httpx

    from ..discovery import BROWSER_UA, _build_backend
    s = settings or load_settings()
    out = {"ok": False, "error": None, "backend": None, "results": 0, "took_s": 0.0,
           "sample": [], "query": SEARCH_QUERY, "delay_s": None}
    try:
        backend, name, conc = _build_backend(s)
        out["backend"], out["concurrency"] = name, conc
        t0 = time.monotonic()
        async with httpx.AsyncClient(headers={"User-Agent": BROWSER_UA}) as client:
            cands = await backend.discover(client, SEARCH_QUERY, min(10, s.discovery.max_results))
        out["took_s"] = round(time.monotonic() - t0, 2)
        out["results"] = len(cands)
        out["sample"] = [_domain(c) for c in cands[:3]]
        out["ok"] = len(cands) > 0
        if not out["ok"]:
            out["error"] = "поиск ответил, но выдача пуста"
        log.info("Проверка поиска (%s): результатов %d за %.1f с", name, len(cands), out["took_s"])
    except Exception as exc:  # noqa: BLE001 — проверка не имеет права падать наружу
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
        log.warning("Проверка поиска не удалась: %s", out["error"])
    return out


def _domain(cand) -> str:
    url = getattr(cand, "url", "") or ""
    from urllib.parse import urlparse
    return urlparse(url).netloc or url[:40]


async def check_model(settings: Settings | None = None, emit=None) -> dict:
    """Два коротких вызова: отклик модели и скорость генерации.

    ping   — полный оборот запроса без стрима: доступность токена, URL и имени модели.
    gen    — стрим: задержка до первого токена (ttft) и скорость генерации (токенов в секунду).
             Скорость считаем по времени ПОСЛЕ первого токена, иначе она смешивается с
             очередью на стороне сервера и получается заниженной вдвое.
    """
    from ..llm.client import build_client, close_client
    s = settings or load_settings()
    out = {"ok": False, "error": None, "model": s.llm.model, "base_url": s.llm.base_url,
           "token_present": bool(s.llm.api_key), "ping_s": None, "ttft_s": None,
           "gen_s": None, "tok_s": None, "completion_tokens": None, "answer": ""}
    if not s.llm.model:
        out["error"] = "модель не выбрана (кнопка «Модель»)"
        return out
    if not s.llm.api_key:
        out["error"] = "не задан токен FOUNDATION_KEY (кнопка «Модель»)"
        return out
    llm = build_client(s)
    try:
        # 1) отклик
        t0 = time.monotonic()
        res = await llm.complete([{"role": "user", "content": PING_PROMPT}],
                                 model=s.llm.model, timeout=60.0)
        out["ping_s"] = round(time.monotonic() - t0, 2)
        out["answer"] = (res.get("content") or "").strip()[:80]
        if res.get("usage"):
            await _emit(emit, {"type": "usage_delta", "usage": res["usage"]})
        await _emit(emit, {"type": "selftest", "step": "model_ping", "state": "ok",
                           "ping_s": out["ping_s"], "answer": out["answer"]})

        # 2) скорость генерации
        marks = {"first": None, "chars": 0}

        def on_chunk(delta):
            if marks["first"] is None:
                marks["first"] = time.monotonic()
            marks["chars"] += len(delta or "")

        t1 = time.monotonic()
        res2 = await llm.complete([{"role": "user", "content": GEN_PROMPT}],
                                  model=s.llm.model, on_chunk=on_chunk, timeout=120.0)
        t2 = time.monotonic()
        usage = res2.get("usage") or {}
        if usage:
            await _emit(emit, {"type": "usage_delta", "usage": usage})
        out["gen_s"] = round(t2 - t1, 2)
        out["ttft_s"] = round((marks["first"] or t2) - t1, 2)
        out["chars"] = marks["chars"]
        tokens = usage.get("completion_tokens")
        out["completion_tokens"] = tokens
        # Скорость считаем по ВСЕМУ времени генерации, а не по времени после первого токена:
        # рассуждающие модели (GLM, o-серия) до первого видимого символа успевают сгенерировать
        # сотни скрытых токенов, и деление на «время после» давало бы вчетверо завышенную цифру.
        if tokens:
            out["tok_s"] = round(tokens / max(0.01, t2 - t1), 1)
        elif marks["chars"]:
            # Сервер не отдал usage — считаем по символам (грубо: ~3 символа на токен русского).
            out["tok_s"] = round((marks["chars"] / 3.0) / max(0.01, t2 - t1), 1)
            out["tok_s_approx"] = True
        # Молчание до первого символа — это не «сеть тормозит», а скрытые рассуждения модели.
        # Их объём объясняет, почему позиция обрабатывается минуты при быстрой генерации.
        if tokens and marks["chars"]:
            hidden = int(tokens - marks["chars"] / 3.0)
            if hidden > tokens * 0.2:
                out["reasoning_tokens"] = hidden
        out["visible_chars_s"] = round(marks["chars"] / max(0.01, t2 - (marks["first"] or t1)), 0)
        out["ok"] = True
        log.info("Проверка модели (%s): отклик %.1f с, первый токен через %.1f с, %.0f ток/с, "
                 "скрытых рассуждений ≈%s токенов",
                 s.llm.model, out["ping_s"] or 0, out["ttft_s"] or 0, out["tok_s"] or 0,
                 out.get("reasoning_tokens", 0))
    except Exception as exc:  # noqa: BLE001 — проверка не имеет права падать наружу
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
        log.warning("Проверка модели не удалась: %s", out["error"])
    finally:
        await close_client(llm)
    return out


def speed_settings(settings: Settings | None = None) -> dict:
    """Действующие лимиты и паузы — то, из чего складывается скорость прогона.

    Читается прямо из конфига, без вычислений: пользователь должен видеть те же числа,
    по которым работает движок, и понимать, где именно у него узкое место.
    """
    s = settings or load_settings()
    try:
        from ..discovery import _build_backend
        _b, backend_name, disc_conc = _build_backend(s)
    except Exception:  # noqa: BLE001 — сводка настроек не должна падать из-за бэкенда
        backend_name, disc_conc = "serper", s.discovery.concurrency
    top = max(1, int(s.extract.concurrency))
    # Несогласованные пределы видно только при сопоставлении чисел, а последствия у них
    # неочевидные — поэтому называем их прямо, рядом с самими числами.
    warnings = []
    if s.llm.timeout < s.extract.orch_timeout:
        warnings.append(
            "предел одного запроса к модели (%.0f с) меньше предела адаптивного исполнителя "
            "(%.0f с): ступенчатое снижение параллельности не успевает сработать"
            % (s.llm.timeout, s.extract.orch_timeout))
    return {
        "warnings": warnings,
        "stage_positions": s.agent.stage_positions,
        "stage_positions_by_kind": dict(getattr(s.agent, "stage_positions_by_kind", None) or {}),
        "fetch_concurrency": s.fetch.concurrency,
        "fetch_backend": s.fetch.backend,
        "fetch_delay_range": list(s.fetch.delay_range or []),
        "fetch_timeout": s.fetch.timeout,
        "model_levels": sorted({top, max(1, top // 2), 1}, reverse=True),
        "model_timeout": s.extract.orch_timeout,
        "llm_timeout": s.llm.timeout,
        "search_backend": backend_name,
        "search_concurrency": disc_conc,
        "search_delay_range": [],      # у платного search-API паузы перед запросом нет
        "search_timeout": s.discovery.timeout,
        "queries_per_item": s.discovery.queries_per_item,
        "parse_batch": s.discovery.llm_queries_batch,
    }


async def run_selftest(settings: Settings | None = None, emit=None) -> dict:
    """Полная проверка по порядку: поиск → модель → сводка настроек.

    По каждому шагу шлём событие: окно проверки заполняется по мере, а не молчит до конца.
    """
    s = settings or load_settings()
    await _emit(emit, {"type": "selftest", "step": "search", "state": "start"})
    search = await check_search(s, emit=emit)
    await _emit(emit, {"type": "selftest", "step": "search",
                       "state": "ok" if search["ok"] else "fail", **search})
    await _emit(emit, {"type": "selftest", "step": "model", "state": "start"})
    model = await check_model(s, emit=emit)
    await _emit(emit, {"type": "selftest", "step": "model",
                       "state": "ok" if model["ok"] else "fail", **model})
    speed = speed_settings(s)
    await _emit(emit, {"type": "selftest", "step": "speed", "state": "ok", "speed": speed})
    return {"ok": bool(search["ok"] and model["ok"]), "search": search, "model": model,
            "speed": speed}
