# -*- coding: utf-8 -*-
"""Оркестратор загрузки: лёгкий путь→браузер, темп по домену, кэш, детект блока, очистка.

Отдаёт по URL: {url, status, blocked, chars, clean_text, jsonld}. Цену НЕ извлекаем.

Два решения, от которых зависит скорость всего прогона (замер: раньше страница обходилась
в ~37 с, из них полезной работы — единицы секунд):

  * ТЕМП СЧИТАЕТСЯ ПО ДОМЕНУ, А НЕ ПО СТРАНИЦЕ. Человекоподобная пауза защищает от того, что мы
    долбим ОДИН сайт; на первый заход на домен она не защищает ни от чего. Поэтому паузы ждёт
    только повторный запрос к тому же домену, и ждёт ровно недостающее до интервала время
    (`_pace`). У позиции полтора десятка разных доменов — раньше платили паузу за каждый.
  * СНАЧАЛА ЛЁГКИЙ ПУТЬ, БРАУЗЕР — ПО НЕОБХОДИМОСТИ (`http_first`). Chromium нужен там, где
    страницу рисует JS или стоит антибот; на обычной странице магазина httpx отдаёт тот же HTML
    за доли секунды. Решение принимается ПО РЕЗУЛЬТАТУ (`_needs_browser`), а не по списку сайтов:
    блок, пустая страница или SPA-оболочка → повторяем браузером. Хуже, чем «всегда браузер»,
    быть не может — только на секунду дольше в худшем случае.
"""
import asyncio
import random
import time
from urllib.parse import urlparse

from . import antibot, cache, clean
from .browser import Browser
from ..config import CleanConfig, FetchConfig
from ..obs.log import get_logger, log_event, timed

log = get_logger("fetch")

# Ниже этого объёма очищенного текста лёгкая загрузка считается неудачной: столько выдаёт
# оболочка SPA или страница-заглушка, товара с ценой в таком объёме не бывает.
LIGHT_MIN_CHARS = 200


