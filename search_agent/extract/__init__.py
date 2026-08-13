# -*- coding: utf-8 -*-
"""Извлечение цен со страницы кандидата → список PriceCandidate.

Всегда через модель (решение цена+match единым проходом); если модель недоступна —
ДЕГРАДАЦИЯ на структурные ценовые метки с видимым WARNING (правило: «ничего не произошло»
недопустимо). Модуль сам НЕ поднимает SSH-соединение — использует переданный llm_client.
"""
import asyncio
from urllib.parse import urlparse

from .inspect import inspect_fetch
from .llm_extract import extract_with_model
from .price import currencies_on_page, is_on_page, numbers_in
from .structured import collect_hints
from ..models import MatchType, NotFoundReason, PriceCandidate
from ..obs.log import get_logger, log_event

log = get_logger("extract")

_MATCH = {"точное": MatchType.EXACT, "аналог": MatchType.ANALOG}


def _fetch_of(cand: dict) -> dict | None:
    """Взять результат fetch из кандидата или из дискового кэша по URL."""
    fetch = cand.get("fetch")
    if fetch:
        return fetch
    from ..fetch import cache
    return cache.get(cand.get("url", ""))


def _domain(cand: dict, url: str) -> str:
    return cand.get("domain") or urlparse(url).netloc.lower()


