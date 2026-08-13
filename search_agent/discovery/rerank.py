# -*- coding: utf-8 -*-
"""Реранк кандидатов ДО загрузки: рейтинг источника + совпадение с товаром.

`sources.yaml` — рейтинг/приоритет, НЕ белый список: незнакомый домен получает
`default_weight` и НЕ исключается; blacklist → 0 (отбрасываем). Дедуп по URL.
"""
import re

from ..models import Candidate

_WORD = re.compile(r"\w+", re.U)


def source_weight(domain: str, sources: dict) -> tuple[float, int]:
    """(вес, тир) домена по sources.yaml. Незнакомый → default_weight/тир 0; blacklist → 0."""
    domain = (domain or "").lower()
    default = float(sources.get("default_weight", 0.3))
    if domain in set(sources.get("blacklist") or []):
        return 0.0, 0
    for tier, info in (sources.get("tiers") or {}).items():
        for d in (info.get("domains") or []):
            d = str(d).lower()
            if domain == d or domain.endswith("." + d) or d in domain:
                return float(info.get("weight", default)), int(tier)
    return default, 0


def name_tokens(name: str) -> set[str]:
    """Значимые слова названия (от 3 символов). Пусто — сравнивать не с чем."""
    return {t for t in _WORD.findall((name or "").lower()) if len(t) > 2}


def relevance(name: str, title: str = "", snippet: str = "", url: str = "") -> float:
    """Доля значимых слов названия, встретившихся в заголовке/сниппете/адресе кандидата (0..1).

    Используется дважды: как слагаемое ранга и как ГЕЙТ перед загрузкой (`agent/run.py`).
    Детерминированно и без модели — решение воспроизводимо и проверяемо.

    Внимание: 0 возвращается и когда совпадений нет, и когда сравнивать не с чем (у названия нет
    значимых слов). Для гейта эти случаи РАЗНЫЕ — там сначала проверяется `name_tokens`.
    """
    toks = name_tokens(name)
    if not toks:
        return 0.0
    hay = ("%s %s %s" % (title, snippet, url)).lower()
    return sum(1 for t in toks if t in hay) / len(toks)


def _overlap(name: str, cand: Candidate) -> float:
    return relevance(name, cand.title, cand.snippet, cand.url)


def rerank(name: str, part_number: str | None, candidates: list[Candidate],
           sources: dict, top_n: int = 0) -> list[Candidate]:
    """Дедуплицировать выдачу и проставить кандидатам вес/тир/ранг.

    По умолчанию (top_n <= 0) НИКОГО НЕ ВЫБРАСЫВАЕМ и не сортируем: рассматриваем всю выдачу в
    исходном порядке. Заблокированные отсеются сами при загрузке и до модели не дойдут.
    Ранжирование нужно ровно для одного — решить, кого выбросить при обрезке; когда обрезки нет,
    оно бессмысленно. top_n > 0 — аварийный тормоз (дорого/долго): сортировка по рангу и топ-N.
    Вес/тир проставляем всегда: их использует свод (adjudicate) и показывает UI.
    """
    by_url: dict[str, Candidate] = {}
    for c in candidates:
        key = c.url.split("#")[0]
        if key in by_url:
            continue
        w, tier = source_weight(c.domain, sources)
        if w <= 0:
            continue                       # чёрный список — отбрасываем
        ov = _overlap(name, c)
        pn = 0.0
        if part_number:
            hay = (c.title + " " + c.snippet + " " + c.url).lower()
            pn = 1.0 if str(part_number).lower() in hay else 0.0
        c.weight = round(w, 3)
        c.tier = tier
        c.score = round(w * 0.5 + ov * 0.4 + pn * 0.3, 4)
        by_url[key] = c
    if top_n and top_n > 0:                # аварийный лимит: только тогда есть смысл ранжировать
        return sorted(by_url.values(), key=lambda c: c.score or 0, reverse=True)[:top_n]
    return list(by_url.values())           # вся выдача, порядок поисковика