class Fetcher:
    def __init__(self, cfg: FetchConfig, clean_cfg: CleanConfig | None = None):
        self.cfg = cfg
        self.clean_cfg = clean_cfg or CleanConfig()
        self._browser: Browser | None = None
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._last_hit: dict[str, float] = {}          # когда последний раз ходили в этот домен

    async def start(self):
        if self.cfg.backend == "browser":
            self._browser = await Browser(self.cfg).start()
        return self

    async def close(self):
        if self._browser is not None:
            await self._browser.close()

    # ---- темп и выбор пути ---------------------------------------------------

    async def _pace(self, dom: str) -> None:
        """Выдержать человекоподобный интервал МЕЖДУ запросами к одному домену.

        Первый заход на домен не ждёт: пауза защищает от частых обращений к ОДНОМУ сайту, а не
        от самого факта загрузки. Ждём ровно недостающее — время, потраченное на предыдущую
        страницу, уже засчитывается в интервал.
        """
        last = self._last_hit.get(dom)
        if last is None:
            return
        gap = random.uniform(*self.cfg.delay_range[:2]) - (time.monotonic() - last)
        if gap > 0:
            await asyncio.sleep(gap)

    def _paths(self) -> list[str]:
        """Пути загрузки по возрастанию тяжести: лёгкий httpx, затем браузер (если разрешён)."""
        if self.cfg.backend == "browser" and self.cfg.http_first:
            return ["http", "browser"]
        return [self.cfg.backend]

    async def _raw(self, url: str, backend: str) -> dict:
        if backend == "browser":
            return await self._browser.fetch(url, timeout=self.cfg.timeout)
        from . import http
        # Лёгкий путь ждёт МЕНЬШЕ основного: он затем и лёгкий, чтобы либо ответить быстро, либо
        # уступить браузеру. С общим таймаутом (30 с) медленный или тупиковый хост съедал их
        # целиком ПЕРЕД тем, как за него возьмётся браузер, — и ускорение оборачивалось задержкой.
        # Когда лёгкий путь единственный (backend=http), урезать нечего: уступать некому.
        timeout = min(self.cfg.timeout, self.cfg.http_timeout) \
            if self.cfg.backend == "browser" and self.cfg.http_first else self.cfg.timeout
        return await http.fetch(url, timeout=timeout)

    # ---- одна попытка / одна загрузка ----------------------------------------

    async def _attempt(self, url: str, backend: str) -> dict:
        """Загрузить одним путём и сразу очистить. Ошибку транспорта возвращает результатом."""
        try:
            # trace=False: недоступный сайт — ОЖИДАЕМЫЙ исход загрузки, а не сбой программы.
            # Причина остаётся в строке ниже; трейсбек httpx на каждый такой сайт (полсотни строк)
            # только прятал бы в консоли настоящие ошибки.
            with timed(log, "fetch.page", trace=False, url=url, via=backend):
                raw = await self._raw(url, backend)
        except Exception as exc:  # noqa: BLE001 — сеть/браузер капризны, решает вызывающий
            log.warning("Загрузка не удалась %s (%s): %s: %s — %s", url, backend,
                        type(exc).__name__, exc,
                        "пробую браузером" if backend != self._paths()[-1] else "страница пропущена")
            return _result(url, None, "error", {}, error=str(exc), via=backend)
        status, html = raw.get("status"), raw.get("html") or ""
        block = antibot.detect_block(status, html)
        if block:
            return _result(url, status, block, {"markdown": "", "structured": {}},
                           html=html, via=backend)
        try:
            # Очистка — CPU-работа на сотнях КБ HTML (selectolax + markdownify). В event loop
            # она вставала поперёк ВСЕГО прогона: стрима модели, событий UI, других загрузок.
            cp = await asyncio.to_thread(clean.clean_page, html, self.clean_cfg)
        except Exception as exc:  # noqa: BLE001 — кривой HTML не должен ронять всю загрузку
            log.warning("Очистка не удалась %s: %s — оставляю html для reclean", url, exc)
            cp = {"markdown": "", "structured": {}}
        res = _result(url, status, None, cp, html=html, via=backend)
        st = cp.get("structured") or {}
        log_event(log, "fetch.clean", url=url, status=status, chars=res["chars"], via=backend,
                  jsonld=len(st.get("jsonld") or []), micro=len(st.get("microdata") or []),
                  meta=len(st.get("meta") or {}))
        return res

    async def _once(self, url: str, dom: str) -> dict:
        """Одна попытка получить страницу: лёгкий путь, а если он не дал страницы — браузер.

        ОШИБКА ЛЁГКОГО ПУТИ — ТАКОЙ ЖЕ ПОВОД ОТКРЫТЬ БРАУЗЕР, как блок или пустая страница.
        Раньше сбой httpx возвращался наружу сразу, и любая его поломка (недоступная зависимость,
        разорванное соединение, кривой TLS) выглядела как «сайт заблокирован» — при том что
        Chromium открыл бы страницу без вопросов.
        """
        paths = self._paths()
        res = None
        for backend in paths:
            await self._pace(dom)
            res = await self._attempt(url, backend)
            self._last_hit[dom] = time.monotonic()
            if backend == paths[-1] or not _needs_browser(res):
                return res
            # «Фоновым» сказано не для красоты: окна на экране не будет, и без этой оговорки
            # сообщение выглядит как обещание открыть Chrome, которое никто не выполнил.
            log.info("Лёгкая загрузка не дала страницы (%s) — открываю браузером (%s): %s",
                     _light_fail_reason(res),
                     "видимое окно" if self.cfg.headed else "фоновым, окна не будет", url)
        return res

    async def _load(self, url: str, dom: str) -> dict:
        """Загрузка под доменным локом: повтор с прогревом и cooldown при мягком блоке."""
        attempts = 1 + max(0, self.cfg.retry_on_block)
        res = None
        for attempt in range(attempts):
            if attempt:
                # Прогрев (заход на главную за куками) осмыслен ровно здесь — на домене, который
                # нас уже не пустил. Раньше он делался ПЕРЕД каждой страницей каждого домена и
                # просто удваивал число навигаций.
                await self._warm(url)
            res = await self._once(url, dom)
            block = res.get("blocked")
            if res.get("error") or not block:
                return res
            soft = block in ("captcha/antibot", "http_429", "http_503")
            if soft and attempt < attempts - 1:
                cd = random.uniform(*self.cfg.cooldown_range[:2])
                log.warning("Мягкий блок (%s) — cooldown %.0fс и повтор: %s", block, cd, url)
                await asyncio.sleep(cd)
                continue
            return res
        return res

    async def _warm(self, url: str) -> None:
        if self.cfg.warmup and self._browser is not None:
            await self._browser.warmup(url)

    async def fetch_one(self, url: str, use_cache: bool = True) -> dict:
        if use_cache:
            hit = cache.get(url)
            # Из кэша отдаём ТОЛЬКО удачно взятую страницу. Отказ (блок/ошибка) — состояние
            # МОМЕНТА, а не свойство URL: закэшировав его, мы навсегда закрепляли неудачу —
            # починенная загрузка продолжала отдавать «заблокировано» из файла и не ходила в сеть.
            if hit is not None and not hit.get("blocked") and not hit.get("error"):
                log_event(log, "fetch.cache_hit", url=url, chars=hit.get("chars"))
                return hit
            if hit is not None:
                log_event(log, "fetch.cache_stale", url=url, blocked=hit.get("blocked"),
                          reason="в кэше отказ — гружу заново")

        dom = urlparse(url).netloc.lower()
        lock = self._domain_locks.setdefault(dom, asyncio.Lock())
        async with lock:                                   # 1 запрос на домен за раз
            res = await self._load(url, dom)
        if res.get("blocked") and not res.get("error"):
            log.warning("Заблокировано (%s): %s", res["blocked"], url)
        cache.put(url, res)
        return res