async def extract_prices(item, cand: dict, llm_client=None, *, cfg=None, model=None,
                         emit=None, idx=None, count=None) -> list[PriceCandidate]:
    """Извлечь цены искомого товара `item` со страницы кандидата `cand`.

    cand: {url, domain, tier, weight, [fetch]}. Возвращает список PriceCandidate (может быть пуст).
    idx/count — «сайт k из N» для прогресса в UI.
    """
    from ..config import ExtractConfig
    cfg = cfg or ExtractConfig()
    url = cand.get("url", "")
    domain = _domain(cand, url)
    row = item.row if hasattr(item, "row") else None
    await _emit(emit, {"type": "position", "row": row, "stage": "extract", "state": "active",
                       "domain": domain, "idx": idx, "count": count})

    fetch = _fetch_of(cand)
    # Триаж (§8): перепроверяем, что получили (карточка/листинг/блок/пусто), кладём в кандидата
    # и показываем в UI. Даёт честную причину пропуска вместо тихого «нет цены».
    insp = inspect_fetch(fetch, url=url)
    cand["inspect"] = insp
    await _emit(emit, {"type": "inspect", "row": row, "url": url, "domain": domain,
                       "kind": insp["kind"], "price_present": insp["price_present"],
                       "reason": insp["reason"], "idx": idx, "count": count})
    if (not fetch or fetch.get("blocked") or insp["kind"] in ("shell", "empty")
            or not (fetch.get("markdown") or "").strip()):
        reason = insp["reason"]
        log.warning("Пропуск извлечения (%s): %s", insp["kind"], url)
        # Причина видна в UI и в колонке «Комментарий» отчёта, а не только в логе.
        cand["comment"] = cand["extract_reason"] = str(reason)
        await _emit(emit, {"type": "position", "row": row, "stage": "extract", "state": "skip",
                           "domain": domain, "idx": idx, "count": count, "note": str(reason)})
        return []

    hints = collect_hints(fetch.get("structured"))
    tier = cand.get("tier") or 4
    weight = cand.get("weight")
    weight = 0.3 if weight is None else weight

    # Текст, который реально уйдёт в модель, отбираем ЗДЕСЬ — чтобы (1) не считать его дважды и
    # (2) не звать модель с пустым промптом (из пустоты цену не извлечь ни на каком сайте).
    page_text, focus_stats = _page_text(fetch.get("markdown") or "", item, cfg)
    if not page_text.strip():
        log.warning("Нечего отправлять модели (после отбора текста пусто): %s", url)
        await _emit(emit, {"type": "position", "row": row, "stage": "extract", "state": "skip",
                           "domain": domain, "idx": idx, "count": count,
                           "note": "на странице нет текста для анализа"})
        cand["comment"] = cand["extract_reason"] = "на странице нет текста для анализа"
        return []

    # Страница, на которой НЕТ НИ ОДНОЙ ЦЕНЫ ни в одном виде, гарантированно даст пустой ответ —
    # и обойдётся в десятки секунд генерации. Признаки должны сойтись ВСЕ разом; одного мало,
    # потому что цену пишут по-разному:
    #   • триаж не нашёл ценовых меток;   • структурных цен (JSON-LD/microdata) нет;
    #   • отбор ценовых блоков не нашёл ни строки с ценой;
    #   • на странице не упомянута НИ ОДНА валюта — иначе пропали бы страницы в сумах/тенге/
    #     гривнах: ценовой шаблон знает рубли, доллары и евро, а список валют шире.
    if (getattr(cfg, "skip_model_without_prices", True) and llm_client is not None
            and not insp.get("price_present") and not hints
            and focus_stats.get("price_lines") == 0
            and not currencies_on_page(page_text)):
        log.info("Модель не зову: на странице нет ни одной цены (%s)", url)
        cand["comment"] = cand["extract_reason"] = "на странице нет ни одной цены"
        await _emit(emit, {"type": "position", "row": row, "stage": "extract", "state": "skip",
                           "domain": domain, "idx": idx, "count": count,
                           "note": "на странице нет ни одной цены"})
        return []

    # стрим «мышления» модели по этому сайту → в UI (панель «Ход» + чат). publish синхронный.
    def _on_chunk(delta):
        if emit is not None:
            emit({"type": "model_stream", "row": row, "domain": domain, "delta": delta})

    records, on_request, note, degraded = [], [], "", False
    degraded_reason = ""                             # ТОЧНАЯ причина: она едет в UI и в отчёт
    diag = {"raw_chars": 0, "parsed": True, "dropped": 0, "fail": None}
    shown_texts = [page_text]                        # всё, что модель реально видела по этой странице
    # Тот же товар + та же страница + та же модель + тот же промпт = тот же ответ. Повторный
    # прогон (дозагрузка, перезапуск после «Стоп», добор второй страницы) не должен ни платить
    # за это второй раз, ни ждать генерацию заново.
    ckey = _cache_key(url, page_text, item, model, cfg)
    hit = _cache_load(ckey)
    if hit is not None and llm_client is not None:
        records, on_request = hit.get("records") or [], hit.get("on_request") or []
        note = hit.get("note") or ""
        if hit.get("snippet"):
            shown_texts.append(hit["snippet"])
        log.info("Ответ модели взят из кэша (%s): цен=%d", domain, len(records))
        await _emit(emit, {"type": "extract_cached", "row": row, "domain": domain, "url": url,
                           "prices": len(records)})
    elif llm_client is not None:
        try:
            await _emit(emit, {"type": "model_stream", "row": row, "domain": domain,
                               "start": True, "item": getattr(item, "name", "")})
            records, on_request, note, usage, diag = await extract_with_model(
                item, url, domain, page_text, hints, llm_client,
                model=model, max_chars=cfg.max_chars, include_hints=cfg.include_structured_hint,
                on_chunk=_on_chunk, focus=False,         # текст уже отобран выше
                max_note=getattr(cfg, "max_note_chars", 1200))
            if usage:                                    # учёт токенов (виден в UI по ходу)
                await _emit(emit, {"type": "usage_delta", "usage": usage,
                                   "row": row, "domain": domain})
            # Пустой ответ на странице, где цена искомого, судя по тексту, ЕСТЬ — самая частая
            # потеря. Переспрашиваем один раз коротко: только самые релевантные ценовые блоки.
            snippet = ""
            if (not records and not on_request
                    and getattr(cfg, "retry_on_suspected_miss", True)):
                recs2, on_req2, note2, usage2, snippet, diag2 = await _retry_extract(
                    item, cand, fetch, llm_client, cfg, model, emit, row, domain, url, hints)
                # Повтор ДОПОЛНЯЕТ первую попытку, а не отменяет её. Раньше его пустой результат
                # затирал пояснение модели, и честное «на странице другой размер» превращалось в
                # «модель вернула пустой ответ без пояснения» — причина пропадала совсем.
                records, on_request = recs2 or records, on_req2 or on_request
                note = note2 or note
                if diag2 is not None and (recs2 or on_req2 or note2 or not diag["fail"]):
                    diag = diag2
                if snippet:
                    # Повтор отбирает блоки другими окнами, поэтому в нём могут оказаться строки,
                    # которых не было в первом тексте. Проверять ответ надо по ВСЕМУ, что модель
                    # реально видела, иначе верная цена отбрасывается как «выдуманная».
                    shown_texts.append(snippet)
                if usage2:
                    await _emit(emit, {"type": "usage_delta", "usage": usage2,
                                       "row": row, "domain": domain})
            # Кладём ИТОГ обеих попыток вместе с показанным фрагментом: попадание в кэш должно
            # воспроизводить состояние целиком, иначе проверка «цена есть в тексте» отбросит
            # верную цену, найденную повтором. Пустой ответ БЕЗ пояснения не кэшируем: он
            # означает либо сбой, либо потерю — а из кэша он вернулся бы вечно, уже без переспроса.
            if records or on_request or note:
                _cache_save(ckey, {"records": records, "on_request": on_request, "note": note,
                                   "snippet": snippet}, url, item, model)
        except Exception as exc:  # noqa: BLE001 — транспорт модели капризен, не роняем весь прогон
            # Сюда попадаем ТОЛЬКО после того, как шлюз (llm/limits.py) исчерпал всё: соблюдение
            # лимитов, повторы с backoff и глобальную паузу с ожиданием восстановления модели.
            # Раньше 429 прилетал сюда мгновенно и сразу превращался в «цену из разметки».
            degraded_reason = getattr(exc, "reason", None) or str(exc)[:200]
            log.warning("Модель недоступна при извлечении (%s): %s — деградация на структурные метки",
                        domain, exc)
            degraded = True
    else:
        degraded_reason = "модель не подключена"
        log.warning("Модель не подключена — извлекаю ТОЛЬКО структурные цены (%s)", domain)
        degraded = True

    # Деградацию помечаем явно: иначе сбой модели неотличим от честного «цены нет» — ни в UI,
    # ни в замере качества, где он бы засчитался как результат работы.
    cand["degraded"] = degraded
    prices: list[PriceCandidate] = []
    judged_out = 0                                   # сколько записей снял код-судья (для причины)
    invented = 0                                     # сколько цен не нашлось в тексте страницы
    wrong_currency = 0                               # сколько записей с непроверяемой валютой
    low_conf = 0                                     # сколько записей не прошли порог уверенности
    # Числа отправленного модели текста — для проверки, что цена не выдумана (см. price.is_on_page).
    seen_text = "\n".join(shown_texts)
    page_numbers = (numbers_in(seen_text) if getattr(cfg, "verify_price_on_page", True) else None)
    page_currencies = (currencies_on_page(seen_text)
                       if getattr(cfg, "verify_currency_on_page", True) else set())
    if degraded:
        prices = _from_hints(hints, domain, url, tier, weight, degraded_reason,
                             fetch.get("structured"))
    else:
        for r in records:
            if r["confidence"] is not None and r["confidence"] < cfg.min_confidence:
                low_conf += 1                        # молча отбрасывать нельзя — это причина «пусто»
                continue
            # Валюта: модель по умолчанию пишет RUB. Если на странице этой валюты нет вовсе,
            # а есть ровно одна другая — это она (реальный случай: узбекские сумы как рубли).
            if page_currencies and r["currency"] not in page_currencies:
                if len(page_currencies) == 1:
                    only = next(iter(page_currencies))
                    log.info("Валюта на странице %s — %s, а не %s: исправляю",
                             domain, only, r["currency"])
                    r["currency"] = only
                else:
                    wrong_currency += 1
                    log.warning("Валюта %s не подтверждается страницей %s (там %s) — отбрасываю",
                                r["currency"], domain, sorted(page_currencies))
                    continue
            # Цены, которой нет в тексте страницы, быть не может — какой бы уверенной модель ни
            # была. Так отсекаются числа, подобранные из соседних блоков на страницах, где цена
            # товара вообще не отрисовалась.
            if page_numbers is not None and not is_on_page(r["value"], page_numbers):
                invented += 1
                log.warning("Цена %s не найдена в тексте страницы (%s) — отбрасываю", r["value"], domain)
                await _emit(emit, {"type": "judge", "row": row, "domain": domain, "url": url,
                                   "rejected": True, "found": str(r.get("found", ""))[:140],
                                   "reason": "цены %s нет в тексте страницы" % r["value"]})
                continue
            # Код-судья (§10.4): отсеять позицию с доказанным измеримым расхождением
            # (25 кг ≠ 50 кг, иной объём/размер) — слабая модель метит такое «аналогом».
            if getattr(cfg, "strict_analog_judge", True):
                from .judge import judge_match
                found_desc = r.get("found") or r.get("note") or ""
                final_match, reason = judge_match(
                    getattr(item, "name", ""), found_desc, r["match"],
                    tolerance=getattr(cfg, "analog_spec_tolerance", 0.02),
                    part_number=getattr(item, "part_number", None))
                if final_match is None:
                    judged_out += 1
                    log.info("Код-судья отклонил '%s' @ %s: %s", found_desc[:60], domain, reason)
                    await _emit(emit, {"type": "judge", "row": row, "domain": domain, "url": url,
                                       "rejected": True, "found": found_desc[:140], "reason": reason})
                    continue
                r["match"] = final_match
            prices.append(PriceCandidate(
                value=r["value"], currency=r["currency"], source_domain=domain, url=url,
                tier=tier, weight=weight, match=_MATCH.get(r["match"], MatchType.EXACT),
                in_stock=r["in_stock"], vat=r["vat"], snippet=r["note"],
                found=r.get("found", ""), extraction_confidence=r["confidence"],
                unit=r.get("unit", ""), is_offer=r.get("is_offer")))

    # Цену в отчёт называет ТОЛЬКО модель. Брать число из структурной разметки, когда модель его
    # не назвала, пробовали — проверка на 4523 сохранённых страницах дала ~40% чужих цен (метка от
    # другого товара на той же карточке: направляющие 1200 мм вместо 600 мм, цена телевизора при
    # искомом кронштейне). Разметка остаётся подсказкой для переспроса (looks_answerable) и
    # источником цен при деградации, когда модели нет вовсе, — но не заменой её решению.

    # «Цена по запросу»: товар на странице есть, цифры нет. Это НЕ «не найдено» — сохраняем
    # отдельно (числовые цены остаются числовыми) вместе с тем, что именно модель нашла.
    if on_request and not prices:
        best = min(on_request, key=lambda r: 0 if r["match"] == "точное" else 1)
        cand["on_request"] = {"found": best.get("found", ""), "match": best["match"],
                              "note": best.get("note", ""), "domain": domain, "url": url}
    # Причина пустого результата — ОБЯЗАТЕЛЬНА. Пустой ответ модели без объяснения тоже причина,
    # и её нужно назвать, иначе сайт выглядит «молча пропущенным» (правило «логировать всё и видимо»).
    if not prices and not cand.get("on_request"):
        if degraded:
            cand["extract_reason"] = "модель недоступна — разобраны только структурные метки"
        elif diag.get("fail") == "truncated":
            cand["extract_failed"] = "truncated"
            cand["extract_reason"] = (
                "ответ модели оборван лимитом токенов (%d симв.) — страница не проанализирована "
                "до конца" % diag.get("raw_chars", 0))
        elif diag.get("fail") == "bad_json":
            # Ответ БЫЛ, но это не JSON: техническая неудача разбора, а не «на странице нет цены».
            cand["extract_failed"] = "bad_json"
            cand["extract_reason"] = (
                "сбой разбора ответа модели: %d симв. не являются JSON (страница не "
                "проанализирована)" % diag.get("raw_chars", 0))
        elif diag.get("fail") == "empty_content":
            cand["extract_failed"] = "empty_content"
            cand["extract_reason"] = (
                "модель вернула пустое тело ответа — технический сбой, страница не "
                "проанализирована")
        elif wrong_currency:
            cand["extract_reason"] = (
                "валюта найденных цен (%d) страницей не подтверждается" % wrong_currency)
        elif invented:
            cand["extract_reason"] = (
                "названные моделью цены (%d) на странице отсутствуют — вероятно, цена не "
                "отрисовалась, а числа взяты из соседних блоков" % invented)
        elif judged_out:
            cand["extract_reason"] = (
                "найденное отклонено проверкой характеристик (расхождение фасовки/размера): %d шт."
                % judged_out)
        elif low_conf:
            cand["extract_reason"] = (
                "уверенность модели ниже порога %.2f: отброшено записей %d"
                % (cfg.min_confidence, low_conf))
        elif note:
            cand["extract_reason"] = note
        elif diag.get("dropped"):
            cand["extract_reason"] = (
                "модель перечислила позиции (%d), но ни одну не признала искомым товаром — "
                "пояснения не дала" % diag["dropped"])
        else:
            cand["extract_reason"] = "модель вернула пустой ответ без пояснения"

    # Комментарий модели по источнику: сравнение искал/нашёл либо объяснение, почему цены нет.
    comment = (prices[0].snippet if prices else
               (cand.get("on_request") or {}).get("note") or note or cand.get("extract_reason"))
    if comment:
        cand["comment"] = comment

    log_event(log, "extract.done", url=url, domain=domain, prices=len(prices),
              on_request=len(on_request), degraded=degraded, hints=len(hints))
    for p in prices:
        await _emit(emit, {"type": "price", "row": row, "domain": domain, "url": url,
                           "value": p.value, "currency": p.currency,
                           "match": p.match.value if p.match else "не проверено",
                           "verified": p.verified,
                           "in_stock": p.in_stock, "confidence": p.extraction_confidence,
                           "degraded": degraded, "note": p.snippet})
    await _emit(emit, _model_answer(row, domain, prices, cand.get("on_request"), note))
    state = "done" if prices else ("on_request" if cand.get("on_request") else "empty")
    await _emit(emit, {"type": "position", "row": row, "stage": "extract",
                       "state": "done" if state != "empty" else "empty",
                       "domain": domain, "idx": idx, "count": count, "found": len(prices)})
    return prices


