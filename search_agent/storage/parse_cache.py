# -*- coding: utf-8 -*-
"""Кэш разбора позиций: «как этот товар называют продавцы» — один раз на позицию, навсегда.

Зачем. Разбор наименования моделью (см. `discovery/query_llm.py`) — единственная часть поиска,
результат которой не должен меняться от прогона к прогону: одно и то же наименование обязано
давать один и тот же запрос, иначе результаты двух прогонов несравнимы. Плюс это экономия: при
повторном прогоне того же файла модель не зовётся вовсе.

Ключ — `storage.history.item_key` (парт-номер, иначе нормализованное наименование), поэтому кэш
работает и между разными файлами с той же позицией. Хранится в той же SQLite, что и история
прогонов: отдельного файла заводить незачем, а переезд в каталог пользователя (Э6) переносит оба.

Пользовательская правка (`source='user'`) приоритетнее машинной: если человек поправил название,
модель его больше не перебивает.
"""
import json
import sqlite3
import time
from pathlib import Path

from .history import item_key
from ..obs.log import get_logger

log = get_logger("storage.parse_cache")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS item_parse (
    item_key   TEXT PRIMARY KEY,
    name       TEXT,
    part_number TEXT,
    parsed     TEXT NOT NULL,          -- JSON: {name_clean, brand, model, is_generic}
    source     TEXT NOT NULL DEFAULT 'model',   -- model | user
    updated_at TEXT NOT NULL
);
"""


def _db(cfg):
    """Соединение с БД истории (та же база) или None, если хранилище отключено/недоступно."""
    if cfg is not None and not getattr(cfg, "enabled", True):
        return None
    path = getattr(cfg, "db_path", None) if cfg is not None else None
    if not path:
        from ..config import load_settings
        st = load_settings().storage
        if not st.enabled:
            return None
        path = st.db_path
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.executescript(_SCHEMA)
        return conn
    except (sqlite3.Error, OSError) as exc:
        log.warning("Кэш разбора недоступен: %s — работаю без него", exc)
        return None


def load_many(items: list[dict], *, cfg=None) -> dict[int, dict]:
    """Достать разбор для позиций из кэша → {row: разбор}. Отсутствующие просто не возвращаются."""
    conn = _db(cfg)
    if conn is None or not items:
        return {}
    keys = {item_key(it.get("name"), it.get("part_number")): it["row"] for it in items}
    out: dict[int, dict] = {}
    try:
        with conn:
            marks = ",".join("?" * len(keys))
            rows = conn.execute(
                "SELECT item_key, parsed FROM item_parse WHERE item_key IN (%s)" % marks,
                list(keys)).fetchall()
        for key, parsed in rows:
            try:
                out[keys[key]] = json.loads(parsed)
            except (ValueError, KeyError):
                continue
    except sqlite3.Error as exc:
        log.warning("Чтение кэша разбора не удалось: %s", exc)
    finally:
        conn.close()
    if out:
        log.info("Разбор из кэша: %d позиций из %d", len(out), len(items))
    return out


def save_many(items: list[dict], parsed: dict[int, dict], *, cfg=None,
              source: str = "model") -> int:
    """Сохранить разбор. Пользовательскую правку машинным разбором НЕ перетираем."""
    conn = _db(cfg)
    if conn is None or not parsed:
        return 0
    by_row = {it["row"]: it for it in items}
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    saved = 0
    try:
        with conn:
            for row, data in parsed.items():
                it = by_row.get(row)
                if not it or not data:
                    continue
                key = item_key(it.get("name"), it.get("part_number"))
                if source != "user":
                    cur = conn.execute("SELECT source FROM item_parse WHERE item_key=?",
                                       (key,)).fetchone()
                    if cur and cur[0] == "user":
                        continue                    # человек уже поправил — не трогаем
                conn.execute(
                    "INSERT INTO item_parse(item_key, name, part_number, parsed, source, updated_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(item_key) DO UPDATE SET "
                    "parsed=excluded.parsed, source=excluded.source, updated_at=excluded.updated_at",
                    (key, it.get("name"), it.get("part_number"),
                     json.dumps(data, ensure_ascii=False), source, now))
                saved += 1
    except sqlite3.Error as exc:
        log.warning("Запись кэша разбора не удалась: %s", exc)
    finally:
        conn.close()
    if saved:
        log.info("Разбор сохранён в кэш: %d позиций (%s)", saved, source)
    return saved


def set_manual(name: str, part_number, name_clean: str, *, cfg=None) -> bool:
    """Ручная правка названия пользователем — приоритетнее машинного разбора."""
    return bool(save_many([{"row": 0, "name": name, "part_number": part_number}],
                          {0: {"name_clean": name_clean, "brand": "", "model": "",
                               "is_generic": False}},
                          cfg=cfg, source="user"))


def forget(name: str, part_number, *, cfg=None) -> None:
    """Забыть разбор позиции (следующий прогон переспросит модель)."""
    conn = _db(cfg)
    if conn is None:
        return
    try:
        with conn:
            conn.execute("DELETE FROM item_parse WHERE item_key=?",
                         (item_key(name, part_number),))
    except sqlite3.Error as exc:
        log.warning("Сброс кэша разбора не удался: %s", exc)
    finally:
        conn.close()
