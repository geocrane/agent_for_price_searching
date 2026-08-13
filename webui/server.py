# -*- coding: utf-8 -*-
"""Локальный backend интерфейса агента (webui).

Локально по умолчанию: загрузка/разбор Excel и стрим логов работают БЕЗ подключения к
модели. Модель выбирается из каталога (config/models.yaml) и зовётся напрямую по HTTP API
(токен FOUNDATION_KEY). Слушает только 127.0.0.1.
"""
import asyncio
import csv
import io
import json
import os
import tempfile

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from search_agent import profile
from search_agent.input import read_table
from search_agent.obs.journal import logs_dir
from search_agent.obs.log import get_logger, new_run_id, setup_logging
from . import usage_store
from .log_bridge import broadcaster, install

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(title="search_agent · webui")
log = get_logger("webui")
_usage_lock = asyncio.Lock()


@app.exception_handler(Exception)
async def _unhandled(request, exc: Exception):
    """Любой неперехваченный сбой — ЧИТАЕМЫЙ JSON и трейсбек в панель «Ход».

    Иначе uvicorn отдаёт 500 текстом, фронт спотыкается на разборе JSON и пользователь видит
    невнятное сообщение браузера вместо причины (правило «логировать всё и видимо»).
    """
    log.error("Необработанная ошибка %s: %s: %s", request.url.path, type(exc).__name__, exc,
              exc_info=True)
    return JSONResponse(status_code=500,
                        content={"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})


class Session:
    """Единственная сессия локального однопользовательского инструмента."""
    def __init__(self):
        self.last_read = None          # ReadResult последней загрузки (для экспорта)
        self.last_filename: str = ""
        self.items: list[dict] = []    # ИСТОЧНИК ПРАВДЫ: товары+кандидаты+статусы+своды (кэш сессии)
        self.discover_task = None      # фоновая задача обнаружения (M2)
        self.fetch_task = None         # фоновая задача загрузки страниц (M3a)
        self.extract_task = None       # фоновая задача извлечения цен (M4)
        self.run2_task = None          # фоновая задача прогона «Найти» / оркестрированного (M7/M8)
        self.orchestrated = None       # текущий OrchestratedRun (для стопа всей цепочки)
        self.react_task = None         # фоновая задача ReAct-прогона (M6)
        # --- чат-исследователь (M9): отдельная от прогона по таблице способность ---
        self.chat_store = None         # ChatStore (runs/chats.sqlite), ленивая инициализация
        self.chat_state = None         # ChatState текущего диалога
        self.chat_agent = None         # ResearchAgent идущего хода (для «Стоп»)
        self.chat_fetcher = None       # Fetcher диалога (браузер переиспользуется между ходами)


session = Session()


def _items_from_read(res) -> list[dict]:
    """Построить рабочие товары сессии из результата разбора Excel (ReadResult)."""
    return [{"row": it.row, "name": it.name,
             "raw": getattr(it, "raw", {}) or {}, "candidates": []} for it in res.items]


# ---- Кэш рабочей сессии между запусками (проблема №2) -----------------------
import time as _time
_last_persist = {"t": 0.0}


def _persist_session(force: bool = False) -> None:
    """Сохранить текущую сессию на диск (дебаунс ~1/сек; force — сразу). Не роняет прогон."""
    now = _time.monotonic()
    if not force and (now - _last_persist["t"]) < 1.0:
        return
    _last_persist["t"] = now
    try:
        from search_agent import session as sess
        sess.save_session(session.items, filename=session.last_filename)
    except Exception as exc:  # noqa: BLE001 — персист не должен ронять прогон
        log.warning("Не удалось сохранить сессию: %s", exc)


# ---- Промежуточные сохранения на диск (отчёт + остаток исходного файла) -----
# Сессия в JSON — служебное состояние; пользователю на сбое нужны ГОТОВЫЕ файлы. Их пишет
# report/checkpoint.py, а здесь — только когда именно (каждые N закрытых позиций и в конце).
_checkpoint = {"dir": None, "done": 0, "busy": False, "headers": None, "every": 0}


def _checkpoint_reset(filename: str, headers=None) -> None:
    """Завести папку прогона под новый загруженный файл (при загрузке — не в середине работы)."""
    from search_agent.config import load_settings
    from search_agent.report import checkpoint
    _checkpoint.update({"dir": None, "done": 0, "busy": False, "headers": list(headers or []),
                        "every": 0})
    try:
        cfg = load_settings().report
        every = int(getattr(cfg, "checkpoint_every", 10))
        if every <= 0:
            log.info("Промежуточные сохранения выключены (report.checkpoint_every=0)")
            return
        _checkpoint["every"] = every
        _checkpoint["dir"] = checkpoint.run_dir(filename, base=cfg.checkpoint_dir)
        log.info("Промежуточные результаты (каждые %d позиций) — в папке %s",
                 every, _checkpoint["dir"])
    except Exception as exc:  # noqa: BLE001 — без чекпоинтов прогон всё равно должен идти
        log.warning("Папка промежуточных результатов не заведена: %s", exc)


async def _checkpoint_save(reason: str = "") -> None:
    """Записать отчёт и остаток. Пропускается, если предыдущая запись ещё идёт."""
    from search_agent.report import checkpoint
    if _checkpoint["dir"] is None or _checkpoint["busy"] or not session.items:
        return
    _checkpoint["busy"] = True
    try:
        # Запись xlsx на тысячах строк — работа на секунды: в event loop она встала бы поперёк
        # всего прогона (стрима модели, событий UI, загрузок).
        res = await asyncio.to_thread(checkpoint.save, session.items, _checkpoint["dir"],
                                      headers=_checkpoint["headers"])
        broadcaster.publish({"type": "checkpoint", "reason": reason, **res})
    except Exception as exc:  # noqa: BLE001 — сохранение не имеет права ронять прогон
        log.warning("Промежуточное сохранение не удалось (%s): %s", reason, exc, exc_info=True)
    finally:
        _checkpoint["busy"] = False


def _find_emit(ev: dict) -> None:
    """emit прогона «Найти»: трансляция в WS + учёт токенов + персист сессии по ключевым событиям."""
    _usage_emit(ev)
    if ev.get("type") in ("candidates", "site_update", "verdict"):
        _persist_session()
    if ev.get("type") != "verdict":
        return
    _checkpoint["done"] += 1                          # позиция закрыта (свод получен)
    every = _checkpoint["every"]
    if every and _checkpoint["done"] % every == 0:
        asyncio.create_task(_checkpoint_save("каждые %d позиций" % every))


@app.on_event("startup")
async def _startup():
    # Файл лога обязателен: часть решений (в частности «почему выбрана эта цена» из adjudicate)
    # пишется обычным логгером, а не событием. Без файла эта строка жила только в консоли и в
    # кольцевом буфере на 500 записей — до вечера прогона от неё не оставалось ничего, и разобрать
    # спорную цену постфактум было нечем.
    setup_logging(console_level="INFO",
                  log_file=os.path.join(logs_dir(), "webui.log"))
    install()                                  # лог → WS
    broadcaster.bind_loop(asyncio.get_running_loop())
    new_run_id()
    _refresh_current_model()                   # выбранная модель (для расчёта стоимости в ₽)
    try:                                       # восстановить прошлую сессию (кэш между запусками)
        from search_agent import session as sess
        data = sess.load_session()
        if data:
            session.items = data.get("items") or []
            session.last_filename = data.get("filename") or ""
            log.info("Восстановлена прошлая сессия: файл=%s, товаров=%d",
                     session.last_filename or "—", len(session.items))
            # Продолжение прерванной работы — это новый прогон: заводим ему свою папку, чтобы
            # результаты прошлой попытки остались нетронутыми (заголовки берём из сохранённых строк).
            _checkpoint_reset(session.last_filename or "позиции")
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось восстановить сессию: %s", exc)
    log.info("webui запущен (локальный режим)")


@app.on_event("shutdown")
async def _shutdown():
    """Закрыть браузер диалога и хранилище — иначе профиль Chrome останется залоченным."""
    if session.chat_fetcher is not None:
        try:
            await session.chat_fetcher.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("Загрузчик диалога не закрыт: %s", exc)
        session.chat_fetcher = None
    if session.chat_store is not None:
        session.chat_store.close()
        session.chat_store = None


# ---- Выбор модели из каталога + учёт стоимости в рублях ---------------------
_current_model: dict = {"info": None}          # ModelInfo выбранной модели (кэш для цен)


def _refresh_current_model() -> None:
    """Обновить кэш выбранной модели (её цены) из текущих настроек (.env)."""
    from search_agent.config import load_settings
    from search_agent.llm.catalog import find_model
    try:
        mid = load_settings().llm.model
        _current_model["info"] = find_model(mid) if mid else None
    except Exception as exc:  # noqa: BLE001 — каталог не должен ронять сервер
        _current_model["info"] = None
        log.warning("Выбранная модель для цен не определена: %s", exc)


def _cost_of(prompt: int, completion: int) -> float:
    """Стоимость запроса в рублях по ценам выбранной модели (0, если модель вне каталога)."""
    info = _current_model["info"]
    return info.cost_rub(int(prompt or 0), int(completion or 0)) if info else 0.0


def _current_model_id() -> str:
    info = _current_model["info"]
    return info.id if info else ""


def _make_llm(settings, base_url: str | None = None):
    """Клиент модели: прямой HTTP к выбранной модели. Возвращает (llm, ярлык-транспорт)."""
    from search_agent.llm.client import HTTPLLMClient
    if base_url:
        settings.llm.base_url = base_url
    return HTTPLLMClient(settings), "http (%s @ %s)" % (settings.llm.model or "—", settings.llm.base_url)


async def _close_llm(llm) -> None:
    """Отпустить транспорт модели по окончании прогона (HTTP-пул)."""
    from search_agent.llm.client import close_client
    await close_client(llm)


# ---- Загрузка и разбор Excel (локально, без подключения) --------------------

@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    new_run_id()                               # id операции для корреляции логов
    log.info("Загрузка файла: %s", file.filename)
    data = await file.read()
    suffix = os.path.splitext(file.filename or "")[1] or ".xlsx"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        res = read_table(tmp_path)
    except Exception as exc:  # noqa: BLE001
        log.error("Не удалось разобрать файл %s: %s", file.filename, exc, exc_info=True)
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # Загрузка нового файла СБРАСЫВАЕТ прогресс (проблема №2): свежая сессия, старый кэш стёрт.
    from search_agent import session as sess
    session.last_read = res
    session.last_filename = file.filename or "items"
    session.items = _items_from_read(res)
    sess.reset_session()
    _persist_session(force=True)
    _checkpoint_reset(file.filename or "позиции", headers=res.headers)
    positions = [{"row": it.row, "name": it.name} for it in res.items]
    return {
        "ok": True, "filename": file.filename,
        "mapping": res.mapping, "confidence": res.confidence,
        "headers": res.headers, "header_row": res.header_row, "sheet": res.sheet,
        "unmapped": res.unmapped,
        "counts": {"total": len(res.items)},
        "positions": positions,
    }


@app.get("/api/export")
async def api_export(fmt: str = "csv"):
    """Выгрузка распознанных позиций (базово). Пока CSV."""
    if session.last_read is None:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Нет данных: загрузите файл."})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["row", "name"])
    for it in session.last_read.items:
        w.writerow([it.row, it.name])
    fname = (os.path.splitext(session.last_filename)[0] or "items") + "_parsed.csv"
    return Response(content=buf.getvalue().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % fname})


class ReportRequest(BaseModel):
    items: list[dict] = []             # [{row, name, part_number, verdict, offers|candidates, not_found_reason}]
    filename: str | None = None        # базовое имя файла (без расширения)
    # Ни модели, ни её адреса здесь нет — отчёт собирается из готовой сессии.
    # Флага «писать комментарии моделью» тоже НЕТ и быть не должно. Комментарии пишет прогон;
    # выгрузка обязана быть мгновенной и бесплатной. Раньше флаг приходил с фронта — и хватило
    # одной устаревшей вкладки в браузере (кэш статики), чтобы нажатие «Отчёт» снова запустило
    # десятки платных запросов. Переписать комментарии моделью можно из CLI (`report --transport`).


@app.post("/api/report")
async def api_report(req: ReportRequest):
    """Итоговый Excel-отчёт: «Итоги» по позициям, «Детали» по каждой найденной цене, «Отбор» —
    разбор того, почему итог именно такой.

    Собирается из серверной сессии (полные своды, статусы сайтов, цены по страницам, комментарии)
    БЕЗ обращений к модели — комментарии по позициям пишет сам прогон сразу после свода. Раньше
    кнопка запускала по запросу к модели на каждую позицию: это стоило токенов, занимало минуты
    и файл до браузера не доезжал. Отдаётся как .xlsx на скачивание.
    """
    from urllib.parse import quote
    # Источник — серверная сессия (полный Verdict с найденным названием/ссылкой); фолбэк — тело запроса.
    items = session.items if session.items else req.items
    if not items:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "Нет данных для отчёта: сначала выполните поиск."})
    new_run_id()
    from search_agent.config import load_settings
    from search_agent.report import build_report
    settings = load_settings()
    # Позиции, по которым прогон комментарий не написал (или взял детерминированную сводку из-за
    # лимита API), — единственные, ради которых стоит звать модель. Раньше клиент сюда не
    # передавался вовсе, и такие строки НАВСЕГДА оставались со сводкой, что бы ни было в настройках.
    need = [it for it in items if not (it.get("rationale") or "").strip()]
    llm = None
    if need and settings.report.use_model:
        try:
            llm, _ = _make_llm(settings)
        except Exception as exc:  # noqa: BLE001 — без модели просто соберём сводку, как раньше
            log.warning("Отчёт: модель недоступна (%s) — комментарии будут сводкой", exc)
    log.info("Отчёт: позиций=%d, комментарии — готовые из прогона%s", len(items),
             ("; допишу моделью: %d" % len(need)) if llm is not None else "")

    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    started = _time.monotonic()
    try:
        summary = await build_report(items, path, cfg=settings.report, emit=_usage_emit,
                                     llm_client=llm)
        _persist_session(force=True)                 # сохранить добавленные комментарии (rationale)
        with open(path, "rb") as f:
            data = f.read()
        log.info("Отчёт собран за %.1f с: позиций=%d, сайтов=%d, с ценой=%d",
                 _time.monotonic() - started, summary["total"], summary["sites"],
                 summary["with_price"])
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    base = req.filename or (os.path.splitext(session.last_filename or "")[0]) or "отчёт"
    fname = base + "_цены.xlsx"
    cd = "attachment; filename=report.xlsx; filename*=UTF-8''%s" % quote(fname)
    return Response(content=data,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": cd})


@app.get("/api/history")
async def api_history(name: str = "", pn: str = "", limit: int = 20):
    """История цен по товару (M5): динамика итоговой цены из прошлых прогонов.

    Если ни name, ни pn не заданы — общая статистика истории. Только чтение.
    """
    from search_agent.config import load_settings
    from search_agent.storage import HistoryStore
    settings = load_settings()
    store = HistoryStore(settings.storage.db_path)
    try:
        if name or pn:
            hist = store.price_history(name=name or None, part_number=pn or None, limit=limit)
            return {"ok": True, "history": hist,
                    "found": [h for h in hist if h["primary_value"] is not None]}
        return {"ok": True, "stats": store.stats(),
                "runs": store.recent_runs(limit=limit),
                "dead_letter": store.dead_letters(limit=limit)}
    finally:
        store.close()


# ---- Готовность модели ------------------------------------------------------

@app.get("/api/status")
async def api_status():
    from search_agent.config import load_settings
    s = load_settings()
    # «готово», если задан токен (FOUNDATION_KEY) и выбрана модель
    return {"connected": bool(s.llm.api_key and s.llm.model), "transport": "http",
            "model": s.llm.model, "base_url": s.llm.base_url, "token_present": bool(s.llm.api_key)}


@app.post("/api/selftest")
async def api_selftest():
    """Явная проверка системы: реальный запрос в поиск и в модель, с замером времени.

    Нужна, потому что «Готово» в шапке означает лишь «токен записан», а когда прогон идёт
    медленно, вопрос другой: что работает и с какой скоростью. Ход проверки транслируется
    событиями `selftest` — окно заполняется по мере, а не молчит до конца.

    Во время прогона тест не запускаем: он отнимал бы у работы ту же квоту модели и поиска,
    а его замеры оказались бы замерами очереди.
    """
    from search_agent.config import load_settings
    from search_agent.ops.selftest import run_selftest
    if session.orchestrated is not None:
        return JSONResponse(status_code=409, content={
            "ok": False, "error": "Идёт поиск. Проверка займёт ту же модель и тот же поисковик — "
                                  "остановите прогон или дождитесь его окончания."})
    log.info("Проверка системы: запуск")
    res = await run_selftest(load_settings(), emit=_usage_emit)
    log.info("Проверка системы: поиск=%s, модель=%s",
             "ок" if res["search"]["ok"] else "сбой", "ок" if res["model"]["ok"] else "сбой")
    # ok=True означает «проверка выполнена», а не «всё исправно»: найденная неисправность — это
    # полезный результат, и фронт должен её ПОКАЗАТЬ, а не считать ошибкой запроса.
    return {"ok": True, "passed": res["ok"], "search": res["search"], "model": res["model"],
            "speed": res["speed"]}


class ModelSelectRequest(BaseModel):
    model: str
    base_url: str | None = None        # переопределить URL (иначе — из каталога модели)
    token: str | None = None           # токен FOUNDATION_KEY; пусто → не менять сохранённый
    # Ключ поиска (SERPER_API_KEY) живёт в той же форме: у бесплатного тарифа serper.dev 2500
    # запросов, и когда они кончаются, ключ меняют посреди работы. Ради этого лезть в .env и
    # перезапускать сервер — терять прогон; поэтому и поле, и запись здесь.
    serper_key: str | None = None      # пусто → не менять сохранённый


@app.get("/api/models")
async def api_models():
    """Каталог моделей для выпадающего списка (config/models.yaml) + текущий выбор."""
    from search_agent.config import load_settings
    from search_agent.llm.catalog import CatalogError, load_catalog
    s = load_settings()
    try:
        cat = load_catalog()
    except CatalogError as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})
    models = [{"id": m.id, "name": m.name, "base_url": m.base_url,
               "context_length": m.context_length, "price_in": m.price_in,
               "price_out": m.price_out, "tags": m.tags} for m in cat]
    return {"ok": True, "models": models, "current": s.llm.model,
            "base_url": s.llm.base_url, "token_present": bool(s.llm.api_key),
            "serper_key_present": bool(s.serper_api_key)}