def _cache_key(url: str, page_text: str, item, model, cfg) -> str:
    """Ключ ответа модели по этой странице или пустая строка, если кэш выключен/недоступен."""
    if not getattr(cfg, "cache_model_answers", True):
        return ""
    try:
        from ..storage.extract_cache import make_key
        from .llm_extract import PROMPT_VERSION
        return make_key(url, page_text, item, model, PROMPT_VERSION)
    except Exception as exc:  # noqa: BLE001 — без кэша просто зовём модель как раньше
        log.warning("Ключ кэша ответов модели не посчитан: %s", exc)
        return ""


def _cache_load(key: str) -> dict | None:
    """Ответ из кэша или None. Куда писать, знает сам кэш (та же база, что история)."""
    if not key:
        return None
    try:
        from ..storage import extract_cache
        return extract_cache.load(key)
    except Exception as exc:  # noqa: BLE001 — промах кэша не имеет права ронять извлечение
        log.warning("Кэш ответов модели не прочитан: %s", exc)
        return None


def _cache_save(key: str, payload: dict, url: str, item, model) -> None:
    if not key:
        return
    try:
        from ..storage import extract_cache
        extract_cache.save(key, payload, url=url, item=getattr(item, "name", ""), model=model)
    except Exception as exc:  # noqa: BLE001 — не записали, значит в следующий раз спросим модель
        log.warning("Кэш ответов модели не записан: %s", exc)


