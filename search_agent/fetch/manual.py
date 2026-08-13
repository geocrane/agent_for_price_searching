# -*- coding: utf-8 -*-
"""Ручной режим: открыть карточку в видимом окне НАШЕГО браузера, дать человеку пройти
капчу/проверку — и АВТОМАТИЧЕСКИ забрать страницу, как только проверка пройдена (без
переключения окон).

Ключевое: это тот же Playwright/patchright-контекст, которым управляет скрипт, поэтому
после ручного прохождения проверки DOM полностью доступен. Забираем ТОЛЬКО когда открыт тот
же сайт (host-guard — не засоряем кэш сёрфингом) и на странице есть контент, а не капча.
Постоянный профиль сохраняет clearance-куку → авто-загрузки того же домена потом могут пройти.

Окно/вкладку САМИ НЕ ЗАКРЫВАЕМ: открыл человек — закрывает человек. Успешный забор виден по
смене статуса сайта на «загружено»; не получилось — статус остаётся прежним.
"""
import asyncio
from urllib.parse import urlparse

from . import antibot, cache, clean
from .browser import Browser
from ..config import load_settings, resolve_fetch
from ..obs.log import get_logger, log_event

log = get_logger("fetch.manual")

# Параметры наблюдателя авто-забора (позже можно вынести в конфиг).
POLL = 2.0            # период опроса открытой страницы, сек
TIMEOUT = 240.0       # сколько ждём прохождения проверки, сек
MIN_CHARS = 1500      # порог «на странице есть содержимое товара, а не заглушка»
STABLE = 2            # столько тиков подряд «готово» перед авто-забором (антидребезг)

# Размер окна ручного режима (не на весь экран). Позиционирование не делаем — оставляем дефолт ОС.
WIN_W = 1000          # ширина окна, px
WIN_H = 1000          # высота окна, px


def _host(url: str) -> str:
    return urlparse(url or "").netloc.lower().split(":")[0].removeprefix("www.")


async def _emit(emit, ev: dict) -> None:
    if emit is None:
        return
    r = emit(ev)
    if asyncio.iscoroutine(r):
        await r


