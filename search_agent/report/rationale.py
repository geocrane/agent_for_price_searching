# -*- coding: utf-8 -*-
"""Комментарий модели по источникам для позиции отчёта (правило report-model-rationale).

Модель обязана по каждой позиции написать краткое обоснование итоговой цены: на каких
источниках она основана, разброс и оговорки (единица измерения/фасовка, аналог, слабое
подтверждение, отсеянные приманки). Универсально — БЕЗ привязки к товарной категории.

Без модели (или при ошибке транспорта) — детерминированный фолбэк-сводка по источникам,
чтобы ячейка не была пустой (правило «ничего не произошло недопустимо»). Реальный вызов
модели инициирует пользователь (правило проекта); здесь только сборка промпта и разбор.
"""
from urllib.parse import urlparse

from .xlsx import money, norm_verdict
from ..obs.log import get_logger, log_event

log = get_logger("report")

_SYSTEM = (
    "Ты пишешь короткий комментарий-обоснование итоговой цены товара для отчёта: 1–3 предложения, "
    "по-русски, деловой стиль, без воды, без Markdown и без списков. Тебе дают искомый товар, "
    "итоговую цену и диапазон, а также РАЗБОР ОТБОРА: все цены, участвовавшие в сравнении, с их "
    "баллами, и цены, отсеянные до сравнения, с номером шага и причиной.\n"
    "Комментарий должен отразить (что применимо):\n"
    "  – на каких источниках основана цена (перечисли ключевые домены);\n"
    "  – разброс цен и чем он объясняется, если это видно из данных (розница/опт, наличие, регион);\n"
    "  – ОБЯЗАТЕЛЬНО: если отсеяно точное совпадение — назови его цену и причину отсева. Читатель "
    "должен понимать, почему в отчёте не оно;\n"
    "  – ОБЯЗАТЕЛЬНО: если итог помечен «требует проверки» — предупреди, что это цена аналога с "
    "единственного сайта, никем не подтверждённая, и брать её без проверки нельзя;\n"
    "  – если помечена ничья по баллам — скажи, что выбор между равными доопределён правилом;\n"
    "  – прочие оговорки: цена за иную единицу измерения/фасовку, это аналог, подтверждение слабое "
    "(один источник), отсеяны приманки/выбросы.\n"
    "Опирайся ТОЛЬКО на переданные данные, ничего не выдумывай и не добавляй цен, которых нет в списке. "
    "Баллы — служебные, в комментарии их не приводи. "
    "Если надёжная цена не найдена — коротко укажи причину и что проверяли. Ответ — только текст "
    "комментария, без префиксов и кавычек."
)


def _domain(offer: dict) -> str:
    return offer.get("source_domain") or offer.get("domain") \
        or urlparse(offer.get("url", "")).netloc.lower()


def offers_from_item(item: dict, *, limit: int | None = 8) -> list[dict]:
    """Собрать плоский список предложений товара из кандидатов (или готового item['offers']).

    Каждое предложение: {domain, value, currency, match, tier, url, in_stock, vat, confidence,
    snippet}. Дедуп по (domain, value). limit=None — без обрезки (для истории — все офферы).
    """
    raw: list[dict] = []
    if isinstance(item.get("offers"), list):
        raw = item["offers"]
    else:
        for c in item.get("candidates", []) or []:
            for p in c.get("prices", []) or []:
                d = dict(p)
                d.setdefault("source_domain", c.get("domain"))
                d.setdefault("url", c.get("url"))
                raw.append(d)
    out, seen = [], set()
    for p in raw:
        try:
            value = float(p.get("value"))
        except (TypeError, ValueError):
            continue
        dom = _domain(p)
        key = (dom, round(value, 2))
        if key in seen:
            continue
        seen.add(key)
        m = p.get("match")
        out.append({"domain": dom, "value": value,
                    "currency": (p.get("currency") or "RUB"),
                    "match": getattr(m, "value", None) or (m if isinstance(m, str) else None),
                    "tier": p.get("tier"), "weight": p.get("weight"), "url": p.get("url"),
                    "in_stock": p.get("in_stock"), "vat": p.get("vat"),
                    "confidence": p.get("extraction_confidence") if p.get("extraction_confidence") is not None
                    else p.get("confidence"),
                    "snippet": p.get("snippet") or p.get("note")})
    out.sort(key=lambda o: o["value"])
    return out if limit is None else out[:limit]