@app.post("/api/model/select")
async def api_model_select(req: ModelSelectRequest):
    """Сохранить подключение: пишем в .env (MODEL_AGENT/BASE_URL/FOUNDATION_KEY/SERPER_API_KEY)
    и в окружение процесса.

    Токены пишем только если переданы непустыми (иначе оставляем сохранённые). Выбор переживает
    перезапуск (читается из .env в load_settings). Обновляем кэш цен для расчёта стоимости.

    Перезапуск сервера НЕ нужен: `load_settings()` собирается заново на каждый вызов и читает
    `os.environ` раньше .env, поэтому новый ключ действует со следующего же обращения.
    """
    from search_agent.config import load_settings, set_env_vars
    from search_agent.llm.catalog import find_model
    info = find_model(req.model)
    base_url = (req.base_url or "").strip() or (info.base_url if info else None)
    updates: dict[str, str | None] = {"MODEL_AGENT": req.model}
    if base_url:
        updates["BASE_URL"] = base_url
    token = (req.token or "").strip()
    if token:
        updates["FOUNDATION_KEY"] = token
    serper_key = (req.serper_key or "").strip()
    if serper_key:
        updates["SERPER_API_KEY"] = serper_key
    set_env_vars(updates)                       # персистентно в .env
    for k, v in updates.items():                # немедленно — в окружение процесса
        if v is not None:
            os.environ[k] = v
    _refresh_current_model()
    s = load_settings()
    # Сами токены НЕ логируем — только факт наличия.
    log.info("Выбрана модель: %s (%s), токен=%s, ключ поиска=%s", s.llm.model, s.llm.base_url,
             "задан" if s.llm.api_key else "нет",
             "обновлён" if serper_key else ("задан" if s.serper_api_key else "нет"))
    return {"ok": True, "model": s.llm.model, "base_url": s.llm.base_url,
            "token_present": bool(s.llm.api_key),
            "serper_key_present": bool(s.serper_api_key)}