async def _retry_extract(item, cand, fetch, llm_client, cfg, model, emit, row, domain, url,
                         hints=None):
    """Вторая попытка по короткому фрагменту, если пустой ответ выглядит пропуском.

    Возвращает (цены, «по запросу», пояснение, usage, показанный_фрагмент, диагностика).
    Фрагмент нужен вызывающему: ответ повторного запроса проверяется по тому тексту, который
    модель видела, а он здесь другой. Диагностика — None, когда повтора не было вовсе: тогда
    вызывающий оставляет диагностику первой попытки.
    """
    from .focus import focus_pricing, looks_answerable
    from .llm_extract import retry_with_model

    limit = int(getattr(cfg, "retry_max_chars", 3000))
    snippet = focus_pricing(fetch.get("markdown") or "", item, limit, before=6, after=3)
    # Значения из структурных меток: на части сайтов цена написана «20 130» без знака валюты, и
    # текстовый признак цены её не видит. Метка доказывает, что это всё-таки цена.
    hint_values = {float(h["value"]) for h in (hints or []) if h.get("value") is not None}
    if not looks_answerable(snippet, item, hint_values=hint_values):
        return [], [], "", None, "", None
    log.info("Пустой ответ выглядит пропуском (%s) — переспрашиваю по фрагменту %d симв.",
             domain, len(snippet))
    await _emit(emit, {"type": "extract_retry", "row": row, "domain": domain, "url": url,
                       "chars": len(snippet)})
    try:
        recs, on_req, note, usage, diag = await retry_with_model(
            item, url, domain, snippet, llm_client, model=model,
            max_note=getattr(cfg, "max_note_chars", 1200))
        return recs, on_req, note, usage, snippet, diag
    except Exception as exc:  # noqa: BLE001 — повтор не обязан удаться
        log.warning("Повторное извлечение не удалось (%s): %s", domain, exc)
        return [], [], "", None, "", None