def _scored_block(scored: list[dict], *, include_excluded: bool) -> str:
    """Разбор отбора для модели: все цены с баллом и судьбой — ровно то, что видел adjudicate.

    Раньше модели показывали 8 самых ДЕШЁВЫХ предложений без баллов и без причин отсева. Из-за
    этого она объясняла итог, которого могла не видеть, и не могла назвать главное — почему
    выбыло точное совпадение. Здесь и приняты́е, и отсеянные, с шагом и формулировкой.
    """
    kept = [d for d in scored if d.get("accepted")]
    dropped = [d for d in scored if not d.get("accepted")]

    def line(d: dict) -> str:
        bits = [d.get("domain") or "?", money(d.get("value"), d.get("currency", "RUB"))]
        if d.get("match"):
            bits.append("[%s]" % d["match"])
        if d.get("unit"):
            bits.append("за %s" % d["unit"])
        if d.get("score") is not None:
            bits.append("балл %.1f" % (float(d["score"]) * 100))
        if d.get("found"):
            bits.append("— «%s»" % str(d["found"])[:90])
        return "  • %s" % " ".join(bits)

    lines = []
    if kept:
        lines.append("Цены, участвовавшие в сравнении (%d):" % len(kept))
        for d in kept:
            suffix = " ← ИТОГ" if d.get("is_primary") else ""
            if d.get("reason"):
                suffix += " (%s)" % d["reason"]
            lines.append(line(d) + suffix)
    else:
        lines.append("Цены, участвовавшие в сравнении: нет.")
    if include_excluded and dropped:
        lines.append("Отсеяно до сравнения (%d):" % len(dropped))
        for d in dropped:
            lines.append("%s — отсеяна на шаге %s (%s): %s"
                         % (line(d), d.get("step"), d.get("step_name") or "", d.get("reason") or ""))
    return "\n".join(lines)


def _offers_block(offers: list[dict], excluded: list[dict], *, include_excluded: bool,
                  limit: int | None = None) -> str:
    """Запасной блок для сводов без разбора отбора (сессии прежних версий, плоское событие UI)."""
    if limit:
        offers = offers[:limit]
    if not offers:
        lines = ["Найденные предложения: нет."]
    else:
        lines = ["Найденные предложения (%d):" % len(offers)]
        for o in offers:
            mark = "" if not o.get("match") else " [%s]" % o["match"]
            lines.append("  • %s — %s%s" % (o["domain"], money(o["value"], o["currency"]), mark))
    if include_excluded and excluded:
        lines.append("Отсеяно:")
        for e in excluded[:6]:
            lines.append("  • %s — %s" % (money(e.get("value"), e.get("currency", "RUB")),
                                          e.get("reason", "")))
    return "\n".join(lines)