# ---- Сессия: восстановление и сброс (проблема №2) ---------------------------

@app.get("/api/session")
async def api_session():
    """Текущая сессия для восстановления UI при открытии (товары + кандидаты + статусы + своды)."""
    run = session.orchestrated
    # started_at идущего прогона — чтобы после перезагрузки страницы таймер продолжил счёт
    # с правильного момента, а не начал с нуля.
    return {"ok": True, "filename": session.last_filename, "items": session.items,
            "running": run is not None,
            "started_at": run.timer.started if run is not None else None}


@app.post("/api/session/reset")
async def api_session_reset():
    """Сбросить прогресс: очистить кэш сессии и рабочие товары (с предупреждением на фронте)."""
    from search_agent import session as sess
    # остановить активный прогон, если идёт
    if session.orchestrated is not None:
        try:
            await session.orchestrated.stop()
        except Exception:  # noqa: BLE001
            pass
    for name in ("run2_task", "extract_task", "fetch_task", "discover_task", "react_task"):
        t = getattr(session, name, None)
        if t is not None and not t.done():
            t.cancel()
    session.items = []
    session.last_read = None
    session.last_filename = ""
    sess.reset_session()
    log.info("Прогресс сброшен пользователем")
    broadcaster.publish({"type": "session_reset"})
    return {"ok": True}