class ManualSession:
    """Единственная сессия ручного режима (локальный однопользовательский инструмент)."""

    def __init__(self):
        self._browser: Browser | None = None
        self._pages: dict[str, object] = {}          # target_url -> Playwright Page
        self._watchers: dict[str, asyncio.Task] = {}  # target_url -> задача наблюдателя
        self._lock = asyncio.Lock()

    async def _ensure_browser(self):
        if self._browser is None:
            # человек сам вводит → берём максимум маскировки, но БЕЗ прогрева/авто-ввода;
            # обязательно видимое окно (headed) и постоянный профиль (сохранит куку).
            cfg = resolve_fetch(load_settings().fetch,
                                {"preset": "max", "warmup": False, "human_input": False})
            extra = ["--window-size=%d,%d" % (WIN_W, WIN_H)]   # не на весь экран; позицию не трогаем
            # Если постоянный профиль уже открыт прогоном — Browser подключится к нему (см.
            # browser._PROFILE_LEASE), окно будет тем же; отдельный Chrome не поднимаем.
            self._browser = await Browser(cfg, extra_args=extra).start()
            log.info("Ручной режим: браузер готов (видимое окно, постоянный профиль)")
        return self._browser

    def _stop_watch(self, url: str) -> None:
        task = self._watchers.pop(url, None)
        if task is not None and not task.done():
            task.cancel()

    # ---- открыть окно + запустить наблюдатель --------------------------------
    async def open(self, url: str, emit=None) -> dict:
        async with self._lock:
            br = await self._ensure_browser()
            self._stop_watch(url)
            old = self._pages.pop(url, None)
            if old is not None:
                try:
                    await old.close()
                except Exception:  # noqa: BLE001
                    pass
            page = await br._ctx.new_page()          # тот же контекст — DOM будет доступен скрипту
            self._pages[url] = page
            status = None
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                status = resp.status if resp else None
            except Exception as exc:  # noqa: BLE001
                log.warning("Ручной режим: goto не удался (%s): %s", exc, url)
            self._watchers[url] = asyncio.create_task(self._watch(url, page, emit))
        log.info("Ручной режим: окно открыто для %s (http=%s). Пройдите проверку — заберу страницу сам.",
                 url, status)
        await _emit(emit, {"type": "manual", "url": url, "state": "watching",
                           "note": "окно открыто — пройдите проверку, страница заберётся сама"})
        return {"ok": True, "status": status}

    # ---- наблюдатель: ждём прохождения проверки и авто-забираем --------------
    async def _watch(self, url: str, page, emit) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TIMEOUT
        stable, last = 0, None
        try:
            while loop.time() < deadline:
                await asyncio.sleep(POLL)
                if self._pages.get(url) is not page:      # окно закрыли/переоткрыли
                    return
                try:
                    cur = page.url
                    html = await page.content()
                except Exception:  # noqa: BLE001 — идёт навигация, пропускаем тик
                    stable = 0
                    continue
                if _host(cur) != _host(url):              # человек ушёл на другой сайт
                    stable = 0
                    if last != "surfed":
                        last = "surfed"
                        await _emit(emit, {"type": "manual", "url": url, "state": "surfed",
                                           "current": cur,
                                           "note": "открыт другой сайт (%s) — вернитесь на страницу товара"
                                                   % (_host(cur) or "?")})
                    continue
                if antibot.detect_block(None, html):      # ещё капча/проверка/заглушка
                    stable = 0
                    if last != "blocked":
                        last = "blocked"
                        await _emit(emit, {"type": "manual", "url": url, "state": "blocked",
                                           "note": "идёт проверка/капча — пройдите её в окне"})
                    continue
                if len(clean.clean_page(html)["markdown"]) < MIN_CHARS:   # ещё грузится
                    stable = 0
                    continue
                stable += 1
                if stable >= STABLE:                     # готово и стабильно → забираем
                    self._watchers.pop(url, None)        # снимаем себя, чтобы close/grab не отменяли
                    # Вкладку НЕ закрываем: окно открыл человек — человек и закрывает. Забрали
                    # успешно → статус сайта сам станет «загружено» (событие grabbed).
                    await self._do_grab(page, url, emit, auto=True)
                    return
            await _emit(emit, {"type": "manual", "url": url, "state": "timeout",
                               "note": "не дождались — нажмите «забрать сейчас» или «переоткрыть»"})
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("Ручной режим: наблюдатель упал (%s): %s", exc, url)

    # ---- собственно забор (общий для авто и ручного override) ---------------
    async def _do_grab(self, page, url: str, emit, auto: bool) -> dict:
        try:
            cur = page.url
            html = await page.content()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "Окно закрыто (%s). Откройте заново." % exc}
        if _host(cur) != _host(url):
            msg = ("Сейчас открыт другой сайт (%s). Вернитесь на страницу товара (%s)."
                   % (_host(cur) or "?", _host(url)))
            await _emit(emit, {"type": "manual", "url": url, "state": "surfed", "current": cur, "note": msg})
            return {"ok": False, "surfed": True, "current": cur, "error": msg}

        block = antibot.detect_block(None, html)       # статус неинформативен — смотрим маркеры/пустоту
        if block:
            log.warning("Ручной режим: на странице всё ещё блок/капча (%s): %s", block, cur)
            cache.put(cur, {"url": cur, "status": None, "blocked": block,
                            "markdown": "", "structured": {}, "chars": 0, "html": html})
            await _emit(emit, {"type": "manual", "url": url, "state": "blocked", "current": cur,
                               "note": "ещё капча (%s) — пройдите проверку и повторите" % block})
            return {"ok": True, "blocked": block, "chars": 0, "current": cur}

        cp = clean.clean_page(html)
        st = cp["structured"]
        cache.put(cur, {"url": cur, "status": 200, "blocked": None, "markdown": cp["markdown"],
                        "structured": st, "chars": cp["chars"], "html": html})   # ключ = фактический url
        log_event(log, "manual.grab", url=cur, chars=cp["chars"], auto=auto,
                  jsonld=len(st.get("jsonld") or []), micro=len(st.get("microdata") or []))
        log.info("Ручной режим: страница забрана %s — %s симв.: %s",
                 "автоматически" if auto else "вручную", cp["chars"], cur)
        await _emit(emit, {"type": "manual", "url": url, "state": "grabbed", "current": cur,
                           "chars": cp["chars"],
                           "note": "забрано %s: %s симв." % ("автоматически" if auto else "по кнопке", cp["chars"])})
        return {"ok": True, "blocked": None, "chars": cp["chars"], "current": cur,
                "structured": {"jsonld": len(st.get("jsonld") or []),
                               "microdata": len(st.get("microdata") or []),
                               "meta": len(st.get("meta") or {})}}

    # ---- ручной override «забрать сейчас» ------------------------------------
    async def grab(self, url: str, emit=None) -> dict:
        async with self._lock:
            page = self._pages.get(url)
            if page is None:
                return {"ok": False, "error": "Окно не открыто — сначала «Открыть вручную»."}
            self._stop_watch(url)                        # override отменяет авто-наблюдатель
            return await self._do_grab(page, url, emit, auto=False)  # вкладку закрывает пользователь

    # ---- закрыть окно(а) ------------------------------------------------------
    async def close(self, url: str | None = None, emit=None) -> dict:
        async with self._lock:
            if url is not None:
                self._stop_watch(url)
                p = self._pages.pop(url, None)
                if p is not None:
                    try:
                        await p.close()
                    except Exception:  # noqa: BLE001
                        pass
                await _emit(emit, {"type": "manual", "url": url, "state": "closed"})
                return {"ok": True}
            for u in list(self._watchers):
                self._stop_watch(u)
            for p in self._pages.values():
                try:
                    await p.close()
                except Exception:  # noqa: BLE001
                    pass
            self._pages = {}
            if self._browser is not None:
                await self._browser.close()
                self._browser = None
            log.info("Ручной режим: окно(а) закрыты")
            return {"ok": True}


SESSION = ManualSession()
