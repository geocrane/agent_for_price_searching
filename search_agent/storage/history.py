# -*- coding: utf-8 -*-
"""История прогонов на SQLite (M5): храним ВСЕ найденные цены + своды + dead-letter.

Зачем (по ТЗ §10.2/§10.5): ни одна найденная цена не теряется — все офферы сохраняются с
рейтингом источника (тир/вес), сводом и причиной, если цена не найдена. Это даёт:
  • историю цен по товару МЕЖДУ прогонами (динамика во времени);
  • dead-letter — позиции «не найдено/заблокировано» для повторного (усиленного) прохода;
  • переиспользование прошлых результатов и прозрачность.

Идентичность товара между прогонами — `item_key` (парт-номер, иначе нормализованное имя),
чтобы сопоставлять один и тот же товар в разных файлах/прогонах. Универсально, без привязки
к категории (правило universal-tool-any-file). Стандартная библиотека, без новых зависимостей.
"""
import re
import sqlite3
from datetime import datetime, timezone

from ..input.normalize import clean_name
from ..obs.log import get_logger

log = get_logger("storage.history")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started TEXT NOT NULL, finished TEXT, source TEXT,
    total INTEGER DEFAULT 0, with_price INTEGER DEFAULT 0, not_found INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL, item_key TEXT NOT NULL, row INTEGER,
    name TEXT, part_number TEXT,
    value REAL NOT NULL, currency TEXT DEFAULT 'RUB',
    source_domain TEXT, url TEXT, tier INTEGER, weight REAL, match TEXT,
    in_stock INTEGER, vat INTEGER, confidence REAL, snippet TEXT, seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_offers_key ON offers(item_key);
CREATE INDEX IF NOT EXISTS idx_offers_run ON offers(run_id);
CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL, item_key TEXT NOT NULL, row INTEGER,
    name TEXT, part_number TEXT,
    primary_value REAL, currency TEXT DEFAULT 'RUB', price_min REAL, price_max REAL,
    match TEXT, confidence REAL, corroborated_by INTEGER DEFAULT 0,
    excluded_count INTEGER DEFAULT 0, not_found_reason TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verdicts_key ON verdicts(item_key);
