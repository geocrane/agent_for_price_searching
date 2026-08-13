# -*- coding: utf-8 -*-
"""CLI агента (click).

    python -m search_agent.cli doctor     # готовность окружения (без вызова модели)
    python -m search_agent.cli tools      # список зарегистрированных скилов
    python -m search_agent.cli run  ...   # прогон (M2+) — заглушка
    python -m search_agent.cli probe ...  # health-check источника (M2) — заглушка
"""
import asyncio

import click

from .config import load_settings
from .obs.log import new_run_id, setup_logging
from .ops.doctor import run_checks
from .tools import registry


@click.group()
@click.option("-v", "--verbose", count=True,
              help="уровень логов: -v = DEBUG (по умолчанию INFO/шаги)")
@click.option("-q", "--quiet", is_flag=True, help="только предупреждения и ошибки")
@click.option("--log-file", default=None, help="писать полный лог (DEBUG) в файл")
def cli(verbose: int, quiet: bool, log_file: str | None) -> None:
    """search_agent — агент поиска надёжных цен."""
    level = "WARNING" if quiet else ("DEBUG" if verbose else "INFO")
    setup_logging(console_level=level, log_file=log_file)
    new_run_id()   # сквозной id для корреляции всех записей этого запуска


@cli.command()
def doctor() -> None:
    """Проверка готовности окружения (конфиг, .env, токены модели и поиска, скилы)."""
    checks = run_checks(load_settings())
    click.echo("Проверка окружения search_agent:\n")
    for c in checks:
        mark = click.style("✓", fg="green") if c.ok else click.style("✗", fg="red")
        click.echo("  %s %-24s %s" % (mark, c.name, c.detail))
    ok = all(c.ok for c in checks)
    click.echo("\nИтог: " + (click.style("готово", fg="green") if ok
               else click.style("есть замечания (см. выше)", fg="yellow")))


@cli.command()
def tools() -> None:
    """Список зарегистрированных скилов (паттерн Tool + реестр)."""
    items = registry.all()
    if not items:
        click.echo("Скилы не зарегистрированы.")
        return
    click.echo("Зарегистрированные скилы (%d):\n" % len(items))
    for t in items:
        click.echo("  • %-12s — %s" % (t.name, t.description))


@cli.command()
@click.argument("path")
@click.option("--sheet", default=None, help="имя листа (по умолчанию активный)")
@click.option("--limit", default=15, help="сколько позиций показать")
def read(path: str, sheet: str | None, limit: int) -> None:
    """Адаптивно прочитать таблицу (Excel/CSV) и показать распознанные позиции."""
    from .input import read_table
    from .input.normalize import extract_codes
    res = read_table(path, sheet=sheet)
    click.echo("Файл: %s | лист: %s | заголовок в строке %d | уверенность: %s"
               % (path, res.sheet or "—", res.header_row, res.confidence))
    click.echo("Колонки: %s" % (res.mapping or "не распознаны"))
    if res.unmapped:
        click.echo("Незамапленные заголовки (уйдут в raw/отчёт): %s" % ", ".join(res.unmapped))
    if res.confidence == "low":
        click.echo(click.style("  ⚠ структура неоднозначна — нужна дизамбигуация моделью "
                               "(input.excel_reader.llm_mapping_request)", fg="yellow"))
    click.echo("\nТоваров: %d (показываю до %d)\n" % (len(res.items), limit))
    for it in res.items[:limit]:
        codes = extract_codes(it.name, it.part_number or "")
        click.echo("  r%-3d %s" % (it.row, (it.name[:72] or "—")))
        meta = []
        if it.part_number:
            meta.append("pn=%s" % it.part_number)
        if codes:
            meta.append("коды=%s" % ",".join(codes[:3]))
        if meta:
            click.echo("       %s" % " | ".join(meta))


@cli.command()
@click.argument("path")
@click.option("--limit", default=5, help="сколько товаров обработать")
@click.option("--affix", default=None,
              help="фраза-суффикс к каждой позиции (напр. 'купить в Ростове какая цена'); пусто — дефолт")
