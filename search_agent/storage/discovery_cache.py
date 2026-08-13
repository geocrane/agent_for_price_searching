# -*- coding: utf-8 -*-
"""Сохранение/загрузка результатов discovery в JSON-фикстуру.

Чтобы при разработке не гонять поиск (Serper) заново: сохранили выдачу один раз и работаем
с ней оффлайн (загрузка страниц, извлечение цены и т.д.).
Структура: {"items": [{"row","name","part_number","queries",[...],"candidates":[{...}]}]}.
"""
import json
from pathlib import Path


def save(path: str | Path, items: list[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")


def load(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data.get("items", data if isinstance(data, list) else [])
