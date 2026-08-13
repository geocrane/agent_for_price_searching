# -*- coding: utf-8 -*-
"""Чат-исследователь: многоходовой агентный цикл поверх реестра скилов.

Отличия от ReAct-агента по товару (`agent/loop.py`), из-за которых это отдельный цикл:
  * ДИАЛОГ, а не одиночный прогон: состояние живёт между сообщениями, агент может переспросить
    (`ask_user`) и продолжить с того же места;
  * итог пишет МОДЕЛЬ (связный ответ пользователю), а не `adjudicate` — здесь нет одной
    правильной цифры, есть разбор. Поэтому вместо арифметической проверки итога работает
    проверка ССЫЛОК: каждое утверждение должно опираться на реально прочитанный источник;
  * набор скилов шире: помимо товарной ветки есть общий поиск, чтение страниц под вопрос и
    аттестация источников данных.

Общее с ReAct сохранено намеренно: текстовый протокол (`agent/protocol.py`), компактные
наблюдения вместо полного контента, бюджеты, подавление повторов и авто-финал.
"""
import asyncio
import json
import time

from .context import NullContext
from .probe import render_catalog
from .prompts import CONTEXT_BLOCK, SYSTEM, fix_citations_message
from .sources import validate_citations
from .store import (KIND_ANSWER, KIND_OBSERVATION, KIND_QUESTION, KIND_STEP, KIND_TEXT, ChatState)
from ..agent.protocol import parse_step, render_tools, thought as _thought
from ..obs.log import get_logger
from ..tools import ToolContext, registry

log = get_logger("research.agent")

# Скилы чата: товарная ветка (цена), обзорная (сравнения) и разведывательная (источники данных).
CURATED = ["web_search", "read_page", "probe_source", "find_sources", "extract_prices",
           "inspect_page", "find_product_link", "escalate_fetch", "ask_user"]

# Скилы, чей результат добавляет источники в реестр цитат.
_SOURCE_TOOLS = {"web_search", "read_page", "probe_source", "find_sources", "extract_prices"}


