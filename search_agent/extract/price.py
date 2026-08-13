# -*- coding: utf-8 -*-
"""Нормализация цены — чистые функции, БЕЗ модели (тестируемо оффлайн).

Парсит «330 ₽», «1 234,50 руб», «79.67», «от 415 р.»; определяет валюту (RUB/USD/EUR)
и НДС по тексту рядом. Универсально — не заточено под конкретный сайт/пример.
"""
import re

# Число с разделителями тысяч (обычный/неразрывный/узкий пробел) и десятичной частью (, или .).
# ВАЖНО: только ГОРИЗОНТАЛЬНЫЕ пробелы. С `\s` регулярка перепрыгивала перевод строки и склеивала
# соседние числа: «400\n\n23 990 руб.» читалось как 40023990, из-за чего ВЕРНАЯ цена не находилась
# в тексте страницы и отбрасывалась проверкой как выдуманная (поймано валидацией на HOLDOUT).
_H_SPACES = " \t\u00a0\u2007\u2009\u202f"
_NUM = re.compile(r"\d[\d%s]*(?:[.,]\d{1,2})?" % re.escape(_H_SPACES))

# ЕДИНАЯ ТАБЛИЦА ВАЛЮТ НА ВЕСЬ ПРОЕКТ. Раньше «что считать ценой» решали пять модулей
# независимо (fetch/clean, extract/inspect, extract/focus, extract/price, research/*), и они
# расходились: «5 697 р.» триаж видел, а отбор ценовых блоков — нет. Из-за этого на 122 страницах
# (замер по runs/fetch_cache) цена была невидима: блоки не отбирались, переспрос не запускался,
# а без структурной разметки страница отбрасывалась как «без единой цены» ещё до вызова модели.
# Любое изменение правил цены делается ЗДЕСЬ и нигде больше.
#
# Написания взяты из частотного замера по 3875 реальным страницам, а не придуманы.
#   strong — самодостаточный признак валюты: встречается где угодно в тексте;
#   near   — слабый признак («р.», «тг.»): засчитывается ТОЛЬКО вплотную к числу, иначе «р. Волга»
#            и «Иванов Р.» читались бы как рубли;
#   spaced — совсем слабый («р» без точки): нужен пробел между числом и буквой, иначе разрешение
#            видео «1080р», «720р», «2160р» превращается в цену (реальные страницы этого набора).
_CUR_TABLE: tuple[tuple[str, str, str, str], ...] = (
    # код,   strong,                                                      near,          spaced
    ("RUB", r"₽|\bруб(?![а-яё])|\bрубл[а-яё]*|\brub\b|\brur\b", r"р\.(?![а-яё])", r"р(?![а-яё\w])"),
    ("USD", r"\$|\busd\b|\bдолл[а-яё]*", r"", r""),
    ("EUR", r"€|\beur\b|\bевро\b", r"", r""),
    ("CNY", r"¥|\bcny\b|\bюан[а-яё]*", r"", r""),
    ("UZS", r"сум(?![а-яё])|сўм|\bso['’]m\b|\buzs\b", r"", r""),
    ("KZT", r"₸|\bтенге\b|\bkzt\b", r"тг\.(?![а-яё])", r""),
    ("UAH", r"₴|\bгрн(?![а-яё])|\buah\b", r"", r""),
    ("BYN", r"\bbyn\b|\bбел[\.\s]*руб|\bбелорус[а-яё]*[\s]+рубл[а-яё]*", r"", r""),
)
# «бел. руб» должен опознаваться как BYN раньше, чем как RUB, — порядок проверки задан отдельно.
_BYN_RUB = re.compile(r"\bбел[\.\s]*руб|\bбелорус[а-яё]*[\s]+рубл[а-яё]*", re.I | re.U)

_CUR_CODES = {"rub": "RUB", "rur": "RUB", "usd": "USD", "eur": "EUR", "cny": "CNY",
              "uzs": "UZS", "kzt": "KZT", "uah": "UAH", "byn": "BYN"}