def build_messages(item: dict, nv: dict, offers: list[dict], *, include_excluded: bool,
                   offers_limit: int | None = None) -> list[dict]:
    """Собрать messages (system + user) для комментария по позиции."""
    name = item.get("name") or ""
    head = ["Искомый товар: %s" % name]
    if nv["value"] is not None:
        head.append("Итоговая цена: %s%s" % (
            money(nv["value"], nv["currency"]),
            "" if not nv["match"] else " (%s)" % nv["match"]))
        if nv["price_min"] is not None or nv["price_max"] is not None:
            head.append("Диапазон найденных цен: %s – %s"
                        % (money(nv["price_min"], nv["currency"]), money(nv["price_max"], nv["currency"])))
        if nv["corroborated_by"]:
            head.append("Подтверждено независимыми источниками: %d" % nv["corroborated_by"])
        if nv.get("needs_review"):
            head.append("ПОМЕТКА: требует проверки — аналог с единственного сайта, "
                        "не подтверждён ни одним другим источником.")
        if nv.get("tie"):
            head.append("ПОМЕТКА: балл итога совпал с баллом другой цены — "
                        "выбор доопределён правилом отбора.")
    elif item.get("on_request"):
        onreq = item["on_request"]
        head.append("Итоговая цена: по запросу у продавца (%s). Найдено: «%s» на %s"
                    % (onreq.get("match") or "точное", onreq.get("found") or "—",
                       onreq.get("domain") or "—"))
        if onreq.get("note"):
            head.append("Комментарий модели: %s" % onreq["note"])
    else:
        head.append("Итоговая цена: не найдена (%s)" % (item.get("not_found_reason") or "нет данных"))
    # Разбор отбора — если он есть; иначе плоский список предложений (сессии прежних версий).
    block = (_scored_block(nv["scored"], include_excluded=include_excluded) if nv.get("scored")
             else _offers_block(offers, nv["excluded_list"], include_excluded=include_excluded,
                                limit=offers_limit))
    user = "%s\n\n%s" % ("\n".join(head), block)
    return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]


def fallback_rationale(item: dict, nv: dict, offers: list[dict], *, include_excluded: bool) -> str:
    """Детерминированная сводка по источникам (когда модель недоступна). Кратко и по делу."""
    if nv["value"] is None:
        if item.get("on_request"):
            onreq = item["on_request"]
            return ("Цена по запросу у продавца: найдено «%s» (%s) на %s. %s"
                    % (onreq.get("found") or "—", onreq.get("match") or "точное",
                       onreq.get("domain") or "—", onreq.get("note") or "")).strip()
        reason = item.get("not_found_reason") or "надёжных предложений не найдено"
        if offers:
            doms = ", ".join(sorted({o["domain"] for o in offers})[:5])
            return "Надёжная цена не установлена (%s). Проверены источники: %s." % (reason, doms)
        return "Надёжная цена не установлена: %s." % reason

    parts = []
    if nv["domain"]:
        note = "" if nv["match"] in (None, "точное") else " (аналог)"
        parts.append("Итог %s по источнику %s%s." % (money(nv["value"], nv["currency"]), nv["domain"], note))
    else:
        parts.append("Итог %s." % money(nv["value"], nv["currency"]))

    if offers:
        doms = ", ".join(sorted({o["domain"] for o in offers})[:5])
        # «Предложений» и «Подтверждено сайтами» считаются по РАЗНЫМ множествам: первое — по всей
        # выдаче, второе — по ценам, прошедшим отбор. Без второй цифры «предложений 3,
        # подтверждено 0» выглядит противоречием, хотя это просто два разных счёта.
        passed = sum(1 for d in (nv.get("scored") or []) if d.get("accepted"))
        through = (", в отбор прошло %d" % passed) if passed and passed != len(offers) else ""
        parts.append("Найдено предложений: %d (%s)%s." % (len(offers), doms, through))
    if nv["price_min"] is not None and nv["price_max"] is not None and nv["price_min"] != nv["price_max"]:
        parts.append("Разброс %s – %s." % (money(nv["price_min"], nv["currency"]),
                                           money(nv["price_max"], nv["currency"])))
    if nv["corroborated_by"]:
        parts.append("Подтверждено %d независимыми источниками." % nv["corroborated_by"])
    if include_excluded and nv["excluded_count"]:
        parts.append(_excluded_summary(nv))
    if nv.get("tie"):
        parts.append("Балл итога совпал с другой ценой — выбор доопределён правилом отбора.")
    if not nv.get("verified", True):
        parts.append("ТРЕБУЕТ ПРОВЕРКИ: цена взята из структурной разметки страницы — модель "
                     "была недоступна и соответствие товару не проверяла.")
    elif nv.get("needs_review"):
        parts.append("ТРЕБУЕТ ПРОВЕРКИ: цена аналога с единственного сайта, "
                     "другими источниками не подтверждена.")
    return " ".join(parts)


