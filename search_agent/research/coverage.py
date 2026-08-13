# -*- coding: utf-8 -*-
"""Покрытие периода: «есть ли на источнике данные за такие-то годы».

Это ФАКТ, а не мнение, поэтому считает код (правило проекта: арифметику и проверяемые факты
модели не отдаём — её дело формулировка и комментарий). Модель может сказать «данные есть за
2022–2025»; здесь мы сверяем это заявление с годами, реально встреченными в тексте страницы.

Универсально: никаких названий порталов и форматов конкретных реестров — только годы, даты и
диапазоны в тексте (правило universal-tool-any-file).
"""
import re
import time

from ..obs.log import get_logger

log = get_logger("research.coverage")

_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")
_DATE_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./]((?:19|20)\d{2})\b")
_ISO_RE = re.compile(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b")
_RANGE_RE = re.compile(r"\b((?:19|20)\d{2})\s*(?:-|–|—|по|до|\.\.)\s*((?:19|20)\d{2})\b", re.I | re.U)
_FROM_RE = re.compile(r"\b(?:с|начиная с|from|since)\s+((?:19|20)\d{2})\b", re.I | re.U)
_LAST_N_RE = re.compile(r"\bза\s+(?:последни[ей]|прошедши[ей])\s+(\d{1,2})\s*(?:год|года|лет)\b",
                        re.I | re.U)

# Строки, где год почти наверняка не про данные, а про оформление сайта.
_CHROME_RE = re.compile(r"©|copyright|все права защищены|cookie|политик[аи] конфиденциальн",
                        re.I | re.U)


def parse_period(text: str, *, now_year: int | None = None) -> tuple[int, int] | None:
    """Разобрать запрошенный период: «2022-2025», «с 2020», «за последние 3 года», «2023».

    Возвращает (год_от, год_до) или None, если период не указан — тогда проверять нечего.
    """
    t = (text or "").strip()
    if not t:
        return None
    year_now = now_year or time.localtime().tm_year
    m = _RANGE_RE.search(t)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return (min(a, b), max(a, b))
    m = _LAST_N_RE.search(t)
    if m:
        n = max(1, int(m.group(1)))
        return (year_now - n + 1, year_now)
    m = _FROM_RE.search(t)
    if m:
        return (int(m.group(1)), year_now)
    years = [int(y) for y in _YEAR_RE.findall(t)]
    if years:
        return (min(years), max(years))
    return None


def observed_period(text: str) -> dict:
    """Какие годы реально встречаются в тексте (и в каких строках).

    Годы из подвала/копирайта отбрасываем: «© 2026» не означает, что за 2026 есть данные.
    """
    years: dict[int, str] = {}
    dates: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or _CHROME_RE.search(line):
            continue
        found = False
        for m in _DATE_RE.finditer(line):
            dates.append(m.group(0))
            found = True
        for m in _ISO_RE.finditer(line):
            dates.append(m.group(0))
            found = True
        for y in _YEAR_RE.findall(line):
            year = int(y)
            years.setdefault(year, line[:200])
            found = True
        if found and len(dates) > 200:              # защита от гигантских выгрузок
            break
    ys = sorted(years)
    return {"years": ys, "min": ys[0] if ys else None, "max": ys[-1] if ys else None,
            "dates": dates[:20], "lines": years}


def coverage(observed: dict, requested: tuple[int, int] | None) -> dict:
    """Сопоставить наблюдаемые годы с запрошенным периодом.

    status: covered — все запрошенные годы встречены; partial — часть; absent — ни одного,
    хотя годы на странице есть; unknown — период не запрошен или годов на странице нет.
    """
    seen = list((observed or {}).get("years") or [])
    seen_str = ("%d–%d" % (seen[0], seen[-1])) if seen else ""
    if not requested:
        return {"status": "unknown", "missing": [], "seen": seen_str,
                "evidence": [], "reason": "период не запрошен"}
    lo, hi = requested
    want = list(range(lo, hi + 1))
    have = [y for y in want if y in set(seen)]
    missing = [y for y in want if y not in set(seen)]
    lines = (observed or {}).get("lines") or {}
    evidence = [lines[y] for y in have if y in lines][:5]
    if not seen:
        status = "unknown"
    elif not have:
        status = "absent"
    elif missing:
        status = "partial"
    else:
        status = "covered"
    return {"status": status, "missing": missing, "seen": seen_str,
            "evidence": evidence, "requested": "%d–%d" % (lo, hi)}


def describe(cov: dict) -> str:
    """Человеческая формулировка покрытия для наблюдения агенту и для UI."""
    st = (cov or {}).get("status")
    seen = (cov or {}).get("seen") or "—"
    if st == "covered":
        return "период покрыт полностью (на странице годы %s)" % seen
    if st == "partial":
        missing = ", ".join(str(y) for y in (cov.get("missing") or [])[:6])
        return "период покрыт частично (есть %s; не подтверждены годы: %s)" % (seen, missing)
    if st == "absent":
        return "за запрошенный период данных не видно (на странице годы %s)" % seen
    return "период подтвердить не удалось"
