# -*- coding: utf-8 -*-
"""Сборка поискового запроса из позиции. Запрос собирает КОД, а не модель.

Один запрос на позицию, в нём всё, что о ней известно:

    <название> + <артикул/код, если он есть в позиции> + <аффикс пользователя>

Название — либо разобранное моделью (`name_clean`, см. `query_llm.py`), либо очищенное правилами.
Аффикс — единая фраза-уточнение пользователя («купить в Ростове», «оптом», «прайс»); она
приклеивается ЗДЕСЬ, поэтому не теряется, когда название готовит модель.

Почему один запрос, а не несколько. Замер на живой выдаче: добавление кода к названию её не
портит даже когда код внутренний и бессмысленный (поисковик просто не учитывает неизвестный
токен), а настоящий артикул выдачу улучшает. Отдельный запрос «голым артикулом» лишь тратил
половину бюджета обращений к поиску. Глубину выдачи наращиваем страницами, а не вариантами запроса.
"""
import re

from ..input.normalize import clean_name
from ..models import Item

# Символы-операторы поисковиков (кавычки и т.п.): у Serper free-tier кавычки → HTTP 400
# «Query pattern not allowed». Чистим, чтобы запрос был обычным текстом.
_OPERATORS = re.compile(r'["\'`«»„“”]+')

DEFAULT_AFFIX = "цена купить"


def _sanitize(q: str) -> str:
    return re.sub(r"\s+", " ", _OPERATORS.sub(" ", q)).strip()


def build_query(name: str, part_number=None, affix: str | None = None) -> str:
    """Собрать одну поисковую строку из названия, артикула и аффикса (в этом порядке)."""
    parts = [_sanitize(name or "")]
    pn = _sanitize(str(part_number or ""))
    if pn and pn.lower() not in parts[0].lower():     # не дублируем код, если он уже в названии
        parts.append(pn)
    phrase = _sanitize(affix if affix is not None else DEFAULT_AFFIX)
    if phrase:
        parts.append(phrase)
    return _sanitize(" ".join(p for p in parts if p))


def keep_identity(base: str, parsed: dict | None) -> str:
    """Вернуть в название то, что опознаёт товар: производителя и обозначение модели.

    Модель, разбирая позицию, склонна принять бренд в начале наименования за служебный префикс и
    выбросить его: «DELTA Аккумулятор Delta DTM 1212» → «Аккумулятор DTM 1212», «Клавиатура и мышь
    Oklick 630M» → «Клавиатура и мышь проводная 630M». В поиске это разные товары. Промпт просит
    бренд сохранять, но просьба — не гарантия: `brand` и `model` модель отдаёт отдельными полями,
    и код возвращает их на место сам, если в названии их не осталось.
    """
    lost = []
    for key in ("brand", "model"):
        value = _sanitize(str((parsed or {}).get(key) or ""))
        if value and value.lower() not in base.lower():
            lost.append(value)
    if not lost:
        return _sanitize(base)
    # Марку ставим В НАЧАЛО, как её пишут в магазинах («Rockwool Лайт Баттс минеральная вата»):
    # первые слова запроса весят больше, а именно они и отличают товар от категории.
    return _sanitize("%s %s" % (" ".join(lost), base))


def plan_queries(item: Item, affix: str | None = None, max_queries: int = 1,
                 name_clean: str | None = None, parsed: dict | None = None) -> list[str]:
    """Запросы по товару. По умолчанию РОВНО ОДИН — название + артикул + аффикс.

    parsed — разбор позиции моделью целиком ({name_clean, brand, model, is_generic}); из него
    берётся название и гарантия, что производитель и модель не потерялись (`keep_identity`).
    name_clean оставлен отдельным параметром для вызовов, у которых разбора нет.
    max_queries > 1 — аварийный запас: добавляет вариант без аффикса (иногда уточнение
    пользователя слишком сужает выдачу). Больше двух вариантов не строим — глубину даёт пагинация.
    """
    parsed = parsed or {}
    base = _sanitize(name_clean or parsed.get("name_clean") or "") or _sanitize(clean_name(item.name))
    base = keep_identity(base, parsed)
    main = build_query(base, item.part_number, affix)

    out = [main]
    if max_queries > 1:
        fallback = build_query(base, item.part_number, affix="")
        if fallback and fallback.lower() != main.lower():
            out.append(fallback)
    return out[:max(1, max_queries)]
