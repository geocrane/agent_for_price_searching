# -*- coding: utf-8 -*-
"""Политика триажа (§8/§9): что делать после осмотра страницы. Чистая функция, тестируема.

Решение зависит от типа страницы (inspect.kind) и остатка бюджета на позицию:
  blocked/empty/shell → escalate (если есть бюджет), иначе сдаёмся (причина проставится при своде);
  listing       → find_link (если есть бюджет) — зайти в карточку; иначе пробуем извлечь из листинга;
  card/other    → extract (извлекаем цену моделью).

`shell` — оболочка одностраничного приложения: страница отдалась, но товара в ней нет. Модель
звать бессмысленно (там нечего читать), а вот эскалация на служебный endpoint маркетплейса —
единственный путь, который может дать данные, поэтому обрабатываем как блокировку.
"""


def plan_after_inspect(insp: dict, *, can_escalate: bool, can_find_link: bool) -> list[str]:
    kind = (insp or {}).get("kind")
    if kind in ("blocked", "empty", "shell"):
        return ["escalate"] if can_escalate else []
    if kind == "listing":
        return ["find_link"] if can_find_link else ["extract"]
    return ["extract"]                       # card / other