class ProfileRequest(BaseModel):
    """Профиль поиска под тип товаров пользователя. Все поля необязательны — правим точечно."""
    name: str | None = None
    affix: str | None = None
    use_llm_queries: bool | None = None
    preferred_domains: list[str] | None = None
    blocked_domains: list[str] | None = None
    second_page_if_empty: bool | None = None
    strict_analog: bool | None = None
    enough_confirmations: int | None = None


@app.get("/api/profile")
async def api_profile_get():
    """Активный профиль поиска и список сохранённых."""
    from search_agent.config import load_settings
    st = load_settings().storage
    return {"ok": True, "profile": profile.load_active(st).model_dump(),
            "names": profile.list_names(st)}


@app.post("/api/profile")
async def api_profile_save(req: ProfileRequest):
    """Сохранить профиль (становится активным). Незаданные поля берутся из текущего."""
    from search_agent.config import load_settings
    st = load_settings().storage
    data = profile.load_active(st).model_dump()
    data.update({k: v for k, v in req.model_dump().items() if v is not None})
    prof = profile.SearchProfile(**data)
    if not profile.save(prof, cfg=st):
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "Профиль не сохранён — хранилище недоступно (см. панель «Ход»)."})
    return {"ok": True, "profile": prof.model_dump(), "names": profile.list_names(st)}


class FindRequest(BaseModel):
    opts: dict | None = None           # «режим открытия» (fetch): {preset, concurrency, human_input, …}
    clean_opts: dict | None = None     # «сценарий очистки»
    affix: str | None = None           # фраза-суффикс к запросу
    use_llm_queries: bool | None = None  # None — взять из профиля поиска пользователя
    model: str | None = None
    base_url: str | None = None
    budgets: dict | None = None        # {escalate_per_row, find_link_per_row, extra_pages_per_row}


@app.post("/api/find")
async def api_find(req: FindRequest):
    """Единая кнопка «Найти»: агент сам ведёт весь пайплайн по товарам сверху вниз (проблема №1).

    Для товаров без выдачи — сначала обнаружение; затем посайтовый движок (fetch ВСЕХ сайтов
    параллельно → извлечение моделью адаптивно → свод по товару). РЕЗЮМИРУЕТ прерванный прогон:
    завершённые сайты/товары пропускаются, незакрытые догружаются. Прогресс кэшируется.
    """
    if not session.items:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Сначала загрузите файл."})
    if session.orchestrated is not None:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Поиск уже идёт — дождитесь или нажмите «Стоп»."})
    new_run_id()
    from search_agent.agent import OrchestratedRun
    from search_agent.config import load_settings, resolve_clean, resolve_fetch
    from search_agent.fetch.fetcher import Fetcher
    settings = load_settings()
    settings.fetch = resolve_fetch(settings.fetch, req.opts)      # движок читает concurrency/human_input отсюда
    settings.clean = resolve_clean(settings.clean, req.clean_opts)
    # «Кандидатов на товар» из настроек интерфейса — это ограничение ВЫДАЧИ: 0 = рассматриваем
    # всю (по умолчанию), иначе ранжируем и берём топ-N. Иначе поле ни на что не влияло.
    settings.discovery.top_n = max(0, int(settings.fetch.top_n_fetch or 0))
    # Профиль поиска пользователя: аффикс и приоритеты источников под ЕГО тип товаров. Что явно
    # передано в запросе — важнее сохранённого профиля (разовая правка не портит настройку).
    prof = profile.load_active(settings.storage)
    profile.apply_to_settings(prof, settings)
    affix = req.affix if req.affix is not None else prof.affix
    use_llm_queries = req.use_llm_queries if req.use_llm_queries is not None else prof.use_llm_queries
    llm, transport = _make_llm(settings, base_url=req.base_url)
    conc = 1 if settings.fetch.human_input else settings.fetch.concurrency
    log.info("Найти: транспорт=%s, товаров=%d, режим=%s, вкладок=%d, человеч.ввод=%s, "
             "позиций на этапе=%d",
             transport, len(session.items), settings.fetch.backend, conc,
             settings.fetch.human_input, settings.agent.stage_positions)

    # Подготовка прогона может упасть (браузер не стартовал, нет Chrome, кривой конфиг) — это
    # НЕ повод отдавать 500 без объяснения: возвращаем причину текстом, прогон не начат.
    try:
        fetcher = await Fetcher(settings.fetch, settings.clean).start()
        # Обнаружение встроено в движок как стадия задач: по мере нахождения источников по товару
        # СРАЗУ стартует его загрузка/извлечение параллельно с поиском по остальным (пайплайн).
        run = OrchestratedRun(session.items, settings=settings, fetcher=fetcher, llm_client=llm,
                              model=req.model, emit=_find_emit, budgets=req.budgets, affix=affix,
                              use_llm_queries=use_llm_queries, max_pages=prof.max_pages,
                              enough_confirmations=prof.enough_confirmations)
    except Exception as exc:  # noqa: BLE001
        log.error("Найти: не удалось начать прогон: %s", exc, exc_info=True)
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "Не удалось начать поиск — %s: %s" % (type(exc).__name__, exc)})
    session.orchestrated = run
    # Общее время задачи считается от нажатия «Найти». Отдаём фронту абсолютную отметку, а не
    # «прошло N секунд»: так таймер переживает перезагрузку страницы и переподключение сокета.
    broadcaster.publish({"type": "run_started", "engine": "find", "started_at": run.timer.started})

    async def _drive():
        try:
            await run.run()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.error("Найти: ошибка прогона: %s", exc, exc_info=True)
        finally:
            await fetcher.close()
            await _close_llm(llm)
            session.orchestrated = None
            _persist_session(force=True)
            # Финальное сохранение обязательно и здесь: прогон мог закончиться на позиции, не
            # кратной шагу, или быть остановлен кнопкой «Стоп» — последняя работа не должна
            # остаться только в сессии.
            await _checkpoint_save("конец прогона")
            broadcaster.publish({"type": "run_done", "engine": "find"})

    session.run2_task = asyncio.create_task(_drive())
    return {"ok": True, "transport": transport, "count": len(session.items)}


