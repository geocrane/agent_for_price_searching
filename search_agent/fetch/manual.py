# -*- coding: utf-8 -*-
"""Ручной режим: открыть карточку в видимом окне НАШЕГО браузера, дать человеку пройти
капчу/проверку — и АВТОМАТИЧЕСКИ забрать страницу, как только проверка пройдена (без
переключения окон).

Ключевое: это тот же Playwright/patchright-контекст, которым управляет скрипт, поэтому
после ручного прохождения проверки DOM полностью доступен. Забираем ТОЛЬКО когда открыт тот
же сайт (host-guard — не засоряем кэш сёрфингом) и на странице есть контент, а не капча.
Постоянный профиль сохраняет clearance-куку → авто-загрузки того же домена потом могут пройти.

Забрали страницу — вкладку СРАЗУ ЗАКРЫВАЕМ. Человек кликает домены пачкой, и открытая вкладка
после успешного забора не несёт информации: она лишь занимает место на экране и оставляет
сомнение «прочиталось или нет». Закрытая вкладка + уход сайта из списка блокировок и есть
однозначный признак «страница прочитана, ушла в очередь». Не получилось — вкладка остаётся
открытой, статус прежний.

КРИТЕРИЙ ГОТОВНОСТИ ЗДЕСЬ СВОЙ, И ЭТО ГЛАВНОЕ. Раньше наблюдатель спрашивал общий
`antibot.detect_block`, который ищет подстроку "captcha" в сыром HTML. У обычного магазина
скрипт reCAPTCHA стоит в <head> ради формы обратного звонка — то есть карточка товара с ценой
на экране ВСЕГДА опознавалась как капча: инструмент бесконечно писал «идёт проверка», страницу
не забирал и она навсегда оставалась в блокировках. Для автозагрузки такая ошибка стоит одной
страницы, здесь — всей ручной работы человека. Поэтому критерий положительный: не «нет
признаков капчи», а «на странице ЕСТЬ товар» (см. `_page_ready`).

Окно браузера человек всё же может закрыть в любой момент (крестиком). Тогда контекст Playwright
мёртв, а ссылка на него остаётся, и следующее «Открыть вручную» падало с TargetClosedError.
Живость спрашиваем у аренды профиля (`browser._lease_alive`) — она снимается при закрытии окна.
"""
import asyncio
import json
from urllib.parse import urlparse

from . import cache, clean
from .browser import Browser
from ..config import load_settings, resolve_fetch
from ..obs.log import get_logger, log_event

log = get_logger("fetch.manual")

# Параметры наблюдателя авто-забора (позже можно вынести в конфиг).
POLL = 1.0            # период опроса открытой страницы, сек
TIMEOUT = 600.0       # сколько ждём прохождения проверки, сек
MIN_CHARS = 400       # порог «на странице есть содержимое», когда структурных данных нет
STABLE = 2            # столько тиков подряд «готово» перед авто-забором (антидребезг)

# Размер окна ручного режима (не на весь экран). Позиционирование не делаем — оставляем дефолт ОС.
WIN_W = 1000          # ширина окна, px
WIN_H = 1000          # высота окна, px

# Фразы страницы-заглушки, которые ЧЕЛОВЕК видит глазами. Ищем их в видимом тексте, а не в HTML:
# в разметке те же слова приходят из скриптов и атрибутов на совершенно нормальных карточках.
_CHALLENGE = ("подтвердите, что вы не робот", "проверка, что вы не робот", "я не робот",
              "проверка браузера", "доступ ограничен", "доступ временно ограничен",
              "are you a robot", "verify you are human", "checking your browser",
              "just a moment", "attention required", "enable javascript and cookies")

# Признаки карточки товара в JSON-LD. Ищем по сериализованному блоку: схемы вкладывают @type
# как угодно глубоко (@graph, itemListElement, offers), и обход дерева тут ничего не добавит.
_PRODUCT_TYPES = ('"product"', '"offer"', '"aggregateoffer"', '"scholarlyarticle"')


def _host(url: str) -> str:
    return urlparse(url or "").netloc.lower().split(":")[0].removeprefix("www.")


def _jsonld_product(blocks) -> bool:
    """Есть ли в JSON-LD карточка товара или предложение цены."""
    for b in blocks or []:
        try:
            raw = json.dumps(b, ensure_ascii=False).lower()
        except (TypeError, ValueError):  # noqa: BLE001 — чужой JSON бывает любым
            continue
        if any(t in raw for t in _PRODUCT_TYPES):
            return True
    return False


