# -*- coding: utf-8 -*-
"""Адаптивное чтение входных таблиц (Excel/CSV) — УНИВЕРСАЛЬНО.

Инструмент работает с ЛЮБОЙ структурой файла (правило универсальности, ТЗ §2). Из строк
извлекаем ровно одно поле — **наименование**. Всё прочее сохраняем в `raw` (для отчёта).
Ничего НЕ исключаем: каждая строка с наименованием — товар для поиска. Колонка определяется
адаптивно (синонимы заголовков RU/EN + содержимое); строка заголовка ищется автоматически
(её может не быть — простой одноколоночный список). При неоднозначности — хук дизамбигуации
моделью (`llm_mapping_request`).

Артикул/парт-номер намеренно НЕ извлекаем. Угадывание «какая колонка артикул» ошибалось и
уводило поисковый запрос в сторону; кому он нужен в запросе — дописывает его прямо в
наименование в своём Excel, и тогда он участвует в поиске как часть названия.
"""
import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..models import Item
from ..obs.log import get_logger, log_event, timed

log = get_logger("input.excel_reader")

# Синонимы логических полей (RU/EN). Инструменту нужно ровно одно поле — наименование.
# Сопоставляются с нормализованным заголовком (ниж. регистр, без пунктуации).
FIELD_SYNONYMS: dict[str, list[str]] = {
    "name": ["наименование", "название", "номенклатура", "товар", "позиция", "описание",
             "модель", "предмет", "item", "name", "product", "description", "goods", "title"],
}

# Слова, по которым узнаётся СТРОКА ЗАГОЛОВКА, но которые не отображаются ни в какое поле.
# Нужны отдельно: «Артикул» перестал быть полем, а признаком шапки быть не перестал. Без них
# файл вида «Артикул | Материал» терял заголовок и его шапка становилась первым товаром.
HEADER_HINTS: tuple[str, ...] = (
    "артикул", "парт номер", "партномер", "sku", "код товара", "код", "pn", "part number",
    "partnumber", "article", "каталожный номер", "кат номер", "mpn", "vendor code",
    "цена", "стоимость", "price", "кол во", "количество", "qty", "quantity",
    "ед изм", "единица", "unit", "производитель", "бренд", "brand", "vendor", "поставщик",
)

_PUNCT = re.compile(r"[^\w\s]", re.U)
_WS = re.compile(r"\s+", re.U)


@dataclass
class ReadResult:
    items: list[Item]
    mapping: dict[str, int]          # логическое поле -> индекс колонки (0-based)
    headers: list[str]              # заголовки как есть (или col1..colN, если заголовка нет)
    header_row: int                 # 1-based строка заголовка; 0 — заголовка нет
    sheet: str | None = None
    unmapped: list[str] = field(default_factory=list)   # заголовки без сопоставления → raw
    confidence: str = "high"        # high | medium | low(needs_llm)
    rows_total: int = 0             # непустых строк в файле (с заголовком) — для диагностики


def _norm(s) -> str:
    if s is None:
        return ""
    return _WS.sub(" ", _PUNCT.sub(" ", str(s).strip().lower())).strip()


def _match_field(header_norm: str) -> str | None:
    """Логическое поле для нормализованного заголовка (по вхождению синонима)."""
    compact = header_norm.replace(" ", "")     # «q ty»→«qty», «кат номер»→«катномер»
    best, best_len = None, 0
    for field_name, syns in FIELD_SYNONYMS.items():
        for syn in syns:
            syn_c = syn.replace(" ", "")
            if (syn == header_norm or syn in header_norm.split() or syn in header_norm
                    or syn_c == compact or syn_c in compact):
                if len(syn) > best_len:
                    best, best_len = field_name, len(syn)
    return best


def _looks_like_header_cell(cell) -> bool:
    """Ячейка похожа на заголовок колонки: либо это наше поле, либо известное служебное слово."""
    h = _norm(cell)
    if not h:
        return False
    if _match_field(h):
        return True
    compact = h.replace(" ", "")
    return any(w == h or w in h.split() or w.replace(" ", "") == compact for w in HEADER_HINTS)


def _find_header_row(rows: list[tuple]) -> tuple[int, int]:
    """(индекс 0-based, число распознанных заголовков) лучшей строки-заголовка.

    Если распознанных заголовков нет нигде — score 0: заголовка нет (простой список),
    все строки считаем данными.
    """
    best_idx, best_metric, best_score = 0, -1, 0
    for i, row in enumerate(rows[:15]):
        score = sum(1 for c in row if _looks_like_header_cell(c))
        nonempty = sum(1 for c in row if _norm(c))
        metric = score * 10 + nonempty
        if score >= 1 and metric > best_metric:
            best_idx, best_metric, best_score = i, metric, score
    return best_idx, best_score


def _map_columns(header: tuple) -> tuple[dict, list[str]]:
    mapping: dict[str, int] = {}
    unmapped: list[str] = []
    for idx, cell in enumerate(header):
        fld = _match_field(_norm(cell))
        if fld and fld not in mapping:
            mapping[fld] = idx
        elif _norm(cell):
            unmapped.append(str(cell))
    return mapping, unmapped


def _content_name_column(data_rows: list[tuple], exclude: set[int]) -> int | None:
    """Фолбэк: колонка-наименование = самая «текстово-длинная» среди данных."""
    best_idx, best_avg = None, 0.0
    ncols = max((len(r) for r in data_rows), default=0)
    for idx in range(ncols):
        if idx in exclude:
            continue
        lengths = [len(str(r[idx])) for r in data_rows[:30]
                   if idx < len(r) and r[idx] is not None and not _is_number(r[idx])]
        if not lengths:
            continue
        avg = sum(lengths) / len(lengths)
        if avg > best_avg:
            best_idx, best_avg = idx, avg
    return best_idx if best_avg >= 4 else None