class DiscoverRequest(BaseModel):
    rows: list[int] | None = None      # какие строки (по номеру); None — все
    limit: int | None = None           # ограничить число товаров
    affix: str | None = None           # фраза-суффикс к наименованию; None — дефолт
    use_llm: bool = False              # LLM-уточнение (M2: игнорируется, задел на потом)


@app.post("/api/discover")
async def api_discover(req: DiscoverRequest):
    """Запустить обнаружение источников (M2) по загруженным товарам. Без модели."""
    if session.last_read is None or not session.last_read.items:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Сначала загрузите файл."})
    items = session.last_read.items
    if req.rows:
        rs = set(req.rows)
        items = [it for it in items if it.row in rs]
    if req.limit:
        items = items[:req.limit]
    if not items:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Нет товаров для обнаружения."})
    new_run_id()
    from search_agent.discovery import discover_items
    session.discover_task = asyncio.create_task(
        discover_items(items, affix=req.affix, emit=broadcaster.publish))
    return {"ok": True, "count": len(items)}


class FetchRequest(BaseModel):
    items: list[dict] = []             # [{row, candidates:[{url, is_file, ...}]}] из фронта
    opts: dict | None = None           # «режим открытия»: {preset, anti_detect, headed, delay_range, …}
    clean_opts: dict | None = None     # «сценарий очистки»: {preset, strip_link_urls, drop_structural, …}


@app.post("/api/fetch")
async def api_fetch(req: FetchRequest):
    """Загрузить страницы кандидатов и очистить контент (M3a). Без цены. Без модели."""
    if not req.items:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Нет кандидатов: сначала «Найти источники»."})
    if session.fetch_task is not None and not session.fetch_task.done():
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "Загрузка уже идёт — дождитесь завершения или нажмите «Стоп»."})
    new_run_id()
    from search_agent.config import load_settings, resolve_clean, resolve_fetch
    from search_agent.fetch.fetcher import fetch_items
    settings = load_settings()
    cfg = resolve_fetch(settings.fetch, req.opts)
    clean_cfg = resolve_clean(settings.clean, req.clean_opts)
    log.info("Режим открытия: backend=%s anti_detect=%s headed=%s profile=%s human=%s паузы=%s",
             cfg.backend, cfg.anti_detect, cfg.headed, cfg.persistent_profile, cfg.human_input, cfg.delay_range)
    log.info("Сценарий очистки: снять_ссылки=%s удалять_блоки=%s(force=%s) cookie=%s плотность_ссылок=%s",
             clean_cfg.strip_link_urls, clean_cfg.drop_structural, clean_cfg.drop_structural_force,
             clean_cfg.remove_cookie_banner, clean_cfg.link_density_prune)
    session.fetch_task = asyncio.create_task(
        fetch_items(req.items, cfg=cfg, clean_cfg=clean_cfg, emit=broadcaster.publish))
    return {"ok": True, "mode": {"backend": cfg.backend, "anti_detect": cfg.anti_detect,
                                 "headed": cfg.headed, "delay_range": cfg.delay_range}}


class ExtractRequest(BaseModel):
    items: list[dict] = []             # [{row, name, part_number, candidates:[{url, domain, tier, weight}]}]
    model: str | None = None           # имя модели (напр. qwen3.5-9b для LM Studio)
    base_url: str | None = None        # переопределить endpoint модели


@app.post("/api/extract")
async def api_extract(req: ExtractRequest):
    """Извлечь цены со загруженных страниц через модель (M4)."""
    if not req.items:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "Нет кандидатов: сначала «Загрузить страницы»."})
    new_run_id()
    from search_agent.config import load_settings
    from search_agent.extract import extract_and_adjudicate
    settings = load_settings()
    llm, transport = _make_llm(settings, base_url=req.base_url)
    log.info("Извлечение цен + свод: транспорт=%s, модель=%s, товаров=%d, старт-параллель=%d",
             transport, req.model or settings.llm.model or "—", len(req.items), settings.extract.concurrency)
    session.extract_task = asyncio.create_task(
        extract_and_adjudicate(req.items, llm_client=llm, cfg=settings.extract, model=req.model,
                               emit=_usage_emit))
    return {"ok": True, "transport": transport, "count": len(req.items)}


def _usage_emit(ev: dict) -> None:
    """Обёртка emit: транслирует событие в WS и учитывает токены (usage_delta → usage_store).
    Синхронна (broadcaster.publish синхронный) — вызовы в одном event-loop, гонок нет."""
    broadcaster.publish(ev)
    if ev.get("type") == "usage_delta":
        fields = _usage_fields(ev.get("usage"))
        if fields:
            cost = _cost_of(fields[0], fields[1])       # ₽ по ценам выбранной модели
            cumulative = usage_store.add(*fields, cost=cost, model=_current_model_id())
            broadcaster.publish({"type": "usage", "cumulative": cumulative})


@app.post("/api/stop")
async def api_stop():
    """Остановить всю цепочку: оркестрированный прогон + фоновые задачи (discover/fetch/extract)."""
    stopped = []
    if session.orchestrated is not None:                # мягкая остановка очереди (M7/M8)
        try:
            await session.orchestrated.stop()
            stopped.append("очередь")
        except Exception as exc:  # noqa: BLE001
            log.warning("Стоп оркестратора: %s", exc)
    if session.chat_agent is not None:                  # идущий ход диалога (M9)
        session.chat_agent.stop()
        stopped.append("чат")
    for name in ("react_task", "run2_task", "extract_task", "fetch_task", "discover_task"):
        t = getattr(session, name, None)
        if t is not None and not t.done():
            t.cancel()
            stopped.append(name.replace("_task", "").replace("run2", "очередь"))
    if stopped:
        log.info("Прогон остановлен пользователем: %s", ", ".join(stopped))
        broadcaster.publish({"type": "stopped", "stage": stopped[0]})
    return {"ok": True, "stopped": stopped}