def _needs_browser(res: dict) -> bool:
    """Лёгкая загрузка не годится: ошибка, блок, пустая страница или SPA-оболочка (дорисует JS)."""
    if res.get("error") or res.get("blocked"):
        return True
    md = (res.get("markdown") or "").strip()
    if len(md) < LIGHT_MIN_CHARS:
        return True
    # Оболочку узнаём тем же признаком, что и триаж страниц (обоснование порога — в inspect.py):
    # заводить вторую эвристику на то же самое нельзя, разъедутся.
    from ..extract.inspect import SHELL_MIN_CHARS, SHELL_RATIO, service_json_ratio
    return len(md) >= SHELL_MIN_CHARS and service_json_ratio(md) >= SHELL_RATIO


def _light_fail_reason(res: dict) -> str:
    if res.get("error"):
        return "ошибка: %s" % str(res["error"])[:120]
    if res.get("blocked"):
        return "блок: %s" % res["blocked"]
    if len((res.get("markdown") or "").strip()) < LIGHT_MIN_CHARS:
        return "пусто"
    return "оболочка без товара"


def reclean_cache(clean_cfg: CleanConfig | None = None) -> dict:
    """Перечистить закэшированные страницы по сохранённому html (оффлайн, без захода на сайты).

    Применяет выбранный сценарий очистки к уже загруженным страницам — для сравнения сценариев
    в превью без повторного забора (быстро и без риска антибота).
    """
    import json
    from pathlib import Path
    clean_cfg = clean_cfg or CleanConfig()
    ok, skip = 0, 0
    for f in sorted(Path(cache.CACHE_DIR).glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        html = d.get("html")
        if not html or d.get("blocked"):
            skip += 1
            continue
        cp = clean.clean_page(html, clean_cfg)
        d.update(markdown=cp["markdown"], structured=cp["structured"], chars=cp["chars"])
        f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        ok += 1
    log.info("reclean: перечищено=%d, пропущено=%d", ok, skip)
    return {"recleaned": ok, "skipped": skip}


def _result(url, status, blocked, cp: dict, error=None, html=None, via=None) -> dict:
    """cp: результат clean_page ({markdown, structured, chars}) или заглушка при блоке.

    via — каким путём страница взята («http»/«browser»): иначе в логе и в UI гибридная загрузка
    неотличима от прежней и непонятно, где браузер действительно понадобился.
    """
    md = cp.get("markdown", "") or ""
    return {"url": url, "status": status, "blocked": blocked, "error": error,
            "markdown": md, "structured": cp.get("structured", {}), "chars": len(md),
            "via": via, "html": html}              # сырой HTML — для оффлайн-reclean


async def fetch_items(items_data: list[dict], cfg: FetchConfig | None = None,
                      clean_cfg: CleanConfig | None = None, emit=None,
                      use_cache: bool = True) -> list[dict]:
    """Загрузить топ-N кандидатов по каждому товару из фикстуры discovery.

    items_data: [{row, name, candidates:[{url, domain, is_file, ...}]}]. Возвращает те же
    товары с полем candidates[i]['fetch'] = результат загрузки. Файлы (is_file) пока пропускаем.

    use_cache=False — принудительная перезагрузка С НУЛЯ: грузим ВСЕ страницы заново, минуя кэш,
    и перезаписываем его свежим результатом (для тестов «прогнать извлечение заново»).
    """
    from ..config import load_settings
    settings = load_settings()
    cfg = cfg or settings.fetch
    clean_cfg = clean_cfg or settings.clean
    fetcher = await Fetcher(cfg, clean_cfg).start()
    sem = asyncio.Semaphore(max(1, cfg.concurrency))
    # top_n_fetch <= 0 → грузить ВСЕХ кандидатов (без ограничения топ-N)
    cap = cfg.top_n_fetch if cfg.top_n_fetch and cfg.top_n_fetch > 0 else None
    total = sum(len(nf) if cap is None else min(cap, len(nf))
                for nf in ([c for c in it.get("candidates", []) if not c.get("is_file")]
                           for it in items_data))
    done = 0
    log.info("Загрузка: товаров=%d, страниц≈%d, лимит=%s, кэш=%s, бэкенд=%s, паузы=%s",
             len(items_data), total, cap or "все", "да" if use_cache else "НЕТ (заново)",
             cfg.backend, cfg.delay_range)

    async def one_cand(row, cand):
        nonlocal done
        async with sem:
            await _emit(emit, {"type": "position", "row": row, "stage": "fetch", "state": "active"})
            url = cand["url"]
            # Повторный прогон: успешные страницы берутся из кэша, заблокированные/ошибочные
            # грузятся заново — это правило живёт в `fetch_one` (одно на весь проект).
            # use_cache=False (принудительно) — грузим заново ВСЁ, кэш игнорируем.
            try:
                res = await fetcher.fetch_one(url, use_cache=use_cache)
            except Exception as exc:  # noqa: BLE001 — падение одного URL не рушит всю загрузку
                log.warning("Загрузка %s упала: %s", url, exc)
                res = {"url": url, "status": None, "blocked": "error", "error": str(exc),
                       "markdown": "", "structured": {}, "chars": 0, "html": None}
            cand["fetch"] = res
            done += 1
            await _emit(emit, {"type": "fetched", "row": row, "url": url,
                               "status": res["status"], "blocked": res["blocked"], "chars": res["chars"]})
            await _emit(emit, {"type": "progress", "done": done, "total": total})
            return res

    try:
        for it in items_data:
            cands = [c for c in it.get("candidates", []) if not c.get("is_file")][:cap]
            if not cands:
                await _emit(emit, {"type": "position", "row": it["row"], "stage": "fetch", "state": "skip"})
                continue
            await asyncio.gather(*[one_cand(it["row"], c) for c in cands],
                                 return_exceptions=True)
            ok = any(c.get("fetch") and not c["fetch"]["blocked"] for c in cands)
            await _emit(emit, {"type": "position", "row": it["row"], "stage": "fetch",
                               "state": "done" if ok else "error"})
    finally:
        await fetcher.close()
    all_cands = [c for it in items_data for c in it.get("candidates", []) if not c.get("is_file")]
    loaded = sum(1 for c in all_cands if c.get("fetch") and not c["fetch"].get("blocked"))
    blocked = sum(1 for c in all_cands if c.get("fetch") and c["fetch"].get("blocked"))
    attempted = loaded + blocked
    # кандидаты ниже top_n_fetch по рангу вообще не грузились — показываем явно, чтобы
    # loaded+blocked+skipped сходилось с total (иначе «где ещё N?» без объяснения).
    skipped = len(all_cands) - attempted
    log.info("Загрузка завершена: успешно=%d, заблокировано/ошибка=%d, пропущено (ниже top_n_fetch=%d)=%d "
             "из %d найденных", loaded, blocked, cfg.top_n_fetch, skipped, len(all_cands))
    await _emit(emit, {"type": "fetch_done", "loaded": loaded, "blocked": blocked,
                       "attempted": attempted, "skipped": skipped, "top_n_fetch": cfg.top_n_fetch,
                       "total": len(all_cands)})
    return items_data


async def _emit(emit, ev):
    if emit is None:
        return
    r = emit(ev)
    if asyncio.iscoroutine(r):
        await r
