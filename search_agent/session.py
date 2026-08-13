# -*- coding: utf-8 -*-
"""Состояние рабочей сессии агента поиска цен: статус по каждому сайту + кэш между запусками.

Единый источник правды о ходе поиска — список товаров (dict), которые мутирует движок
(`agent/run.py`). У каждого кандидата (сайта) — явный `status` и производные поля результата,
по которым: (1) UI рисует таблицу выдачи, (2) движок РЕЗЮМИРУЕТ прерванный прогон (пропускает
завершённые сайты/товары, догружает незакрытые), (3) сессия сохраняется между запусками.

Тяжёлый контент страниц (markdown/html) здесь НЕ храним — он лежит в дисковом кэше
`runs/fetch_cache` по URL (см. `fetch/cache.py`); в сессии — только компактные статусы и цены.
"""
import json
import os
import time

# Статусы кандидата (сайта). Терминальные — по ним резюме не повторяет работу.
STATUS_DISCOVERED = "discovered"   # источник найден в выдаче, ещё не грузился
STATUS_FETCHING = "fetching"       # идёт загрузка страницы
STATUS_FETCHED = "fetched"         # страница загружена, но цена ещё не извлечена
STATUS_EXTRACTING = "extracting"   # модель извлекает цену
STATUS_DONE = "done"               # проанализирован, цена найдена
STATUS_ON_REQUEST = "on_request"   # товар найден, но цена только по запросу («уточняйте», КП)
STATUS_EMPTY = "empty"             # проанализирован, цены нет
STATUS_BLOCKED = "blocked"         # антибот/капча/пусто — страница не открылась
STATUS_ERROR = "error"             # ошибка загрузки/извлечения
STATUS_SKIPPED = "skipped"         # не смотрели: цена по позиции уже подтверждена (профиль)

TERMINAL = frozenset({STATUS_DONE, STATUS_ON_REQUEST, STATUS_EMPTY, STATUS_BLOCKED, STATUS_ERROR,
                      STATUS_SKIPPED})

# Статус человеческим словом. Ровно те же формулировки, что в таблице выдачи интерфейса
# (SITE_STATUS_LABEL, webui/static/app.js): отчёт и экран обязаны называть одно и то же одинаково,
# иначе пользователь сверяет файл с экраном и видит разные слова про одно состояние.
STATUS_LABELS = {
    STATUS_DISCOVERED: "ожидает",
    STATUS_FETCHING: "загрузка",
    STATUS_FETCHED: "загружено",
    STATUS_EXTRACTING: "извлечение",
    STATUS_DONE: "цена найдена",
    STATUS_ON_REQUEST: "цена по запросу",
    STATUS_EMPTY: "цена не найдена",
    STATUS_BLOCKED: "блокировка",
    STATUS_ERROR: "ошибка",
    STATUS_SKIPPED: "не понадобился",
}


def status_label(status: str | None) -> str:
    """Человеческое название статуса сайта (неизвестный статус показываем как есть)."""
    return STATUS_LABELS.get(status or "", status or "")

_SESSION_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                             "runs", "session", "current.json")

# Тяжёлые ключи fetch-результата, которые НЕ сохраняем в сессию (они в runs/fetch_cache).
_HEAVY_FETCH_KEYS = ("markdown", "html", "structured")


def cand_status(cand: dict) -> str:
    """Вывести статус кандидата из его полей (обратная совместимость + резюме).

    Если движок уже проставил явный `status` — доверяем ему; иначе выводим из
    fetch/prices, чтобы корректно резюмировать сессии, сохранённые прежними версиями.
    """
    st = cand.get("status")
    if st:
        return st
    if "prices" in cand:                       # извлечение уже прошло
        if cand.get("prices"):
            return STATUS_DONE
        return STATUS_ON_REQUEST if cand.get("on_request") else STATUS_EMPTY
    fetch = cand.get("fetch")
    if fetch:
        if fetch.get("blocked"):
            return STATUS_BLOCKED
        if fetch.get("error"):
            return STATUS_ERROR
        return STATUS_FETCHED
    return STATUS_DISCOVERED


def is_terminal(cand: dict) -> bool:
    """True, если по кандидату работать больше не нужно (проанализирован или заблокирован)."""
    return cand_status(cand) in TERMINAL


