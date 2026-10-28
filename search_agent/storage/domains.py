# -*- coding: utf-8 -*-
"""Реестр доменов: какие сайты вообще встречались в работе.

Зачем отдельная таблица. Приоритеты источников пользователь задаёт по домену, но ВСПОМНИТЬ
домен по памяти невозможно: в одной реальной сессии их 547. Поэтому реестр накапливается сам —
всё, что встретилось в выдаче, попадает в список настроек, где сайту можно назначить уровень.

Живёт в той же SQLite, что история и профиль поиска: одно место состояния на пользователя.
Счётчики (`hits`/`priced`/`blocked`) нужны не для красоты — по ним видно, какой домен вообще
приносит цены, а какой только тратит загрузки, и решение о его уровне становится осмысленным.

Ошибки хранилища никогда не роняют прогон: реестр — удобство, а не результат работы.
"""
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse

from ..obs.log import get_logger

log = get_logger("storage.domains")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS domain_seen (
    domain     TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    hits       INTEGER NOT NULL DEFAULT 0,
    priced     INTEGER NOT NULL DEFAULT 0,
    blocked    INTEGER NOT NULL DEFAULT 0
);
"""


def normalize(raw: str) -> str:
    """Домен в сравнимом виде: нижний регистр, без схемы, без www, без пути и порта."""
    d = str(raw or "").strip().lower()
    if "//" in d:
        d = urlparse(d).netloc or d.split("//", 1)[1]
    d = d.split("/")[0].split(":")[0].removeprefix("www.")
    return d


def _db(cfg=None):
    """Соединение с хранилищем или None, если оно отключено/недоступно."""
    path = getattr(cfg, "db_path", None) if cfg is not None else None
    if cfg is not None and not getattr(cfg, "enabled", True):
        return None
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
        log.warning("Реестр доменов недоступен: %s", exc)
        return None


def tally(items: list[dict]) -> dict[str, dict]:
    """Посчитать по товарам сессии: сколько у домена страниц, с ценой и заблокированных."""
    from .. import session as sess
    out: dict[str, dict] = {}
    for it in items or []:
        for c in it.get("candidates", []) or []:
            if c.get("is_file"):
                continue
            dom = normalize(c.get("domain") or c.get("url") or "")
            if not dom:
                continue
            row = out.setdefault(dom, {"hits": 0, "priced": 0, "blocked": 0})
            row["hits"] += 1
            if c.get("prices") or c.get("price") is not None:
                row["priced"] += 1
            if sess.cand_status(c) in sess.UNBLOCKABLE:
                row["blocked"] += 1
    return out


def remember(items: list[dict], *, cfg=None) -> int:
    """Дописать домены прогона в реестр. Возвращает число затронутых доменов."""
    counts = tally(items)
    if not counts:
        return 0
    conn = _db(cfg)
    if conn is None:
        return 0
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with conn:
            conn.executemany(
                "INSERT INTO domain_seen(domain, first_seen, last_seen, hits, priced, blocked) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(domain) DO UPDATE SET last_seen=excluded.last_seen, "
                "hits=excluded.hits, priced=excluded.priced, blocked=excluded.blocked",
                [(d, now, now, c["hits"], c["priced"], c["blocked"]) for d, c in counts.items()])
        log.info("Реестр доменов пополнен: %d доменов", len(counts))
        return len(counts)
    except sqlite3.Error as exc:
        log.warning("Реестр доменов не записан: %s", exc)
        return 0
    finally:
        conn.close()


def load(*, cfg=None) -> dict[str, dict]:
    """Весь реестр: домен → {first_seen, last_seen, hits, priced, blocked}."""
    conn = _db(cfg)
    if conn is None:
        return {}
    try:
        rows = conn.execute("SELECT domain, first_seen, last_seen, hits, priced, blocked "
                            "FROM domain_seen").fetchall()
    except sqlite3.Error as exc:
        log.warning("Реестр доменов не прочитан: %s", exc)
        return {}
    finally:
        conn.close()
    return {r[0]: {"first_seen": r[1], "last_seen": r[2], "hits": r[3],
                   "priced": r[4], "blocked": r[5]} for r in rows}


def merged(items: list[dict] | None = None, *, cfg=None) -> dict[str, dict]:
    """Реестр + домены ТЕКУЩЕЙ сессии.

    Реестр пополняется в конце прогона, а домены нужны в настройках сразу: пользователь
    открывает «Сайты» посреди работы и обязан увидеть то, что видит в таблице выдачи.
    """
    out = load(cfg=cfg)
    for dom, c in tally(items or []).items():
        row = out.setdefault(dom, {"first_seen": "", "last_seen": "",
                                   "hits": 0, "priced": 0, "blocked": 0})
        row.update(c)                     # у сессии данные свежее, чем у последней записи
    return out