@click.option("--save", "save_path", default=None, help="сохранить выдачу в JSON-фикстуру (для реюза)")
def discover(path: str, limit: int, affix: str | None, save_path: str | None) -> None:
    """Обнаружение источников по товарам (Serper). --save пишет фикстуру для оффлайн-реюза."""
    from .input import read_table
    from .discovery import discover_items
    res = read_table(path)
    items = res.items[:limit]
    by_row = {it.row: it for it in items}
    collected: dict[int, dict] = {}
    click.echo("Обнаружение по %d товарам (аффикс: %s)\n" % (len(items), affix or "дефолт"))

    def emit(ev: dict) -> None:
        if ev.get("type") == "candidates":
            it = by_row.get(ev["row"])
            collected[ev["row"]] = {
                "row": ev["row"], "name": it.name if it else "",
                "part_number": it.part_number if it else None,
                "queries": ev["queries"], "candidates": ev["list"],
            }
            click.echo("r%s  запросы=%s" % (ev["row"], ev["queries"]))
            for c in ev["list"]:
                mark = " [файл]" if c.get("is_file") else ""
                click.echo("     %.3f  тир%s  %s%s" % (c.get("score") or 0, c.get("tier"), c["domain"], mark))
            if not ev["list"]:
                click.echo("     — источников не найдено")

    asyncio.run(discover_items(items, affix=affix, emit=emit))
    if save_path:
        from .storage import discovery_cache
        ordered = [collected[it.row] for it in items if it.row in collected]
        discovery_cache.save(save_path, ordered)
        click.echo("\nВыдача сохранена: %s (%d товаров)" % (save_path, len(ordered)))


@cli.command()
@click.argument("fixture")
@click.option("--limit", default=None, type=int, help="сколько товаров грузить")
@click.option("--level", default=None,
              type=click.Choice(["fast", "basic", "strong", "max", "swarm", "cursor"]),
              help="режим открытия (пресет анти-блока / слой параллельности)")
@click.option("--fresh", is_flag=True, help="грузить всё заново, минуя кэш (перезаписать runs/fetch_cache)")
def fetch(fixture: str, limit: int | None, level: str | None, fresh: bool) -> None:
    """Загрузить страницы кандидатов из фикстуры discovery и очистить контент (без цены)."""
    from .storage import discovery_cache
    from .fetch.fetcher import fetch_items
    from .config import load_settings, resolve_fetch
    items = discovery_cache.load(fixture)
    if limit:
        items = items[:limit]
    cfg = resolve_fetch(load_settings().fetch, {"preset": level} if level else None)
    click.echo("Загрузка страниц по %d товарам из %s (режим: %s, кэш: %s, anti_detect=%s, headed=%s)\n"
               % (len(items), fixture, level or "дефолт", "нет (заново)" if fresh else "да",
                  cfg.anti_detect, cfg.headed))

    def emit(ev: dict) -> None:
        if ev.get("type") == "fetched":
            b = ev.get("blocked")
            mark = ("БЛОК:" + str(b)) if b else ("%d симв." % (ev.get("chars") or 0))
            click.echo("  r%s  http=%s  [%s]  %s" % (ev["row"], ev.get("status"), mark, ev["url"][:68]))

    asyncio.run(fetch_items(items, cfg=cfg, emit=emit, use_cache=not fresh))
    click.echo("\nГотово. Очищенный контент — в кэше runs/fetch_cache/")


@cli.command()
def reclean() -> None:
    """Перечистить закэшированные страницы новым алгоритмом (оффлайн, по сохранённому HTML)."""
    from .fetch.fetcher import reclean_cache
    r = reclean_cache()
    click.echo("Перечищено: %d, пропущено (без html/блок): %d" % (r["recleaned"], r["skipped"]))


@cli.command()
@click.argument("fixture")
@click.option("--limit", default=None, type=int, help="сколько товаров обработать")
@click.option("--row", default=None, type=int, help="только один товар (номер строки)")
@click.option("--llm/--no-llm", "use_llm", default=False,
              help="звать модель (иначе — деградация на структурные ценовые метки)")
