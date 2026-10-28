# -*- coding: utf-8 -*-
"""Оркестратор агента (§9/M7): очередь задач + бюджеты + стоп.

Substrate, независимый от конкретных обработчиков: kind → async-handler регистрируется снаружи.
Поставленная задача выполняется сразу; сколько работы идёт одновременно, решают гейты
(agent/gates.py) и адаптивный исполнитель модели, а не оркестратор. См. Orchestrator и AgentTask.
"""
from .loop import ReActAgent, run_react
from .orchestrator import Orchestrator
from .run import OrchestratedRun, run_orchestrated
from .tasks import AgentTask, Priority, TaskState

__all__ = ["Orchestrator", "AgentTask", "Priority", "TaskState",
           "OrchestratedRun", "run_orchestrated", "ReActAgent", "run_react"]