def _page_text(markdown: str, item, cfg) -> tuple[str, dict]:
    """Текст страницы для промпта и статистика отбора: ценовые блоки либо простая обрезка.

    Статистика нужна вызывающему, чтобы не звать модель на странице, где цен нет вовсе
    (`price_lines == 0`): считать это второй раз отдельным проходом незачем.
    """
    if getattr(cfg, "focus_pricing", True):
        from .focus import select_pricing_blocks
        return select_pricing_blocks(markdown, item, cfg.max_chars,
                                     before=getattr(cfg, "focus_before", 20),
                                     after=getattr(cfg, "focus_after", 20))
    from .focus import has_price
    text = markdown[:cfg.max_chars]
    return text, {"price_lines": sum(1 for ln in text.splitlines() if has_price(ln))}


def _model_answer(row, domain: str, prices, on_request: dict | None, note: str) -> dict | None:
    """Готовая строка итога по сайту для панели «Диалог».

    Только когда модели есть что сказать: цена, «цена по запросу» или объяснение, почему цены
    нет. Пустой ответ модели в диалог не попадает (правило: там только результат).
    """
    if prices:
        p = prices[0]
        text = "%s — «%s» — %s %s" % (domain, (p.found or "без названия"), round(p.value), p.currency)
        if p.snippet:
            text += "\n" + p.snippet
    elif on_request:
        text = "%s — «%s» — цена по запросу (%s)" % (
            domain, on_request.get("found") or "без названия", on_request.get("match"))
        if on_request.get("note"):
            text += "\n" + on_request["note"]
    elif note:
        text = "%s — цены нет: %s" % (domain, note)
    else:
        return None
    return {"type": "model_answer", "row": row, "domain": domain, "text": text}