class ResearchAgent:
    """Один диалог. `turn()` обрабатывает одно сообщение пользователя."""

    def __init__(self, ctx: ToolContext, *, cfg=None, chat: ChatState | None = None,
                 context_provider=None, tools=None, executor=None, reg=None,
                 on_message=None) -> None:
        from ..config import ResearchConfig
        self.ctx = ctx
        self.cfg = cfg or ResearchConfig()
        self.chat = chat or ChatState("mem")
        self.context = context_provider or NullContext()
        self.registry = reg or registry
        self.tools = tools or (CURATED + list(self.context.extra_tools() or []))
        self.executor = executor
        self.on_message = on_message          # колбэк персиста: сообщение → хранилище
        self._stop = False

    def stop(self) -> None:
        """Мягкая остановка текущего хода (кнопка «Стоп» в интерфейсе)."""
        self._stop = True

    # ---- бюджеты -----------------------------------------------------------

    def _budget(self, tool: str) -> int:
        c = self.cfg
        return {"web_search": c.search_budget, "find_sources": c.search_budget,
                "read_page": c.read_budget, "fetch_page": c.read_budget,
                "probe_source": c.probe_budget, "extract_prices": c.extract_budget,
                "escalate_fetch": c.escalate_budget}.get(tool, 999)

    # ---- наблюдения --------------------------------------------------------

    def _remember(self, url: str, title: str = "") -> str:
        """Зарегистрировать источник и вернуть метку `[Sn]` (или пусто при переполнении)."""
        if not url:
            return ""
        if len(self.chat.registry) >= self.cfg.max_sources and not self.chat.registry.label(url):
            return ""
        sid = self.chat.registry.add(url, title=title)
        if sid:
            self._emit({"type": "chat_source", **(self.chat.registry.get(sid).to_dict())})
        return "[%s]" % sid if sid else ""

    def _obs_search(self, res: dict) -> str:
        items = (res or {}).get("results") or []
        if not items:
            return "Ничего не найдено по этому запросу. Попробуй другую формулировку."
        lines = ["Найдено: %d" % len(items)]
        for r in items[:10]:
            mark = self._remember(r.get("url"), r.get("title") or "")
            lines.append("  %s %s — %s\n     %s" % (
                mark, r.get("domain") or "?", (r.get("title") or "")[:120],
                (r.get("snippet") or "")[:160]))
        lines.append("Ссылки пока не открыты — чтобы утверждать что-либо, прочитай страницу "
                     "(read_page) или проверь источник (probe_source).")
        return "\n".join(lines)

    def _obs_read(self, res: dict) -> str:
        res = res or {}
        mark = self._remember(res.get("url"), res.get("title") or "")
        if res.get("blocked"):
            return ("Страница %s не открылась: %s. Можно попробовать escalate_fetch или другой "
                    "источник." % (res.get("url"), res["blocked"]))
        if not (res.get("excerpt") or "").strip():
            return "Страница %s открылась (%s симв.), но по вопросу на ней ничего не нашлось." % (
                res.get("url"), res.get("chars"))
        return "Прочитано %s %s (тип %s):\n%s" % (
            mark, res.get("url"), res.get("kind") or "?", res["excerpt"])

    def _obs_probe(self, res: dict) -> str:
        res = res or {}
        mark = self._remember(res.get("url"), res.get("title") or "")
        d = dict(res)
        d["sid"] = (mark or "").strip("[]")
        self.chat.add_dossier(d)
        lines = ["Проверен источник %s %s" % (mark, d.get("domain") or d.get("url"))]
        from .probe import SourceDossier
        lines.append(SourceDossier(**{k: v for k, v in d.items()
                                      if k in SourceDossier.__dataclass_fields__}).summary())
        if d.get("how_to"):
            lines.append("Как добраться: %s" % d["how_to"])
        if d.get("evidence"):
            lines.append("Подтверждено цитатами: " + " | ".join(x[:120] for x in d["evidence"][:3]))
        else:
            lines.append("ВНИМАНИЕ: подтверждающих цитат нет — утверждать, что данные там есть, "
                         "нельзя.")
        return "\n".join(lines)

    def _obs_prices(self, res) -> str:
        items = res or []
        if not items:
            return "Цен на этой странице не извлечено."
        lines = ["Извлечено цен: %d" % len(items)]
        for p in items[:8]:
            mark = self._remember(p.get("url"), p.get("found") or "")
            lines.append("  %s %s %s — %s (%s)" % (
                mark, p.get("value"), p.get("currency") or "RUB",
                (p.get("found") or "")[:80], p.get("match") or ""))
        return "\n".join(lines)

    def _observe(self, tool: str, args: dict, result) -> str:
        if tool == "web_search":
            return self._obs_search(result)
        if tool == "read_page":
            return self._obs_read(result)
        if tool == "probe_source":
            return self._obs_probe(result)
        if tool == "find_sources":
            return self._obs_search({"results": (result or {}).get("candidates") or []})
        if tool == "extract_prices":
            return self._obs_prices(result)
        if tool == "inspect_page":
            r = result or {}
            return "Страница %s: тип=%s, цена на странице=%s (%s)" % (
                args.get("url"), r.get("kind"), "да" if r.get("price_present") else "нет",
                r.get("reason"))
        if tool == "find_product_link":
            links = result or []
            if not links:
                return "Карточек товара на листинге не найдено."
            return "Найдены карточки:\n" + "\n".join("  %s" % l.get("url") for l in links[:5])
        if tool == "escalate_fetch":
            r = result or {}
            return ("Эскалация удалась (%s), доступен url=%s." % (r.get("via"), r.get("url"))
                    if r.get("resolved") else "Эскалация не помогла: источник остаётся закрытым.")
        return "Готово."

    # ---- цикл --------------------------------------------------------------

    async def turn(self, user_text: str) -> dict:
        """Обработать одно сообщение пользователя. Возвращает результат хода."""
        self._stop = False
        started = time.time()
        self.chat.pending_question = ""
        self._record(self.chat.add("user", user_text, kind=KIND_TEXT))

        used: dict[str, int] = {}
        seen: set = set()
        steps, answer, question, options = 0, "", "", []
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        parse_fails = 0

        for step in range(1, self.cfg.max_steps + 1):
            if self._stop:
                log.info("Чат: ход остановлен пользователем на шаге %d", step)
                break
            steps = step
            messages = self._build_messages()
            try:
                # Стрим дельт — «живое мышление» в консоли «Ход» (правило: видно всё, что идёт).
                res = await self._complete(messages, on_chunk=self._on_delta)
            except Exception as exc:  # noqa: BLE001 — транспорт капризен; отвечаем честно
                log.warning("Чат: сбой модели на шаге %d: %s", step, exc)
                answer = "Не удалось получить ответ модели: %s" % exc
                break
            content = ((res or {}).get("content") or "").strip()
            _add_usage(usage_total, (res or {}).get("usage"))
            if (res or {}).get("usage"):
                self._emit({"type": "usage_delta", "usage": res["usage"]})

            parsed = parse_step(content)

            if parsed.get("finish"):
                answer = _strip_finish(content)
                break
            if parsed.get("error"):
                parse_fails += 1
                if parse_fails > 2:
                    log.info("Чат: 3 нераспознанных шага подряд — принимаю ответ как есть")
                    answer = content
                    break
                self._record(self.chat.add(
                    "user", "Не понял формат. Ответь строго «Действие:» + «Аргументы:» (JSON), "
                            "либо «Итог» и следом ответ.", kind=KIND_OBSERVATION))
                continue
            parse_fails = 0

            tool, args = parsed["action"], parsed.get("args") or {}
            self._record(self.chat.add("assistant", content, kind=KIND_STEP))

            if tool == "ask_user":                       # пауза: ждём ответа пользователя
                question = str(args.get("question") or "").strip()
                options = [str(o) for o in (args.get("options") or [])][:5]
                if question:
                    break
                self._record(self.chat.add("user", "Пустой вопрос — задай конкретный или "
                                                    "продолжай сам.", kind=KIND_OBSERVATION))
                continue

            if tool not in self.tools:
                self._record(self.chat.add(
                    "user", "Неизвестный инструмент %r. Доступны: %s" % (tool, ", ".join(self.tools)),
                    kind=KIND_OBSERVATION))
                continue

            key = tool + ":" + json.dumps(args, ensure_ascii=False, sort_keys=True)
            if key in seen:
                self._record(self.chat.add(
                    "user", "Это действие с теми же аргументами уже выполнялось — не повторяй, "
                            "сделай следующий шаг или заверши «Итог».", kind=KIND_OBSERVATION))
                continue
            if used.get(tool, 0) >= self._budget(tool):
                self._record(self.chat.add(
                    "user", "Исчерпан бюджет вызовов %s. Используй уже собранное или заверши "
                            "«Итог»." % tool, kind=KIND_OBSERVATION))
                continue
            seen.add(key)
            used[tool] = used.get(tool, 0) + 1

            self._emit({"type": "chat_step", "step": step, "tool": tool, "args": args,
                        "thought": _thought(content)})
            try:
                result = await self.registry.get(tool).run(self.ctx, **args)
            except Exception as exc:  # noqa: BLE001 — падение скила не роняет диалог
                log.warning("Чат: скил %s упал: %s", tool, exc)
                self._record(self.chat.add("user", "Наблюдение: инструмент %s дал ошибку: %s"
                                           % (tool, exc), kind=KIND_OBSERVATION))
                continue

            obs = self._observe(tool, args, result)[: self.cfg.obs_max_chars]
            self._emit({"type": "chat_observation", "step": step, "tool": tool, "text": obs})
            self._record(self.chat.add("user", "Наблюдение: " + obs, kind=KIND_OBSERVATION))

        if question:
            return self._finish_question(question, options, steps, usage_total, started)
        if not answer:
            answer = ("Не успел собрать доказательства за отведённые шаги. "
                      "Что удалось найти — в шагах выше; уточните вопрос, и я продолжу.")
            log.info("Чат: ход завершён без итога (шагов %d)", steps)

        answer, citations = await self._ensure_citations(answer, usage_total)
        answer = self._decorate(answer)
        self._record(self.chat.add("assistant", answer, kind=KIND_ANSWER,
                                   meta={"citations": citations}))
        self._emit({"type": "chat_answer", "text": answer, "citations": citations})
        log.info("Чат: ход завершён за %.1fс, шагов=%d, источников=%d, ссылок в ответе=%d",
                 time.time() - started, steps, len(self.chat.registry), len(citations.get("used") or []))
        return {"answer": answer, "question": "", "options": [], "paused": False,
                "steps": steps, "citations": citations, "usage": usage_total,
                "sources": [s.to_dict() for s in self.chat.registry.known()],
                "dossiers": list(self.chat.dossiers)}

    # ---- вспомогательное ---------------------------------------------------

    def _finish_question(self, question, options, steps, usage, started) -> dict:
        self.chat.pending_question = question
        self._record(self.chat.add("assistant", question, kind=KIND_QUESTION,
                                   meta={"options": options}))
        self._emit({"type": "chat_question", "text": question, "options": options})
        log.info("Чат: уточняющий вопрос на шаге %d («%s»)", steps, question[:80])
        return {"answer": "", "question": question, "options": options, "paused": True,
                "steps": steps, "citations": {"used": [], "unknown": [], "uncited": [], "ok": True},
                "usage": usage, "sources": [s.to_dict() for s in self.chat.registry.known()],
                "dossiers": list(self.chat.dossiers)}

    def _build_messages(self) -> list[dict]:
        system = SYSTEM % render_tools(self.tools, self.registry)
        ctx_note = (self.context.summary() or "").strip()
        if ctx_note:
            system += "\n\n" + CONTEXT_BLOCK % ctx_note
        return [{"role": "system", "content": system}] + \
               self.chat.to_llm_messages(keep_steps=self.cfg.keep_steps)

    async def _complete(self, messages: list[dict], on_chunk=None) -> dict:
        async def _call():
            return await self.ctx.llm_client.complete(messages, model=self.ctx.model,
                                                      on_chunk=on_chunk)
        if self.executor is not None:
            return await self.executor.run(_call)
        return await _call()

    async def _ensure_citations(self, answer: str, usage: dict) -> tuple[str, dict]:
        """Проверить ссылки и дать модели ОДИН шанс исправиться.

        Одну итерацию, а не цикл: если модель не смогла подтвердить утверждение со второй
        попытки, значит подтверждать нечем — честнее показать пользователю пометку, чем
        крутить модель до тех пор, пока она не сочинит ссылку.
        """
        citations = validate_citations(answer, self.chat.registry)
        if citations["ok"] or not self.cfg.fix_citations:
            return answer, citations
        known = [s.sid for s in self.chat.registry.known()]
        self._record(self.chat.add("user", fix_citations_message(citations, known),
                                   kind=KIND_OBSERVATION))
        try:
            res = await self._complete(self._build_messages())
        except Exception as exc:  # noqa: BLE001
            log.warning("Чат: коррекция ссылок не удалась: %s", exc)
            return self._flag(answer, citations), citations
        _add_usage(usage, (res or {}).get("usage"))
        fixed = _strip_finish(((res or {}).get("content") or "").strip())
        if not fixed:
            return self._flag(answer, citations), citations
        again = validate_citations(fixed, self.chat.registry)
        if again["ok"]:
            log.info("Чат: ссылки исправлены со второй попытки")
            return fixed, again
        return self._flag(fixed, again), again

    def _flag(self, answer: str, citations: dict) -> str:
        """Честная пометка о недоказанных утверждениях (правило: о деградации не молчим)."""
        n = len(citations.get("uncited") or [])
        bad = len(citations.get("unknown") or [])
        parts = []
        if n:
            parts.append("%d утверждени(й) без ссылки на источник" % n)
        if bad:
            parts.append("%d ссылк(и) на несуществующий источник" % bad)
        if not parts:
            return answer
        log.warning("Чат: ответ отдан с непроверенными утверждениями: %s", "; ".join(parts))
        return answer + "\n\n> ⚠ В ответе осталось %s — проверьте эти места отдельно." % \
            " и ".join(parts)

    def _decorate(self, answer: str) -> str:
        """Дописать каталог источников и список ссылок. Пишет КОД — значит, они верны."""
        parts = [answer]
        fresh = [d for d in self.chat.dossiers if d.get("verdict")]
        if fresh and "Каталог источников" not in answer:
            parts.append(render_catalog(fresh))
        used = validate_citations(answer, self.chat.registry)["used"]
        block = self.chat.registry.render(only=used or None)
        if block:
            parts.append(block)
        return "\n\n".join(p for p in parts if p)

    def _record(self, msg: dict) -> None:
        """Сообщение уже в состоянии — отдать его наружу для сохранения (если подписаны)."""
        if self.on_message is None:
            return
        try:
            self.on_message(msg)
        except Exception as exc:  # noqa: BLE001 — персист не роняет диалог
            log.warning("Сообщение не сохранено: %s", exc)

    def _on_delta(self, delta: str) -> None:
        self._emit({"type": "model_stream", "domain": "chat", "delta": delta})

    def _emit(self, ev: dict) -> None:
        emit = self.ctx.emit if self.ctx else None
        if emit is None:
            return
        try:
            res = emit(ev)
            if asyncio.iscoroutine(res):
                asyncio.create_task(res)
        except Exception as exc:  # noqa: BLE001 — UI не должен ронять диалог
            log.debug("Событие чата не отправлено: %s", exc)


def _strip_finish(content: str) -> str:
    """Убрать служебные строки шага, оставив ответ пользователю."""
    lines = (content or "").splitlines()
    out, started = [], False
    for ln in lines:
        s = ln.strip()
        if not started:
            if s.lower().startswith(("мысль:", "thought:")):
                continue
            if s.lower() in ("итог", "итог:", "готово", "final", "answer"):
                started = True
                continue
            if s.lower().startswith(("итог:", "final:", "answer:")):
                started = True
                rest = s.split(":", 1)[1].strip()
                if rest:
                    out.append(rest)
                continue
        out.append(ln)
    return "\n".join(out).strip()


def _add_usage(total: dict, usage) -> None:
    if not usage:
        return
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        try:
            total[key] = int(total.get(key) or 0) + int(usage.get(key) or 0)
        except (TypeError, ValueError, AttributeError):
            continue
