# -*- coding: utf-8 -*-
"""ReAct-агент (M6): модель ведёт поиск цены сама, вызывая скилы реестра.

В отличие от `OrchestratedRun` (детерминированная политика триажа), здесь РЕШЕНИЯ принимает
модель: на каждом шаге выдаёт «Мысль → Действие → Аргументы», система выполняет скил и
возвращает КОМПАКТНОЕ наблюдение. Тяжёлый контент (страницы) не гоняется через модель — он
лежит в кэше fetch, а между шагами передаётся лишь URL (скилы `extract_prices`/`inspect_page`
читают страницу из кэша по URL). Так слабая модель ведёт агентный цикл без переполнения контекста.

Протокол текстовый (а не native function-calling) — работает и на серверном агенте, и на
локальной модели через один и тот же `llm_client.complete`. Итог позиции считает КОД
(adjudicate), не модель (правило: арифметику не отдаём модели). Есть бюджеты и авто-финал —
защита от зацикливания. Реальные вызовы модели инициирует пользователь (правило проекта).
"""
import asyncio
import json

from .orchestrator import Orchestrator  # noqa: F401 — переиспользуется в оркестрированном режиме
from .protocol import parse_step as _parse_step, render_tools, thought as _thought
from ..extract import _ItemView, infer_not_found_reason, verdict_event
from ..extract.adjudicate import adjudicate
from ..models import MatchType, NotFoundReason, PriceCandidate, Verdict
from ..obs.log import get_logger
from ..tools import ToolContext, registry

log = get_logger("agent.react")

# Скилы, доступные ReAct-агенту (курируем: только релевантные поиску цены).
CURATED = ["find_sources", "fetch_page", "inspect_page", "find_product_link",
           "escalate_fetch", "extract_prices"]

_CUR = {"RUB": "₽", "USD": "$", "EUR": "€", "CNY": "¥"}


def _money(v, cur="RUB") -> str:
    sym = _CUR.get((cur or "RUB").upper(), " " + (cur or ""))
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return ("%d%s" if f.is_integer() else "%.2f%s") % (f, sym)


def _render_tools(names: list[str]) -> str:
    """Описание инструментов для промпта. Реестр берём модульный — тесты его подменяют."""
    return render_tools(names, registry)


_SYSTEM = (
    "Ты — агент поиска НАДЁЖНОЙ цены на товар. Действуешь пошагово, вызывая инструменты.\n"
    "На КАЖДОМ шаге выведи РОВНО такой блок (и ничего лишнего):\n"
    "Мысль: <очень кратко, что делаешь и зачем>\n"
    "Действие: <имя инструмента>\n"
    "Аргументы: <ОДНострочный JSON с аргументами>\n\n"
    "Когда данных достаточно для итога — вместо действия выведи:\n"
    "Мысль: <кратко>\n"
    "Итог\n"
    "(итоговую цену и диапазон посчитает система из уже извлечённых цен — тебе считать не нужно).\n\n"
    "Доступные инструменты:\n%s\n\n"
    "Как обычно идёт поиск:\n"
    "  1) find_sources — получить источники (если их ещё нет);\n"
    "  2) fetch_page по 2–4 лучшим источникам (можно указывать index из списка вместо url);\n"
    "  3) если страница — листинг, вызови find_product_link и загрузи карточку; если блок — escalate_fetch;\n"
    "  4) extract_prices по загруженным страницам искомого товара;\n"
    "  5) когда набрал достаточно цен из разных источников — «Итог».\n\n"
    "Правила: не повторяй одно и то же действие с теми же аргументами; не выдумывай URL — бери из "
    "результатов find_sources; если всё заблокировано или цен нет — всё равно заверши «Итог» "
    "(система честно пометит «не найдено» с причиной). Экономь шаги."
)


