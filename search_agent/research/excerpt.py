# -*- coding: utf-8 -*-
"""Выжимка страницы под вопрос пользователя (компактный контент для диалога).

Зачем отдельно от `extract/focus.py`: тот отбирает окна вокруг ЦЕН — он и должен оставаться
таким, на нём стоит рабочий движок поиска цен. В чате же вопрос произвольный («какие отзывы»,
«за какие годы есть данные», «нужна ли регистрация»), и релевантность считается по словам
вопроса, а не по валютным маркерам.

Принцип тот же и проверенный: окна вокруг релевантных строк → слияние пересечений → сборка в
исходном порядке с пометкой пропусков `[…]`. Тупая обрезка `markdown[:N]` не годится — нужное
обычно не в начале страницы. Универсально, без привязки к теме (правило universal-tool-any-file).
"""
import re

_WORD_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9]+", re.U)
_HEADING_RE = re.compile(r"^\s*[*+-]?\s*#{1,6}\s")

# Слишком общие слова: они есть на любой странице и «релевантны» всему, поэтому как признак
# бесполезны. Список короткий и намеренно тематически нейтральный.
_STOP = {"и", "в", "на", "по", "с", "за", "для", "или", "the", "of", "a", "to",
         "что", "как", "это", "есть", "у", "от", "до", "не", "мне", "нужно", "найди"}

from ..extract.price import PRICE_RE                       # единое определение цены

_MARKER_RE = re.compile(
    r"%s"                                                 # цена
    r"|\b(?:19|20)\d{2}\b"                                # год
    r"|\b\d{1,2}[./]\d{1,2}[./](?:19|20)?\d{2}\b" % PRICE_RE.pattern,   # дата
    re.I | re.U)


def query_tokens(query: str) -> set:
    """Значимые слова вопроса (нижний регистр, без стоп-слов и однобуквенных)."""
    return {t.lower() for t in _WORD_RE.findall(query or "")
            if len(t) >= 3 and t.lower() not in _STOP}


def _content_lines(markdown: str) -> list[str]:
    return [s for ln in (markdown or "").splitlines() if (s := ln.strip())]


def _relevance(line: str, tokens: set) -> int:
    """Насколько строка отвечает вопросу: совпавшие слова + бонус за цену/дату."""
    if not line:
        return 0
    words = {t.lower() for t in _WORD_RE.findall(line)}
    hits = len(tokens & words) if tokens else 0
    return hits * 2 + (1 if _MARKER_RE.search(line) else 0)


def select_blocks(markdown: str, query: str, max_chars: int, *,
                  before: int = 3, after: int = 3) -> tuple[str, dict]:
    """Вернуть (выжимка, статистика). Пусто по релевантности — отдаём начало страницы.

    Фолбэк осознанный: лучше показать модели начало реальной страницы, чем ничего — она сама
    скажет, что ответа тут нет. Выдумывать содержимое мы не имеем права.
    """
    lines = _content_lines(markdown)
    stats = {"in_chars": len(markdown or ""), "lines": len(lines),
             "hit_lines": 0, "segments": 0, "out_chars": 0, "fallback": False}
    if not lines:
        return "", stats

    tokens = query_tokens(query)
    scored = [(i, _relevance(ln, tokens)) for i, ln in enumerate(lines)]
    hits = [i for i, s in scored if s > 0]
    stats["hit_lines"] = len(hits)
    if not hits:
        stats["fallback"] = True
        out = "\n".join(lines)[:max_chars]
        stats["out_chars"] = len(out)
        return out, stats

    windows = sorted((max(0, i - before), min(len(lines), i + after + 1)) for i in hits)
    segments = []
    cs, ce = windows[0]
    for s, e in windows[1:]:
        if s <= ce:
            ce = max(ce, e)
        else:
            segments.append((cs, ce)); cs, ce = s, e
    segments.append((cs, ce))
    stats["segments"] = len(segments)

    title = next((ln for ln in lines[:8] if _HEADING_RE.match(ln)), "")
    seg_text = ["\n".join(lines[s:e]) for s, e in segments]
    weight = {k: sum(_relevance(lines[j], tokens) for j in range(*segments[k]))
              for k in range(len(segments))}

    budget = max_chars - (len(title) + 1 if title else 0)
    chosen, used = set(), 0
    for k in sorted(range(len(segments)), key=lambda k: weight[k], reverse=True):
        size = len(seg_text[k]) + 6
        if chosen and used + size > budget:
            continue
        chosen.add(k); used += size
        if used >= budget:
            break

    parts = [title] if title else []
    prev_end = None
    for k in sorted(chosen):
        s, e = segments[k]
        if prev_end is not None and s > prev_end:
            parts.append("[…]")
        parts.append(seg_text[k])
        prev_end = e
    out = "\n".join(parts)[:max_chars]
    stats["out_chars"] = len(out)
    return out, stats


def focus_text(markdown: str, query: str, max_chars: int = 3000, *,
               before: int = 3, after: int = 3) -> str:
    """Выжимка страницы под вопрос (обёртка над select_blocks)."""
    return select_blocks(markdown, query, max_chars, before=before, after=after)[0]


def normalize_line(text: str) -> str:
    """Строка в сравнимом виде: схлопнутые пробелы, нижний регистр.

    Нужна для проверки, что цитата модели ДОСЛОВНО есть на странице (см. research/verify.py).
    """
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def quote_on_page(quote: str, page_text: str, *, min_len: int = 12) -> bool:
    """Есть ли цитата на странице (после нормализации пробелов/регистра).

    Слишком короткие «цитаты» не проверяемы — совпадут случайно; их считаем неподтверждёнными.
    """
    q = normalize_line(quote)
    if len(q) < min_len:
        return False
    return q in normalize_line(page_text)