class _ItemView:
    """Лёгкий вид товара из dict под интерфейс extract и свода.

    Кроме исходных полей несёт разбор позиции (`brand`, `is_generic`, см. discovery/query_llm.py):
    свод по ним решает, считать ли диапазон коридором рынка и какой домен принадлежит вендору.
    """
    def __init__(self, d: dict) -> None:
        self.row = d.get("row")
        self.name = d.get("name") or ""
        self.part_number = d.get("part_number")
        self.raw = d.get("raw") or {}
        parsed = d.get("parsed") or {}
        self.brand = parsed.get("brand") or ""
        self.is_generic = bool(parsed.get("is_generic"))


def _build_executor(cfg, emit):
    """Адаптивный исполнитель вызовов модели (лимит 8→4→1, таймаут+повтор, авто-восстановление)."""
    from ..llm.orchestrator import build_executor
    return build_executor(cfg, emit=emit, name="extract")


async def _extract_item_cands(it: dict, item, llm_client, cfg, model, emit, executor) -> None:
    """Извлечь цены по всем (не-файловым) кандидатам ОДНОГО товара — через адаптивный executor.

    executor — AdaptiveExecutor: держит параллель под общим лимитом, при таймауте (>180 с)
    понижает лимит и повторяет; если исчерпал ступени даже синхронно — деградация (пустой
    результат с видимым событием), чтобы прогон не падал целиком.
    """
    cands = [c for c in it.get("candidates", []) if not c.get("is_file")]
    count = len(cands)

    async def one(idx, cand):
        try:
            prices = await executor.run(lambda: extract_prices(
                item, cand, llm_client=llm_client, cfg=cfg, model=model,
                emit=emit, idx=idx, count=count))
        except (asyncio.TimeoutError, TimeoutError):     # упёрлись в таймаут на всех ступенях
            log.warning("Извлечение отменено по таймауту модели (все ступени): %s", cand.get("url", ""))
            await _emit(emit, {"type": "position", "row": getattr(item, "row", None),
                               "stage": "extract", "state": "skip", "domain": cand.get("domain"),
                               "idx": idx, "count": count, "note": "таймаут модели"})
            prices = []
        cand["prices"] = [p.model_dump() for p in prices]

    await asyncio.gather(*[one(i + 1, c) for i, c in enumerate(cands)])


