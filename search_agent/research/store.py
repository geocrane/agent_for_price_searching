# -*- coding: utf-8 -*-
"""Хранилище диалогов, источников и досье (SQLite).

Отдельный файл `runs/chats.sqlite`, а НЕ общая `history.sqlite`: там схема прогонов цен, за
которой стоит рабочий инструмент, и мешать в неё переписку незачем. Разные жизненные циклы —
разные базы.

Что храним и зачем:
  * `chats`/`messages` — история диалога, чтобы после перезагрузки страницы разговор продолжался,
    а не начинался заново. Служебные шаги агента (kind=step/observation) хранятся вместе с
    репликами: без них следующий ход потерял бы контекст поиска;
  * `sources` — реестр `[Sn]`. Ссылка, поставленная три хода назад, обязана указывать на тот же
    источник и сегодня, иначе история врёт задним числом;
  * `dossiers` — результаты разведки. Накапливаются между диалогами: проверенный источник
    незачем перепроверять в следующий раз.

Стандартный `sqlite3`, без новых зависимостей. Ошибки БД не роняют диалог — переписка важнее
персистентности (логируем и продолжаем в памяти).
"""
import json
import sqlite3
import time
import uuid
from pathlib import Path

from .probe import SourceDossier
from .sources import Source, SourceRegistry
from ..obs.log import get_logger

log = get_logger("research.store")

DEFAULT_DB = "runs/chats.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    model      TEXT NOT NULL DEFAULT '',
    created    REAL NOT NULL,
    updated    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  TEXT NOT NULL,
    role     TEXT NOT NULL,
    kind     TEXT NOT NULL DEFAULT 'text',
    content  TEXT NOT NULL DEFAULT '',
    meta     TEXT NOT NULL DEFAULT '{}',
    ts       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
