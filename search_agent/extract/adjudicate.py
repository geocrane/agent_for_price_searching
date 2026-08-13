# -*- coding: utf-8 -*-
"""Свод по позиции (adjudicate) — надёжная цена + диапазон. Детерминированно, БЕЗ модели.

Из всех найденных цен (PriceCandidate) по одному товару выбирает наиболее достоверную (primary)
и диапазон (min/max), отсекая фейки/приманки («купи за 1 ₽»), опт-за-тонну и опечатки.
Пороги — ОТНОСИТЕЛЬНЫЕ (доля от медианы), без привязки к категории товара (универсально).

Каждое решение сохраняется в `Verdict.scored`: принята цена или на каком шаге отсеяна, с каким
баллом и из чего этот балл сложился. Раньше баллы считала локальная функция и выбрасывала — итог
в отчёте нельзя было проверить, а расхождение между листами «Итоги» и «Детали» выглядело как
произвол. Отсеянным баллы тоже считаются («теневой балл»): видно не только ЧТО отсеяно, но и
чего это стоило.
"""
import re
from statistics import median

from .units import normalize_unit
from ..models import MatchType, PriceCandidate, Verdict
from ..obs.log import get_logger

log = get_logger("adjudicate")

# Шаги конвейера отсева. Номер попадает в отчёт («отсеяна на шаге 2 (единица)»), поэтому порядок
# и названия менять нельзя, не сверившись с листом «Отбор».
STEP_ACCEPTED = 0
STEP_CURRENCY = 1
STEP_UNIT = 2
STEP_ANALOG = 3
STEP_OUTLIER = 4
STEP_NAMES = {
    STEP_ACCEPTED: "принята",
    STEP_CURRENCY: "валюта",
    STEP_UNIT: "единица",
    STEP_ANALOG: "аналог при точных",
    STEP_OUTLIER: "выброс",
}

# Слагаемые балла в порядке показа. Ключ → как называется в отчёте; бизнес читает эту формулу
# вместо исходников, поэтому названия деловые, а не имена весов.
PART_LABELS = [
    ("tier", "надёжность сайта"),
    ("conf", "уверенность извлечения"),
    ("corr", "подтверждение другими сайтами"),
    ("match", "тип совпадения"),        # не «точное совпадение»: у аналога здесь половина веса,
                                        # и «точное совпадение 6.0» в строке аналога читалось бы ложью
    ("stock", "в наличии"),
    ("median", "близость к середине"),
    ("vendor", "сайт производителя"),
]


def _cfg(cfg=None):
    """Правила отбора: переданный конфиг или прочитанный из `config/adjudicate.yaml`."""
    if cfg is not None:
        return cfg
    from ..config import default_adjudicate_config
    return default_adjudicate_config()


def _dominant_currency(prices: list[PriceCandidate]) -> str:
    counts: dict[str, int] = {}
    for p in prices:
        counts[p.currency] = counts.get(p.currency, 0) + 1
    return max(counts, key=counts.get) if counts else "RUB"


