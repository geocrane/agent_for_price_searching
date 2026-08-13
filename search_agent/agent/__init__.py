# -*- coding: utf-8 -*-
"""Оркестратор агента (§9/M7): очередь задач + одна полоса модели + бюджеты + стоп.

Substrate, независимый от конкретных обработчиков: kind → async-handler регистрируется снаружи.
Модельные задачи (uses_model) сериализуются одной полосой; не-модельные (fetch/браузер) идут
параллельно, чтобы полоса модели не простаивала. См. Orchestrator и AgentTask.
"""
from .loop import ReActAgent, run_react
from .orchestrator import Orchestrator
from .run import OrchestratedRun, run_orchestrated
from .tasks import AgentTask, Priority, TaskState

__all__ = ["Orchestrator", "AgentTask", "Priority", "TaskState",
           "OrchestratedRun", "run_orchestrated", "ReActAgent", "run_react"]