@click.option("--model", default=None, help="имя модели (для LM Studio, напр. qwen3.5-9b)")
@click.option("--base-url", default=None, help="base_url модели (напр. локальный LM Studio)")
def extract(fixture: str, limit: int | None, row: int | None, use_llm: bool,
            model: str | None, base_url: str | None) -> None:
    """Извлечь цены со страниц кандидатов (из фикстуры discovery + кэша fetch) через модель.

    Оффлайн по закэшированным страницам. --base-url переключает на локальный LM Studio.
    Без --llm — деградация на структурные ценовые метки (видно в выводе).
    """
    from .storage import discovery_cache
    from .extract import extract_prices
    from .config import load_settings
    from .llm.client import build_client
    settings = load_settings()
    if base_url:
        settings.llm.base_url = base_url
    llm = build_client(settings) if use_llm else None

    items = discovery_cache.load(fixture)
    if row is not None:
        items = [it for it in items if it.get("row") == row]
    elif limit:
        items = items[:limit]
    click.echo("Извлечение цен по %d товарам (модель: %s)\n"
               % (len(items), (model or settings.llm.model or "—") if use_llm
                  else "нет (структурные метки)"))

    async def go() -> None:
        for it in items:
            item = _Item(it)
            click.echo(click.style("r%s  %s" % (it.get("row"), (it.get("name") or "")[:70]), bold=True))
            for cand in it.get("candidates", []):
                if cand.get("is_file"):
                    continue
                prices = await extract_prices(item, cand, llm_client=llm, cfg=settings.extract, model=model)
                if not prices:
                    continue
                for p in prices:
                    conf = "" if p.extraction_confidence is None else " conf=%.2f" % p.extraction_confidence
                    click.echo("     %s%s %-6s [%s]%s  %s"
                               % (p.value, {"RUB": "₽"}.get(p.currency, " " + p.currency),
                                  "", p.match.value if p.match else "не проверено", conf,
                                  cand.get("domain", "")))
                    if p.snippet:
                        click.echo("        %s" % p.snippet[:100])

    asyncio.run(go())
    click.echo("\nГотово.")


@cli.command()
@click.argument("fixture")
@click.option("--limit", default=None, type=int, help="сколько товаров обработать")
@click.option("--row", default=None, type=int, help="только один товар (номер строки)")
@click.option("--llm/--no-llm", "use_llm", default=False,
              help="звать модель при извлечении (иначе — структурные ценовые метки)")
@click.option("--model", default=None, help="имя модели (для LM Studio, напр. qwen3.5-9b)")
@click.option("--base-url", default=None, help="base_url модели (напр. локальный LM Studio)")
def adjudicate(fixture: str, limit: int | None, row: int | None, use_llm: bool,
               model: str | None, base_url: str | None) -> None:
    """Свод по позиции: извлечь цены (модель) → надёжная итоговая цена + диапазон (min–max).

    Завершает M4. Оффлайн по кэшу fetch. Отсекает фейки/приманки и выбросы (детерминированно).
    """
    from .storage import discovery_cache
    from .extract import extract_prices
    from .extract.adjudicate import adjudicate as do_adjudicate
    from .config import load_settings
    from .llm.client import build_client
    settings = load_settings()
    if base_url:
        settings.llm.base_url = base_url
    llm = build_client(settings) if use_llm else None

    items = discovery_cache.load(fixture)
    if row is not None:
        items = [it for it in items if it.get("row") == row]
    elif limit:
        items = items[:limit]
    click.echo("Свод цен по %d товарам (модель: %s)\n"
               % (len(items), (model or settings.llm.model or "—") if use_llm
                  else "нет (структурные метки)"))

    async def go() -> None:
        for it in items:
            item = _Item(it)
            all_prices = []
            for cand in it.get("candidates", []):
                if cand.get("is_file"):
                    continue
                prices = await extract_prices(item, cand, llm_client=llm, cfg=settings.extract, model=model)
                all_prices.extend(prices)
            v = do_adjudicate(item, all_prices)
            click.echo(click.style("r%s  %s" % (it.get("row"), (it.get("name") or "")[:64]), bold=True))
            if v.primary is None:
                click.echo(click.style("     цена не установлена (нет валидных предложений)", fg="yellow"))
            else:
                p = v.primary
                click.echo("     %s %s%s  [%s]  достоверность %.2f  · подтв. доменов: %d"
                           % (click.style("ИТОГ:", fg="green"), p.value,
                              {"RUB": "₽"}.get(p.currency, " " + p.currency),
                              " (%s)" % (p.match.value if p.match else "не проверено"),
                              v.confidence or 0, v.corroborated_by))
                click.echo("     диапазон: %s–%s %s · источник итога: %s"
                           % (v.price_min, v.price_max, v.currency, p.source_domain))
            if v.excluded:
                click.echo("     отсеяно (%d):" % len(v.excluded))
                for e in v.excluded[:5]:
                    click.echo("        %s%s — %s" % (e["value"], v.currency, e["reason"]))
            click.echo("")

    asyncio.run(go())
    click.echo("Готово.")


