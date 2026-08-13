# -*- coding: utf-8 -*-
"""Модели данных агента (pydantic v2).

Единый словарь типов для всех слоёв: позиция из Excel → запросы → кандидаты →
цены → результат по позиции → отчёт по прогону.
"""
from enum import Enum

from pydantic import BaseModel, Field


class MatchType(str, Enum):
    """Соответствие найденного оффера искомой позиции (решает модель, §10.4 ТЗ)."""
    EXACT = "точное"
    ANALOG = "аналог"
    NONE = "не найдено"


class NotFoundReason(str, Enum):
    """Причина, почему цена не найдена (указываем явно, не предполагаем; §10.5 ТЗ)."""
    BLOCKED = "заблокировано"        # жёсткий антибот, источник не открылся
    NO_OFFERS = "нет офферов"        # искали, предложений с ценой нет
    AMBIGUOUS = "неоднозначно"       # нашли, но не удалось подтвердить товар
    NOT_SEARCHED = "не искали"       # прогон не дошёл/прерван — надо доискать


class Item(BaseModel):
    """Товар из входного файла (универсально: любая структура).

    Обязательно только наименование. Парт-номер — опционально. Все исходные ячейки
    строки сохраняем в raw (отчёт зеркалит вход). Ничего не «исключаем» — каждая строка
    с наименованием ищется.
    """
    row: int                                   # номер строки в исходнике
    name: str                                  # наименование / модель
    part_number: str | None = None             # парт-номер / SKU / артикул (может отсутствовать)
    raw: dict = Field(default_factory=dict)    # исходные ячейки строки (для отчёта)


class Query(BaseModel):
    """Вариант поискового запроса по позиции."""
    item_row: int
    text: str
    kind: str = "generic"                      # partnumber | name_buy | vendor | category


class Candidate(BaseModel):
    """Кандидат из обнаружения (страница или файл) до загрузки."""
    url: str
    title: str = ""
    snippet: str = ""
    engine: str = ""                           # какой движок/адаптер дал результат
    domain: str = ""
    is_file: bool = False                      # прямая ссылка на файл-прайс (xlsx/csv/pdf)
    tier: int | None = None                    # тир источника (из sources.yaml), заполняет rerank
    weight: float | None = None                # вес доверия источника
    score: float | None = None                 # итоговый ранг кандидата


class PriceCandidate(BaseModel):
    """Найденная цена с атрибутами источника и доверия (храним ВСЕ; §10.2 ТЗ)."""
    value: float
    currency: str = "RUB"
    source_domain: str
    url: str
    tier: int = 4
    weight: float = 0.3                        # рейтинг источника (из sources.yaml)
    # None — соответствие НЕИЗВЕСТНО (модель товар не сверяла). Отличать от «точное» и
    # «аналог» обязательно: раньше непроверенная цена уезжала в отчёт как точное совпадение.
    match: MatchType | None = MatchType.EXACT  # точное | аналог | None (не проверялось)
    in_stock: bool | None = None
    vat: bool | None = None                    # цена с НДС?
    date: str | None = None
    snippet: str = ""                          # комментарий модели (сравнение искал/нашёл)
    found: str = ""                            # название найденной позиции ДОСЛОВНО со страницы
    extraction_confidence: float | None = None
    # Единица, ЗА КОТОРУЮ названа цена («шт», «м³», «упаковка», «м»). Без неё цены несравнимы:
    # 6500 ₽ за куб газобетона и 176 ₽ за блок — про один товар, но складывать их нельзя.
    unit: str = ""
    # Страница похожа на предложение к покупке (корзина/заказ/schema.org Offer), а не на обзор
    # или поисковую выдачу. У обзоров цены настоящие, но доверие к ним ниже.
    is_offer: bool | None = None
    # Цену назвала МОДЕЛЬ, сверив товар со страницей. False — цена взята из структурной разметки,
    # когда модель была недоступна: соответствие товару не проверял никто. Свод обязан это
    # учитывать (см. adjudicate), а отчёт — показывать; молча выдавать такое за «точное» нельзя.
    verified: bool = True
    unverified_reason: str = ""                # чем именно кончилась попытка спросить модель


class Verdict(BaseModel):
    """Свод по позиции: надёжная цена + диапазон (результат adjudicate; §10.2/10.6 ТЗ).

    primary — наиболее достоверная цена (тир источника + корроборация, после отсева фейков).
    price_min/price_max — диапазон валидных цен. excluded — отсеянные с причиной (прозрачность).
    """
    primary: PriceCandidate | None = None
    price_min: float | None = None
    price_max: float | None = None
    currency: str = "RUB"
    confidence: float | None = None                  # доверие к итогу (0..1)
    corroborated_by: int = 0                          # сколько независимых доменов подтвердили итог
    excluded: list[dict] = Field(default_factory=list)  # [{value, domain, reason}] — отсеянные
    unit: str = ""                                   # единица, за которую названа итоговая цена
    # Родовая позиция (бренд/модель не заданы): «диапазон» по ней — это разброс КАТЕГОРИИ, а не
    # цены товара. Показываем срединный коридор, а не крайние значения, и говорим об этом прямо.
    market_corridor: bool = False
    price_median: float | None = None
    # Решение по КАЖДОЙ цене, дошедшей до свода: принята или на каком шаге отсеяна, с каким баллом
    # и из чего этот балл сложился. Без этого итог невозможно проверить: отчёт показывал цену,
    # которой не было ни в одной строке детализации, и объяснить её могла только пересборка вручную.
    # Формат записи — DECISION_FIELDS в extract/adjudicate.py; excluded — проекция этого списка.
    scored: list[dict] = Field(default_factory=list)
    # Балл победителя совпал с баллом соперника: выбор доопределён правилом тай-брейка, а не
    # качеством источника. Такой итог помечается в отчёте — доверие к нему ниже.
    tie: bool = False
    # Итог — аналог, который никто не подтвердил и который найден на единственном домене
    # (цена похожего товара другого бренда). Цена в отчёте остаётся, но с явной пометкой.
    needs_review: bool = False


class ItemResult(BaseModel):
    """Результат по одной позиции: все найденные цены + свод + прозрачность попыток."""
    item: Item
    prices: list[PriceCandidate] = Field(default_factory=list)
    verdict: Verdict | None = None                   # свод (надёжная цена + диапазон)
    match: MatchType = MatchType.NONE
    not_found_reason: NotFoundReason | None = None
    tried: list[str] = Field(default_factory=list)   # что пробовали (запросы/источники/причины)


class RunReport(BaseModel):
    """Сводка прогона."""
    total: int = 0
    with_price: int = 0
    not_found: int = 0
    results: list[ItemResult] = Field(default_factory=list)
