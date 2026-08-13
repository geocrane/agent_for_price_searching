# -*- coding: utf-8 -*-
"""Разбор позиций моделью: техническое обозначение → название, каким товар зовут продавцы.

Зачем. Наименования из спецификаций часто нечитаемы для поисковика: «Модуль памяти
MEM_DDR4_64GB», «Блок питания PSU_1300W». Дословный запрос уводит выдачу в общие категории.
Превратить такой код в «модуль памяти DDR4 64 ГБ» — языковая задача, правилами её не решить, не
скатившись в подгонку под конкретный список.

ВАЖНО: модель отдаёт РАЗБОР позиции, а не готовый запрос. Запрос собирает код
(`query_planner.plan_queries`) — иначе теряется пользовательский аффикс («купить в Ростове»,
«оптом»), а модель получает возможность дописать в запрос то, чего в позиции не было.

Надёжность пакета (главный риск — разбор разъедется по позициям):
  • каждая позиция подаётся с номером строки, ответ обязан вернуть тот же номер (привязка по эху,
    не по порядку); чужие и повторные номера отбрасываем;
  • проверка на подмену: разбор должен быть похож на СВОЮ позицию сильнее, чем на любую другую в
    пакете (сравнение относительное — без магических порогов). Если для памяти вернулся разбор
    процессора, он проиграет позиции с процессором и будет отклонён;
  • всё, что не прошло проверку или не вернулось, работает по правилам — молча ничего не теряется.
"""
import asyncio
import re
import time

from ..llm.json_utils import extract_json
from ..obs.log import get_logger

log = get_logger("discovery.query_llm")

_WORD = re.compile(r"[\w/+.\-]{2,}", re.U)

_SYSTEM = (
    "Ты готовишь названия товаров для поиска в интернет-магазинах.\n"
    "На вход — список позиций из спецификации, по одной в строке, в формате:\n"
    "  <номер> | <наименование> | <парт-номер или пусто>\n\n"
    "Для КАЖДОЙ позиции верни РАЗБОР — как этот товар называют продавцы:\n"
    "  • name_clean — читаемое название: технические обозначения вида MEM_DDR4_64GB, PSU_1300W, "
    "NIC_1GbE_4Port разверни в слова («модуль памяти DDR4 64 ГБ», «блок питания 1300 Вт», "
    "«сетевая карта 1GbE 4 порта»); убери служебный мусор — коды строк, номера позиций сметы, "
    "длинные скобочные перечисления комплектации, если без них смысл сохраняется.\n"
    "    ПРОИЗВОДИТЕЛЬ И ОБОЗНАЧЕНИЕ МОДЕЛИ ОБЯЗАНЫ ОСТАТЬСЯ в name_clean, даже если название "
    "начинается с них и они выглядят как префикс: «DELTA Аккумулятор Delta DTM 1212» и "
    "«Интеграл+ Прибор приемно-контрольный БРО-6» — это марки товара, без них поиск найдёт "
    "другой товар;\n"
    "  • brand — производитель, если он назван в позиции; ПУСТЫЕ ПОЛЯ НЕ ПИШИ, просто опусти их;\n"
    "  • model — обозначение модели, если оно есть в позиции; пустое — опусти;\n"
    "  • is_generic — true, если в позиции НЕ указаны ни производитель, ни модель (задан только "
    "тип предмета и характеристики); если false — поле опусти.\n\n"
    "ЗАПРЕЩЕНО добавлять то, чего нет в наименовании и парт-номере: не придумывай бренд, объём, "
    "мощность, поколение, совместимость. Только перефразирование имеющегося. Парт-номер в "
    "name_clean не переписывай — его подставит программа.\n\n"
    "Ответ — ТОЛЬКО JSON-массив, номер строки обязателен и должен совпадать с входным. Пиши "
    "компактно, без отступов и переводов строк внутри массива — длинный ответ не успевает "
    "сгенерироваться:\n"
    '  [{"row":10,"name_clean":"модуль памяти DDR4 64 ГБ","is_generic":true},'
    '{"row":11,"name_clean":"аккумулятор Delta DTM 1212","brand":"Delta","model":"DTM 1212"}]'
)

# Поля разбора, которые отдаём наружу (остальное из ответа модели игнорируем).
FIELDS = ("name_clean", "brand", "model", "is_generic")


def _tokens(text: str) -> set[str]:
    """Значимые токены строки в нижнем регистре (слова и коды от 2 символов)."""
    return {t.lower() for t in _WORD.findall(str(text or "")) if len(t) >= 2}


