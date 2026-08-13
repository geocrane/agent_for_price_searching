# -*- coding: utf-8 -*-
"""Адаптивный разбор JSON из ответа модели.

Модель может обернуть JSON в ```-заборы, добавить пояснения до/после или слегка
намусорить. Здесь — устойчивое извлечение объекта/массива без внешних зависимостей.
"""
import json
import re

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def extract_json(text: str):
    """Вернуть первый разумный JSON-объект/массив из текста или None."""
    if not text:
        return None

    # 1) Прямой разбор.
    candidates = [text.strip()]

    # 2) Содержимое ```json ... ``` заборов.
    candidates += [m.group(1).strip() for m in _FENCE.finditer(text)]

    # 3) Срез от первой { или [ до парной закрывающей скобки.
    sliced = _slice_braces(text)
    if sliced:
        candidates.append(sliced)

    for cand in candidates:
        obj = _try_load(cand)
        if obj is not None:
            return obj
    return None


def _try_load(s: str):
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        return _try_load_repaired(s)


def _try_load_repaired(s: str):
    """Мелкий ремонт: убрать хвостовые запятые перед } или ]."""
    repaired = re.sub(r",\s*([}\]])", r"\1", s)
    if repaired != s:
        try:
            return json.loads(repaired)
        except Exception:  # noqa: BLE001
            return None
    return None


def _slice_braces(text: str):
    """Найти сбалансированный фрагмент от первой { или [ до её пары."""
    start = None
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            open_ch, close_ch = ch, ("}" if ch == "{" else "]")
            break
    if start is None:
        return None
    depth = 0
    in_str = False
    esc = False
    for j in range(start, len(text)):
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
    return None