def _excluded_summary(nv: dict) -> str:
    """Отсев по существу: «Отсеяно: 2 (аналоги при точных)», а не «как выброс/приманка».

    Прежняя формулировка была одна на все шаги и врала: аналоги, снятые при наличии точного
    совпадения, объявлялись выбросами. Отдельно называем отсеянное ТОЧНОЕ совпадение — читателю
    важнее всего понять, почему в отчёте не оно.
    """
    excluded = nv["excluded_list"]
    if not excluded:
        return "Отсеяно значений: %d." % nv["excluded_count"]
    groups: dict[str, int] = {}
    for e in excluded:
        groups[e.get("step_name") or "выброс"] = groups.get(e.get("step_name") or "выброс", 0) + 1
    body = ", ".join("%s: %d" % (k, n) for k, n in groups.items())
    out = "Отсеяно %d (%s)." % (len(excluded), body)
    exact = next((d for d in (nv.get("scored") or [])
                  if not d.get("accepted") and d.get("match") == "точное"), None)
    if exact:
        out += (" В том числе точное совпадение %s (%s) — %s."
                % (money(exact.get("value"), exact.get("currency", "RUB")),
                   exact.get("domain") or "—", exact.get("reason") or "отсеяно"))
    return out


def _why(exc: BaseException) -> str:
    """Короткая причина, по которой комментарий писала не модель, — для колонки отчёта."""
    from ..llm.limits import FATAL, OVERLOAD, classify, limit_text
    kind = classify(exc)
    if kind == OVERLOAD:
        return "лимит API: %s" % limit_text(exc)
    if kind == FATAL:
        return "модель отклонила запрос"
    return "модель не ответила вовремя"


async def write_rationale(item: dict, llm_client=None, *, model=None, cfg=None,
                          on_chunk=None) -> dict:
    """Комментарий по источникам для позиции. Возвращает {text, usage, degraded, by}.

    С моделью — краткое обоснование; без модели/при ошибке — детерминированная сводка.

    `by` — КТО написал текст («модель» / «сводка (…)»). Это едет в отчёт отдельной колонкой:
    заголовок «Комментарий модели» врал, когда модель была недоступна, а отличить одно от
    другого читатель не мог (на прогоне 09.08 сводкой оказались 42% комментариев).
    """
    from ..config import ReportConfig
    cfg = cfg or ReportConfig()
    nv = norm_verdict(item.get("verdict"))
    # Полный пул: модель обосновывает ИТОГ, а он мог не попасть в топ самых дешёвых (список
    # отсортирован по возрастанию цены). Обрезка остаётся только для запасного блока — там нет
    # ни баллов, ни причин, и длинное перечисление модели не помогает.
    offers = offers_from_item(item, limit=None)

    if llm_client is None or not cfg.use_model:
        text = fallback_rationale(item, nv, offers, include_excluded=cfg.include_excluded)
        log.info("Комментарий (без модели, сводка): r%s", item.get("row"))
        return {"text": text, "usage": None, "degraded": True,
                "by": "сводка (модель отключена)"}

    try:
        messages = build_messages(item, nv, offers, include_excluded=cfg.include_excluded,
                                  offers_limit=cfg.max_offers_in_comment)
        res = await llm_client.complete(messages, model=model, on_chunk=on_chunk)
        text = (res.get("content") or "").strip().strip('"').strip()
        if not text:
            raise ValueError("модель вернула пустой комментарий")
        text = text[:cfg.max_chars]
        log_event(log, "report.rationale", row=item.get("row"), chars=len(text), offers=len(offers))
        return {"text": text, "usage": res.get("usage"), "degraded": False, "by": "модель"}
    except Exception as exc:  # noqa: BLE001 — транспорт модели капризен, не роняем отчёт
        log.warning("Комментарий моделью не удался (r%s): %s — деградация на сводку",
                    item.get("row"), exc)
        text = fallback_rationale(item, nv, offers, include_excluded=cfg.include_excluded)
        return {"text": text, "usage": None, "degraded": True,
                "by": "сводка (%s)" % _why(exc)}