@cli.command()
@click.argument("fixture")
@click.option("--out", default=None, help="путь к .xlsx (по умолчанию рядом с фикстурой)")
@click.option("--limit", default=None, type=int, help="сколько товаров включить")
@click.option("--row", default=None, type=int, help="только один товар (номер строки)")
@click.option("--llm/--no-llm", "use_llm", default=False,
              help="звать модель (без неё комментарий пишется детерминированной сводкой)")
@click.option("--model", default=None, help="имя модели (для LM Studio, напр. qwen3.5-9b)")
@click.option("--base-url", default=None, help="base_url модели (напр. локальный LM Studio)")
@click.option("--no-model", is_flag=True, help="комментарий детерминированной сводкой, без модели")
def report(fixture: str, out: str | None, limit: int | None, row: int | None,
           use_llm: bool, model: str | None, base_url: str | None, no_model: bool) -> None:
    """Итоговый Excel-отчёт (M7): своды по позициям + комментарий модели по источникам → .xlsx.

    Колонки: Товар, Номер, Цена, Диапазон найденных цен, Комментарий модели по источникам.
    Если у позиций ещё нет свода — считает его (extract → adjudicate) по кэшу fetch.
    """
    import os

    from .storage import discovery_cache
    from .extract import extract_and_adjudicate
    from .report import build_report
    from .llm.client import build_client
    settings = load_settings()
    if base_url:
        settings.llm.base_url = base_url
    if no_model:
        settings.report.use_model = False
    llm = build_client(settings) if use_llm else None

    items = discovery_cache.load(fixture)
    if row is not None:
        items = [it for it in items if it.get("row") == row]
    elif limit:
        items = items[:limit]
    out = out or (os.path.splitext(fixture)[0] + "_отчёт.xlsx")
    click.echo("Отчёт по %d товарам (модель: %s) → %s\n"
               % (len(items), (model or settings.llm.model or "—") if llm else "нет (сводка)", out))

    async def go() -> dict:
        need = [it for it in items if not it.get("verdict")]
        if need:
            click.echo("Считаю своды по %d позициям (нет готового вердикта)…" % len(need))
            await extract_and_adjudicate(need, llm_client=llm, cfg=settings.extract, model=model)
            from .storage import persist_run_async       # M5: сохранить свежий прогон в историю
            h = await persist_run_async(items, source="report:" + os.path.basename(fixture),
                                        cfg=settings.storage)
            if h.get("run_id"):
                click.echo("История: прогон #%d сохранён (офферов %d, dead-letter %d)"
                           % (h["run_id"], h.get("offers", 0), h.get("dead_letter", 0)))
        # В CLI модель просят ЯВНО (--llm) — значит, комментарии просят у неё же.
        # В интерфейсе наоборот: комментарии пишет прогон, а кнопка «Отчёт» только собирает файл.
        return await build_report(items, out, llm_client=llm, model=model, cfg=settings.report,
                                  use_model=llm is not None)

    summary = asyncio.run(go())
    click.echo(click.style("\nГотово: %s" % summary["path"], fg="green"))
    click.echo("Позиций: %d · с ценой: %d · не найдено: %d"
               % (summary["total"], summary["with_price"], summary["not_found"]))


