# -*- coding: utf-8 -*-
"""Слой ввода: адаптивное чтение любых Excel/CSV и нормализация (M1)."""
from .excel_reader import ReadResult, explain_empty, read_table

__all__ = ["ReadResult", "explain_empty", "read_table"]