class _State:
    """Рабочее состояние агента по одному товару (кандидаты, страницы, накопленные цены)."""
    def __init__(self, item: dict) -> None:
        self.item = item
        self.candidates: list[dict] = list(item.get("candidates") or [])
        self.pages: dict[str, dict] = {}        # url -> {blocked, chars, inspect}
        self.prices: list[dict] = []            # накопленные цены (dict PriceCandidate)
        self.used: dict[str, int] = {}          # счётчики вызовов по инструменту
        self.seen_actions: set = set()          # (tool, args_json) — против повторов

    def cand_by_url(self, url: str) -> dict | None:
        for c in self.candidates:
            if c.get("url") == url:
                return c
        return None


async def _emit(emit, ev):
    if emit is None:
        return
    r = emit(ev)
    if asyncio.iscoroutine(r):
        await r


class ReActAgent:
    """Один ReAct-цикл по товару поверх реестра скилов."""

    def __init__(self, ctx: ToolContext, *, cfg=None, tools=None) -> None:
        from ..config import ReActConfig
        self.ctx = ctx
        self.cfg = cfg or ReActConfig()
        self.tools = tools or CURATED

    # ---- бюджеты -------------------------------------------------------------

    def _budget(self, tool: str) -> int:
        return {"find_sources": self.cfg.source_budget, "fetch_page": self.cfg.fetch_budget,
                "extract_prices": self.cfg.extract_budget,
                "escalate_fetch": self.cfg.escalate_budget}.get(tool, 999)

    # ---- подготовка аргументов ----------------------------------------------

    def _prepare(self, st: _State, tool: str, args: dict) -> dict:
        """Дозаполнить аргументы: name/part_number из товара, index→url из кандидатов."""
        a = dict(args or {})
        it = st.item
        if tool in ("find_sources", "find_product_link", "extract_prices"):
            a.setdefault("name", it.get("name") or "")
            if it.get("part_number"):
                a.setdefault("part_number", it["part_number"])
        # index (1-based) → url + атрибуты источника
        if "url" not in a and a.get("index"):
            try:
                c = st.candidates[int(a["index"]) - 1]
                a["url"] = c.get("url")
            except (ValueError, IndexError, TypeError):
                pass
        a.pop("index", None)
        if tool == "extract_prices" and a.get("url"):
            c = st.cand_by_url(a["url"])
            if c:
                a.setdefault("domain", c.get("domain"))
                a.setdefault("tier", c.get("tier"))
                a.setdefault("weight", c.get("weight"))
        return a

    # ---- наблюдения ----------------------------------------------------------

    def _obs_find_sources(self, st: _State, res: dict) -> str:
        cands = res.get("candidates") or []
        st.candidates = cands
        if not cands:
            return "Источники не найдены (пустая выдача)."
        lines = ["Найдено источников: %d" % len(cands)]
        for i, c in enumerate(cands[:8], 1):
            lines.append("  %d) %s [тир %s] %s" % (i, c.get("domain") or "?",
                                                   c.get("tier"), c.get("url")))
        return "\n".join(lines)

    async def _obs_fetch(self, st: _State, url: str, res: dict) -> str:
        from ..extract.inspect import inspect_fetch
        from ..fetch import cache
        insp = inspect_fetch(cache.get(url), url=url)
        st.pages[url] = {"blocked": res.get("blocked"), "chars": res.get("chars"), "inspect": insp}
        c = st.cand_by_url(url)
        if c is not None:
            c["inspect"] = insp
        if res.get("blocked"):
            return "Загрузка %s: ЗАБЛОКИРОВАНО (%s). Можно escalate_fetch." % (url, res["blocked"])
        return "Загружено %s: тип=%s, цена на странице=%s, символов=%s (%s)" % (
            url, insp["kind"], "да" if insp["price_present"] else "нет",
            res.get("chars"), insp["reason"])

    def _obs_inspect(self, st: _State, url: str, insp: dict) -> str:
        st.pages.setdefault(url, {})["inspect"] = insp
        return "Страница %s: тип=%s, цена=%s (%s)" % (
            url, insp.get("kind"), "да" if insp.get("price_present") else "нет", insp.get("reason"))

    def _obs_links(self, res: list) -> str:
        if not res:
            return "Карточек товара на листинге не найдено."
        lines = ["Найдены карточки:"]
        for i, l in enumerate(res[:5], 1):
            lines.append("  %d) %s" % (i, l.get("url")))
        return "\n".join(lines)

    def _obs_escalate(self, st: _State, url: str, res: dict) -> str:
        if res.get("resolved"):
            new = res.get("url")
            if new and new != url and st.cand_by_url(url):
                st.candidates.append({"url": new, "domain": st.cand_by_url(url).get("domain"),
                                      "tier": st.cand_by_url(url).get("tier"),
                                      "weight": st.cand_by_url(url).get("weight")})
            return "Эскалация удалась (%s), доступен url=%s — загрузи/извлекай его." % (res.get("via"), new)
        return "Эскалация не помогла: источник остаётся заблокированным."

    def _obs_extract(self, st: _State, res: list) -> str:
        if not res:
            return "Цен не извлечено (не тот товар, пусто или страница не загружена)."
        for p in res:
            st.prices.append(p)
        parts = []
        for p in res[:8]:
            parts.append("%s %s" % (_money(p["value"], p.get("currency", "RUB")),
                                    p.get("match", "")))
        return "Извлечено цен: %d (%s). Всего накоплено: %d." % (
            len(res), "; ".join(parts), len(st.prices))

    async def _observe(self, st: _State, tool: str, args: dict, result) -> str:
        if tool == "find_sources":
            return self._obs_find_sources(st, result or {})
        if tool == "fetch_page":
            return await self._obs_fetch(st, args.get("url", ""), result or {})
        if tool == "inspect_page":
            return self._obs_inspect(st, args.get("url", ""), result or {})
        if tool == "find_product_link":
            return self._obs_links(result or [])
        if tool == "escalate_fetch":
            return self._obs_escalate(st, args.get("url", ""), result or {})
        if tool == "extract_prices":
            return self._obs_extract(st, result or [])
        return "Готово."

    # ---- финал ---------------------------------------------------------------

    def _finalize(self, st: _State) -> Verdict:
        prices = [PriceCandidate(**p) for p in st.prices]
        item = _ItemView(st.item)
        v = adjudicate(item, prices)
        st.item["verdict"] = v.model_dump()
        if v.primary is None:
            # причина из того, что видели (заблокировано/нет офферов/не искали)
            cands = [{"url": u, "is_file": False, "fetch": {"blocked": pg.get("blocked")},
                      "inspect": pg.get("inspect")} for u, pg in st.pages.items()]
            if not cands and not st.candidates:
                reason = NotFoundReason.NOT_SEARCHED
            else:
                reason = infer_not_found_reason(cands or st.candidates) or NotFoundReason.NO_OFFERS
            st.item["not_found_reason"] = reason.value
        else:
            st.item["not_found_reason"] = None
        return v

    # ---- цикл ----------------------------------------------------------------

    async def run_item(self, item: dict) -> dict:
        st = _State(item)
        row = item.get("row")
        emit = self.ctx.emit
        name = item.get("name") or ""
        await _emit(emit, {"type": "agent_start", "row": row, "name": name})

        messages = [
            {"role": "system", "content": _SYSTEM % _render_tools(self.tools)},
            {"role": "user", "content": (
                "Найди надёжную цену на товар.\n=== ТОВАР ===\nНаименование: %s%s\n\n"
                "Начинай. Один шаг за раз." % (
                    name, ("\nПарт-номер: %s" % item["part_number"]) if item.get("part_number") else ""))},
        ]

        parse_fails = 0
        for step in range(1, self.cfg.max_steps + 1):
            def _chunk(delta, _row=row):
                if emit is not None:
                    emit({"type": "model_stream", "row": _row, "domain": "react", "delta": delta})
            try:
                res = await self.ctx.llm_client.complete(messages, model=self.ctx.model, on_chunk=_chunk)
            except Exception as exc:  # noqa: BLE001 — транспорт капризен; завершаем честно
                log.warning("ReAct r%s: сбой модели на шаге %d: %s — финал", row, step, exc)
                break
            content = (res.get("content") or "").strip()
            if res.get("usage"):
                await _emit(emit, {"type": "usage_delta", "usage": res["usage"], "row": row})
            messages.append({"role": "assistant", "content": content})

            parsed = _parse_step(content)
            if parsed.get("finish"):
                await _emit(emit, {"type": "agent_step", "row": row, "step": step, "action": "Итог"})
                break
            if parsed.get("error"):
                parse_fails += 1
                if parse_fails > 2:
                    log.info("ReAct r%s: 3 нераспознанных шага подряд — финал", row)
                    break
                messages.append({"role": "user", "content":
                                 "Не понял формат. Ответь строго: 'Действие:' + 'Аргументы:' (JSON), "
                                 "либо 'Итог'."})
                continue
            parse_fails = 0

            tool = parsed["action"]
            if tool not in self.tools:
                messages.append({"role": "user", "content":
                                 "Неизвестный инструмент %r. Доступны: %s" % (tool, ", ".join(self.tools))})
                continue

            args = self._prepare(st, tool, parsed["args"])
            key = tool + ":" + json.dumps(args, ensure_ascii=False, sort_keys=True)
            if key in st.seen_actions:
                messages.append({"role": "user", "content":
                                 "Это действие с теми же аргументами уже выполнялось — не повторяй, "
                                 "сделай следующий шаг или заверши «Итог»."})
                continue
            if st.used.get(tool, 0) >= self._budget(tool):
                messages.append({"role": "user", "content":
                                 "Исчерпан бюджет вызовов %s. Используй уже полученное или заверши «Итог»." % tool})
                continue

            st.seen_actions.add(key)
            st.used[tool] = st.used.get(tool, 0) + 1
            await _emit(emit, {"type": "agent_step", "row": row, "step": step,
                               "thought": _thought(content), "action": tool, "args": args})
            try:
                result = await registry.get(tool).run(self.ctx, **args)
            except Exception as exc:  # noqa: BLE001 — падение скила не роняет цикл
                log.warning("ReAct r%s: скил %s упал: %s", row, tool, exc)
                messages.append({"role": "user", "content": "Наблюдение: инструмент %s дал ошибку: %s" % (tool, exc)})
                continue

            obs = await self._observe(st, tool, args, result)
            obs = obs[: self.cfg.obs_max_chars]
            await _emit(emit, {"type": "agent_observation", "row": row, "step": step,
                               "action": tool, "observation": obs})
            messages.append({"role": "user", "content": "Наблюдение: " + obs})

        v = self._finalize(st)
        await _emit(emit, {"type": "agent_final", "row": row, "steps": step, "prices": len(st.prices)})
        await _emit(emit, verdict_event(row, v, st.item.get("not_found_reason")))
        log.info("ReAct r%s завершён: шагов=%d, цен=%d, итог=%s", row, step, len(st.prices),
                 v.primary.value if v.primary else "не найдено")
        return item


async def run_react(items: list[dict], *, settings, fetcher, llm_client, model=None, emit=None,
                    cfg=None, persist: bool = True) -> list[dict]:
    """Прогнать список товаров ReAct-агентом (модель ведёт шаги). Возвращает items со сводами.

    Товары идут с ограниченной конкурентностью (модель серийна → обычно 1). В конце — история (M5).
    """
    cfg = cfg or settings.react
    ctx = ToolContext(settings=settings, llm_client=llm_client, emit=emit, fetcher=fetcher, model=model)
    sem = asyncio.Semaphore(max(1, int(cfg.concurrency)))
    log.info("ReAct-прогон: товаров=%d, max_steps=%d, конкурентность=%d",
             len(items), cfg.max_steps, cfg.concurrency)

    async def one(it):
        async with sem:
            agent = ReActAgent(ctx, cfg=cfg)
            await agent.run_item(it)

    await asyncio.gather(*[one(it) for it in items])
    if persist and getattr(settings, "storage", None) and settings.storage.enabled:
        from ..storage import persist_run_async
        res = await persist_run_async(items, source="react", cfg=settings.storage)
        if res.get("run_id"):
            await _emit(emit, {"type": "history_saved", **res})
    log.info("ReAct-прогон завершён")
    return items
