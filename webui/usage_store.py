# -*- coding: utf-8 -*-
"""
Персистентный счётчик токенов за весь период разработки.

Хранит накопительные значения в JSON-файле рядом с backend (token_usage.json):
prompt/completion/total токены, число запросов и дату начала учёта. Источник
правды — этот файл (не браузер), поэтому счётчик переживает перезагрузку страницы
и перезапуск сервера. Запись атомарная (во временный файл + os.replace).

Функции синхронные (файл крошечный); сериализацию одновременных записей
обеспечивает вызывающая сторона (server.py держит asyncio.Lock).
"""
import os
import json
import datetime

STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token_usage.json")

_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "requests")


def _today():
    return datetime.date.today().isoformat()


def _blank():
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "requests": 0, "cost_rub": 0.0, "model": "", "since": _today()}


def load():
    """Прочитать накопительный счётчик; при отсутствии/повреждении — нули."""
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return _blank()
    # Нормализуем: недостающие поля добиваем дефолтами.
    result = _blank()
    for k in _KEYS:
        try:
            result[k] = int(data.get(k, 0))
        except (TypeError, ValueError):
            result[k] = 0
    try:
        result["cost_rub"] = float(data.get("cost_rub", 0.0))
    except (TypeError, ValueError):
        result["cost_rub"] = 0.0
    result["model"] = data.get("model") or ""
    result["since"] = data.get("since") or _today()
    return result


def _write(data):
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE_PATH)


def add(prompt=0, completion=0, total=0, cost=0.0, model=None):
    """Прибавить расход одного запроса и вернуть новые накопительные тоталы.

    cost — стоимость запроса в рублях (по ценам выбранной модели); model — её id (для лога,
    т.к. модели/цены меняются от сессии к сессии — правило «логировать всё»)."""
    data = load()
    # Если сервер не прислал total — считаем как prompt+completion.
    total = total or (int(prompt or 0) + int(completion or 0))
    data["prompt_tokens"] += int(prompt or 0)
    data["completion_tokens"] += int(completion or 0)
    data["total_tokens"] += int(total or 0)
    data["requests"] += 1
    data["cost_rub"] = round(float(data.get("cost_rub", 0.0)) + float(cost or 0.0), 6)
    if model:
        data["model"] = model
    _write(data)
    return data


def reset():
    """Обнулить счётчики и начать новый период (since = сегодня)."""
    data = _blank()
    _write(data)
    return data