def _page_ready(html: str) -> tuple[str, str, dict]:
    """Состояние открытой вкладки: ("ready"|"blocked"|"loading", пояснение, очищенная страница).

    Порядок проверок — ровно тот, что описан в докстринге модуля: сперва ищем ТОВАР, и только не
    найдя его, спрашиваем, не заглушка ли это. Обратный порядок и был прежней ошибкой.
    """
    cp = clean.clean_page(html)
    st = cp.get("structured") or {}
    # microdata и meta здесь уже отфильтрованы `clean` до товарных/ценовых свойств, поэтому сам
    # факт их непустоты — сигнал «это карточка», а не просто «в разметке что-то есть».
    has_product = bool(st.get("microdata")) or bool(st.get("meta")) or _jsonld_product(st.get("jsonld"))
    text = (cp.get("markdown") or "").strip()
    if has_product or len(text) >= MIN_CHARS:
        return "ready", "", cp
    low = text[:4000].lower()
    for m in _CHALLENGE:
        if m in low:
            return "blocked", "идёт проверка — пройдите её в окне", cp
    return "loading", "", cp


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
        # Состояние окна по URL: его показывает СТРОКА СПИСКА блокировок. Раньше «идёт проверка»
        # жило только в глобальной строке статуса, которую перетирало любое следующее событие,
        # и понять, что происходит с конкретной страницей, было нельзя.
        self._states: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    # ---- состояние окна (для интерфейса) -------------------------------------

    def state_of(self, url: str) -> dict | None:
        """Что сейчас с ручным окном этой страницы (или None, если окна нет)."""
        return self._states.get(url)

    def open_urls(self) -> list[str]:
        """Адреса, окна которых открыты прямо сейчас. Пока список не пуст, работа не закончена."""
        return list(self._pages)

    async def _set_state(self, url: str, state: str, emit, **extra) -> None:
        """Запомнить состояние окна и отправить его в интерфейс одним и тем же событием."""
        ev = {"type": "manual", "url": url, "state": state, **extra}
        if state in ("grabbed", "closed"):
            self._states.pop(url, None)
        else:
            self._states[url] = ev
        await _emit(emit, ev)

    def _forget_browser(self) -> None:
        """Забыть мёртвый браузер: окна нет, значит и наших вкладок в нём нет.

        Саму аренду здесь НЕ трогаем: её снимает `browser._lease_died` при закрытии контекста.
        Снимать её отсюда было бы опасно — если контекст успел подняться заново, мы бы выбросили
        живую аренду, и на том же каталоге профиля стартовал бы второй Chrome.
        """
        for u in list(self._watchers):
            self._stop_watch(u)
        self._pages.clear()
        self._states.clear()
        self._browser = None

    async def _ensure_browser(self):
        # Живость спрашиваем у аренды, а не у своего флага: единственный источник правды о
        # состоянии Chrome — `browser._PROFILE_LEASE`, и он же снимается при закрытии окна.
        from . import browser as _browser_mod
        if self._browser is not None and not _browser_mod._lease_alive():
            log.info("Ручной режим: окно браузера было закрыто — поднимаю заново")
            self._forget_browser()
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

    def _forget_page(self, url: str, page) -> None:
        """Вкладка закрыта (нами после забора или человеком) — снять её и наблюдателя."""
        if self._pages.get(url) is page:
            self._pages.pop(url, None)
            self._stop_watch(url)

    # ---- открыть окно + запустить наблюдатель --------------------------------
    async def open(self, url: str, emit=None) -> dict:
        async with self._lock:
            self._stop_watch(url)
            old = self._pages.pop(url, None)
            if old is not None:
                try:
                    await old.close()
                except Exception:  # noqa: BLE001
                    pass
            # Окно могли закрыть ПРЯМО СЕЙЧАС — между проверкой живости и созданием вкладки.
            # Один повтор с поднятием браузера заново; второй раз — честная ошибка наверх,
            # долбить одно и то же бессмысленно.
            page = None
            for attempt in (1, 2):
                br = await self._ensure_browser()
                try:
                    page = await br._new_page()       # тот же контекст — DOM доступен скрипту
                    break
                except Exception as exc:  # noqa: BLE001
                    if attempt == 2:
                        raise
                    log.warning("Ручной режим: вкладку открыть не удалось (%s) — поднимаю заново", exc)
                    self._forget_browser()
            self._pages[url] = page
            # Вкладку может закрыть и человек — тогда наблюдателю ждать нечего.
            try:
                page.on("close", lambda *_a: self._forget_page(url, page))
            except Exception:  # noqa: BLE001
                pass
            await self._set_state(url, "opening", emit, note="открываю страницу")
            status = None
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                status = resp.status if resp else None
            except Exception as exc:  # noqa: BLE001
                log.warning("Ручной режим: goto не удался (%s): %s", exc, url)
            self._watchers[url] = asyncio.create_task(self._watch(url, page, emit))
        log.info("Ручной режим: окно открыто для %s (http=%s). Пройдите проверку — заберу страницу сам.",
                 url, status)
        return {"ok": True, "status": status}

    # ---- наблюдатель: ждём прохождения проверки и авто-забираем --------------
    async def _watch(self, url: str, page, emit) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TIMEOUT
        stable, last = 0, "opening"
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
                        await self._set_state(
                            url, "surfed", emit, current=cur,
                            note="открыт другой сайт (%s) — вернитесь на страницу товара"
                                 % (_host(cur) or "?"))
                    continue
                state, note, cp = _page_ready(html)
                if state != "ready":
                    stable = 0
                    if last != state:
                        last = state
                        await self._set_state(url, state, emit,
                                              note=note or "страница ещё загружается")
                    continue
                stable += 1
                if stable >= STABLE:                     # готово и стабильно → забираем
                    self._watchers.pop(url, None)        # снимаем себя, чтобы close/grab не отменяли
                    # Забрали успешно → _do_grab закрывает вкладку сам, а событие grabbed уводит
                    # сайт из «Блокировок» в очередь извлечения.
                    await self._do_grab(page, url, emit, auto=True)
                    return
            await self._set_state(url, "timeout", emit,
                                  note="не дождались — нажмите «забрать сейчас» или «переоткрыть»")
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
            await self._set_state(url, "surfed", emit, current=cur, note=msg)
            return {"ok": False, "surfed": True, "current": cur, "error": msg}

        state, note, cp = _page_ready(html)
        if state != "ready" and not auto:
            # Ручной override берёт то, что есть: человек смотрит на страницу своими глазами и
            # его решение старше нашего критерия. Но пустоту забирать нечего — это не страница.
            if not (cp.get("markdown") or "").strip():
                state = "blocked"
            else:
                state = "ready"
        if state != "ready":
            log.warning("Ручной режим: страница ещё не готова (%s): %s", state, cur)
            # В КЭШ ЗДЕСЬ НЕ ПИШЕМ. Раньше сюда клали запись с blocked, и она навсегда отравляла
            # адрес: следующий забор той же страницы отбрасывался ещё до разбора («ещё под
            # защитой»), сколько бы раз человек её ни открывал. Кэш — про содержимое страницы,
            # а «нас не пустили» содержимым не является.
            await self._set_state(url, "blocked", emit, current=cur,
                                  note=note or "страница пуста — пройдите проверку и повторите")
            return {"ok": True, "blocked": state, "chars": 0, "current": cur}

        st = cp["structured"]
        cache.put(cur, {"url": cur, "status": 200, "blocked": None, "markdown": cp["markdown"],
                        "structured": st, "chars": cp["chars"], "html": html})   # ключ = фактический url
        log_event(log, "manual.grab", url=cur, chars=cp["chars"], auto=auto,
                  jsonld=len(st.get("jsonld") or []), micro=len(st.get("microdata") or []))
        log.info("Ручной режим: страница забрана %s — %s симв.: %s",
                 "автоматически" if auto else "вручную", cp["chars"], cur)
        # Страница прочитана — вкладка больше не нужна. Закрываем ДО события: человек кликает
        # домены пачкой, и оставленная вкладка не даёт понять, что этот сайт уже отработан.
        self._forget_page(url, page)
        try:
            await page.close()
        except Exception:  # noqa: BLE001 — вкладку мог закрыть и человек, это не ошибка забора
            pass
        await self._set_state(url, "grabbed", emit, current=cur, chars=cp["chars"],
                              note="забрано %s: %s симв. — вкладка закрыта, сайт в очереди"
                                   % ("автоматически" if auto else "по кнопке", cp["chars"]))
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
            return await self._do_grab(page, url, emit, auto=False)  # вкладку закроет _do_grab

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
                await self._set_state(url, "closed", emit)
                return {"ok": True}
            for u in list(self._watchers):
                self._stop_watch(u)
            for p in self._pages.values():
                try:
                    await p.close()
                except Exception:  # noqa: BLE001
                    pass
            urls, self._pages = list(self._pages), {}
            for u in urls:
                await self._set_state(u, "closed", emit)
            if self._browser is not None:
                await self._browser.close()
                self._browser = None
            log.info("Ручной режим: окно(а) закрыты")
            return {"ok": True}


SESSION = ManualSession()