# Символы валюты, которые пишут ПЕРЕД числом («₽ 1 200», «$500»).
_CUR_PREFIX = r"₽|\$|€|¥|₸|₴"
# Число вместе с типографским мусором вокруг разрядов: «*490* руб.», «(1 668) ₽».
_NUM_LOOSE = r"\d[\d%s.,*()]{0,15}" % re.escape(_H_SPACES)

_STRONG_ALL = "|".join(s for _, s, _, _ in _CUR_TABLE if s)
_NEAR_ALL = "|".join(n for _, _, n, _ in _CUR_TABLE if n)
_SPACED_ALL = "|".join(sp for _, _, _, sp in _CUR_TABLE if sp)

# Цена = число рядом с валютой. Единственное определение цены в проекте; им пользуются отбор
# ценовых блоков (extract/focus), триаж страницы (extract/inspect), очистка (fetch/clean) и
# исследовательский режим (research/*). Слэш поддержан ради «490 ₽/шт», «1 200 руб/м2».
PRICE_RE = re.compile(
    r"(?:%(num)s)\s*/?\s*(?:%(strong)s|%(near)s)"     # 490 ₽ | 5 697 р. | 1 200 руб/м2
    r"|(?:%(num)s)[%(sp)s]+(?:%(spaced)s)"            # 18 811 р  (пробел обязателен)
    r"|(?:%(pre)s)\s*\d" % {                          # ₽ 1 200 | $500
        "num": _NUM_LOOSE, "strong": _STRONG_ALL, "near": _NEAR_ALL,
        "spaced": _SPACED_ALL, "pre": _CUR_PREFIX,
        "sp": re.escape(_H_SPACES)},
    re.I | re.U)

_VAT_WITHOUT = re.compile(r"без\s*ндс|ндс\s*не\s*включ|excl\.?\s*vat", re.I)
_VAT_WITH = re.compile(r"(?:с|включая|в\s*т\.?\s*ч\.?|вкл\.?)\s*ндс|ндс\s*включ|incl\.?\s*vat", re.I)


def to_number(s) -> float | None:
    """Достать число из строки цены с учётом разделителей тысяч и десятичной , или ."""
    if s is None:
        return None
    m = _NUM.search(str(s))
    if not m:
        return None
    raw = re.sub(r"[\s  ]", "", m.group(0))
    if not raw:
        return None
    last_comma, last_dot = raw.rfind(","), raw.rfind(".")
    dec = max(last_comma, last_dot)
    if dec == -1:                                      # только цифры
        num = raw
    else:
        frac = raw[dec + 1:]
        if len(frac) in (1, 2):                        # 1-2 цифры после → десятичная часть
            num = re.sub(r"[.,]", "", raw[:dec]) + "." + frac
        else:                                          # иначе . / , — разделители тысяч
            num = re.sub(r"[.,]", "", raw)
    try:
        return float(num)
    except ValueError:
        return None


# Готовые проверки по единой таблице: сильный признак ищем где угодно, слабый — только рядом
# с числом (иначе «р. Волга» и «Иванов Р.» станут рублями).
_CUR_PATTERNS: tuple[tuple[str, "re.Pattern", "re.Pattern | None"], ...] = tuple(
    (code,
     re.compile(strong, re.I | re.U),
     re.compile(r"(?:%s)\s*/?\s*(?:%s)" % (_NUM_LOOSE, near) if near else "",
                re.I | re.U) if near else None)
    for code, strong, near, _sp in _CUR_TABLE)

# «р» без точки: слабейший признак, требует пробела между числом и буквой.
_SPACED_PATTERNS: tuple[tuple[str, "re.Pattern"], ...] = tuple(
    (code, re.compile(r"(?:%s)[%s]+(?:%s)" % (_NUM_LOOSE, re.escape(_H_SPACES), sp), re.I | re.U))
    for code, _s, _n, sp in _CUR_TABLE if sp)