def _similarity(text: str, name: str, part_number: str | None) -> float:
    """Доля токенов позиции, встретившихся в тексте (0..1). Парт-номер считается за токен."""
    src = _tokens(name) | _tokens(part_number)
    if not src:
        return 0.0
    q = _tokens(text)
    # коды вида mem_ddr4_64gb модель разбивает на части — учитываем вхождение по подстроке
    hit = sum(1 for t in src if t in q or any(t in qt or qt in t for qt in q))
    return hit / len(src)


def _belongs_to(text: str, row: int, batch: list[dict]) -> bool:
    """Разбор относится именно к своей позиции, а не к соседней по пакету.

    Сравнение относительное: у своей позиции сходство должно быть не ниже, чем у любой другой.
    Так ловится именно перепутывание строк, и не нужны подобранные пороги.
    """
    own = next((it for it in batch if it["row"] == row), None)
    if own is None:
        return False
    own_sim = _similarity(text, own.get("name"), own.get("part_number"))
    if own_sim <= 0:
        return False                       # ни одного общего токена — модель ушла в сторону
    for other in batch:
        if other["row"] == row:
            continue
        if _similarity(text, other.get("name"), other.get("part_number")) > own_sim:
            return False                   # чужая позиция подходит лучше → строки разъехались
    return True


def _batch_prompt(batch: list[dict]) -> list[dict]:
    lines = ["%s | %s | %s" % (it["row"], it.get("name") or "", it.get("part_number") or "")
             for it in batch]
    return [{"role": "system", "content": _SYSTEM},
            {"role": "user", "content": "\n".join(lines)}]


def parse_batch(raw: str, batch: list[dict], *, max_name: int = 200) -> dict[int, dict]:
    """Разобрать ответ модели по пакету → {row: {name_clean, brand, model, is_generic}}.

    Возвращает только проверенные позиции: чужие/повторные номера и разборы, «съехавшие» на
    соседнюю позицию, отбрасываются.
    """
    return parse_batch_detailed(raw, batch, max_name=max_name)[0]