CREATE INDEX IF NOT EXISTS idx_verdicts_run ON verdicts(run_id);
CREATE TABLE IF NOT EXISTS dead_letter (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL, item_key TEXT NOT NULL, row INTEGER,
    name TEXT, part_number TEXT, reason TEXT, detail TEXT,
    resolved INTEGER DEFAULT 0, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dead_run ON dead_letter(run_id);
"""

_WS = re.compile(r"\s+", re.U)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def item_key(name: str | None, part_number: str | None) -> str:
    """Стабильный ключ товара для сопоставления между прогонами.

    Приоритет — парт-номер (без пробелов, верхний регистр); иначе нормализованное имя.
    """
    pn = _WS.sub("", (part_number or "").strip().upper())
    if pn:
        return "pn:" + pn
    return "nm:" + clean_name(name or "").lower()


def _b2i(v) -> int | None:
    return None if v is None else (1 if v else 0)


class HistoryStore:
    """Обёртка над SQLite-историей. Одна БД на всё; соединение на время операций.

    Потокобезопасность: `check_same_thread=False` + короткие транзакции; в проекте пишем в
    конце прогона (единичный вызов), читаем по запросу — конкуренции нет.
    """

    def __init__(self, db_path: str) -> None:
        from pathlib import Path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.con = sqlite3.connect(db_path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(_SCHEMA)
        self.con.commit()

    def close(self) -> None:
        try:
            self.con.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- запись -------------------------------------------------------------

    def start_run(self, source: str = "") -> int:
        cur = self.con.execute("INSERT INTO runs(started, source) VALUES (?, ?)",
                               (_now(), source or ""))
        self.con.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, total: int, with_price: int, not_found: int) -> None:
        self.con.execute(
            "UPDATE runs SET finished=?, total=?, with_price=?, not_found=? WHERE id=?",
            (_now(), total, with_price, not_found, run_id))
        self.con.commit()

    def record_item(self, run_id: int, item: dict) -> dict:
        """Сохранить один товар прогона: ВСЕ офферы + свод (+ dead-letter, если цены нет).

        Возвращает {offers, has_price, dead_letter}. Дедуп офферов — по (домен, цена).
        """
        from ..report.rationale import offers_from_item
        from ..report.xlsx import norm_verdict

        name = item.get("name") or ""
        pn = item.get("part_number")
        key = item_key(name, pn)
        row = item.get("row")
        seen = _now()

        offers = offers_from_item(item, limit=None)      # ВСЕ офферы (без обрезки), с дедупом
        for o in offers:
            self.con.execute(
                "INSERT INTO offers(run_id, item_key, row, name, part_number, value, currency, "
                "source_domain, url, tier, weight, match, in_stock, vat, confidence, snippet, seen_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, key, row, name, pn, o["value"], o.get("currency", "RUB"),
                 o.get("domain"), o.get("url"), o.get("tier"), o.get("weight"), o.get("match"),
                 _b2i(o.get("in_stock")), _b2i(o.get("vat")), o.get("confidence"),
                 (o.get("snippet") or "")[:300], seen))

        nv = norm_verdict(item.get("verdict"))
        nfr = item.get("not_found_reason")
        self.con.execute(
            "INSERT INTO verdicts(run_id, item_key, row, name, part_number, primary_value, "
            "currency, price_min, price_max, match, confidence, corroborated_by, excluded_count, "
            "not_found_reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, key, row, name, pn, nv["value"], nv["currency"], nv["price_min"],
             nv["price_max"], nv["match"], nv["confidence"], nv["corroborated_by"],
             nv["excluded_count"], nfr, seen))

        has_price = nv["value"] is not None
        dl = False
        if not has_price:                                # не нашли → в dead-letter для доразбора
            reason = nfr or "нет офферов"
            detail = "проверено доменов: %d; офферов: %d" % (
                len({o.get("domain") for o in offers}), len(offers))
            self.con.execute(
                "INSERT INTO dead_letter(run_id, item_key, row, name, part_number, reason, detail, "
                "created_at) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, key, row, name, pn, reason, detail, seen))
            dl = True

        self.con.commit()
        return {"offers": len(offers), "has_price": has_price, "dead_letter": dl}

    # ---- чтение -------------------------------------------------------------

    def recent_runs(self, limit: int = 20) -> list[dict]:
        cur = self.con.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

    def price_history(self, *, name: str | None = None, part_number: str | None = None,
                      key: str | None = None, limit: int = 50) -> list[dict]:
        """История сводов по товару (по item_key) во времени — динамика итоговой цены."""
        k = key or item_key(name, part_number)
        cur = self.con.execute(
            "SELECT v.*, r.started AS run_started FROM verdicts v JOIN runs r ON r.id=v.run_id "
            "WHERE v.item_key=? ORDER BY v.id DESC LIMIT ?", (k, limit))
        return [dict(x) for x in cur.fetchall()]

    def offers_for(self, *, name: str | None = None, part_number: str | None = None,
                   key: str | None = None, run_id: int | None = None,
                   limit: int = 200) -> list[dict]:
        """Все сохранённые офферы по товару (опционально в рамках одного прогона)."""
        k = key or item_key(name, part_number)
        if run_id is not None:
            cur = self.con.execute(
                "SELECT * FROM offers WHERE item_key=? AND run_id=? ORDER BY value LIMIT ?",
                (k, run_id, limit))
        else:
            cur = self.con.execute(
                "SELECT * FROM offers WHERE item_key=? ORDER BY id DESC LIMIT ?", (k, limit))
        return [dict(x) for x in cur.fetchall()]

    def dead_letters(self, *, run_id: int | None = None, include_resolved: bool = False,
                     limit: int = 500) -> list[dict]:
        """Позиции без надёжной цены (для повторного прохода). По умолчанию — нерешённые."""
        where, args = [], []
        if run_id is not None:
            where.append("run_id=?"); args.append(run_id)
        if not include_resolved:
            where.append("resolved=0")
        sql = "SELECT * FROM dead_letter"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"; args.append(limit)
        cur = self.con.execute(sql, tuple(args))
        return [dict(x) for x in cur.fetchall()]

    def resolve_dead_letter(self, *, key: str, run_id: int | None = None) -> int:
        """Пометить dead-letter как решённый (после успешного повторного прохода)."""
        if run_id is not None:
            cur = self.con.execute(
                "UPDATE dead_letter SET resolved=1 WHERE item_key=? AND run_id=?", (key, run_id))
        else:
            cur = self.con.execute("UPDATE dead_letter SET resolved=1 WHERE item_key=?", (key,))
        self.con.commit()
        return cur.rowcount

    def stats(self) -> dict:
        def one(sql):
            return self.con.execute(sql).fetchone()[0]
        return {
            "runs": one("SELECT COUNT(*) FROM runs"),
            "offers": one("SELECT COUNT(*) FROM offers"),
            "verdicts": one("SELECT COUNT(*) FROM verdicts"),
            "dead_letter_open": one("SELECT COUNT(*) FROM dead_letter WHERE resolved=0"),
            "items_tracked": one("SELECT COUNT(DISTINCT item_key) FROM verdicts"),
        }
