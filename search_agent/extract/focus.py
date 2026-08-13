# -*- coding: utf-8 -*-
"""Умный отбор ценовых блоков со страницы перед подачей в модель (скорость + фокус).

Проблема: markdown листинга — 20–40k символов, из которых бОльшая часть — меню/футер/описания/
отзывы БЕЗ цен. Тупая обрезка `markdown[:max_chars]` и медленна (модель читает всё), и рискует
срезать нужную карточку в хвосте. Здесь оставляем только релевантное:
  1) окна вокруг ЦЕН (название товара обычно строкой выше цены);
  2) из них — блоки, релевантные ИСКОМОМУ товару (перекрытие токенов запроса), в пределах лимита.

Универсально, БЕЗ привязки к категории (правило universal-tool-any-file): цена — по валютным
маркерам, релевантность — по общим токенам названия/парт-номера.

Фильтр РАБОТАЕТ ВСЕГДА (не только на больших страницах): боилерплейт срезаем стабильно. `max_chars`
— это ВЕРХНЯЯ СТРАХОВКА: она бьёт, только если ценовых блоков после фильтра всё равно больше лимита
(тогда берём самые релевантные запросу). В норме max_chars не срабатывает и на результат не влияет.
Если цен на странице не нашли — безопасный фолбэк: отдаём страницу как есть (усечённо), не выдумываем.
"""
import re

# Что считать ценой, решает ОДИН модуль на весь проект — extract/price.py. Своя регулярка здесь
# уже приводила к тихой потере: «5 697 р.» триаж видел, а этот отбор — нет, из-за чего ценовые
# блоки не отбирались и переспрос после пустого ответа модели не запускался.
from .price import has_price, is_on_page, numbers_in     # noqa: F401 — has_price ре-экспортируется

_WORD_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9]+", re.U)
_HEADING_RE = re.compile(r"^\s*[*+-]?\s*#{1,6}\s")               # markdown-заголовок (в т.ч. в списке)


def _query_tokens(item) -> set:
    name = getattr(item, "name", "") or ""
    pn = getattr(item, "part_number", "") or ""
    return {t.lower() for t in _WORD_RE.findall(name + " " + str(pn)) if len(t) >= 2}


def _content_lines(markdown: str) -> list[str]:
    """Непустые строки (схлопываем пустые — они раздувают окна и не несут смысла)."""
    return [s for ln in (markdown or "").splitlines() if (s := ln.strip())]


def _segments(lines: list[str], price_idx: list[int], before: int, after: int) -> list[tuple]:
    """Окна вокруг ценовых строк, слитые по пересечению → список (start, end)."""
    windows = sorted((max(0, i - before), min(len(lines), i + after + 1)) for i in price_idx)
    out = []
    cs, ce = windows[0]
    for s, e in windows[1:]:
        if s <= ce:
            ce = max(ce, e)
        else:
            out.append((cs, ce)); cs, ce = s, e
    out.append((cs, ce))
    return out


def _segments_size(lines: list[str], segments: list[tuple]) -> int:
    """Во сколько символов обойдётся такой набор сегментов (с разделителями)."""
    return sum(len("\n".join(lines[s:e])) + 6 for s, e in segments)


