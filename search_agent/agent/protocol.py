# -*- coding: utf-8 -*-
"""Текстовый протокол агентного шага: «Мысль → Действие → Аргументы» либо «Итог».

Почему текстом, а не native function-calling: один и тот же формат работает и на прямом HTTP
API, и на резервном SSH-воркере (он tool-calling не умеет), и на локальной модели — через
единственный метод `llm_client.complete`. Ничего не меняем в транспорте ради агента.

Модуль общий для ВСЕХ агентов проекта (ReAct по товару — `agent/loop.py`; чат-исследователь —
`research/agent.py`). Держим разбор в одном месте: два независимых парсера одного формата
неизбежно разъезжаются, и «модель вдруг перестала слушаться» ловится в двух местах вместо одного.
"""
import re

from ..tools import registry as _default_registry

_ACTION_RE = re.compile(r"(?:Действие|Action)\s*:\s*([a-zA-Z_]+)")
_ARGS_RE = re.compile(r"(?:Аргументы|Arguments|Args)\s*:\s*(.+)", re.S)
_FINISH_RE = re.compile(r"(?:^|\n)\s*(?:Итог|Готово|Final|Answer)\b", re.I)
_THOUGHT_RE = re.compile(r"(?:Мысль|Thought)\s*:\s*(.+)")


def render_tools(names: list[str], reg=None) -> str:
    """Описание доступных инструментов для системного промпта (из схем реестра).

    Берём описания из `registry.schemas()`, а не пишем их в промпте руками: добавленный скилл
    сразу виден агенту, и описание живёт рядом с кодом скилла. `reg` — реестр вызывающего
    (агент может работать на подменённом реестре; тесты этим пользуются).
    """
    lines = []
    have = {t["name"]: t for t in (reg or _default_registry).schemas()}
    for n in names:
        t = have.get(n)
        if not t:
            continue
        props = (t.get("parameters") or {}).get("properties", {})
        req = set((t.get("parameters") or {}).get("required", []))
        args = ", ".join(("%s*" % k if k in req else k) for k in props)
        lines.append("- %s(%s) — %s" % (n, args, t["description"]))
    return "\n".join(lines)


def parse_step(text: str) -> dict:
    """Разобрать шаг модели: {'finish':True} | {'action','args'} | {'error':...}.

    Действие ищем ПЕРВЫМ: модель часто пишет «Итог» в рассуждении, а потом всё равно вызывает
    инструмент — приоритет действия не даёт оборвать цикл раньше времени.
    """
    t = text or ""
    m_act = _ACTION_RE.search(t)
    if m_act:
        tool = m_act.group(1).strip()
        args = {}
        m_args = _ARGS_RE.search(t)
        if m_args:
            from ..llm.json_utils import extract_json
            parsed = extract_json(m_args.group(1))
            if isinstance(parsed, dict):
                args = parsed
        return {"action": tool, "args": args}
    if _FINISH_RE.search(t):
        return {"finish": True}
    return {"error": "не распознан формат шага"}


def thought(content: str) -> str:
    """Короткая «мысль» шага для UI (первая строка, обрезанная)."""
    m = _THOUGHT_RE.search(content or "")
    return (m.group(1).splitlines()[0][:200]) if m else ""