class RunOrchestratedRequest(BaseModel):
    items: list[dict] = []
    opts: dict | None = None           # «режим открытия» (fetch)
    clean_opts: dict | None = None     # «сценарий очистки»
    model: str | None = None
    base_url: str | None = None
    budgets: dict | None = None        # {escalate_per_row, find_link_per_row, extra_pages_per_row}


@app.post("/api/run_orchestrated")
async def api_run_orchestrated(req: RunOrchestratedRequest):
    """Оркестрированный прогон (M7/M8): очередь задач + одна полоса модели + авто-триаж.

    Триаж порождает под бюджетом follow-up задания (листинг→карточка, блок→эскалация спец-
    эндпоинтом Ozon/WB). Поток `task`-событий идёт в UI (панель очереди + таймлайн). Стоп —
    через /api/stop (останавливает всю цепочку). Транспорт модели — как в /api/run.
    """
    if not req.items:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "Нет кандидатов: сначала «Найти источники»."})
    if session.orchestrated is not None:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Прогон уже идёт — дождитесь или остановите."})
    new_run_id()
    from search_agent.agent import OrchestratedRun
    from search_agent.config import load_settings, resolve_clean, resolve_fetch
    from search_agent.fetch.fetcher import Fetcher
    settings = load_settings()
    fetch_cfg = resolve_fetch(settings.fetch, req.opts)
    clean_cfg = resolve_clean(settings.clean, req.clean_opts)
    llm, transport = _make_llm(settings, base_url=req.base_url)
    log.info("Оркестрированный прогон: транспорт=%s, товаров=%d, режим=%s, бюджеты=%s",
             transport, len(req.items), fetch_cfg.backend, req.budgets or "по умолчанию")

    fetcher = await Fetcher(fetch_cfg, clean_cfg).start()
    run = OrchestratedRun(req.items, settings=settings, fetcher=fetcher, llm_client=llm,
                          model=req.model, emit=_usage_emit, budgets=req.budgets)
    session.orchestrated = run

    async def _drive():
        try:
            await run.run()
        except asyncio.CancelledError:
            pass
        finally:
            await fetcher.close()
            await _close_llm(llm)
            session.orchestrated = None
            broadcaster.publish({"type": "run_done", "engine": "orchestrated"})

    session.run2_task = asyncio.create_task(_drive())
    return {"ok": True, "transport": transport, "count": len(req.items)}


class RunReactRequest(BaseModel):
    items: list[dict] = []             # [{row, name, part_number, candidates?}]
    opts: dict | None = None           # «режим открытия» (fetch)
    clean_opts: dict | None = None     # «сценарий очистки»
    model: str | None = None
    base_url: str | None = None
    max_steps: int | None = None


@app.post("/api/run_react")
async def api_run_react(req: RunReactRequest):
    """ReAct-прогон (M6): модель сама ведёт шаги (find_sources→fetch→extract→свод), вызывая скилы.

    Требует модель (живой мост | http LM Studio). События agent_step/agent_observation/verdict
    идут в UI. Стоп — через /api/stop. Агент ищет источники сам, если кандидатов нет.
    """
    if not req.items:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "Нет товаров: загрузите файл."})
    if session.react_task is not None and not session.react_task.done():
        return JSONResponse(status_code=400, content={"ok": False, "error": "ReAct-прогон уже идёт."})
    new_run_id()
    from search_agent.agent import run_react
    from search_agent.config import load_settings, resolve_clean, resolve_fetch
    from search_agent.fetch.fetcher import Fetcher
    settings = load_settings()
    if req.max_steps:
        settings.react.max_steps = req.max_steps
    fetch_cfg = resolve_fetch(settings.fetch, req.opts)
    clean_cfg = resolve_clean(settings.clean, req.clean_opts)
    llm, transport = _make_llm(settings, base_url=req.base_url)
    log.info("ReAct-прогон: транспорт=%s, товаров=%d, max_steps=%d",
             transport, len(req.items), settings.react.max_steps)

    fetcher = await Fetcher(fetch_cfg, clean_cfg).start()

    async def _drive():
        try:
            await run_react(req.items, settings=settings, fetcher=fetcher, llm_client=llm,
                            model=req.model, emit=_usage_emit)
        except asyncio.CancelledError:
            pass
        finally:
            await fetcher.close()
            await _close_llm(llm)
            broadcaster.publish({"type": "run_done", "engine": "react"})

    session.react_task = asyncio.create_task(_drive())
    return {"ok": True, "transport": transport, "count": len(req.items)}


@app.post("/api/browser/reset")
async def api_browser_reset():
    """Аварийно закрыть зависший Chrome нашего профиля и снять устаревший SingletonLock."""
    import subprocess
    from search_agent.fetch import browser as _browser
    from search_agent.fetch.manual import SESSION as _manual
    try:
        await _manual.close()
    except Exception:  # noqa: BLE001
        pass
    subprocess.run(["pkill", "-9", "-f", "user-data-dir=%s" % _browser.PROFILE_DIR],
                   capture_output=True)
    _browser._clear_stale_singleton(_browser.PROFILE_DIR)
    _browser._lease_drop()                               # забыть аренду профиля (Chrome уже убит)
    log.info("Браузер сброшен пользователем: зависший Chrome закрыт, лок снят")
    return {"ok": True}


class RunRequest(BaseModel):
    items: list[dict] = []             # [{row, name, part_number, candidates:[{url, domain, is_file, ...}]}]
    opts: dict | None = None           # «режим открытия» (fetch)
    clean_opts: dict | None = None     # «сценарий очистки»
    model: str | None = None
    base_url: str | None = None


@app.post("/api/run")
async def api_run(req: RunRequest):
    """Конвейер: загрузка страниц + извлечение цены ОВЕРЛАПОМ (пер-товар, как только загрузился)."""
    if not req.items:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "Нет кандидатов: сначала «Найти источники»."})
    new_run_id()
    from search_agent.config import load_settings, resolve_clean, resolve_fetch
    from search_agent.pipeline import run_price_search
    settings = load_settings()
    fetch_cfg = resolve_fetch(settings.fetch, req.opts)
    clean_cfg = resolve_clean(settings.clean, req.clean_opts)
    llm, transport = _make_llm(settings, base_url=req.base_url)
    log.info("Конвейер run: транспорт=%s, товаров=%d, режим=%s, старт-параллель=%d",
             transport, len(req.items), fetch_cfg.backend, settings.extract.concurrency)
    session.extract_task = asyncio.create_task(
        run_price_search(req.items, fetch_cfg=fetch_cfg, clean_cfg=clean_cfg,
                         extract_cfg=settings.extract, llm_client=llm, model=req.model,
                         emit=_usage_emit))
    return {"ok": True, "transport": transport, "count": len(req.items)}


class RecleanRequest(BaseModel):
    clean_opts: dict | None = None     # «сценарий очистки» для переочистки кэша (без захода на сайты)