def visible_note(cand: dict) -> str | None:
    """Строка под статусом сайта в UI: для завершённого — ИТОГ, для работающего — что идёт сейчас.

    Прогресс и результат хранятся в РАЗНЫХ полях (`progress_note` / `result_note`). Раньше поле было
    одно, прогресс его затирал («загрузка страницы» → «извлечение цены»), и пользователь на любом
    отказе видел «извлечение цены» вместо причины — правило «логировать всё и видимо» нарушалось.
    `note` читается только как запас для сессий, сохранённых прежними версиями.
    """
    if cand_status(cand) in TERMINAL:
        return cand.get("result_note") or cand.get("note")
    return cand.get("progress_note") or cand.get("note")


def item_done(item: dict) -> bool:
    """True, если по товару есть свод и все его не-файловые кандидаты в терминале."""
    if not item.get("verdict"):
        return False
    cands = [c for c in item.get("candidates", []) or [] if not c.get("is_file")]
    return bool(cands) and all(is_terminal(c) for c in cands)


def best_price(prices: list[dict]) -> dict | None:
    """Представительная цена сайта: приоритет точному совпадению, затем минимальная."""
    if not prices:
        return None
    def key(p):
        m = p.get("match")
        m = getattr(m, "value", m)
        exact = 0 if m == "точное" else 1
        try:
            val = float(p.get("value"))
        except (TypeError, ValueError):
            val = float("inf")
        return (exact, val)
    return sorted(prices, key=key)[0]


def summarize_cand(cand: dict) -> dict:
    """Компактная сводка кандидата для UI-таблицы/сессии из его текущих полей.

    Функция ИДЕМПОТЕНТНА: её можно звать и на живом кандидате прогона, и на уже сведённом
    (восстановленная сессия). Это не теоретическая аккуратность — сведённый кандидат приходит
    сюда каждый раз, когда прогон продолжает восстановленную сессию и она пересохраняется.
    Поэтому `on_request` принимается в ОБЕИХ формах: словарь-предложение у живого кандидата и
    просто флаг у сведённого.
    """
    fetch = cand.get("fetch") or {}
    bp = best_price(cand.get("prices") or [])
    raw_onreq = cand.get("on_request")            # товар найден, цена только по запросу
    onreq = raw_onreq if isinstance(raw_onreq, dict) else {}
    match = bp.get("match") if bp else None
    match = getattr(match, "value", match)
    price = None
    if bp is not None and bp.get("value") is not None:
        try:
            price = int(round(float(bp["value"])))
        except (TypeError, ValueError):
            price = None
    return {
        "status": cand_status(cand),
        "chars": cand.get("chars", fetch.get("chars")),
        "found": (bp.get("found") if bp else None) or onreq.get("found") or cand.get("found"),
        "match": match or onreq.get("match") or cand.get("match"),
        "price": price if price is not None else cand.get("price"),
        "on_request": bool(raw_onreq),
        "comment": (bp.get("snippet") if bp else None) or cand.get("comment"),
        "note": visible_note(cand),
    }


def _slim_cand(cand: dict) -> dict:
    """Копия кандидата для сессии: без тяжёлого контента страницы (он в fetch_cache)."""
    out = {k: v for k, v in cand.items() if k not in ("fetch", "inspect")}
    out.update(summarize_cand(cand))
    fetch = cand.get("fetch")
    if isinstance(fetch, dict):                # сохранить лёгкую сводку загрузки
        out["blocked"] = fetch.get("blocked")
        out.setdefault("chars", fetch.get("chars"))
    return out


def _slim_item(item: dict) -> dict:
    # Белый список: поле, которого здесь нет, не переживёт сохранение сессии. `active_s` —
    # активное время позиции (obs/timing.py), иначе после перезагрузки страницы колонка «Время»
    # опустела бы, хотя работа по позиции давно закончена.
    keep = ("row", "name", "part_number", "raw", "queries", "query", "affix", "verdict",
            "not_found_reason", "on_request", "rationale", "parsed", "active_s")
    out = {k: item.get(k) for k in keep if k in item}
    out["candidates"] = [_slim_cand(c) for c in item.get("candidates", []) or []]
    return out


def save_session(items: list[dict], *, filename: str | None = None,
                 path: str = _SESSION_PATH) -> str:
    """Атомарно сохранить текущую сессию (список товаров) в JSON. Возвращает путь."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {"filename": filename, "saved_at": time.time(),
            "items": [_slim_item(it) for it in items]}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def load_session(path: str = _SESSION_PATH) -> dict | None:
    """Загрузить сохранённую сессию или None, если файла нет/битый."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "items" not in data:
        return None
    return data


def reset_session(path: str = _SESSION_PATH) -> None:
    """Удалить файл сессии (сброс прогресса)."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
