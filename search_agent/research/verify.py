# -*- coding: utf-8 -*-
"""Проверка источника моделью — с обязательной верификацией её ответа кодом.

Требование пользователя: разведка источников должна ПРОВЕРЯТЬСЯ моделью, а не быть пересказом
поисковой выдачи. Модель открывает текст источника и отвечает, есть ли там нужные данные и за
какой период. Но верить ей на слово нельзя — она охотно «подтверждает» ожидаемое. Поэтому:

  * модель обязана привести ДОСЛОВНЫЕ строки-доказательства со страницы;
  * код проверяет каждую строку на фактическое присутствие в тексте (тот же приём, что
    `extract/price.is_on_page` против выдуманных цен);
  * не осталось доказательств — вывод понижается до «не подтверждено»;
  * заявленный период сверяется с годами, реально встреченными на странице;
  * «открытый доступ» перебивается кодом, если на странице видны платность или регистрация.

Модель здесь формулирует, код — решает. Это тот же принцип, по которому итог позиции в
пайплайне считает `adjudicate`, а не модель.
"""
from .coverage import coverage, observed_period, parse_period
from .excerpt import quote_on_page
from .probe import ACCESS_OPEN, ACCESS_PAID, ACCESS_REG, detect_access
from .prompts import VERIFY_SYSTEM, verify_user
from ..llm.json_utils import extract_json
from ..obs.log import get_logger

log = get_logger("research.verify")

_HAS_DATA = {"да", "частично", "нет", "неизвестно"}
_ACCESS = {ACCESS_OPEN, ACCESS_REG, ACCESS_PAID}
_ACCESS_RANK = {ACCESS_OPEN: 0, ACCESS_REG: 1, ACCESS_PAID: 2}


def build_messages(need: str, period: str | None, url: str, title: str, excerpt: str,
                   links: list[str] | None = None) -> list[dict]:
    return [{"role": "system", "content": VERIFY_SYSTEM},
            {"role": "user", "content": verify_user(need, period, url, title, excerpt, links)}]


def parse_verdict(raw: str) -> dict:
    """Разобрать ответ модели в нормализованный словарь (с защитой от кривого JSON)."""
    data = extract_json(raw or "")
    if not isinstance(data, dict):
        log.info("Проверка источника: ответ модели не разобран как JSON")
        return {"has_data": "неизвестно", "evidence": [], "note": "ответ модели не разобран"}
    has = str(data.get("has_data") or "неизвестно").strip().lower()
    if has not in _HAS_DATA:
        has = "неизвестно"
    access = str(data.get("access") or ACCESS_OPEN).strip().lower()
    if access not in _ACCESS:
        access = ACCESS_OPEN
    ev = [str(x).strip() for x in (data.get("evidence") or []) if str(x).strip()]
    kinds = [str(x).strip() for x in (data.get("kinds") or []) if str(x).strip()]
    export = [str(x).strip().lower() for x in (data.get("export") or []) if str(x).strip()]
    return {"has_data": has, "kinds": kinds[:8], "export": export[:6], "access": access,
            "period_from": _int_or_none(data.get("period_from")),
            "period_to": _int_or_none(data.get("period_to")),
            "how_to": str(data.get("how_to") or "").strip()[:400],
            "limits": str(data.get("limits") or "").strip()[:300],
            "note": str(data.get("note") or "").strip()[:300],
            "confidence": _float_or_none(data.get("confidence")),
            "evidence": ev[:10]}