@app.post("/api/reclean")
async def api_reclean(req: RecleanRequest):
    """Применить сценарий очистки к уже загруженным страницам (по сохранённому html, без сети)."""
    from search_agent.config import load_settings, resolve_clean
    from search_agent.fetch.fetcher import reclean_cache
    clean_cfg = resolve_clean(load_settings().clean, req.clean_opts)
    log.info("Переочистка кэша: снять_ссылки=%s удалять_блоки=%s(force=%s) cookie=%s плотность_ссылок=%s",
             clean_cfg.strip_link_urls, clean_cfg.drop_structural, clean_cfg.drop_structural_force,
             clean_cfg.remove_cookie_banner, clean_cfg.link_density_prune)
    res = await asyncio.to_thread(reclean_cache, clean_cfg)
    log.info("Переочистка завершена: перечищено=%d, пропущено=%d", res["recleaned"], res["skipped"])
    return {"ok": True, **res}


@app.get("/api/content")
async def api_content(url: str):
    """Очищенный контент страницы из кэша (для превью в UI)."""
    from search_agent.fetch import cache
    r = cache.get(url)
    if not r:
        return {"ok": False, "error": "нет контента в кэше (страница ещё не загружена)"}
    st = r.get("structured") or {}
    return {"ok": True, "markdown": r.get("markdown", ""), "chars": r.get("chars", 0),
            "structured": {"jsonld": len(st.get("jsonld") or []),
                           "microdata": len(st.get("microdata") or []),
                           "meta": len(st.get("meta") or {})},
            "blocked": r.get("blocked")}


class ManualRequest(BaseModel):
    url: str