def parse_batch_detailed(raw: str, batch: list[dict], *,
                         max_name: int = 200) -> tuple[dict[int, dict], set]:
    """То же, плюс множество строк, на которые модель ВООБЩЕ дала ответ.

    Различие принципиально для повторов: если модель на позицию не ответила (оборвался длинный
    ответ, не уложилась в таймаут), помогает дробление пакета. А если ответила, но разбор
    отклонён проверкой на подмену, дробить бессмысленно — переспрашивать будем то же самое,
    только дороже. Такие позиции работают по правилам.
    """
    data = extract_json(raw)
    if isinstance(data, dict):
        for key in ("items", "result", "data", "positions"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        log.warning("Разбор позиций: ответ модели не разобран (%d симв.)", len(raw or ""))
        return {}, set()
    allowed = {it["row"] for it in batch}
    out: dict[int, dict] = {}
    answered: set = set()                             # на что модель ответила (до проверок)
    for el in data:
        if not isinstance(el, dict):
            continue
        try:
            row = int(el.get("row"))
        except (TypeError, ValueError):
            continue
        if row not in allowed or row in out:          # чужой или повторный номер
            continue
        name_clean = str(el.get("name_clean") or "").strip()[:max_name]
        if not name_clean:
            continue
        answered.add(row)
        if not _belongs_to(name_clean, row, batch):
            log.warning("Разбор строки %s не относится к её товару — отклоняю: %r", row, name_clean)
            continue
        out[row] = {
            "name_clean": name_clean,
            "brand": str(el.get("brand") or "").strip()[:80],
            "model": str(el.get("model") or "").strip()[:80],
            "is_generic": bool(el.get("is_generic")),
        }
    return out, answered


MIN_SPLIT = 5          # мельче дробить бессмысленно: промпт на пакет перевесит пользу
# Потолок запросов на один пакет. Дерево дробления 40 → 2×20 → 4×10 → 8×5 стоит до 15 вызовов;
# бюджет обязан позволить пройти его до конца, иначе часть позиций молча останется без разбора.
# Было 12 — то есть до трёх подпакетов (до 15 позиций) обрывались молча, вопреки этому же
# комментарию.
MAX_CALLS_PER_BATCH = 15
# Пакеты разбираются СТРОГО ПО ОДНОМУ, последовательно (см. цикл в parse_items_llm).
# Параллельные пакеты конкурировали за модель между собой и с извлечением цен, порядок работы
# становился непредсказуемым, а выигрыш съедался очередью на стороне модели.
# Время ожидания ОДНОГО пакета. 40 позиций — это ~3500 токенов генерации, то есть минуты:
# короткий предел означал бы дробление каждого пакета по таймауту, то есть двойную оплату.
BATCH_TIMEOUT = 420.0


def _batch_tokens(rows: int) -> int:
    """Предел генерации на пакет разбора: рассуждение + по строке JSON на позицию.

    Замер на прогоне 09.08: 40 позиций → 11 449 токенов ответа (9 451 из них — рассуждение).
    Отсюда постоянная часть на рассуждение и линейная — на сам ответ, обе с запасом.
    """
    return 8000 + 300 * max(1, rows)


async def _emit(emit, ev) -> None:
    if emit is None:
        return
    r = emit(ev)
    if asyncio.iscoroutine(r):
        await r


async def parse_items_llm(items: list[dict], *, llm_client, model=None, batch_size: int = 40,
                          executor=None, emit=None, on_ready=None,
                          timeout: float = BATCH_TIMEOUT, should_stop=None) -> dict[int, dict]:
    """Разобрать список позиций пакетами, СТРОГО ПО ОДНОМУ пакету за раз.

    Позиции, которых нет в ответе (или чей разбор отклонён), вызывающая сторона обрабатывает
    правилами — молча ничего не теряется.

    on_ready(dict, rows) — вызывается по готовности очередного пакета (нужен вызывающему, чтобы
    складывать разбор по мере поступления и переживать остановку на середине).

    should_stop() — кооперативная остановка: проверяется перед каждым пакетом. Ответ на пакет из
    40 позиций идёт минутами, и без этой проверки «Стоп» не срабатывал бы всё это время.

    Расход токенов уходит в `emit` событием usage_delta, как у всех прочих вызовов модели. Без
    этого предразбор тратил деньги невидимо: в счётчике интерфейса оставался ноль.
    """
    if not items or llm_client is None:
        return {}
    batches = [items[i:i + batch_size] for i in range(0, len(items), max(1, batch_size))]
    log.info("Разбор позиций моделью: позиций=%d, пакетов=%d (по %d), последовательно",
             len(items), len(batches), batch_size)

    async def one(idx: int, batch: list[dict]) -> dict[int, dict]:
        budget = [MAX_CALLS_PER_BATCH]

        async def call(b):
            # Ожидание ответа видно пользователю: без этого «модель думает две минуты» и «модель
            # висит» на экране неразличимы, а разница в том, ждать или вмешиваться.
            seq = MAX_CALLS_PER_BATCH - budget[0]        # номер вызова внутри пакета (дробление)
            sid = "parse:%d:%d" % (idx, seq)
            rows = [it["row"] for it in b]
            await _emit(emit, {"type": "parse_wait", "batch": idx, "batches": len(batches),
                               "rows": len(b), "state": "sent", "row_nums": rows})
            # Живой поток ответа: пакет генерируется минутами, и пользователь должен видеть, что
            # модель работает, а не висит. Стрим синхронный (publish в WS), как в извлечении.
            await _emit(emit, {"type": "model_stream", "sid": sid, "stage": "parse", "start": True,
                               "batch": idx, "batches": len(batches), "row_nums": rows})

            def _on_chunk(delta):
                if emit is not None:
                    emit({"type": "model_stream", "sid": sid, "stage": "parse", "delta": delta})

            t0 = time.monotonic()
            # Свой предел генерации: ответ на пакет — это строка JSON на каждую позицию ПЛЮС
            # рассуждение модели, а оно длиннее самого ответа (в прогоне 09.08 пакет из 40
            # позиций стоил 11 449 токенов, из них 9 451 — рассуждение). Общий дефолт брать
            # нельзя: обрезанный ответ не разберётся, пакет уйдёт на дробление и оплатится заново.
            res = await llm_client.complete(_batch_prompt(b), model=model,
                                            on_chunk=_on_chunk, timeout=timeout,
                                            max_tokens=_batch_tokens(len(b)))
            took = time.monotonic() - t0
            if res.get("usage"):
                await _emit(emit, {"type": "usage_delta", "usage": res["usage"]})
            out = parse_batch_detailed(res.get("content", ""), b)
            log.info("Пакет разбора %d/%d (%d позиций): ответ за %.0f с, разобрано %d",
                     idx, len(batches), len(b), took, len(out[0]))
            await _emit(emit, {"type": "parse_wait", "batch": idx, "batches": len(batches),
                               "rows": len(b), "state": "done", "took_s": round(took, 1),
                               "parsed": len(out[0]), "row_nums": rows})
            return out

        async def resolve(part: list[dict], depth: int) -> dict[int, dict]:
            """Разобрать часть пакета; на что модель не ответила — поделить пополам и повторить.

            Пакет держим крупным (в нём модель видит соседние позиции и реже путает их между
            собой), а на таймаут и обрыв по длине отвечаем дроблением: короткий промпт и короткий
            ответ укладываются там, где длинный не успел.

            Дробим ТОЛЬКО молчание модели. Позиции, на которые она ответила, но чей разбор снят
            проверкой на подмену, переспрашивать бессмысленно — ответ будет тот же, а токены
            потратятся; такие позиции работают по правилам.
            """
            if not part or budget[0] <= 0:
                return {}
            budget[0] -= 1
            try:
                # Свой предел ожидания — страховка НАД таймаутом транспорта (он уже получил
                # timeout этого вызова): с запасом, чтобы срабатывал только если завис не HTTP,
                # а что-то вокруг него. Иначе оба предела гасили бы вызов одновременно.
                got, answered = await asyncio.wait_for(
                    executor.run(lambda: call(part)) if executor else call(part),
                    timeout=timeout + 30.0)
            except asyncio.CancelledError:
                # Остановка прогона посреди пакета. Без этого события отправленный пакет
                # оставался в интерфейсе «ожидающим» навсегда — ровно так и появилась строка
                # «ждём ответ модели по пакету 2 из 13 — 4:31:23» спустя часы после «Стоп».
                await _emit(emit, {"type": "parse_wait", "batch": idx, "batches": len(batches),
                                   "rows": len(part), "state": "cancelled"})
                raise
            except (asyncio.TimeoutError, TimeoutError):
                log.warning("Пакет разбора (%d позиций) не уложился в %.0f с — дроблю",
                            len(part), timeout)
                await _emit(emit, {"type": "parse_wait", "batch": idx, "batches": len(batches),
                                   "rows": len(part), "state": "timeout"})
                got, answered = {}, set()
            except Exception as exc:  # noqa: BLE001 — пакет не должен ронять прогон
                # Таймаут транспорта (APITimeoutError) — это тот же случай «не уложился»:
                # называем вещи своими именами, иначе в логе он выглядит как поломка.
                if "timeout" in type(exc).__name__.lower():
                    log.warning("Пакет разбора (%d позиций) оборван по таймауту модели — дроблю",
                                len(part))
                    await _emit(emit, {"type": "parse_wait", "batch": idx, "batches": len(batches),
                                       "rows": len(part), "state": "timeout"})
                else:
                    # Раньше эта ветка молчала: ни события, ни снятия ожидания. Любой не-таймаут
                    # (429, обрыв связи) превращал строку статуса в вечный фантом.
                    log.warning("Пакет разбора не удался (%d позиций): %s", len(part), exc)
                    await _emit(emit, {"type": "parse_wait", "batch": idx, "batches": len(batches),
                                       "rows": len(part), "state": "error",
                                       "error": str(exc)[:200]})
                got, answered = {}, set()
            silent = [it for it in part if it["row"] not in answered]
            if silent and len(part) > MIN_SPLIT and budget[0] > 0:
                half = max(1, len(silent) // 2)
                for sub in (silent[:half], silent[half:]):
                    got.update(await resolve(sub, depth + 1))
            return got

        return await resolve(batch, 0)

    result: dict[int, dict] = {}
    # ПОРЯДОК СТРОК НЕРУШИМ и параллельности здесь нет: пакеты идут один за другим, сверху вниз.
    # От затора защищаемся не обгоном, а дроблением пакета при молчании модели.
    for i, batch in enumerate(batches, 1):
        if should_stop is not None and should_stop():
            log.info("Разбор позиций прерван на пакете %d из %d", i, len(batches))
            break
        got = await one(i, batch)
        result.update(got)
        if got:
            await _emit(emit, {"type": "items_parsed", "rows": sorted(got),
                               "batch": i, "batches": len(batches)})
            # Разбор каждой позиции — тоже ответ модели, и он должен быть виден рядом со своей
            # позицией, а не тонуть в общем пакете.
            for row in sorted(got):
                await _emit(emit, {"type": "model_answer", "row": row, "stage": "parse",
                                   "text": _parsed_text(got[row])})
        if on_ready is not None:
            # Вторым аргументом — ВСЕ строки пакета: вызывающему нужно знать не только что
            # разобрано, но и что уже решено окончательно (разбор не пришёл — работаем по правилам).
            r = on_ready(got, [it["row"] for it in batch])
            if asyncio.iscoroutine(r):
                await r
    log.info("Разбор позиций: принято %d из %d (остальные — по правилам)", len(result), len(items))
    # Итог виден пользователю, а не только в логе: сколько позиций пойдёт с разбором модели,
    # а сколько — по правилам (правило «логировать всё и видимо»).
    await _emit(emit, {"type": "parse_done", "parsed": len(result), "total": len(items),
                       "batches": len(batches)})
    return result


def _parsed_text(data: dict) -> str:
    """Разбор одной позиции одной строкой — для показа рядом с самой позицией."""
    parts = [str(data.get("name_clean") or "")]
    if data.get("brand"):
        parts.append("бренд: %s" % data["brand"])
    if data.get("model"):
        parts.append("модель: %s" % data["model"])
    if data.get("is_generic"):
        parts.append("без марки")
    return " · ".join(p for p in parts if p)