def verify_claims(parsed: dict, page_text: str, *, period_request: str | None = None,
                  blocked: str | None = None) -> dict:
    """Проверить вывод модели по тексту страницы. Возвращает уточнённый вывод + отчёт проверки.

    Ключевой шаг разведки: без него скил вернул бы пересказ, а не проверенный факт.
    """
    ev_ok, ev_bad = [], []
    for quote in parsed.get("evidence") or []:
        (ev_ok if quote_on_page(quote, page_text) else ev_bad).append(quote)

    out = dict(parsed)
    out["evidence"] = ev_ok
    out["rejected_evidence"] = ev_bad
    notes = []

    if ev_bad:
        log.info("Проверка источника: %d цитат нет на странице — отброшены", len(ev_bad))
        notes.append("%d цитат(ы) модели не найдено на странице" % len(ev_bad))

    if not ev_ok and out.get("has_data") in ("да", "частично"):
        out["has_data"] = "неизвестно"
        notes.append("вывод не подтверждён ни одной цитатой со страницы")

    # Период: доверяем тому, что реально видно в тексте, а не заявлению модели.
    observed = observed_period(page_text)
    requested = parse_period(period_request or "")
    cov = coverage(observed, requested)
    claimed = _claimed_period(out)
    if claimed and observed["years"]:
        lo, hi = claimed
        unseen = [y for y in range(lo, hi + 1) if y not in set(observed["years"])]
        if len(unseen) == (hi - lo + 1):
            notes.append("заявленный моделью период %d–%d на странице не подтверждается" % (lo, hi))
            if out.get("has_data") == "да":
                out["has_data"] = "частично"
    out["period"] = cov

    # Доступ: код видит платность/регистрацию надёжнее — берём более строгую оценку.
    code_access, access_ev = detect_access(page_text, blocked=blocked)
    if _ACCESS_RANK.get(code_access, 0) > _ACCESS_RANK.get(out.get("access"), 0) or blocked:
        if out.get("access") != code_access:
            notes.append("доступ уточнён по странице: %s" % code_access)
        out["access"] = code_access
        for line in access_ev:
            if line not in out["evidence"]:
                out["evidence"].append(line)

    out["check_notes"] = notes
    if notes:
        log.info("Проверка источника: %s", "; ".join(notes))
    return out


async def verify_source(llm_client, *, need: str, period: str | None, url: str, title: str,
                        excerpt: str, page_text: str, links=None, model=None,
                        blocked: str | None = None, executor=None) -> dict:
    """Полный цикл: спросить модель → разобрать → проверить кодом.

    Без модели (или при сбое транспорта) честно возвращаем «не подтверждено» — молча выдавать
    непроверенный источник за проверенный нельзя (правило «ничего не произошло» недопустимо).
    """
    if llm_client is None:
        log.warning("Проверка источника %s пропущена: модель недоступна", url)
        return _unverified(page_text, period, blocked, "модель недоступна — источник не проверен")

    messages = build_messages(need, period, url, title, excerpt, links)

    async def _call():
        return await llm_client.complete(messages, model=model)

    try:
        res = await (executor.run(_call) if executor is not None else _call())
    except Exception as exc:  # noqa: BLE001 — транспорт капризен, разведка не должна падать
        log.warning("Проверка источника %s: сбой модели: %s", url, exc)
        return _unverified(page_text, period, blocked, "сбой модели: %s" % exc)

    parsed = parse_verdict((res or {}).get("content") or "")
    out = verify_claims(parsed, page_text, period_request=period, blocked=blocked)
    out["usage"] = (res or {}).get("usage")
    return out


def _unverified(page_text: str, period: str | None, blocked: str | None, why: str) -> dict:
    """Честный «не проверено»: доказательств нет, причина названа."""
    from .probe import ACCESS_BLOCKED
    return {"has_data": "неизвестно", "evidence": [], "rejected_evidence": [], "kinds": [],
            "export": [], "access": ACCESS_BLOCKED if blocked else ACCESS_OPEN,
            "how_to": "", "limits": "", "usage": None,
            "period": coverage(observed_period(page_text), parse_period(period or "")),
            "check_notes": [why], "note": "источник не проверен"}


def _claimed_period(parsed: dict):
    lo, hi = parsed.get("period_from"), parsed.get("period_to")
    if lo and hi:
        return (min(lo, hi), max(lo, hi))
    if lo:
        return (lo, lo)
    return None


def _int_or_none(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if 1900 <= n <= 2100 else None


def _float_or_none(v):
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return None
