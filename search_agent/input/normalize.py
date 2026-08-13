# -*- coding: utf-8 -*-
"""Нормализация наименований — универсально, без привязки к категории товара.

Чистка имени, извлечение кандидатов парт-номера (обобщённый regex кодов),
транслитерация RU↔EN для вариантов запроса. Определение бренда/модели для
произвольных товаров надёжнее делать моделью (query_planner, M2+) — здесь только
детерминированная подготовка.
"""
import re

_WS = re.compile(r"\s+", re.U)

# Кандидат парт-номера/кода: токен с буквами И цифрами (или явные коды с дефисами),
# длиной >= 4, не чисто число. Обобщённо, без списка вендоров.
_CODE = re.compile(r"\b(?=[A-Za-zА-Яа-я0-9]*\d)(?=[A-Za-zА-Яа-я0-9]*[A-Za-zА-Яа-я])"
                   r"[A-Za-z0-9][A-Za-z0-9\-_./]{3,}\b")

# Минимальная таблица транслитерации (для формирования EN-варианта запроса).
_RU2LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def clean_name(name: str) -> str:
    """Убрать лишние пробелы/переносы, привести к аккуратной строке."""
    if not name:
        return ""
    return _WS.sub(" ", str(name).replace("\n", " ").strip())


def extract_codes(*texts: str) -> list[str]:
    """Кандидаты парт-номеров/кодов из имени и/или явного поля (по убыванию длины)."""
    found: list[str] = []
    for t in texts:
        if not t:
            continue
        for m in _CODE.finditer(str(t)):
            code = m.group(0).strip(".-_/")
            if code and code not in found:
                found.append(code)
    return sorted(found, key=len, reverse=True)


def transliterate(text: str) -> str:
    """Грубая RU→LAT транслитерация (для EN-варианта запроса)."""
    out = []
    for ch in str(text):
        low = ch.lower()
        rep = _RU2LAT.get(low)
        if rep is None:
            out.append(ch)
        else:
            out.append(rep.upper() if ch.isupper() else rep)
    return "".join(out)