def select_pricing_blocks(markdown, item, max_chars: int, *, before: int = 20, after: int = 20):
    """Вернуть (focused_text, stats). Фильтр РАБОТАЕТ ВСЕГДА: оставляет окна вокруг цен + заголовок,
    безценовой боилерплейт срезает. max_chars — ВЕРХНЯЯ СТРАХОВКА: режет, только если после фильтра
    ценовых блоков всё равно больше лимита (тогда берём самые релевантные запросу).
    """
    lines = _content_lines(markdown)
    stats = {"in_chars": len(markdown or ""), "lines": len(lines), "price_lines": 0,
             "segments_total": 0, "segments_kept": 0, "out_chars": 0,
             "fallback": False, "trimmed": False}

    price_idx = [i for i, ln in enumerate(lines) if has_price(ln)]
    stats["price_lines"] = len(price_idx)
    if not price_idx:                                   # цен нет — отдаём как есть (усечённо), не выдумываем
        full = "\n".join(lines)
        stats["fallback"] = True
        stats["out_chars"] = min(len(full), max_chars)
        return full[:max_chars], stats

    # Окна вокруг каждой цены → сегменты. Размер окна подобран ЗАМЕРОМ на 2236 ответах модели,
    # где известны и цена, и дословное название найденной позиции: замерено, на сколько строк они
    # разнесены на живых страницах. Название выше цены в 85% случаев (99% укладываются в 23 строки),
    # ниже — в 8% (99% укладываются в 33 строки), поэтому окно симметричное, а не «больше вверх»:
    # редкий случай «название под ценой» оказался как раз самым дальним.
    # Покрытие/цена по замеру: 5/2 — 86% названий и 460 ₽ за прогон, 12/8 — 96% и 607 ₽,
    # 20/20 — 98.9% и 768 ₽, 40/16 — 99.4% и 838 ₽. Взято 20/20: дальше доплата идёт за десятые
    # доли процента, а лишний текст вокруг цены повышает риск, что модель возьмёт соседний товар.
    #
    # На плотном листинге (сотни цен подряд) такие окна слились бы в целую страницу и свели бы
    # отбор на нет — поэтому если итог не помещается в бюджет, окно ступенчато сужается.
    target = max_chars * 0.6
    segments, narrow = _segments(lines, price_idx, before, after), 1
    for div in (2, 4, 8):
        if _segments_size(lines, segments) <= target:
            break
        narrow = div
        segments = _segments(lines, price_idx, max(1, before // div), max(1, after // div))
    stats["segments_total"] = len(segments)
    stats["window_narrowed"] = narrow                   # 1 — окно полное, >1 — во сколько раз сужено

    # Заголовок страницы (первый markdown-заголовок вверху) — глобальный контекст (город/категория).
    title = next((ln for ln in lines[:8] if _HEADING_RE.match(ln)), "")
    seg_text = ["\n".join(lines[s:e]) for s, e in segments]
    total = sum(len(t) + 6 for t in seg_text) + (len(title) + 1 if title else 0)

    if total <= max_chars:
        chosen = set(range(len(segments)))              # обычный случай: все ценовые блоки влезли
    else:
        # Только если ПОСЛЕ фильтра всё равно много — ранжируем по релевантности и берём топ до лимита.
        stats["trimmed"] = True
        qtok = _query_tokens(item)

        def score(k):
            s, e = segments[k]
            toks = set(_WORD_RE.findall(" ".join(lines[s:e]).lower()))
            return (len(qtok & toks), sum(1 for j in range(s, e) if has_price(lines[j])))

        budget = max_chars - (len(title) + 1 if title else 0)
        chosen, used = set(), 0
        for k in sorted(range(len(segments)), key=score, reverse=True):
            size = len(seg_text[k]) + 6
            if chosen and used + size > budget:
                continue
            chosen.add(k); used += size
            if used >= budget:
                break

    parts = [title] if title else []                    # собираем в ИСХОДНОМ порядке, помечая пропуски
    prev_end = None
    for k in sorted(chosen):
        s, e = segments[k]
        if prev_end is not None and s > prev_end:
            parts.append("[…]")
        parts.append(seg_text[k])
        prev_end = e
    focused = "\n".join(parts)[:max_chars]
    stats["segments_kept"] = len(chosen)
    stats["out_chars"] = len(focused)
    return focused, stats


def focus_pricing(markdown, item, max_chars: int, *, before: int = 5, after: int = 2) -> str:
    """Отобрать ценовые блоки страницы под искомый товар (обёртка над select_pricing_blocks)."""
    return select_pricing_blocks(markdown, item, max_chars, before=before, after=after)[0]


def _hint_in_text(text: str, hint_values) -> bool:
    """Встречается ли в тексте число из структурной ценовой метки (см. looks_answerable)."""
    if not hint_values:
        return False
    from .price import is_on_page, numbers_in
    page_numbers = numbers_in(text)
    return any(is_on_page(v, page_numbers) for v in hint_values)


def looks_answerable(text: str, item, *, min_overlap: float = 0.4, hint_values=()) -> bool:
    """Похоже ли, что ответ на странице ЕСТЬ: цена рядом с признаками искомого предмета.

    Детерминированная проверка после пустого ответа модели: если в отправленном ей тексте есть и
    цена, и заметные следы искомого (дословный артикул или значимая доля слов названия), то пустой
    ответ подозрителен и имеет смысл переспросить короче. Без модели и без привязки к категории.

    hint_values — цены из структурной разметки страницы. Нужны потому, что цену пишут и без знака
    валюты рядом («20 130» отдельной строкой): текстовый признак её не видит, а метка доказывает,
    что это цена. Совпадение засчитываем только если само число встречается в тексте.
    """
    if not text:
        return False
    if not any(has_price(ln) for ln in text.splitlines()) and not _hint_in_text(text, hint_values):
        return False
    pn = str(getattr(item, "part_number", "") or "").strip()
    if pn and pn.lower() in text.lower():
        return True
    toks = {t.lower() for t in _WORD_RE.findall(getattr(item, "name", "") or "") if len(t) >= 3}
    if not toks:
        return False
    hay = {t.lower() for t in _WORD_RE.findall(text)}
    return sum(1 for t in toks if t in hay) / len(toks) >= min_overlap
