# -*- coding: utf-8 -*-
"""Загрузка страницы браузером с настраиваемым «режимом открытия» (анти-блок уровни).

Флаги из FetchConfig: anti_detect (patchright вместо playwright — прячет CDP), real_chrome
(channel=chrome), headed (реальное окно), persistent_profile (куки-допуск), warmup (главная→куки),
human_input (мышь/скролл), wait_networkidle (дать JS дорисовать цену). Комбинация max = наш
эксперимент (~79%, берёт Ozon).
"""
import asyncio
import os
import random
from pathlib import Path
from urllib.parse import urlparse

from ..config import FetchConfig
from ..obs.log import get_logger

log = get_logger("fetch.browser")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
PROFILE_DIR = str(Path(__file__).resolve().parent.parent.parent / "runs" / "browser_profile")

# Один Chrome на постоянный профиль — но НЕ «кто первый, тот и работает»: второй желающий
# (ручное окно ↔ прогон «Найти») ПОДКЛЮЧАЕТСЯ к уже открытому контексту и работает своими
# вкладками. Аренда живёт, пока есть хоть один пользователь; закрывает её последний уходящий.
_PROFILE_LEASE: dict | None = None       # {"pw", "ctx", "browser", "headed": bool, "users": int}


def profile_in_use() -> bool:
    return _PROFILE_LEASE is not None


def _lease_drop() -> None:
    """Забыть аренду (аварийный сброс из /api/browser/reset — процесс Chrome уже убит)."""
    global _PROFILE_LEASE
    _PROFILE_LEASE = None