@cli.command()
@click.option("--item", "item_name", default=None, help="история цен по товару (по имени)")
@click.option("--pn", default=None, help="история цен по товару (по парт-номеру/артикулу)")
@click.option("--dead", is_flag=True, help="показать dead-letter (не найдено/заблокировано)")
@click.option("--runs", "show_runs", is_flag=True, help="список последних прогонов")
@click.option("--db", default=None, help="путь к БД истории (по умолчанию из настроек)")
def history(item_name: str | None, pn: str | None, dead: bool, show_runs: bool,
            db: str | None) -> None:
    """История прогонов (M5): все цены/своды по товару, dead-letter, список прогонов.

    Без опций — общая статистика. С --item/--pn — динамика итоговой цены товара во времени.
    """
    from .storage import HistoryStore
    settings = load_settings()
    store = HistoryStore(db or settings.storage.db_path)
    try:
        if item_name or pn:
            hist = store.price_history(name=item_name, part_number=pn)
            title = pn or item_name
            click.echo(click.style("История цен: %s (%d записей)\n" % (title, len(hist)), bold=True))
            for v in hist:
                when = (v.get("run_started") or v.get("created_at") or "")[:16].replace("T", " ")
                if v["primary_value"] is None:
                    click.echo("  %s  — не найдено (%s)" % (when, v.get("not_found_reason") or "?"))
                else:
                    rng = ""
                    if v["price_min"] is not None and v["price_max"] is not None and v["price_min"] != v["price_max"]:
                        rng = " · диапазон %s–%s" % (v["price_min"], v["price_max"])
                    click.echo("  %s  %s %s  [%s]  достоверность %.2f%s"
                               % (when, v["primary_value"], v["currency"], v.get("match") or "?",
                                  v.get("confidence") or 0, rng))
            return
        if dead:
            rows = store.dead_letters()
            click.echo(click.style("Dead-letter (нерешённых: %d)\n" % len(rows), bold=True))
            for d in rows:
                click.echo("  r%s  %s — %s (%s)" % (d.get("row"), (d.get("name") or "")[:50],
                                                    d.get("reason"), d.get("detail") or ""))
            return
        if show_runs:
            rows = store.recent_runs()
            click.echo(click.style("Последние прогоны (%d)\n" % len(rows), bold=True))
            for r in rows:
                click.echo("  #%d  %s  %s  позиций=%s, с ценой=%s, не найдено=%s"
                           % (r["id"], (r["started"] or "")[:16].replace("T", " "),
                              r.get("source") or "", r.get("total"), r.get("with_price"),
                              r.get("not_found")))
            return
        s = store.stats()
        click.echo(click.style("История поиска цен\n", bold=True))
        click.echo("  прогонов: %d · товаров отслеживается: %d" % (s["runs"], s["items_tracked"]))
        click.echo("  сохранено офферов: %d · сводов: %d" % (s["offers"], s["verdicts"]))
        click.echo("  dead-letter (открытых): %d" % s["dead_letter_open"])
        click.echo("\nПодсказки: --runs · --dead · --item «имя» · --pn АРТИКУЛ")
    finally:
        store.close()


@cli.command()
@click.argument("input_path")
@click.option("--limit", default=None, type=int, help="сколько товаров обработать")
@click.option("--row", default=None, type=int, help="только один товар (номер строки)")
@click.option("--model", default=None, help="имя модели (для LM Studio, напр. qwen3.5-9b)")
@click.option("--base-url", default=None, help="base_url модели (напр. локальный LM Studio)")
@click.option("--level", default=None, type=click.Choice(["fast", "basic", "strong", "max"]),
              help="режим открытия сайтов (антиблок)")
@click.option("--max-steps", default=None, type=int, help="предел шагов агента на товар")
def react(input_path: str, limit: int | None, row: int | None,
          model: str | None, base_url: str | None, level: str | None, max_steps: int | None) -> None:
    """ReAct-агент (M6): модель САМА ведёт поиск цены, вызывая скилы (find_sources→fetch→extract→свод).

    input — .xlsx/.csv (агент ищет источники сам) или .json-фикстура discovery (кандидаты готовы).
    Требует подключения к модели. Реальные вызовы модели инициирует пользователь.
    """
    import os

    from .config import load_settings, resolve_fetch
    from .fetch.fetcher import Fetcher
    from .llm.client import build_client, close_client
    from .agent import run_react

    settings = load_settings()
    if base_url:
        settings.llm.base_url = base_url
    if max_steps:
        settings.react.max_steps = max_steps
    llm = build_client(settings)

    if input_path.lower().endswith(".json"):
        from .storage import discovery_cache
        items = discovery_cache.load(input_path)
    else:
        from .input import read_table
        res = read_table(input_path)
        items = [{"row": it.row, "name": it.name, "part_number": it.part_number, "raw": it.raw}
                 for it in res.items]
    if row is not None:
        items = [it for it in items if it.get("row") == row]
    elif limit:
        items = items[:limit]
    click.echo("ReAct-агент по %d товарам (модель: %s, режим: %s)\n"
               % (len(items), model or settings.llm.model or "—", level or "по умолчанию"))

    def emit(ev: dict) -> None:
        t = ev.get("type")
        if t == "agent_step" and ev.get("action"):
            th = (" · %s" % ev["thought"]) if ev.get("thought") else ""
            click.echo(click.style("  r%s шаг %s → %s" % (ev.get("row"), ev.get("step"), ev["action"]),
                                   fg="cyan") + th)
        elif t == "agent_observation":
            click.echo("      %s" % (ev.get("observation") or "")[:160])
        elif t == "verdict":
            if ev.get("primary") is None:
                click.echo(click.style("  r%s ИТОГ: не найдено — %s" % (ev.get("row"), ev.get("not_found_reason")),
                                       fg="yellow"))
            else:
                click.echo(click.style("  r%s ИТОГ: %s %s [%s]" % (
                    ev.get("row"), ev["primary"], ev.get("currency"), ev.get("match")), fg="green"))

    async def go():
        cfg = resolve_fetch(settings.fetch, {"preset": level} if level else None)
        fetcher = await Fetcher(cfg, settings.clean).start()
        try:
            return await run_react(items, settings=settings, fetcher=fetcher, llm_client=llm,
                                   model=model, emit=emit)
        finally:
            await fetcher.close()
            await close_client(llm)

    asyncio.run(go())
    click.echo(click.style("\nГотово.", fg="green"))


