# -*- coding: utf-8 -*-
"""Извлечение обобщённых характеристик из названия — для матчинга/аналогов.

Универсально: тянем «число + единица» (объём, масса, фасовка, частота, мощность, размеры)
без привязки к категории. Модель определяет точное/аналог по смыслу (§10.4 ТЗ), а
детерминированный КОД-СУДЬЯ (extract/judge.py) использует эти характеристики, чтобы отсеять
измеримые расхождения (25 кг ≠ 50 кг, 5 л ≠ 10 л, 600х300х200 ≠ 600х300х250).
"""
import re

# Обобщённые единицы (RU/EN). Регистронезависимо; список расширяемый.
# Порядок для регэкспа — по длине (длинные раньше), чтобы «гб» матчился раньше «б» и т.п.
_UNITS = [
    # объём памяти / частота / электрика
    "tb", "gb", "mb", "kb", "тб", "гб", "мб", "кб",
    "ghz", "mhz", "ггц", "мгц", "гц",
    "kw", "w", "квт", "вт", "va", "ва",
    "mah", "мач", "v", "в", "a", "а",
    # масса / фасовка
    "kg", "кг", "mg", "мг", "g", "г", "t", "т",
    # объём жидкости
    "ml", "мл", "l", "л",
    # длина / габариты
    "mm", "cm", "мм", "см", "m", "м",
    # штучность / прочее
    "pcs", "шт", "уп", "port", "порт", "портов", "core", "ядер", "ядра", "u", "rpm", "об",
]
_UNIT_RE = "|".join(sorted((re.escape(u) for u in _UNITS), key=len, reverse=True))
_SPEC = re.compile(r"(\d+(?:[.,]\d+)?)\s*(%s)\b" % _UNIT_RE, re.I | re.U)

# Габариты вида 600x300x200 / 600х300х200 / 2500х1200 (2..3 числа через x/х/*/×).
# Хвостовой `\b` здесь не годится: в «3x0.75мм2» он заставлял регулярку откатиться до «3x0»,
# и дробная часть терялась (0.75 → 0), из-за чего сверка габаритов давала ложное расхождение.
# Нужен запрет на продолжение ЧИСЛА, а не на букву после него.
_DIM = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*[xх×*]\s*(\d+(?:[.,]\d+)?)"
                  r"(?:\s*[xх×*]\s*(\d+(?:[.,]\d+)?))?(?![\d.,]\d)", re.I | re.U)

# Синонимичные единицы приводим к одному ключу (чтобы «l» и «л» сравнивались как объём).
_UNIT_ALIAS = {
    "tb": "tb", "тб": "tb", "gb": "gb", "гб": "gb", "mb": "mb", "мб": "mb", "kb": "kb", "кб": "kb",
    "ghz": "ghz", "ггц": "ghz", "mhz": "mhz", "мгц": "mhz", "гц": "hz",
    "kw": "kw", "квт": "kw", "w": "w", "вт": "w", "va": "va", "ва": "va",
    "mah": "mah", "мач": "mah", "v": "v", "в": "v", "a": "a", "а": "a",
    "kg": "kg", "кг": "kg", "mg": "mg", "мг": "mg", "g": "g", "г": "g", "t": "t", "т": "t",
    "ml": "ml", "мл": "ml", "l": "l", "л": "l",
    "mm": "mm", "мм": "mm", "cm": "cm", "см": "cm", "m": "m", "м": "m",
    "pcs": "pcs", "шт": "pcs", "уп": "pack", "port": "port", "порт": "port", "портов": "port",
    "core": "core", "ядер": "core", "ядра": "core", "u": "u", "rpm": "rpm", "об": "rpm",
}


def _num(s: str) -> float:
    return float(str(s).replace(",", "."))


def extract_specs(text: str) -> list[dict]:
    """Список характеристик [{value, unit}] из текста (обобщённо, единицы как в тексте)."""
    if not text:
        return []
    specs: list[dict] = []
    seen = set()
    for m in _SPEC.finditer(str(text)):
        value = m.group(1).replace(",", ".")
        unit = m.group(2).lower()
        key = (value, unit)
        if key not in seen:
            seen.add(key)
            specs.append({"value": value, "unit": unit})
    return specs


def spec_index(text: str) -> dict[str, set]:
    """Характеристики как {нормализованная_единица: {значения(float)}} — для сравнения."""
    idx: dict[str, set] = {}
    for m in _SPEC.finditer(str(text or "")):
        unit = _UNIT_ALIAS.get(m.group(2).lower())
        if not unit:
            continue
        idx.setdefault(unit, set()).add(_num(m.group(1)))
    return idx


def dimensions(text: str) -> set:
    """Габаритные наборы (отсортированные кортежи) из текста: 600х300х200 → (200,300,600)."""
    out: set = set()
    for m in _DIM.finditer(str(text or "")):
        nums = [_num(g) for g in m.groups() if g]
        if len(nums) >= 2:
            out.add(tuple(sorted(nums)))
    return out