def _is_number(v) -> bool:
    if isinstance(v, (int, float)):
        return True
    try:
        float(str(v).replace(" ", "").replace(",", "."))
        return True
    except (ValueError, AttributeError):
        return False


def _load_rows(path: Path, sheet: str | None):
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xlsm", ".xltx"):
        import openpyxl
        # read_only не берём намеренно: в потоковом режиме openpyxl доверяет объявленному в файле
        # <dimension> и режет по нему и строки, и колонки. Экспортёры (напр. Яндекс Таблицы) пишут
        # туда «A1» при полном листе — от таблицы оставалась одна ячейка и товары терялись целиком.
        # Обычный режим считает границы по фактическим ячейкам и выравнивает строки по ширине.
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb[sheet] if sheet else wb.active
        rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
        name = ws.title
        wb.close()
        return rows, name
    if ext == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = [tuple(r) for r in csv.reader(fh)]
        return rows, None
    raise ValueError("Неподдерживаемый формат: %s (нужны .xlsx/.csv)" % ext)


def read_table(path: str | Path, sheet: str | None = None,
               mapping_override: dict[str, int] | None = None) -> ReadResult:
    """Прочитать таблицу и извлечь товары (Item[]) из любой структуры."""
    path = Path(path)
    with timed(log, "input.read", path=str(path), sheet=sheet):
        rows, sheet_name = _load_rows(path, sheet)
    rows = [r for r in rows if any(_norm(c) for c in r)]     # выкинуть пустые строки
    if not rows:
        log.warning("Файл пуст или без данных: %s", path)
        return ReadResult([], {}, [], 0, sheet_name, [], "low", 0)

    hdr_idx, hdr_score = _find_header_row(rows)
    has_header = hdr_score >= 1
    if has_header:
        header = rows[hdr_idx]
        data_rows = rows[hdr_idx + 1:]
        header_row_num, first_data_row = hdr_idx + 1, hdr_idx + 2
    else:
        header = tuple()
        data_rows = rows
        header_row_num, first_data_row = 0, 1

    if mapping_override:
        mapping, unmapped = dict(mapping_override), []
    else:
        mapping, unmapped = _map_columns(header)

    confidence = "high" if has_header else "medium"
    if "name" not in mapping:
        guess = _content_name_column(data_rows, exclude=set(mapping.values()))
        if guess is not None:
            mapping["name"] = guess
            confidence = "medium"
            log.warning("Колонка наименования не распознана по заголовку — взята по "
                        "содержимому (col%d).", guess)
        else:
            confidence = "low"      # needs_llm: структура непонятна
            log.warning("Структура файла неоднозначна: наименование не определено — нужна "
                        "дизамбигуация моделью (llm_mapping_request). Файл: %s", path)

    log_event(log, "input.mapping", header_row=header_row_num, has_header=has_header,
              mapping=mapping, confidence=confidence, unmapped=unmapped)

    headers = [("" if c is None else str(c)) for c in header]
    name_col = mapping.get("name")
    items: list[Item] = []
    for offset, row in enumerate(data_rows):
        name = "" if name_col is None or name_col >= len(row) or row[name_col] is None \
            else str(row[name_col]).strip()
        if not name:
            continue                # нет наименования — пустая/служебная строка файла
        # Исходная строка целиком уходит в raw: артикул из неё никуда не делся — он просто не
        # становится отдельным полем и не попадает в поисковый запрос.
        raw = {(headers[i] if i < len(headers) and headers[i] else "col%d" % i): v
               for i, v in enumerate(row)}
        items.append(Item(row=first_data_row + offset, name=name, raw=raw))

    log_event(log, "input.read.done", items=len(items), rows_data=len(data_rows),
              rows_file=len(rows))
    res = ReadResult(items, mapping, headers, header_row_num, sheet_name, unmapped, confidence,
                     len(rows))
    if not items:
        log.warning("Товаров не распознано: %s", explain_empty(res))
    return res


def explain_empty(res: ReadResult) -> str | None:
    """Почему в результате нет товаров. None — товары есть, объяснять нечего.

    Единственный источник формулировки: её показывают и веб, и CLI. Без неё пустой результат
    выглядел как поломка поиска, хотя причина всегда в структуре файла.
    """
    if res.items:
        return None
    if not res.rows_total:
        return "файл пуст: в нём нет ни одной непустой строки"
    if res.confidence == "low":
        seen = [h for h in res.headers if h]
        hint = (", заголовки: %s" % ", ".join(seen)) if seen else ""
        return ("колонка с наименованием не распознана (прочитано строк: %d%s)"
                % (res.rows_total, hint))
    if res.header_row and res.rows_total <= res.header_row:
        return "в файле только строка заголовка — строк с товарами нет"
    return ("прочитано строк: %d, но ни в одной нет непустого наименования" % res.rows_total)


def llm_mapping_request(headers: list[str], sample_rows: list[list]) -> list[dict]:
    """Сообщения для дизамбигуации колонки наименования моделью (вызывает пользователь).

    Модель возвращает ТОЛЬКО JSON {"name": <idx>}.
    Используется, когда эвристика не уверена (confidence == 'low').
    """
    preview = {"headers": headers, "sample": sample_rows[:5]}
    return [
        {"role": "system", "content":
            "Ты находишь в произвольной товарной таблице колонку с НАИМЕНОВАНИЕМ товара. "
            "Верни ТОЛЬКО JSON {\"name\": индекс_колонки_0based}."},
        {"role": "user", "content":
            "Заголовки и примеры строк:\n%s\nВерни маппинг колонок." % preview},
    ]
