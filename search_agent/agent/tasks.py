# -*- coding: utf-8 -*-
"""Задача агента (§9): first-class объект с жизненным циклом и приоритетом.

kind — что делать (discover|fetch|inspect|find_link|escalate|extract|adjudicate|report_gap|chat…);
uses_model — нужна ли полоса модели (тогда задача идёт через сериализующую очередь);
agent_id — контекст/подагент (на будущее, для раздельных тредов). Приоритет: меньше = раньше.
"""
import itertools
import time
from dataclasses import dataclass, field
from enum import Enum

_seq = itertools.count(1)


class TaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class Priority:
    HIGH = 0
    NORMAL = 50
    LOW = 100


@dataclass
class AgentTask:
    kind: str
    agent_id: str = "default"
    row: int | None = None
    priority: int = Priority.NORMAL
    uses_model: bool = False
    payload: dict = field(default_factory=dict)

    id: str = ""
    seq: int = 0
    state: TaskState = TaskState.QUEUED
    result: object = None
    error: str | None = None
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None

    def __post_init__(self) -> None:
        if not self.seq:
            self.seq = next(_seq)
        if not self.id:
            self.id = "t%d" % self.seq

    def event(self) -> dict:
        """Событие `task` для UI (панель очереди + таймлайн шагов)."""
        return {"type": "task", "id": self.id, "kind": self.kind, "agent_id": self.agent_id,
                "row": self.row, "priority": self.priority, "uses_model": self.uses_model,
                "state": self.state.value, "error": self.error,
                "started": self.started, "finished": self.finished}