@app.post("/api/manual/open")
async def api_manual_open(req: ManualRequest):
    """Открыть карточку в видимом окне нашего браузера + запустить авто-забор по прохождении проверки."""
    from search_agent.fetch.manual import SESSION
    try:
        return await SESSION.open(req.url, emit=broadcaster.publish)
    except Exception as exc:  # noqa: BLE001
        log.error("Ручной режим open: %s", exc, exc_info=True)
        return JSONResponse(status_code=400, content={"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})


@app.post("/api/manual/grab")
async def api_manual_grab(req: ManualRequest):
    """Забрать итоговую страницу сейчас (override; только если открыт тот же сайт)."""
    from search_agent.fetch.manual import SESSION
    return await SESSION.grab(req.url, emit=broadcaster.publish)


@app.post("/api/manual/close")
async def api_manual_close(req: ManualRequest):
    from search_agent.fetch.manual import SESSION
    return await SESSION.close(req.url or None, emit=broadcaster.publish)   # пустой url → закрыть всё


@app.get("/api/usage")
async def api_usage():
    return usage_store.load()


@app.post("/api/usage/reset")
async def api_usage_reset():
    async with _usage_lock:
        return usage_store.reset()


# ---- WebSocket: события (логи) ----------------------------------------------

@app.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    await ws.accept()
    for ev in broadcaster.recent():            # догоняем историю
        await ws.send_json(ev)
    q = broadcaster.subscribe()
    try:
        while True:
            ev = await q.get()
            await ws.send_json(ev)
    except WebSocketDisconnect:
        return
    finally:
        broadcaster.unsubscribe(q)


# ---- Чат-исследователь (M9) -------------------------------------------------
# Отдельная способность: живой диалог, где модель сама ищет, читает страницы и ПРОВЕРЯЕТ
# источники данных, а каждое утверждение подкрепляет ссылкой. Поиск по таблице не затрагивает:
# у чата свой Fetcher, своя история (runs/chats.sqlite) и свои события (chat_*).

def _chat_store():
    """Хранилище диалогов (ленивая инициализация, переживает перезапуск)."""
    if session.chat_store is None:
        from search_agent.config import load_settings
        from search_agent.research.store import open_store
        session.chat_store = open_store(load_settings().storage)
    return session.chat_store


async def _chat_fetcher(settings):
    """Загрузчик на ОДИН ход диалога.

    Держать браузер открытым между ходами заманчиво (старт стоит секунды), но нельзя: постоянный
    профиль Chrome (`runs/browser_profile`) допускает один инстанс за раз, и висящий браузер чата
    не дал бы запуститься прогону по таблице. Поиск по таблице — основная функция, ломать её
    ради экономии секунд неправильно.
    """
    from search_agent.fetch.fetcher import Fetcher
    session.chat_fetcher = await Fetcher(settings.fetch, settings.clean).start()
    return session.chat_fetcher


async def _close_chat_fetcher() -> None:
    if session.chat_fetcher is None:
        return
    try:
        await session.chat_fetcher.close()
    except Exception as exc:  # noqa: BLE001 — закрытие не должно ронять ответ пользователю
        log.warning("Загрузчик диалога не закрыт: %s", exc)
    session.chat_fetcher = None


def _chat_state(chat_id: str | None):
    """Состояние диалога: продолжаем текущий/запрошенный или начинаем новый."""
    from search_agent.research.store import ChatState
    store = _chat_store()
    if chat_id:
        if session.chat_state is not None and session.chat_state.chat_id == chat_id:
            return session.chat_state
        loaded = store.load(chat_id) if store.available else None
        session.chat_state = loaded or ChatState(chat_id)
        return session.chat_state
    if session.chat_state is not None:
        return session.chat_state
    new_id = store.create(model=_current_model_id()) if store.available else "mem"
    session.chat_state = ChatState(new_id)
    return session.chat_state


@app.get("/api/chats")
async def api_chats(limit: int = 50):
    """Список диалогов (для селектора истории в интерфейсе)."""
    store = _chat_store()
    return {"ok": True, "chats": store.list_chats(limit), "current":
            session.chat_state.chat_id if session.chat_state else None,
            "available": store.available}


@app.post("/api/chats")
async def api_chat_new():
    """Начать новый диалог (текущий остаётся в истории)."""
    from search_agent.research.store import ChatState
    store = _chat_store()
    chat_id = store.create(model=_current_model_id()) if store.available else "mem"
    session.chat_state = ChatState(chat_id)
    log.info("Новый диалог: %s", chat_id)
    return {"ok": True, "chat_id": chat_id}


@app.get("/api/chats/{chat_id}")
async def api_chat_get(chat_id: str):
    """История диалога для восстановления интерфейса после перезагрузки страницы."""
    state = _chat_state(chat_id)
    visible = [m for m in state.messages
               if m.get("kind") in ("text", "answer", "question")]
    return {"ok": True, "chat_id": state.chat_id,
            "messages": [{"role": m["role"], "kind": m["kind"], "content": m["content"],
                          "meta": m.get("meta") or {}, "ts": m.get("ts")} for m in visible],
            "sources": [s.to_dict() for s in state.registry.known()],
            "dossiers": state.dossiers, "pending_question": state.pending_question}


@app.delete("/api/chats/{chat_id}")
async def api_chat_delete(chat_id: str):
    ok = _chat_store().delete(chat_id)
    if session.chat_state is not None and session.chat_state.chat_id == chat_id:
        session.chat_state = None
    return {"ok": ok}


@app.post("/api/chats/{chat_id}/stop")
async def api_chat_stop(chat_id: str):
    """Мягко остановить идущий ход диалога (ответ всё равно будет отдан)."""
    if session.chat_agent is None:
        return {"ok": True, "stopped": False}
    session.chat_agent.stop()
    log.info("Ход диалога %s остановлен пользователем", chat_id)
    broadcaster.publish({"type": "stopped", "stage": "чат"})
    return {"ok": True, "stopped": True}


@app.get("/api/sources/catalog")
async def api_sources_catalog(verdict: str | None = None, limit: int = 200):
    """Накопленный каталог проверенных источников (по всем диалогам)."""
    return {"ok": True, "sources": _chat_store().all_dossiers(verdict=verdict, limit=limit)}


class PromoteRequest(BaseModel):
    domains: list[str] = []
    blocked: bool = False              # True — в чёрный список вместо приоритетных


@app.post("/api/sources/promote")
async def api_sources_promote(req: PromoteRequest):
    """Отправить найденные домены в профиль поиска — разведка улучшает поиск по таблице.

    Это замыкание петли: источник, проверенный в чате, начинает приоритетно учитываться
    движком (`profile.apply_to_settings` поднимает его вес в рейтинге).
    """
    from search_agent.config import load_settings
    domains = [d.strip().lower().split("//")[-1].split("/")[0].removeprefix("www.")
               for d in (req.domains or []) if str(d).strip()]
    if not domains:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Не переданы домены."})
    settings = load_settings()
    prof = profile.load_active(settings.storage)
    field = "blocked_domains" if req.blocked else "preferred_domains"
    current = list(getattr(prof, field) or [])
    added = [d for d in domains if d not in current]
    setattr(prof, field, current + added)
    saved = profile.save(prof, cfg=settings.storage)
    log.info("Профиль поиска: %s +%d доменов (%s)", field, len(added), ", ".join(added) or "—")
    return {"ok": saved, "added": added, "field": field,
            "preferred_domains": prof.preferred_domains, "blocked_domains": prof.blocked_domains}


# ---- WebSocket: диалог с моделью --------------------------------------------

@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    """Живой диалог: сообщение пользователя → ход агента → ответ со ссылками.

    Ход идёт отдельной задачей, а события агента (шаги/источники/досье) стримятся в этот же
    сокет по мере поступления — пользователь видит, что происходит, а не ждёт молча.
    """
    await ws.accept()
    try:
        while True:
            data = await ws.receive_json()
            content = (data.get("content") or "").strip()
            if not content:
                continue
            if session.chat_agent is not None:
                await ws.send_json({"type": "error",
                                    "error": "Идёт предыдущий запрос — дождитесь ответа или «Стоп»."})
                continue
            try:
                await _run_chat_turn(ws, content, data.get("chat_id"))
            except Exception as exc:  # noqa: BLE001 — диалог не должен рваться от одной ошибки
                log.error("Чат: ошибка хода: %s", exc, exc_info=True)
                await ws.send_json({"type": "error", "error": "%s: %s" % (type(exc).__name__, exc)})
            finally:
                session.chat_agent = None
    except WebSocketDisconnect:
        return


async def _run_chat_turn(ws: WebSocket, content: str, chat_id: str | None) -> None:
    from search_agent.config import load_settings
    from search_agent.research.agent import ResearchAgent
    from search_agent.research.context import build_context
    from search_agent.tools import ToolContext

    settings = load_settings()
    state = _chat_state(chat_id)
    store = _chat_store()
    llm, transport = _make_llm(settings)
    new_run_id()
    log.info("Чат %s: вопрос (%d симв.), транспорт=%s", state.chat_id, len(content), transport)

    queue: asyncio.Queue = asyncio.Queue()

    def emit(ev: dict) -> None:
        _usage_emit(ev)                                  # в «Ход» + учёт токенов и рублей
        if (ev.get("type") or "").startswith("chat_"):
            queue.put_nowait(ev)

    fetcher = await _chat_fetcher(settings)
    ctx = ToolContext(settings=settings, llm_client=llm, emit=emit, fetcher=fetcher, model=None)
    agent = ResearchAgent(
        ctx, cfg=settings.research, chat=state,
        context_provider=build_context(session.items, session.last_filename),
        on_message=(lambda m: store.append(state.chat_id, m)) if store.available else None)
    session.chat_agent = agent

    await ws.send_json({"type": "start", "chat_id": state.chat_id})
    task = asyncio.create_task(agent.turn(content))
    try:
        while True:                                      # стримим события, пока идёт ход
            drain = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({drain, task}, return_when=asyncio.FIRST_COMPLETED)
            if drain in done:
                await ws.send_json(drain.result())
                continue
            drain.cancel()
            break
        res = await task
        while not queue.empty():                         # хвост событий, пришедший на финише
            await ws.send_json(queue.get_nowait())
    finally:
        if not task.done():
            task.cancel()
        await _close_chat_fetcher()                      # браузер не держим между ходами

    if store.available:                                # источники и досье переживают перезапуск
        store.save_sources(state.chat_id, state.registry)
        store.save_dossiers(state.chat_id, state.dossiers)
        store.touch(state.chat_id, title=(state.title or content[:80]))
        state.title = state.title or content[:80]

    async with _usage_lock:
        cumulative = usage_store.load()
    if res.get("paused"):
        await ws.send_json({"type": "question", "text": res["question"],
                            "options": res.get("options") or [], "chat_id": state.chat_id})
    await ws.send_json({"type": "done", "chat_id": state.chat_id,
                        "answer": res.get("answer") or "", "paused": bool(res.get("paused")),
                        "sources": res.get("sources") or [], "dossiers": res.get("dossiers") or [],
                        "citations": res.get("citations") or {}, "steps": res.get("steps"),
                        "usage": res.get("usage"), "cumulative": cumulative})


def _usage_fields(usage):
    if not usage:
        return None
    p, c, t = usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("total_tokens")
    if p is None and c is None and t is None:
        return None
    return int(p or 0), int(c or 0), int(t or 0)


@app.get("/")
async def index():
    """Саму страницу отдаём БЕЗ кэша.

    В index.html зашиты версии статики (`app.js?v=…`), но пока браузер держит в кэше сам HTML,
    он продолжает грузить старые версии — правки «не появляются», пока не сделаешь жёсткую
    перезагрузку. Файлы по версионированным ссылкам кэшируются как обычно.
    """
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(STATIC_DIR, "index.html"),
                        headers={"Cache-Control": "no-store, must-revalidate"})


# Статику монтируем ПОСЛЕ API-маршрутов.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