def _dominant_unit(prices: list[PriceCandidate], *, prefer_exact: bool = True) -> str:
    """Самая массовая единица (в каноне). «» — единицу никто не назвал, не разделяем.

    Считается по ТОЧНЫМ совпадениям, если они есть: единица товара — та, за которую продают сам
    товар, а не его аналоги. Без этого правила два аналога за «шт.» задавали единицу и выбрасывали
    единственную точную цену за «шт» (реальный случай LTV RTM-085 00).
    """
    pool = [p for p in prices if p.match == MatchType.EXACT] if prefer_exact else []
    counts: dict[str, int] = {}
    for p in (pool or prices):
        u = normalize_unit(p.unit)
        if u:
            counts[u] = counts.get(u, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Перцентиль по отсортированному списку (линейная интерполяция)."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _mad(values: list[float], med: float) -> float:
    return median([abs(v - med) for v in values]) if values else 0.0


def _corroboration(price: PriceCandidate, valid: list[PriceCandidate], rel: float) -> int:
    """Сколько НЕЗАВИСИМЫХ доменов (кроме своего) дали близкую цену (±rel)."""
    domains = set()
    for other in valid:
        if other.source_domain == price.source_domain:
            continue
        if price.value > 0 and abs(other.value - price.value) / price.value <= rel:
            domains.add(other.source_domain)
    return len(domains)


def vendor_domain(item_name: str, brand: str, domain: str) -> bool:
    """Домен принадлежит производителю искомого товара?

    Универсально и без списков: сравниваем ИМЯ домена второго уровня со словами названия/бренда.
    «Шнур питания NTSS …» + `ntss.ru` → да. Цена на сайте производителя — самая защищённая от
    накрутки посредника, поэтому при прочих равных выбираем её.
    """
    host = (domain or "").lower().split(":")[0].removeprefix("www.")
    labels = [l for l in host.split(".") if l not in ("com", "ru", "org", "net", "su", "рф")]
    if not labels:
        return False
    label = labels[-1] if len(labels) > 1 else labels[0]
    if len(label) < 3:
        return False
    words = {w.lower() for w in re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9]{3,}", "%s %s" % (item_name, brand))}
    return label in words


def _median_closeness(value: float, med: float) -> float:
    """1.0 в медиане, 0.0 при отклонении на медиану и больше. Одиночная крайность — подозрительна."""
    if not med:
        return 0.0
    return max(0.0, 1.0 - abs(value - med) / med)


def _score_parts(p: PriceCandidate, corr: int, max_corr: int, med: float,
                 item_name: str, brand: str, cfg) -> tuple[float, dict]:
    """Балл кандидата и его слагаемые. Возвращает (балл, {ключ: вклад в баллах})."""
    tier = p.weight if p.weight is not None else 0.3
    conf = p.extraction_confidence if p.extraction_confidence is not None else 0.5
    corr_norm = (corr / max_corr) if max_corr else 0.0
    # Непроверенная цена (взята из разметки, когда модель была недоступна) НЕ получает вес
    # точного совпадения: товар ей никто не сверял. Раньше `_from_hints` ставила match=EXACT,
    # и такая цена шла в свод с полным весом, выигрывала ничьи и глушила пометку «проверить».
    if not getattr(p, "verified", True):
        match = cfg.unverified_match
    else:
        match = 1.0 if p.match == MatchType.EXACT else 0.5
    stock = 1.0 if p.in_stock else 0.0
    vendor = 1.0 if vendor_domain(item_name, brand, p.source_domain) else 0.0
    parts = {
        "tier": cfg.w_tier * tier,
        "conf": cfg.w_conf * conf,
        "corr": cfg.w_corr * corr_norm,
        "match": cfg.w_match * match,
        "stock": cfg.w_stock * stock,
        "median": cfg.w_median * _median_closeness(p.value, med),
        "vendor": cfg.w_vendor * vendor,
    }
    factor = cfg.non_offer_penalty if p.is_offer is False else 1.0
    if factor != 1.0:                       # обзор/выдача: цена настоящая, но не предложение
        parts = {k: v * factor for k, v in parts.items()}
    return sum(parts.values()), parts


def _score(p: PriceCandidate, corr: int, max_corr: int, med: float = 0.0,
           item_name: str = "", brand: str = "", cfg=None) -> float:
    """Балл кандидата (совместимость: часть тестов и вызовов считает только число)."""
    return _score_parts(p, corr, max_corr, med, item_name, brand, _cfg(cfg))[0]


def parts_formula(parts: dict, scale: float = 100.0) -> str:
    """Слагаемые балла словами: «надёжность сайта 8.4 + уверенность извлечения 11.3 + …».

    Нулевые слагаемые опускаем — они не объясняют выбор, а только удлиняют строку.
    """
    out = []
    for key, label in PART_LABELS:
        v = (parts or {}).get(key) or 0.0
        if round(v * scale, 1) > 0:
            out.append("%s %.1f" % (label, v * scale))
    return " + ".join(out)


def _decision(p: PriceCandidate, *, step: int, reason: str | None) -> dict:
    """Запись решения по одной цене (заготовка; балл проставляется, когда известен пул)."""
    return {
        "value": p.value,
        "currency": p.currency,
        "domain": p.source_domain,
        "url": p.url,
        "found": p.found,
        "match": p.match.value if p.match else "не проверено",
        "verified": getattr(p, "verified", True),
        "unit": p.unit or "",
        "unit_norm": normalize_unit(p.unit),
        "in_stock": p.in_stock,
        "is_offer": p.is_offer,
        "accepted": step == STEP_ACCEPTED,
        "step": step,
        "step_name": STEP_NAMES.get(step, ""),
        "reason": reason,
        "is_primary": False,
        "score": None,
        "parts": {},
    }


def adjudicate(item, prices: list[PriceCandidate], cfg=None) -> Verdict:
    """Свести все цены по товару к надёжному итогу + диапазону. Пустой вход → пустой Verdict."""
    if not prices:
        return Verdict(primary=None, confidence=0.0)

    cfg = _cfg(cfg)
    currency = _dominant_currency(prices)
    decisions: dict[int, dict] = {}          # id(цена) → решение (порядок = порядок находки)
    order: list[PriceCandidate] = list(prices)

    def drop(p: PriceCandidate, step: int, reason: str) -> None:
        decisions[id(p)] = _decision(p, step=step, reason=reason)

    # 1) отсечь иную валюту (конверсию откладываем — помечаем прозрачно)
    base = []
    for p in prices:
        if p.currency != currency:
            drop(p, STEP_CURRENCY, "другая валюта (%s), конверсия отложена" % p.currency)
        else:
            base.append(p)

    # 1б) отсечь цены за ДРУГУЮ единицу: 6500 ₽ за куб газобетона и 176 ₽ за блок — про один
    # товар, но это не диапазон, а разные величины. Единицы сравниваем в КАНОНЕ: «шт.» и «шт» —
    # одна и та же величина, и цена не должна выпадать из-за точки в сокращении.
    unit = _dominant_unit(base, prefer_exact=cfg.unit_by_exact)
    if unit:
        kept = []
        for p in base:
            u = normalize_unit(p.unit)
            if u and u != unit:
                drop(p, STEP_UNIT, "цена за другую единицу (%s), а не %s" % (p.unit, unit))
            else:
                kept.append(p)
        base = kept

    # 2) приоритет точных; аналоги — только если точных нет.
    # Непроверенная цена (из разметки, модель была недоступна) в пул точных НЕ попадает: её
    # соответствие товару никто не сверял, и вытеснять ею проверенные нельзя.
    exacts = [p for p in base if p.match == MatchType.EXACT and getattr(p, "verified", True)]
    pool = exacts if exacts else base
    analog_only = not exacts
    if exacts:
        pool_ids = {id(x) for x in pool}     # по id, а не `in pool`: две одинаковые цены с разных
        for p in base:                       # страниц равны по значению и схлопнулись бы в одну
            if id(p) not in pool_ids:
                drop(p, STEP_ANALOG,
                     "соответствие товару моделью не проверено, а проверенные точные есть"
                     if not getattr(p, "verified", True) else "аналог при наличии точных совпадений")

    if not pool:
        return _finish(Verdict(primary=None, currency=currency, confidence=0.0),
                       decisions, order, [], item, cfg)

    # 3) отсев фейков/выбросов по медиане (относительные пороги + MAD при достаточном N).
    #
    # Если МОДЕЛЬ НЕ ПРОВЕРИЛА НИ ОДНУ цену, медиана — это медиана мусора, и отсев по ней
    # выбрасывает как раз настоящую цену. Реальный случай r483 (09.08): у «SkyNet Кабель витая
    # пара UTP outdoor 4x2x0,51 Cu Premium 305» из разметки пришли 32,59 / 58 / 43 739,72 ₽,
    # медианой стала 58 ₽, и 43 739,72 ₽ улетели как «аномально высокая (>6x медианы)» —
    # в отчёт попали 32,59 ₽. Таких позиций набралось девять. Поэтому: не судим, а показываем
    # весь разброс и требуем проверки (см. needs_review ниже) — пока цену не подтвердит модель.
    all_unverified = bool(pool) and not any(getattr(p, "verified", True) for p in pool)
    values = [p.value for p in pool]
    med = median(values)
    mad = _mad(values, med)
    valid: list[PriceCandidate] = []
    for p in pool:
        reason = None
        if all_unverified:
            reason = None                    # отсеивать нечем: сравнивать мусор с мусором нельзя
        elif med > 0 and p.value < med * cfg.k_low:
            reason = ("аномально низкая (<%.0f%% медианы) — вероятно приманка/часть/образец"
                      % (cfg.k_low * 100))
        elif med > 0 and p.value > med * cfg.k_high:
            reason = "аномально высокая (>%gx медианы) — вероятно опт/опечатка" % cfg.k_high
        elif len(values) >= cfg.min_n_for_mad and mad > 0 and abs(p.value - med) / mad > cfg.mad_k:
            reason = "статистический выброс (|z|>%.0f по MAD)" % cfg.mad_k
        if reason:
            drop(p, STEP_OUTLIER, reason)
        else:
            valid.append(p)
            decisions[id(p)] = _decision(p, step=STEP_ACCEPTED, reason=None)

    if not valid:
        log.warning("adjudicate: все цены отсеяны как выбросы (товар r%s)", getattr(item, "row", "?"))
        return _finish(Verdict(primary=None, currency=currency, confidence=0.0),
                       decisions, order, [], item, cfg)

    # 4) корроборация + скоринг → primary. Баллы считаем и отсеянным тоже («теневой балл»):
    # в отчёте видно, чего стоил отсев — по случаю LTV точная цена набирала бы больше победителя.
    name = getattr(item, "name", "") or ""
    brand = getattr(item, "brand", "") or ""
    valid_med = median([p.value for p in valid])
    corrs = {id(p): _corroboration(p, valid, cfg.corroborate_rel) for p in order}
    max_corr = max((corrs[id(p)] for p in valid), default=0)

    for p in order:
        d = decisions.get(id(p))
        if d is None:
            continue
        score, parts = _score_parts(p, corrs[id(p)], max_corr, valid_med, name, brand, cfg)
        d["score"], d["parts"] = score, parts

    # Тай-брейк: балл ↓, точное вперёд, больше подтверждений, в наличии, дешевле. Раньше здесь
    # стоял max(), и при равных баллах побеждала цена, которую модель просто назвала первой —
    # итог зависел от порядка абзацев на странице.
    ranked = sorted(valid, key=lambda p: (
        -decisions[id(p)]["score"],
        0 if getattr(p, "verified", True) else 1,   # проверенная моделью всегда впереди
        0 if p.match == MatchType.EXACT else 1,
        -corrs[id(p)],
        0 if p.in_stock else 1,
        p.value,
    ))
    primary = ranked[0]
    decisions[id(primary)]["is_primary"] = True
    prim_corr = corrs[id(primary)]
    tie = (len(ranked) > 1
           and abs(decisions[id(ranked[1])]["score"] - decisions[id(primary)]["score"]) < 1e-9)
    if tie:
        decisions[id(ranked[1])]["reason"] = "ничья с итогом по баллам — итог доопределён правилом"

    # 5) диапазон и доверие
    vals = sorted(p.value for p in valid)
    score = decisions[id(primary)]["score"]
    if analog_only:
        score *= cfg.analog_only_penalty
    if tie:
        score *= cfg.tie_penalty
    confidence = max(0.0, min(1.0, score))

    # Итог-аналог, которого никто не подтвердил и который найден на единственном домене, — это
    # цена ПОХОЖЕГО товара другого бренда. Цену показываем, но помечаем: доверять ей нельзя.
    needs_review = bool(analog_only and prim_corr == 0
                        and len({p.source_domain for p in valid}) == 1)
    # Итог, который модель не проверяла, требует проверки ВСЕГДА: соответствие товару не
    # сверял никто, и «точным» такое называть нельзя ни при каких баллах.
    if not getattr(primary, "verified", True):
        needs_review = True
        confidence = min(confidence, cfg.unverified_confidence)

    # Родовая позиция (бренд/модель не заданы): крайние значения — это разброс КАТЕГОРИИ, а не
    # цены товара («термос 1 л» — от 726 до 4104 ₽). Показываем срединный коридор и говорим,
    # что это цена категории. Для опознаваемого товара оставляем честные min/max.
    corridor = bool(getattr(item, "is_generic", False)) and len(vals) >= cfg.min_n_for_corridor
    lo, hi = (_percentile(vals, 0.25), _percentile(vals, 0.75)) if corridor else (vals[0], vals[-1])

    v = Verdict(primary=primary, price_min=lo, price_max=hi, currency=currency,
                confidence=round(confidence, 3), corroborated_by=prim_corr,
                unit=unit or (primary.unit or ""), market_corridor=corridor,
                price_median=round(valid_med, 2), tie=tie, needs_review=needs_review)
    v = _finish(v, decisions, order, valid, item, cfg)
    log.info("adjudicate r%s: primary=%s%s [%s], %s %s–%s, подтв.доменов=%d, отсеяно=%d%s%s",
             getattr(item, "row", "?"), primary.value, currency,
             primary.match.value if primary.match else "НЕ ПРОВЕРЕНО МОДЕЛЬЮ",
             "коридор рынка" if corridor else "диапазон", lo, hi, prim_corr, len(v.excluded),
             ", НИЧЬЯ по баллам" if tie else "",
             ", требует проверки (одинокий аналог)" if needs_review else "")
    return v


def _finish(v: Verdict, decisions: dict, order: list, valid: list, item, cfg) -> Verdict:
    """Разложить решения в Verdict.scored (в порядке находки) и собрать excluded как проекцию.

    Для цен, до скоринга не доживших (пул оказался пуст), баллы не считаем: сравнивать не с чем,
    а выдуманное число в отчёте хуже пустой ячейки.
    """
    v.scored = [decisions[id(p)] for p in order if id(p) in decisions]
    v.excluded = [{"value": d["value"], "domain": d["domain"], "reason": d["reason"],
                   "currency": d["currency"], "step": d["step"], "step_name": d["step_name"]}
                  for d in v.scored if not d["accepted"]]
    return v