def has_price(text: str) -> bool:
    """Есть ли в тексте число рядом с валютой — ЕДИНСТВЕННОЕ определение цены в проекте.

    Им пользуются отбор ценовых блоков, триаж страницы, очистка и исследовательский режим:
    расхождение между ними уже приводило к тихой потере цен (см. комментарий к _CUR_TABLE).
    """
    return bool(PRICE_RE.search(text or ""))


def price_tokens(text: str) -> list[str]:
    """Все ценовые метки текста (для оценки плотности цен в триаже страницы)."""
    return PRICE_RE.findall(text or "")


def currencies_on_page(text: str) -> set[str]:
    """Какие валюты РЕАЛЬНО упомянуты в тексте страницы.

    Нужно, чтобы поймать подмену валюты: модель по умолчанию пишет «RUB», хотя на странице
    узбекского магазина стоит «1 547 000 Сум». Пустое множество означает «валюта на странице не
    обозначена» — тогда ничего не доказано и вмешиваться нельзя.
    """
    s = text or ""
    found = set()
    for code, strong, near in _CUR_PATTERNS:
        if strong.search(s) or (near is not None and near.search(s)):
            found.add(code)
    for code, spaced in _SPACED_PATTERNS:
        if spaced.search(s):
            found.add(code)
    # «бел. руб», «белорусских рублей» — это BYN, а не рубль РФ: строка подходит под оба правила.
    if "BYN" in found and _BYN_RUB.search(s) and not re.search(r"₽|\brub\b|\brur\b", s, re.I):
        found.discard("RUB")
    return found


def numbers_in(text: str) -> set[float]:
    """Все числа, встречающиеся в тексте (с учётом разделителей тысяч и десятичной части).

    Нужно, чтобы проверить: названная моделью цена ДЕЙСТВИТЕЛЬНО есть на странице. Реальный
    случай — карточка магазина, где цена не отрисовалась, а модель всё равно вернула число
    «77 990» из соседнего блока и пометила его точным совпадением с уверенностью 1.0.
    """
    out: set[float] = set()
    for m in _NUM.finditer(text or ""):
        v = to_number(m.group(0))
        if v is not None:
            out.add(round(v, 2))
    return out


def is_on_page(value: float, page_numbers: set[float], *, tolerance: float = 0.01) -> bool:
    """Встречается ли значение среди чисел страницы (с допуском на округление копеек)."""
    if value is None:
        return False
    v = round(float(value), 2)
    if v in page_numbers:
        return True
    return any(abs(v - n) <= tolerance for n in page_numbers)


def detect_currency(text: str, default: str = "RUB") -> str:
    """Определить валюту по символам/словам в тексте. По умолчанию RUB (рынок РФ).

    Идёт по той же таблице, что и остальные проверки: отдельного списка написаний, который мог
    бы разойтись с общим, в проекте больше нет.
    """
    found = currencies_on_page(text or "")
    if not found:
        return default
    for code, _s, _n, _sp in _CUR_TABLE:              # порядок таблицы = приоритет
        if code in found:
            return code
    return default


def normalize_currency(code: str | None) -> str | None:
    """Привести код валюты (RUB/RUR/USD/…) к каноническому виду или None."""
    if not code:
        return None
    c = str(code).strip().lower()
    if c in _CUR_CODES:
        return _CUR_CODES[c]
    return detect_currency(c, default="") or None


def detect_vat(text: str) -> bool | None:
    """Есть ли пометка про НДС рядом: True (с НДС) / False (без НДС) / None (не сказано)."""
    low = text or ""
    if _VAT_WITHOUT.search(low):
        return False
    if _VAT_WITH.search(low):
        return True
    return None


def parse_price(raw, context: str = "", currency_hint: str | None = None) -> dict | None:
    """raw → {value, currency, vat} или None, если разумного числа нет.

    context — окружающий текст (для валюты/НДС), currency_hint — явный код (напр. из microdata).
    """
    value = to_number(raw)
    if value is None or value <= 0:
        return None
    text = "%s %s" % (raw, context)
    currency = normalize_currency(currency_hint) or detect_currency(text)
    return {"value": value, "currency": currency, "vat": detect_vat(text)}
