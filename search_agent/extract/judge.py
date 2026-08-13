# -*- coding: utf-8 -*-
"""Код-судья строгости «аналога» (детерминированно, БЕЗ модели).

Проблема (из практики): слабая модель (Qwen 9b / серверный агент) ВИДИТ измеримое отличие и
пишет о нём в пояснении, но всё равно метит позицию «аналогом». Правило пользователя: аналог —
только при совпадении ВСЕХ ключевых характеристик (отличие фасовки/объёма/размера/класса → не
подходит). Здесь код сверяет ИЗМЕРИМЫЕ характеристики искомого и найденного (через input.specs) и
отклоняет позицию при доказанном расхождении — не заменяя модель, а дополняя её как страховка.

Универсально (правило universal-tool-any-file): опирается только на «число+единица» и габариты,
без списков категорий/брендов. Срабатывает лишь при ПОЛОЖИТЕЛЬНОМ доказательстве расхождения
(общая единица есть у обоих, но значения не пересекаются) — иначе доверяем решению модели.
"""
from ..input.specs import dimensions, spec_index
from ..obs.log import get_logger

log = get_logger("extract.judge")


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol * max(abs(a), abs(b), 1e-9)


def _sets_overlap(sv: set, fv: set, tol: float) -> bool:
    return any(_close(a, b, tol) for a in sv for b in fv)


def _dims_overlap(sd: set, fd: set, tol: float) -> bool:
    for s in sd:
        for f in fd:
            if len(s) == len(f) and all(_close(x, y, tol) for x, y in zip(s, f)):
                return True
    return False


def _fmt(vals: set) -> str:
    return "/".join("%g" % v for v in sorted(vals))


def _fmt_dims(dims: set) -> str:
    return "; ".join("x".join("%g" % v for v in d) for d in sorted(dims))


def contradiction(sought: str, found: str, tolerance: float = 0.02) -> str | None:
    """Есть ли доказанное измеримое расхождение искомого и найденного. Причина или None.

    Сверяем только ОБЩИЕ единицы (есть у обоих) и габариты. Отсутствие характеристики у одной
    из сторон — НЕ расхождение (нечего сверять): не отклоняем по незнанию.
    """
    si, fi = spec_index(sought), spec_index(found)
    for unit in set(si) & set(fi):
        if not _sets_overlap(si[unit], fi[unit], tolerance):
            return "различается характеристика %s: искомое %s ≠ найдено %s" % (
                unit, _fmt(si[unit]), _fmt(fi[unit]))
    sd, fd = dimensions(sought), dimensions(found)
    if sd and fd and not _dims_overlap(sd, fd, tolerance):
        return "различаются габариты: искомое %s ≠ найдено %s" % (_fmt_dims(sd), _fmt_dims(fd))
    return None


def _pn_key(s: str) -> str:
    """Нормализованный артикул для сравнения: без пробелов, дефисов и регистра."""
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def judge_match(sought_name: str, found_desc: str, model_match: str,
                *, tolerance: float = 0.02, part_number: str | None = None) -> tuple[str | None, str]:
    """Проверить соответствие найденной позиции искомой по измеримым характеристикам.

    Возвращает (итоговый_match | None, причина). None = отклонить (несовместимо: разная
    фасовка/объём/размер/класс). Иначе сохраняем match модели (доказательств против нет).

    Дословно совпавший артикул НЕ перебивается сверкой характеристик: это сильнейший признак
    того же предмета, а разбор «числа с единицей» на живых названиях ошибается (сечение кабеля
    «3х0.75» выглядит как габариты). Раньше из-за этого отсеивалась верная позиция с точным
    артикулом.
    """
    if not (found_desc or "").strip():
        return model_match, ""                     # нечего сверять — доверяем модели
    pn = _pn_key(part_number)
    if len(pn) >= 5 and pn in _pn_key(found_desc):
        return model_match, "артикул совпал дословно — сверка характеристик не применяется"
    reason = contradiction(sought_name or "", found_desc, tolerance)
    if reason:
        return None, reason
    return model_match, ""