def _clear_stale_singleton(profile_dir: str) -> None:
    """Снять устаревший SingletonLock профиля, если процесс, который его держал, уже мёртв.

    Живой лок НЕ трогаем (иначе повредим чужой Chrome). SingletonLock на macOS/Linux — симлинк
    вида `host-<pid>`; если pid не жив, лок устарел (краш/недобитый процесс) и мешает запуску.
    """
    import os
    p = Path(profile_dir)
    lock = p / "SingletonLock"
    try:
        target = os.readlink(lock)                       # "hostname-12345"
    except OSError:
        return                                           # не симлинк/нет файла — чистить нечего
    pid = None
    if "-" in target:
        try:
            pid = int(target.rsplit("-", 1)[1])
        except ValueError:
            pid = None
    if pid:
        try:
            os.kill(pid, 0)
            return                                       # процесс жив — лок настоящий, не трогаем
        except ProcessLookupError:
            pass                                         # мёртв → лок устарел
        except PermissionError:
            return                                       # существует (чужой) — не трогаем
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            (p / name).unlink()
        except OSError:
            pass
    log.info("Снят устаревший SingletonLock профиля (мёртвый pid=%s)", pid)

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU','ru','en-US','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = window.chrome || { runtime: {} };
"""


class Browser:
    def __init__(self, cfg: FetchConfig, extra_args: list[str] | None = None):
        self.cfg = cfg
        self._extra_args = list(extra_args or [])     # доп. флаги окна (напр. --window-position)
        self._pw = None
        self._browser = None
        self._ctx = None
        self._owns_profile = False                     # мы владелец аренды постоянного профиля
        self._borrowed = False                         # мы подключились к чужому живому контексту
        self._warmed: set[str] = set()

    async def start(self):
        global _PROFILE_LEASE
        # Постоянный профиль уже открыт кем-то (ручное окно или другой прогон) — не падаем и не
        # запускаем второй Chrome на тот же каталог, а работаем в том же контексте своими вкладками.
        if self.cfg.persistent_profile and _PROFILE_LEASE is not None:
            # Исключение — когда нам нужно ВИДИМОЕ окно (ручной режим), а живой контекст headless:
            # подключаться нельзя, человек просто не увидит окна. Говорим об этом прямо.
            if self.cfg.headed and not _PROFILE_LEASE["headed"]:
                raise RuntimeError(
                    "Профиль занят фоновым (headless) браузером — видимое окно сейчас не открыть. "
                    "Дождитесь окончания загрузки страниц или нажмите «Стоп».")
            _PROFILE_LEASE["users"] += 1
            self._ctx = _PROFILE_LEASE["ctx"]
            self._borrowed = True
            log.info("Браузер: переиспользую уже открытый Chrome постоянного профиля "
                     "(окно%s, пользователей: %d) — свои вкладки в том же окне",
                     " видимое" if _PROFILE_LEASE["headed"] else " headless", _PROFILE_LEASE["users"])
            return self

        # patchright (анти-детект) — drop-in замена playwright
        if self.cfg.anti_detect:
            from patchright.async_api import async_playwright
        else:
            from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()

        args = ["--disable-blink-features=AutomationControlled", *self._extra_args]
        # Песочница Chromium отказывается стартовать ТОЛЬКО от root (Docker/CI) — там нужен
        # --no-sandbox. На обычном десктопе песочница работает; флаг лишний и вредный: Chrome
        # вешает баннер «стабильность и безопасность будут нарушены» и реально снимает изоляцию
        # рендерера (мы открываем недоверенные страницы). Поэтому добавляем его только под root.
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args.append("--no-sandbox")
        launch_kw = {"headless": not self.cfg.headed, "args": args}
        if self.cfg.real_chrome:
            launch_kw["channel"] = "chrome"
        ctx_kw = {"locale": "ru-RU", "timezone_id": "Europe/Moscow"}
        if self.cfg.headed:
            ctx_kw["no_viewport"] = True
        else:
            ctx_kw["viewport"] = {"width": 1366, "height": 900}
        if not self.cfg.anti_detect:                 # patchright сам ставит UA/стелс — не мешаем ему
            ctx_kw["user_agent"] = UA
            ctx_kw["extra_http_headers"] = {"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}

        if self.cfg.persistent_profile:
            _clear_stale_singleton(PROFILE_DIR)
            self._owns_profile = True
        try:
            try:
                if self.cfg.persistent_profile:
                    self._ctx = await self._pw.chromium.launch_persistent_context(
                        PROFILE_DIR, **launch_kw, **ctx_kw)
                else:
                    self._browser = await self._pw.chromium.launch(**launch_kw)
                    self._ctx = await self._browser.new_context(**ctx_kw)
            except Exception as exc:  # noqa: BLE001 — напр. нет Chrome-канала → bundled Chromium
                log.warning("Запуск с channel=chrome не удался (%s) — bundled Chromium", exc)
                launch_kw.pop("channel", None)
                if self.cfg.persistent_profile:
                    self._ctx = await self._pw.chromium.launch_persistent_context(PROFILE_DIR, **launch_kw, **ctx_kw)
                else:
                    self._browser = await self._pw.chromium.launch(**launch_kw)
                    self._ctx = await self._browser.new_context(**ctx_kw)
        except Exception:                                # полный провал запуска — аренду не заводим
            self._owns_profile = False
            try:
                await self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            raise

        if self._owns_profile:                           # мы открыли профиль — заводим аренду
            _PROFILE_LEASE = {"pw": self._pw, "ctx": self._ctx, "browser": self._browser,
                              "headed": bool(self.cfg.headed), "users": 1}

        if not self.cfg.anti_detect:                 # ручной стелс только для обычного playwright
            await self._ctx.add_init_script(_STEALTH_JS)
            try:
                from playwright_stealth import Stealth
                await Stealth().apply_stealth_async(self._ctx)
            except Exception as exc:  # noqa: BLE001
                log.debug("playwright-stealth не применён: %s", exc)
        log.info("Браузер: %s%s%s%s", "chrome" if self.cfg.real_chrome else "chromium",
                 " +patchright" if self.cfg.anti_detect else "",
                 " +headed" if self.cfg.headed else " (headless)",
                 " +profile" if self.cfg.persistent_profile else "")
        return self

    async def _human(self, page):
        if not self.cfg.human_input:
            return
        try:
            for _ in range(3):
                await page.mouse.move(random.randint(120, 1200), random.randint(120, 700),
                                      steps=random.randint(6, 16))
                await asyncio.sleep(random.uniform(0.2, 0.6))
            await page.mouse.wheel(0, random.randint(400, 1000))
            await asyncio.sleep(random.uniform(0.5, 1.2))
        except Exception:  # noqa: BLE001
            pass

    async def warmup(self, url: str):
        """Зайти на главную домена за куками. Зовётся ПЕРЕД ПОВТОРОМ после блока (fetch/fetcher.py).

        Раньше это делалось перед каждой страницей каждого нового домена и просто удваивало число
        навигаций: на домене, который и так нас пускает, прогрев ничего не даёт. Один раз на домен
        (`_warmed`): если блок повторяется, дело не в куках.
        """
        if not self.cfg.warmup:
            return
        parts = urlparse(url)
        dom = parts.netloc.lower()
        if dom in self._warmed:
            return
        self._warmed.add(dom)
        page = await self._ctx.new_page()
        try:
            await page.goto("%s://%s/" % (parts.scheme or "https", dom),
                            wait_until="domcontentloaded", timeout=20000)
            await self._human(page)
            await asyncio.sleep(random.uniform(1.5, 3.0))
            log.debug("Прогрев домена: %s", dom)
        except Exception as exc:  # noqa: BLE001
            log.debug("Прогрев %s не удался: %s", dom, exc)
        finally:
            await page.close()

    async def fetch(self, url: str, timeout: float | None = None, scroll: bool = True) -> dict:
        """Открыть страницу и вернуть её HTML.

        Ожидание дорисовки (networkidle до 7 с + скролл + пауза 1,2–2,2 с) стоит около десяти
        секунд НА КАЖДОЙ странице, а нужно оно ровно в одном случае — когда цену рисует JS уже
        после загрузки DOM. Поэтому сначала смотрим, есть ли цена в готовом тексте: если есть —
        ждать нечего; если нет — отрабатываем прежний тяжёлый путь целиком.
        """
        timeout = timeout or self.cfg.timeout
        page = await self._ctx.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            status = resp.status if resp else None
            if await self._price_drawn(page):
                await asyncio.sleep(random.uniform(0.2, 0.4))
                return {"status": status, "html": await page.content()}
            if self.cfg.wait_networkidle:
                try:
                    await page.wait_for_load_state("networkidle", timeout=7000)
                except Exception:  # noqa: BLE001
                    pass
            await self._human(page)
            if scroll:
                try:
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight*0.6)")
                    await asyncio.sleep(random.uniform(1.2, 2.2))
                except Exception:  # noqa: BLE001
                    pass
            html = await page.content()
            return {"status": status, "html": html}
        finally:
            await page.close()

    async def _price_drawn(self, page) -> bool:
        """Цена уже есть в видимом тексте страницы — дорисовки ждать не нужно.

        Признак тот же, что и у отбора ценовых блоков (`extract/focus.has_price`): одна проверка
        на весь проект. Не смогли прочитать текст — считаем, что цены нет: лишнее ожидание
        безопаснее пропущенной цены.
        """
        from ..extract.focus import has_price
        try:
            return has_price(await page.inner_text("body"))
        except Exception:  # noqa: BLE001 — страница может ещё не иметь body
            return False

    async def close(self):
        """Отпустить браузер. Реальный Chrome постоянного профиля закрывает ПОСЛЕДНИЙ уходящий.

        Свои вкладки закрывать не нужно: fetch()/warmup() всегда закрывают страницу за собой,
        поэтому заимствующий не оставляет мусора в чужом окне.
        """
        global _PROFILE_LEASE
        if _PROFILE_LEASE is not None and (self._borrowed or self._owns_profile):
            _PROFILE_LEASE["users"] -= 1
            if _PROFILE_LEASE["users"] > 0:              # профилем ещё кто-то пользуется
                log.info("Браузер: отпустил постоянный профиль (осталось пользователей: %d)",
                         _PROFILE_LEASE["users"])
                self._ctx = self._browser = self._pw = None
                self._borrowed = self._owns_profile = False
                return
            pw, ctx, br = _PROFILE_LEASE["pw"], _PROFILE_LEASE["ctx"], _PROFILE_LEASE["browser"]
            _PROFILE_LEASE = None
            self._ctx, self._browser, self._pw = ctx, br, pw   # закрываем то, что реально открыто
            self._borrowed = self._owns_profile = False
        for obj, meth in ((self._ctx, "close"), (self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    await getattr(obj, meth)()
            except Exception:  # noqa: BLE001
                pass
        self._ctx = self._browser = self._pw = None
