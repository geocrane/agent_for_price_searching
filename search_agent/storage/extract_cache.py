# -*- coding: utf-8 -*-
"""Кэш ответов модели по странице: та же страница + тот же товар → тот же разбор, без вызова.

Зачем. Повторный прогон файла (дозагрузка позиций, перезапуск после «Стоп», добор второй
страницы выдачи) заново оплачивал и заново ЖДАЛ каждый вызов модели, хотя страницы берутся из
`runs/fetch_cache` мгновенно и текст на входе побайтово тот же. Замер: 271 вызов на 30 позиций,
хвост генерации — тысячи токенов на страницу.

Ключ включает ВСЁ, от чего зависит ответ: адрес страницы, отправленный модели текст, товар,
модель и версия промпта. Промпт особенно важен: без его версии кэш пережил бы правку `_SYSTEM`
и продолжал бы отдавать ответы по старым правилам — тихо и незаметно.

Хранится в той же SQLite, что история и разбор позиций (`storage/parse_cache.py` — тот же
приём), поэтому отдельного файла и отдельной чистки не появляется.
"""
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from .history import item_key
from ..obs.log import get_logger

log = get_logger("storage.extract_cache")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS page_extract (
    key        TEXT PRIMARY KEY,
    url        TEXT,
    item       TEXT,
    model      TEXT,
    payload    TEXT NOT NULL,         -- JSON: {records, on_request, note, usage}
    updated_at TEXT NOT NULL
);
"""


def make_key(url: str, page_text: str, item, model: str | None, prompt_version: str) -> str:
    """Ключ ответа модели. Меняется вместе с любым входом — иначе кэш начнёт врать."""
    parts = [url or "", hashlib.sha256((page_text or "").encode("utf-8")).hexdigest(),
             item_key(getattr(item, "name", ""), getattr(item, "part_number", None)),
             model or "", prompt_version]
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


_STORAGE = None            # конфиг хранилища, прочитанный один раз за процесс


def _storage_cfg(cfg):
    """Конфиг хранилища: переданный явно либо прочитанный ОДИН РАЗ за процесс.

    `load_settings()` каждый раз перечитывает .env и все config/*.yaml. Кэш ответов дёргается на
    КАЖДУЮ страницу, поэтому звать его здесь напрямую — сотни лишних чтений с диска в самом
    горячем месте прогона. Путь к базе и признак «включено» за время процесса не меняются.
    """
    global _STORAGE
    if cfg is not None:
        return cfg
    if _STORAGE is None:
        from ..config import load_settings
        _STORAGE = load_settings().storage
    return _STORAGE


def _db(cfg):
    """Соединение с БД истории (та же база) или None, если хранилище отключено/недоступно."""
    cfg = _storage_cfg(cfg)
    if cfg is not None and not getattr(cfg, "enabled", True):
        return None
    path = getattr(cfg, "db_path", None) if cfg is not None else None
    if not path:
        return None
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.executescript(_SCHEMA)
        return conn
    except (sqlite3.Error, OSError) as exc:
        log.warning("Кэш ответов модели недоступен: %s — работаю без него", exc)
        return None


_PURGED = False            # разовая чистка пустых записей за процесс


def count(*, cfg=None) -> int:
    """Сколько ответов лежит в кэше (диагностика и тесты). При недоступной базе — 0."""
    conn = _db(cfg)
    if conn is None:
        return 0
    try:
        return conn.execute("SELECT COUNT(*) FROM page_extract").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def purge_empty(*, cfg=None) -> int:
    """Удалить закэшированные ПУСТЫЕ ответы (ни цены, ни «по запросу», ни пояснения).

    Такие записи — след прежней ошибки, когда пояснение модели терялось при повторном запросе.
    TTL у кэша нет, поэтому без чистки страница вечно возвращала бы «пустой ответ без пояснения»
    и модель по ней больше никогда не спрашивали бы. Полезных данных здесь нет по определению:
    удаляется только то, в чём нечего было хранить. Возвращает число удалённых записей.
    """
    conn = _db(cfg)
    if conn is None:
        return 0
    try:
        with conn:
            cur = conn.execute(
                "DELETE FROM page_extract WHERE "
                "COALESCE(json_extract(payload,'$.note'),'')='' "
                "AND COALESCE(json_array_length(json_extract(payload,'$.records')),0)=0 "
                "AND COALESCE(json_array_length(json_extract(payload,'$.on_request')),0)=0")
        n = cur.rowcount or 0
        if n:
            log.info("Из кэша ответов модели убрано пустых записей: %d "
                     "(по ним модель будет спрошена заново)", n)
        return n
    except sqlite3.Error as exc:
        log.warning("Чистка пустых записей кэша не удалась: %s", exc)
        return 0
    finally:
        conn.close()


def load(key: str, *, cfg=None) -> dict | None:
    """Ответ модели по ключу или None. Любая ошибка чтения = промах: зовём модель как обычно."""
    global _PURGED
    conn = _db(cfg)
    if conn is None or not key:
        return None
    if not _PURGED:                     # один раз за процесс, на первом же обращении к кэшу
        _PURGED = True
        conn.close()
        purge_empty(cfg=cfg)
        conn = _db(cfg)
        if conn is None:
            return None
    try:
        row = conn.execute("SELECT payload FROM page_extract WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None
    except (sqlite3.Error, ValueError) as exc:
        log.warning("Чтение кэша ответов модели не удалось: %s", exc)
        return None
    finally:
        conn.close()


def save(key: str, payload: dict, *, url: str = "", item: str = "", model: str | None = None,
         cfg=None) -> bool:
    """Сохранить ответ модели. Возвращает True, если записан."""
    conn = _db(cfg)
    if conn is None or not key:
        return False
    try:
        with conn:
            conn.execute(
                "INSERT INTO page_extract(key, url, item, model, payload, updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "payload=excluded.payload, updated_at=excluded.updated_at",
                (key, url, item, model or "", json.dumps(payload, ensure_ascii=False),
                 time.strftime("%Y-%m-%dT%H:%M:%S")))
        return True
    except sqlite3.Error as exc:
        log.warning("Запись кэша ответов модели не удалась: %s", exc)
        return False
    finally:
        conn.close()