class _Item:
    """Лёгкая обёртка товара из фикстуры под интерфейс extract (row/name/part_number/raw)."""
    def __init__(self, d: dict) -> None:
        self.row = d.get("row")
        self.name = d.get("name") or ""
        self.part_number = d.get("part_number")
        self.raw = d.get("raw") or {}


@cli.command()
@click.argument("url")
def probe(url: str) -> None:
    """Health-check источника: доступен ли, не блокирует ли."""
    from .ops.probe import probe as do_probe
    res = asyncio.run(do_probe(url))
    for k, v in res.items():
        click.echo("  %-13s %s" % (k, v))


@cli.command()
@click.argument("question")
@click.option("--chat", "chat_id", default=None, help="продолжить существующий диалог (id)")
@click.option("--model", default=None, help="имя модели (по умолчанию — выбранная в настройках)")
@click.option("--level", default=None, type=click.Choice(["fast", "basic", "strong", "max"]),
              help="режим открытия сайтов (антиблок)")
@click.option("--max-steps", default=None, type=int, help="предел шагов на один ход")
@click.option("--no-store", is_flag=True, help="не сохранять диалог в runs/chats.sqlite")
def chat(question: str, chat_id: str | None, model: str | None, level: str | None,
         max_steps: int | None, no_store: bool) -> None:
    """Чат-исследователь (M9): один ход диалога из терминала.

    Модель сама ищет, читает страницы и проверяет источники данных, а затем отвечает разбором,
    где каждое утверждение подкреплено ссылкой. Требует подключения к модели — реальные вызовы
    инициирует пользователь.
    """
    import json

    from .config import load_settings, resolve_fetch
    from .fetch.fetcher import Fetcher
    from .llm.client import build_client, close_client
    from .research.agent import ResearchAgent
    from .research.store import ChatState, open_store
    from .tools import ToolContext

    settings = load_settings()
    if max_steps:
        settings.research.max_steps = max_steps
    llm = build_client(settings)

    store = None if no_store else open_store(settings.storage)
    if store is not None and store.available:
        state = (store.load(chat_id) if chat_id else None) or \
            ChatState(chat_id or store.create(title=question[:80], model=model or settings.llm.model))
    else:
        state = ChatState(chat_id or "mem")
    if state.pending_question:
        click.echo(click.style("Агент ждал ответа на: %s" % state.pending_question, fg="yellow"))

    def emit(ev: dict) -> None:
        t = ev.get("type")
        if t == "chat_step":
            th = (" · %s" % ev["thought"]) if ev.get("thought") else ""
            click.echo(click.style("  шаг %s → %s" % (ev.get("step"), ev.get("tool")), fg="cyan")
                       + th + " " + json.dumps(ev.get("args") or {}, ensure_ascii=False)[:120])
        elif t == "chat_observation":
            click.echo("      %s" % (ev.get("text") or "").replace("\n", " ")[:200])
        elif t == "chat_source":
            click.echo(click.style("      + [%s] %s" % (ev.get("sid"), ev.get("url")), fg="blue"))
        elif t == "chat_dossier":
            click.echo(click.style("      ~ %s: %s" % (ev.get("domain"), ev.get("verdict")),
                                   fg="magenta"))

    async def go():
        cfg = resolve_fetch(settings.fetch, {"preset": level} if level else None)
        fetcher = await Fetcher(cfg, settings.clean).start()
        try:
            ctx = ToolContext(settings=settings, llm_client=llm, emit=emit, fetcher=fetcher,
                              model=model)
            agent = ResearchAgent(ctx, cfg=settings.research, chat=state,
                                  on_message=(lambda m: store.append(state.chat_id, m))
                                  if (store is not None and store.available) else None)
            return await agent.turn(question)
        finally:
            await fetcher.close()
            await close_client(llm)

    click.echo("Диалог %s · модель %s\n" % (state.chat_id, model or settings.llm.model or "—"))
    res = asyncio.run(go())
    if store is not None and store.available:
        store.save_sources(state.chat_id, state.registry)
        store.save_dossiers(state.chat_id, state.dossiers)
        store.touch(state.chat_id)

    click.echo("")
    if res.get("paused"):
        click.echo(click.style("ВОПРОС: %s" % res["question"], fg="yellow"))
        if res.get("options"):
            click.echo("  варианты: %s" % ", ".join(res["options"]))
        click.echo("\nОтветьте: search-agent chat «ваш ответ» --chat %s" % state.chat_id)
        return
    click.echo(res.get("answer") or "(пусто)")
    cit = res.get("citations") or {}
    if not cit.get("ok", True):
        click.echo(click.style("\n⚠ без ссылок: %d, выдуманных ссылок: %d"
                               % (len(cit.get("uncited") or []), len(cit.get("unknown") or [])),
                               fg="yellow"))
    u = res.get("usage") or {}
    click.echo("\nШагов: %s · источников: %s · токенов: %s"
               % (res.get("steps"), len(res.get("sources") or []), u.get("total_tokens")))


