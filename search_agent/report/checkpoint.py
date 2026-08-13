# -*- coding: utf-8 -*-
"""Промежуточные сохранения прогона: готовый отчёт и остаток исходного файла — на диск, по ходу.

Зачем. Файл на 2500 позиций считается часами. Сбой питания, падение браузера, случайное закрытие
окна — и сделанная работа существует только в сессии, которую ещё надо восстановить. Поэтому
каждые N закрытых позиций на диск ложатся ДВА готовых файла:

  • «отчёт.xlsx» — ровно то, что отдала бы кнопка «Отчёт» (листы «Итоги» и «Детали») по уже
    закрытым позициям. Отдельного формата не заводим: на сбое пользователь должен получить
    привычный файл, а не служебный дамп, который потом надо во что-то превращать;
  • «осталось_найти.xlsx» — исходная таблица МИНУС обработанные строки. Её можно тут же загрузить
    в инструмент и продолжить с того места, где остановились, ничего не вычёркивая руками.

Оба файла перезаписываются целиком и атомарно (запись во временный файл рядом → `os.replace`):
частично записанный .xlsx не откроется, а именно за него пользователь и хватается после сбоя.

Остаток собирается из `item["raw"]` — исходных ячеек строки, которые `input/excel_reader` кладёт
в позицию, а сессия сохраняет. Поэтому он переживает перезапуск инструмента и не зависит от того,
жив ли ещё загруженный временный файл. Плата: оформление исходника не переносится (состав и
порядок колонок — переносятся).
"""
import os
import re
import time
from pathlib import Path

from .xlsx import build_detail_rows, build_rows, build_site_rows, write_xlsx
from ..obs.log import get_logger

log = get_logger("report.checkpoint")

ROOT = Path(__file__).resolve().parent.parent.parent
REPORT_NAME = "отчёт.xlsx"
REMAINDER_NAME = "осталось_найти.xlsx"

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _safe(name: str, limit: int = 60) -> str:
    """Имя, пригодное для папки на любой файловой системе."""
    out = _BAD_CHARS.sub("_", str(name or "")).strip(" .")
    return (out[:limit] or "прогон").strip()


def run_dir(filename: str, *, base: str = "результаты", root: Path | None = None,
            create: bool = False) -> Path:
    """Путь папки прогона: <корень>/<база>/<имя файла>__<дата_время>.

    Отдельная папка на прогон, а не общая свалка: прошлые результаты не затираются и остаются
    рядом со своим исходником — их можно сравнить, а не гадать, к какому запуску относится файл.

    По умолчанию только СЧИТАЕТ путь, не создавая каталог: иначе каждый запуск инструмента и
    каждая загрузка файла оставляли бы пустую папку, даже если поиск так и не запускали. Каталог
    заводит первое реальное сохранение (`save`).
    """
    stem = _safe(os.path.splitext(os.path.basename(filename or ""))[0] or "прогон")
    path = (root or ROOT) / base / ("%s__%s" % (stem, time.strftime("%Y-%m-%d_%H-%M")))
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic(path: Path, write) -> None:
    """Записать файл через временный рядом и переставить на место (частичный .xlsx не откроется)."""
    tmp = path.with_name(path.name + ".tmp")
    write(str(tmp))
    os.replace(tmp, path)


def _headers(items: list[dict], headers: list[str] | None) -> list[str]:
    """Заголовки исходной таблицы: из разбора файла, иначе — по ключам сохранённых строк."""
    if headers:
        return list(headers)
    out: list[str] = []
    for it in items:                                   # порядок ключей = порядок колонок исходника
        for key in (it.get("raw") or {}):
            if key not in out:
                out.append(key)
    return out


def write_remainder(items: list[dict], path: Path, *, headers: list[str] | None = None,
                    done_rows: set | None = None) -> int:
    """Записать исходную таблицу без обработанных строк. Возвращает число оставшихся позиций."""
    from openpyxl import Workbook

    cols = _headers(items, headers)
    left = [it for it in items if it.get("row") not in (done_rows or set())]

    def write(target: str) -> None:
        wb = Workbook()
        ws = wb.active
        ws.title = "Позиции"
        ws.append(cols)
        for it in left:
            raw = it.get("raw") or {}
            # Позиции без сохранённых исходных ячеек (сессия прежней версии) не теряем: в файле
            # должно остаться хотя бы наименование — иначе строка молча исчезнет.
            if not raw:
                raw = {cols[0] if cols else "Наименование": it.get("name")}
            ws.append([raw.get(c) for c in cols])
        wb.save(target)

    _atomic(path, write)
    return len(left)


def save(items: list[dict], directory: Path | str, *, headers: list[str] | None = None) -> dict:
    """Сохранить оба файла по текущему состоянию позиций. Возвращает сводку для лога/UI.

    Обработанной считается позиция со сводом — независимо от того, нашлась цена или нет: в отчёт
    она уже попала, и повторно искать её незачем (иначе остаток никогда не сойдётся с отчётом).
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    done = [it for it in items if it.get("verdict")]
    done_rows = {it.get("row") for it in done}

    rows = build_rows(done)
    _atomic(directory / REPORT_NAME,
            lambda target: write_xlsx(rows, target, detail_rows=build_detail_rows(done),
                                      site_rows=build_site_rows(done)))
    left = write_remainder(items, directory / REMAINDER_NAME, headers=headers, done_rows=done_rows)

    summary = {"dir": str(directory), "report": str(directory / REPORT_NAME),
               "remainder": str(directory / REMAINDER_NAME),
               "done": len(done), "left": left, "total": len(items),
               "with_price": sum(1 for r in rows if r["_found"])}
    log.info("Промежуточное сохранение: готово %d из %d (с ценой %d), осталось %d → %s",
             summary["done"], summary["total"], summary["with_price"], left, directory)
    return summary