CREATE TABLE IF NOT EXISTS sources (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  TEXT NOT NULL,
    sid      TEXT NOT NULL,
    url      TEXT NOT NULL,
    domain   TEXT NOT NULL DEFAULT '',
    title    TEXT NOT NULL DEFAULT '',
    ts       REAL NOT NULL,
    UNIQUE(chat_id, sid)
);
CREATE TABLE IF NOT EXISTS dossiers (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  TEXT NOT NULL,
    sid      TEXT NOT NULL DEFAULT '',
    url      TEXT NOT NULL,
    domain   TEXT NOT NULL DEFAULT '',
    verdict  TEXT NOT NULL DEFAULT '',
    access   TEXT NOT NULL DEFAULT '',
    data     TEXT NOT NULL DEFAULT '{}',
    ts       REAL NOT NULL,
    UNIQUE(chat_id, url)
);
CREATE INDEX IF NOT EXISTS idx_dossiers_verdict ON dossiers(verdict);
"""

# Роли сообщений, которые уходят в модель как контекст предыдущих ходов.
LLM_ROLES = ("user", "assistant")
# Виды сообщений: реплики и служебные шаги цикла.
KIND_TEXT, KIND_STEP, KIND_OBSERVATION, KIND_QUESTION, KIND_ANSWER = (
    "text", "step", "observation", "question", "answer")


class ChatState:
    """Состояние одного диалога в памяти: сообщения, источники, досье, вопрос на паузе."""

    def __init__(self, chat_id: str, *, title: str = "", messages=None, registry=None,
                 dossiers=None, pending_question: str = "") -> None:
        self.chat_id = chat_id
        self.title = title
        self.messages: list[dict] = list(messages or [])
        self.registry: SourceRegistry = registry or SourceRegistry()
        self.dossiers: list[dict] = list(dossiers or [])
        self.pending_question = pending_question

    def add(self, role: str, content: str, *, kind: str = KIND_TEXT, meta: dict | None = None):
        msg = {"role": role, "content": content, "kind": kind, "meta": meta or {},
               "ts": time.time()}
        self.messages.append(msg)
        return msg

    def to_llm_messages(self, *, keep_steps: int = 12) -> list[dict]:
        """Собрать список сообщений для модели.

        Последние `keep_steps` служебных шагов отдаём дословно, более старые сворачиваем в одну
        строку: без свёртки контекст растёт линейно с длиной беседы и рано или поздно упрётся в
        лимит модели прямо посреди разговора. Реплики пользователя и итоговые ответы НЕ
        сворачиваем никогда — это смысл диалога.
        """
        service = [i for i, m in enumerate(self.messages)
                   if m.get("kind") in (KIND_STEP, KIND_OBSERVATION)]
        verbatim = set(service[-keep_steps:]) if keep_steps > 0 else set()
        out, folded = [], []
        for i, m in enumerate(self.messages):
            if m.get("role") not in LLM_ROLES:
                continue
            if m.get("kind") in (KIND_STEP, KIND_OBSERVATION) and i not in verbatim:
                folded.append(_fold(m))
                continue
            if folded:
                out.append({"role": "user",
                            "content": "Ранее в этом диалоге: " + "; ".join(folded[-20:])})
                folded = []
            out.append({"role": m["role"], "content": m["content"]})
        if folded:
            out.append({"role": "user",
                        "content": "Ранее в этом диалоге: " + "; ".join(folded[-20:])})
        return out

    def add_dossier(self, dossier) -> dict:
        """Добавить/обновить досье источника (по url — источник один, проверок может быть много)."""
        d = dossier.to_dict() if isinstance(dossier, SourceDossier) else dict(dossier)
        for i, old in enumerate(self.dossiers):
            if old.get("url") == d.get("url"):
                self.dossiers[i] = d
                return d
        self.dossiers.append(d)
        return d


def _fold(msg: dict) -> str:
    """Свёртка служебного шага в одну строку (для старых шагов длинного диалога)."""
    text = (msg.get("content") or "").strip().replace("\n", " ")
    return text[:120]


class ChatStore:
    """Доступ к `runs/chats.sqlite`. Ошибки БД логируются и не роняют диалог."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = str(db_path or DEFAULT_DB)
        self._conn = None
        self._connect()

    def _connect(self) -> None:
        try:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA)
        except (sqlite3.Error, OSError) as exc:
            self._conn = None
            log.warning("Хранилище диалогов недоступно (%s): %s — работаю без сохранения",
                        self.db_path, exc)

    @property
    def available(self) -> bool:
        return self._conn is not None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---- диалоги -----------------------------------------------------------

    def create(self, title: str = "", model: str = "") -> str:
        chat_id = uuid.uuid4().hex[:12]
        now = time.time()
        if self._conn is not None:
            try:
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO chats(id, title, model, created, updated) VALUES(?,?,?,?,?)",
                        (chat_id, title[:200], model, now, now))
            except sqlite3.Error as exc:
                log.warning("Диалог не создан в БД: %s", exc)
        return chat_id

    def touch(self, chat_id: str, *, title: str | None = None) -> None:
        if self._conn is None:
            return
        try:
            with self._conn:
                if title:
                    self._conn.execute("UPDATE chats SET updated=?, title=? WHERE id=?",
                                       (time.time(), title[:200], chat_id))
                else:
                    self._conn.execute("UPDATE chats SET updated=? WHERE id=?",
                                       (time.time(), chat_id))
        except sqlite3.Error as exc:
            log.warning("Диалог не обновлён: %s", exc)

    def append(self, chat_id: str, msg: dict) -> None:
        if self._conn is None:
            return
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO messages(chat_id, role, kind, content, meta, ts) "
                    "VALUES(?,?,?,?,?,?)",
                    (chat_id, msg.get("role") or "user", msg.get("kind") or KIND_TEXT,
                     msg.get("content") or "", json.dumps(msg.get("meta") or {},
                                                          ensure_ascii=False),
                     msg.get("ts") or time.time()))
        except sqlite3.Error as exc:
            log.warning("Сообщение не сохранено: %s", exc)

    def save_sources(self, chat_id: str, registry: SourceRegistry) -> None:
        if self._conn is None:
            return
        try:
            with self._conn:
                for s in registry.known():
                    self._conn.execute(
                        "INSERT INTO sources(chat_id, sid, url, domain, title, ts) "
                        "VALUES(?,?,?,?,?,?) ON CONFLICT(chat_id, sid) DO UPDATE SET "
                        "title=excluded.title", (chat_id, s.sid, s.url, s.domain, s.title, s.ts))
        except sqlite3.Error as exc:
            log.warning("Источники не сохранены: %s", exc)

    def save_dossiers(self, chat_id: str, dossiers: list) -> None:
        if self._conn is None:
            return
        rows = [d.to_dict() if isinstance(d, SourceDossier) else dict(d) for d in dossiers or []]
        try:
            with self._conn:
                for d in rows:
                    self._conn.execute(
                        "INSERT INTO dossiers(chat_id, sid, url, domain, verdict, access, data, ts)"
                        " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(chat_id, url) DO UPDATE SET "
                        "sid=excluded.sid, verdict=excluded.verdict, access=excluded.access, "
                        "data=excluded.data, ts=excluded.ts",
                        (chat_id, d.get("sid") or "", d.get("url") or "", d.get("domain") or "",
                         d.get("verdict") or "", d.get("access") or "",
                         json.dumps(d, ensure_ascii=False), time.time()))
        except sqlite3.Error as exc:
            log.warning("Досье источников не сохранены: %s", exc)

    def load(self, chat_id: str) -> ChatState | None:
        if self._conn is None:
            return None
        try:
            row = self._conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
            if row is None:
                return None
            msgs = [{"role": r["role"], "kind": r["kind"], "content": r["content"],
                     "meta": _json(r["meta"]), "ts": r["ts"]}
                    for r in self._conn.execute(
                        "SELECT * FROM messages WHERE chat_id=? ORDER BY id", (chat_id,))]
            srcs = [Source(sid=r["sid"], url=r["url"], domain=r["domain"], title=r["title"],
                           ts=r["ts"])
                    for r in self._conn.execute(
                        "SELECT * FROM sources WHERE chat_id=? ORDER BY id", (chat_id,))]
            doss = [_json(r["data"]) for r in self._conn.execute(
                "SELECT data FROM dossiers WHERE chat_id=? ORDER BY id", (chat_id,))]
        except sqlite3.Error as exc:
            log.warning("Диалог не прочитан: %s", exc)
            return None
        pending = ""
        for m in reversed(msgs):
            if m["kind"] == KIND_QUESTION:
                pending = m["content"]
                break
            if m["kind"] == KIND_TEXT and m["role"] == "user":
                break                                  # на вопрос уже ответили
        return ChatState(chat_id, title=row["title"], messages=msgs,
                         registry=SourceRegistry(srcs), dossiers=doss, pending_question=pending)

    def list_chats(self, limit: int = 50) -> list[dict]:
        if self._conn is None:
            return []
        try:
            rows = self._conn.execute(
                "SELECT c.id, c.title, c.created, c.updated, "
                "(SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.id AND m.kind IN ('text','answer')) "
                "AS n FROM chats c ORDER BY c.updated DESC LIMIT ?", (int(limit),)).fetchall()
        except sqlite3.Error as exc:
            log.warning("Список диалогов не прочитан: %s", exc)
            return []
        return [{"id": r["id"], "title": r["title"], "created": r["created"],
                 "updated": r["updated"], "messages": r["n"]} for r in rows]

    def rename(self, chat_id: str, title: str) -> bool:
        if self._conn is None:
            return False
        try:
            with self._conn:
                self._conn.execute("UPDATE chats SET title=?, updated=? WHERE id=?",
                                   (title[:200], time.time(), chat_id))
            return True
        except sqlite3.Error as exc:
            log.warning("Диалог не переименован: %s", exc)
            return False

    def delete(self, chat_id: str) -> bool:
        """Удалить диалог целиком (по явной команде пользователя)."""
        if self._conn is None:
            return False
        try:
            with self._conn:
                for table in ("messages", "sources", "dossiers"):
                    self._conn.execute("DELETE FROM %s WHERE chat_id=?" % table, (chat_id,))
                self._conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))
            log.info("Диалог %s удалён", chat_id)
            return True
        except sqlite3.Error as exc:
            log.warning("Диалог не удалён: %s", exc)
            return False

    def all_dossiers(self, *, verdict: str | None = None, limit: int = 200) -> list[dict]:
        """Каталог проверенных источников по всем диалогам (накопленное знание)."""
        if self._conn is None:
            return []
        sql = "SELECT data, ts FROM dossiers"
        args: tuple = ()
        if verdict:
            sql += " WHERE verdict=?"
            args = (verdict,)
        sql += " ORDER BY ts DESC LIMIT ?"
        try:
            rows = self._conn.execute(sql, args + (int(limit),)).fetchall()
        except sqlite3.Error as exc:
            log.warning("Каталог источников не прочитан: %s", exc)
            return []
        seen, out = set(), []
        for r in rows:                                  # один источник — одна строка каталога
            d = _json(r["data"])
            url = d.get("url")
            if url and url not in seen:
                seen.add(url)
                out.append(d)
        return out


def _json(raw) -> dict:
    try:
        val = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


def open_store(cfg=None) -> ChatStore:
    """Хранилище рядом с историей прогонов (тот же каталог runs/), но отдельным файлом."""
    path = DEFAULT_DB
    db = getattr(cfg, "db_path", None) if cfg is not None else None
    if db:
        path = str(Path(db).parent / "chats.sqlite")
    return ChatStore(path)