async def extract_items(items_data: list[dict], llm_client=None, *, cfg=None, model=None,
                        emit=None, executor=None) -> list[dict]:
    """Извлечь цены по всем кандидатам списка товаров ПАРАЛЛЕЛЬНО (адаптивный лимит).

    items_data: [{row, name, part_number, candidates:[{url, domain, tier, weight, [fetch]}]}].
    Кладёт в каждый кандидат поле `prices` (список dict). Возвращает items_data.
    executor — общий на прогон AdaptiveExecutor (если не передан — строится из cfg).
    """
    log.info("Извлечение цен: товаров=%d, модель=%s, конкурентность=%s", len(items_data),
             "есть" if llm_client is not None else "нет (структурные метки)",
             getattr(cfg, "concurrency", "?"))
    executor = executor or _build_executor(cfg, emit)
    await asyncio.gather(*[
        _extract_item_cands(it, _ItemView(it), llm_client, cfg, model, emit, executor)
        for it in items_data])
    log.info("Извлечение цен завершено")
    return items_data


async def extract_and_adjudicate(items_data: list[dict], llm_client=None, *, cfg=None, model=None,
                                 emit=None) -> list[dict]:
    """Извлечь цены по кандидатам и свести КАЖДЫЙ товар к итогу, эмитя `verdict` по мере готовности.

    Товары обрабатываются параллельно (общий лимит cfg.concurrency); свод товара считается сразу,
    как только по нему извлечены все страницы, — не ждём весь список.
    """
    from .adjudicate import adjudicate
    executor = _build_executor(cfg, emit)
    total, done, lock = len(items_data), 0, asyncio.Lock()

    async def do_item(it):
        nonlocal done
        item = _ItemView(it)
        await _extract_item_cands(it, item, llm_client, cfg, model, emit, executor)
        prices = [PriceCandidate(**p) for c in it.get("candidates", []) for p in c.get("prices", [])]
        v = adjudicate(item, prices)
        it["verdict"] = v.model_dump()
        it["on_request"] = pick_on_request(it.get("candidates", [])) if v.primary is None else None
        reason = (infer_not_found_reason(it.get("candidates", []))
                  if v.primary is None and not it["on_request"] else None)
        it["not_found_reason"] = reason.value if reason else None
        await _emit(emit, verdict_event(it.get("row"), v, it["not_found_reason"], it["on_request"]))
        async with lock:
            done += 1
            await _emit(emit, {"type": "stage_progress", "stage": "extract",
                               "item_done": done, "item_total": total, "row": it.get("row")})

    await asyncio.gather(*[do_item(it) for it in items_data])
    log.info("Свод по позициям завершён")
    return items_data


def pick_on_request(candidates: list[dict]) -> dict | None:
    """Лучшая запись «цена по запросу» среди кандидатов (точное совпадение важнее аналога).

    Применяется, когда числовой цены по позиции нет: товар-то найден, просто продавец не
    публикует цифру. Для пользователя это НЕ «не найдено».
    """
    found = [c["on_request"] for c in candidates or [] if c.get("on_request")]
    if not found:
        return None
    return min(found, key=lambda r: 0 if r.get("match") == "точное" else 1)


def verdict_event(row, v, not_found_reason=None, on_request=None) -> dict:
    """Событие `verdict` для UI из Verdict (переиспользуется в extract и pipeline).

    not_found_reason — явная причина, если надёжной цены нет (заблокировано/нет офферов/…),
    чтобы UI показывал «не найдено — <причина>» вместо пустоты (правило «логировать всё»).

    `excluded` и `scored` идут ЦЕЛИКОМ, а не счётчиком: журнал прогона — единственное место, где
    решение сохраняется навсегда, и по счётчику «отсеяно: 1» разобрать постфактум, ЧТО и почему
    выбыло, было невозможно. Отчёт, собранный по этому событию (путь webui), тоже нуждается в
    полном списке — иначе лист «Отбор» останется пустым.
    """
    return {"type": "verdict", "row": row,
            "primary": v.primary.value if v.primary else None,
            "currency": v.currency,
            "match": (v.primary.match.value if v.primary.match else "не проверено")
                     if v.primary else None,
            "verified": v.primary.verified if v.primary else None,
            "price_min": v.price_min, "price_max": v.price_max,
            "unit": v.unit, "market_corridor": v.market_corridor, "price_median": v.price_median,
            "confidence": v.confidence, "corroborated_by": v.corroborated_by,
            "domain": v.primary.source_domain if v.primary else None,
            "url": v.primary.url if v.primary else None,
            "found": v.primary.found if v.primary else None,
            "excluded": list(v.excluded),
            "scored": list(v.scored), "tie": v.tie, "needs_review": v.needs_review,
            "not_found_reason": not_found_reason,
            "on_request": on_request}


