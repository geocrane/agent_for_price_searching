# -*- coding: utf-8 -*-
"""Каталог моделей: чтение редактируемого config/models.yaml.

Файл config/models.yaml генерируется разово из снимка foundation-models-*.json
(см. scripts/gen_models_yaml.py) и затем правится вручную. Здесь — только ЧТЕНИЕ:
loader отдаёт список моделей в порядке файла (он уже отсортирован по цене) и модель
по умолчанию. Никакой фильтрации сырого снимка тут нет — источник истины именно yaml.
"""
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent      # корень проекта search_agent/
MODELS_PATH = ROOT / "config" / "models.yaml"


@dataclass
class ModelInfo:
    id: str
    name: str
    base_url: str
    context_length: int | None = None
    price_in: float = 0.0          # руб / 1 млн входящих токенов
    price_out: float = 0.0         # руб / 1 млн исходящих токенов
    tags: list[str] = field(default_factory=list)

    def cost_rub(self, prompt_tokens: int, completion_tokens: int) -> float:
        """Стоимость запроса в рублях по ценам модели (руб/1млн)."""
        return prompt_tokens * self.price_in / 1e6 + completion_tokens * self.price_out / 1e6


class CatalogError(RuntimeError):
    """Проблема с config/models.yaml (нет файла / битый YAML / пустой список)."""


def _load_raw(path: Path | None = None) -> dict:
    path = path or MODELS_PATH
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise CatalogError(
            f"Не найден каталог моделей {path}. Сгенерируйте его: "
            f"python scripts/gen_models_yaml.py") from e
    except yaml.YAMLError as e:
        raise CatalogError(f"Битый YAML в каталоге моделей {path}: {e}") from e
    if not isinstance(raw, dict) or not raw.get("models"):
        raise CatalogError(f"Пустой или некорректный каталог моделей {path} (нет ключа 'models').")
    return raw


def load_catalog(path: Path | None = None) -> list[ModelInfo]:
    """Список моделей из config/models.yaml в порядке файла (уже отсортирован по цене)."""
    raw = _load_raw(path)
    out: list[ModelInfo] = []
    for m in raw["models"]:
        out.append(ModelInfo(
            id=m["id"],
            name=m.get("name") or m["id"].split("/")[-1],
            base_url=m["base_url"],
            context_length=m.get("context_length"),
            price_in=float(m.get("price_in", 0.0)),
            price_out=float(m.get("price_out", 0.0)),
            tags=list(m.get("tags", []) or []),
        ))
    return out


def default_model(path: Path | None = None) -> str:
    """id модели по умолчанию (поле 'default'); фолбэк — первая в списке."""
    raw = _load_raw(path)
    if raw.get("default"):
        return raw["default"]
    return raw["models"][0]["id"]


def find_model(model_id: str, path: Path | None = None) -> ModelInfo | None:
    """Модель по id (для расчёта стоимости / автозаполнения URL)."""
    for m in load_catalog(path):
        if m.id == model_id:
            return m
    return None