@cli.command("probe-source")
@click.argument("url")
@click.option("--need", required=True, help="какие данные нужны (напр. «цены закупок на насосы»)")
@click.option("--period", default=None, help="требуемый период: «2022-2025», «с 2020»")
@click.option("--model", default=None, help="имя модели (по умолчанию — выбранная в настройках)")
@click.option("--level", default=None, type=click.Choice(["fast", "basic", "strong", "max"]),
              help="режим открытия сайтов (антиблок)")
@click.option("--shallow", is_flag=True, help="не обходить разделы источника")
@click.option("--no-model", is_flag=True, help="только детекторы, без проверки моделью")
def probe_source_cmd(url: str, need: str, period: str | None, model: str | None,
                     level: str | None, shallow: bool, no_model: bool) -> None:
    """Аттестация источника данных: есть ли там нужные данные и за какой период.

    Проверку выносит МОДЕЛЬ, а её цитаты-доказательства сверяются с текстом страницы. Без модели
    (--no-model) источник честно помечается «не проверено» — детекторы только собирают факты.
    """
    from .config import load_settings, resolve_fetch
    from .fetch.fetcher import Fetcher
    from .llm.client import build_client, close_client
    from .research.probe import ACCESS_RU
    from .tools import ToolContext, registry

    settings = load_settings()
    llm = None if no_model else build_client(settings)

    async def go():
        cfg = resolve_fetch(settings.fetch, {"preset": level} if level else None)
        fetcher = await Fetcher(cfg, settings.clean).start()
        try:
            ctx = ToolContext(settings=settings, llm_client=llm, fetcher=fetcher, model=model)
            return await registry.get("probe_source").run(
                ctx, url=url, need=need, period=period, deep=not shallow)
        finally:
            await fetcher.close()
            await close_client(llm)

    d = asyncio.run(go())
    click.echo(click.style("\n%s — %s" % (d.get("domain") or url, d.get("verdict")), bold=True))
    click.echo("  что есть      %s" % (", ".join(d.get("kinds") or []) or "—"))
    period_info = d.get("period") or {}
    click.echo("  период        %s (%s)" % (period_info.get("seen") or "—",
                                            period_info.get("status") or "—"))
    click.echo("  доступ        %s" % ACCESS_RU.get(d.get("access"), d.get("access") or "—"))
    click.echo("  выгрузка      %s" % (", ".join(d.get("export") or []) or "—"))
    click.echo("  как добраться %s" % (d.get("how_to") or "—"))
    click.echo("  ограничения   %s" % (d.get("limits") or "—"))
    click.echo("  проверено     %s" % ", ".join(d.get("checked_urls") or []))
    if d.get("evidence"):
        click.echo("  доказательства:")
        for line in d["evidence"][:5]:
            click.echo("    · %s" % line[:160])
    else:
        click.echo(click.style("  доказательств нет — источник не подтверждён", fg="yellow"))
    if d.get("note"):
        click.echo("  примечание    %s" % d["note"])


if __name__ == "__main__":
    cli()