def infer_not_found_reason(candidates: list[dict]) -> "NotFoundReason | None":
    """Вывести причину отсутствия надёжной цены из результатов триажа кандидатов (§8/§10.5 ТЗ).

    Использует `cand['inspect']` (kind/price_present) и `cand['fetch']`. Различает:
    заблокировано (всё — антибот/пусто), нет офферов (страницы открылись, цен нет),
    неоднозначно (цены на страницах были, но товар не подтверждён), не искали (ничего не грузили).
    """
    attempted = [c for c in candidates if not c.get("is_file")]
    fetched = [c for c in attempted if c.get("inspect") or c.get("fetch")]
    if not fetched:
        return NotFoundReason.NOT_SEARCHED
    inspects = [c.get("inspect") for c in attempted if c.get("inspect")]
    if not inspects:
        return NotFoundReason.NOT_SEARCHED
    kinds = [i.get("kind") for i in inspects]
    # Страница, отданная сервером с ошибкой статуса (404 на снятый с продажи товар), — это не
    # блокировка: сайт открылся и честно сказал, что предложения нет. Называть такое
    # «заблокировано» значило бы отправить пользователя разбираться с несуществующим антиботом.
    http_gone = [i for i in inspects
                 if i.get("kind") == "empty" and (i.get("signals") or {}).get("status")]
    if http_gone and len(http_gone) == len(inspects):
        return NotFoundReason.NO_OFFERS
    if all(k in ("blocked", "empty") for k in kinds):
        return NotFoundReason.BLOCKED
    if any(i.get("price_present") for i in inspects):
        return NotFoundReason.AMBIGUOUS           # цены на страницах были, но товар не подтверждён
    if any(k in ("card", "listing", "other") for k in kinds):
        return NotFoundReason.NO_OFFERS            # страницы открылись, цен нет
    return NotFoundReason.AMBIGUOUS


def _from_hints(hints: list[dict], domain, url, tier, weight, reason: str = "",
                structured: dict | None = None) -> list[PriceCandidate]:
    """Последний рубеж: структурные метки → PriceCandidate с пометкой «не проверено моделью».

    Применяется ТОЛЬКО когда шлюз модели (llm/limits.py) исчерпал соблюдение лимитов, повторы
    и ожидание восстановления. Раньше здесь стоял `match=EXACT` — то есть цена уезжала в отчёт
    как «точное совпадение», хотя товар никто не сверял; на прогоне 09.08 так получились 32%
    цен отчёта и девять позиций с ценой в тысячу раз меньше настоящей.

    Поэтому: `verified=False`, `match=None` (соответствие НЕИЗВЕСТНО, а не «точное»), точная
    причина и название товара из самой разметки — чтобы колонка «Найдено» не была прочерком,
    а код-судья мог отсеять явное расхождение.
    """
    from .structured import product_name
    found = product_name(structured)
    out = []
    for h in hints:
        out.append(PriceCandidate(
            value=h["value"], currency=h["currency"], source_domain=domain, url=url,
            tier=tier, weight=weight, match=None, in_stock=h.get("in_stock"),
            snippet="структурная метка (%s): соответствие товару моделью НЕ проверялось%s"
                    % (h.get("source", ""), (" — %s" % reason) if reason else ""),
            found=found, verified=False,
            unverified_reason=reason or "модель недоступна",
            extraction_confidence=0.4))
    return out


async def _emit(emit, ev):
    if emit is None or ev is None:      # None — «событию нечего сообщить», молча пропускаем
        return
    r = emit(ev)
    if asyncio.iscoroutine(r):
        await r
