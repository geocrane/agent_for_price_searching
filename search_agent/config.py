# -*- coding: utf-8 -*-
"""Конфигурация агента: .env (корень проекта) + config/*.yaml.

Профили (free/balanced/accurate) отложены — работаем на одном простом конфиге (§10.10 ТЗ).
"""
import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .obs.log import get_logger

log = get_logger("config")

ROOT = Path(__file__).resolve().parent.parent      # корень проекта search_agent/
CONFIG_DIR = ROOT / "config"
ENV_PATH = ROOT / ".env"


def _strip_inline_comment(val: str) -> str:
    """Отрезать пояснение в конце значения: `MODEL_AGENT=x/y   # id модели` → `x/y`.

    Так принято в dotenv: комментарием считается «#» ПОСЛЕ пробела, поэтому «#» внутри самого
    значения (например в пароле `a#b`) сохраняется. Значение в кавычках берём целиком до кавычки —
    внутри кавычек «#» тоже часть значения.

    Без этого .env, созданный из .env.example (а там у каждой строки пояснение), давал значения
    вида «zai-org/GLM-5.2       # id модели…»: API получал несуществующий id модели, запрос падал,
    и извлечение молча деградировало на структурные метки.
    """
    val = val.strip()
    for q in ('"', "'"):
        if val.startswith(q):
            end = val.find(q, 1)
            return val[1:end] if end > 0 else val[1:]
    for sep in ("  #", " #", "\t#"):
        i = val.find(sep)
        if i >= 0:
            return val[:i].strip()
    return val


def _load_dotenv(path: Path) -> dict:
    """Минимальный парсер .env (реальное окружение имеет приоритет)."""
    values: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            values[key.strip()] = _strip_inline_comment(val)
    except FileNotFoundError:
        pass
    return values


def _load_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}


def set_env_vars(updates: dict[str, str | None], path: Path = ENV_PATH) -> None:
    """Записать/обновить пары KEY=VALUE в .env, сохранив прочие строки и комментарии.

    Существующая строка `KEY=...` заменяется, отсутствующий ключ дописывается в конец.
    Значение None означает «не менять этот ключ» (пропускаем). Пустая строка — записать пусто.
    Используется UI для сохранения выбранной модели/URL и токена (FOUNDATION_KEY).
    """
    updates = {k: v for k, v in updates.items() if v is not None}
    if not updates:
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    seen: set[str] = set()
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        key = stripped.split("=", 1)[0].strip() if ("=" in stripped and not stripped.startswith("#")) else None
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(raw)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# Транспорт модели один — прямой HTTP к Foundation Models (OpenAI-совместимый), ключ
# FOUNDATION_KEY. Тот же путь ведёт к локальному LM Studio: достаточно сменить base_url.
FOUNDATION_BASE_URL = "https://foundation-models.api.cloud.ru/v1"
FOUNDATION_DEFAULT_MODEL = "zai-org/GLM-5.2"       # фолбэк, если каталог недоступен


class LLMConfig(BaseModel):
    base_url: str = FOUNDATION_BASE_URL
    model: str | None = None
    api_key: str = ""                              # токен модели (из .env FOUNDATION_KEY)
    # Предел ожидания ОДНОГО запроса к модели. У SDK дефолт — 600 с плюс ретраи: один залипший
    # вызов держал операцию десятки минут, и пользователь видел «долгое сохранение» без причины.
    timeout: float = 120.0
    # Повторы SDK ВЫКЛЮЧЕНЫ (было 1): они дублируют запрос мимо нашего лимитера ровно тогда,
    # когда сервер просит снизить нагрузку. Повторами управляет llm/limits.py.
    max_retries: int = 0

    # --- лимиты провайдера и политика на отказ (config/llm.yaml, см. llm/limits.py) ---
    # Квота берётся ДО отправки: учёт задним числом от 429 не защищает.
    rps: float = 3.0                               # запросов в секунду (паспорт 4)
    rpm: float = 80.0                              # запросов в минуту (паспорт 100)
    tpm: float = 700_000.0                         # токенов в минуту (паспорт 1 000 000)
    max_tokens: int = 12000                        # верхняя граница генерации (нужна для резерва)
    overload_backoff: list[float] = Field(default_factory=lambda: [2.0, 4.0, 8.0, 16.0, 32.0])
    overload_attempts: int = 6                     # повторов вызова до «модель недоступна»
    open_after: int = 3                            # отказов подряд до глобальной паузы
    probe_every: float = 15.0                      # период пробника доступности, с
    degrade_after_s: float = 900.0                 # предел терпения (0 — ждать сколько угодно)
    limits_up_after: int = 20                      # успехов подряд до возврата лимитов вверх


class DiscoveryConfig(BaseModel):
    """Слой обнаружения источников (config/discovery.yaml). Бэкенд один — Serper."""
    timeout: int = 15                  # сек на один запрос к поисковому API
    max_results: int = 20              # сколько кандидатов брать из выдачи одного запроса
    queries_per_item: int = 2          # вариантов запроса на товар
    top_n: int = 0                     # 0 = рассматриваем ВСЮ выдачу без ранжирования (по умолчанию).
                                       # Обрезка выбрасывала живые магазины ради маркетплейсов под
                                       # антиботом. >0 — аварийный лимит, если станет долго/дорого.
    llm_queries_batch: int = 40        # позиций в одном пакете при разборе наименований моделью.
                                       # Крупный пакет выгоден качеством: модель видит соседние
                                       # позиции и реже путает их между собой, а промпт платится
                                       # один раз на 40 строк. Ответ на такой пакет идёт минуты —
                                       # поэтому пакеты гоняются ПОСЛЕДОВАТЕЛЬНО, до начала поиска,
                                       # и дробятся пополам, если модель не уложилась в таймаут
                                       # (см. discovery/query_llm.py).
    concurrency: int = 4               # товаров одновременно (search-API надёжен → можно параллелить)
    default_affix: str = "цена купить"     # фраза-суффикс к наименованию, если не задана явно
    relevance_gate: bool = True        # не грузить страницы без единого общего слова с названием
    relevance_low: float = 0.2         # ниже этой доли совпадения — грузим в последнюю очередь


class FetchConfig(BaseModel):
    # --- движок / анти-блок (галочки «режима открытия») ---
    backend: str = "browser"           # browser (Playwright/patchright) | http (httpx)
    real_chrome: bool = True           # channel=chrome (настоящий Chrome вместо bundled Chromium)
    anti_detect: bool = False          # patchright: прячет CDP-следы (против DataDome/Ozon)
    headed: bool = False               # графический режим (реальное окно; на сервере — Xvfb)
    persistent_profile: bool = False   # постоянный профиль (куки-допуск накапливается)
    warmup: bool = True                # заход на главную домена ПЕРЕД ПОВТОРОМ после блока (куки)
    human_input: bool = False          # движения мыши/скролл (человекоподобность)
    wait_networkidle: bool = True      # дать JS дорисовать (только если цены в тексте ещё нет)
    http_first: bool = True            # сначала лёгкий httpx, браузер — если пришёл блок/пусто/
                                       # оболочка SPA (см. fetch/fetcher.py). Хуже «всегда браузер»
                                       # быть не может: на плохой ответ мы всё равно эскалируем.
    # --- числовые (best practices) ---
    timeout: float = 30.0
    http_timeout: float = 8.0          # предел ЛЁГКОГО пути (когда за ним стоит браузер). Смысл
                                       # лёгкой попытки — быстро ответить или быстро уступить:
                                       # ждать на ней все 30 с бессмысленно, браузер всё равно
                                       # пойдёт следом и потратит своё время сверху.
    # Пауза МЕЖДУ ЗАПРОСАМИ К ОДНОМУ ДОМЕНУ (не перед каждой страницей): на первый заход на домен
    # она не тратится, а дальше ждём лишь недостающее до интервала время.
    delay_range: list[float] = Field(default_factory=lambda: [8.0, 25.0])
    top_n_fetch: int = 0               # 0 = грузить ВСЕХ кандидатов (по умолчанию). Движок «Найти»
                                       # и так грузит всех; параметр остаётся для старого пайплайна
                                       # и как аварийный тормоз.
    concurrency: int = 2               # параллелизм между доменами (1-на-домен всё равно)
    retry_on_block: int = 1            # повторов при мягком блоке (капча/429) после cooldown
    cooldown_range: list[float] = Field(default_factory=lambda: [20.0, 40.0])


class CleanConfig(BaseModel):
    # --- сценарий очистки контента страницы перед LLM (галочки «сценария очистки») ---
    # Всё на структурных признаках (семантика/плотность ссылок/ценовой токен), без привязки к сайтам.
    strip_link_urls: bool = True       # <a> → его текст (URL убираем): объём ×0.5, цена не теряется
    drop_structural: bool = True       # удалять nav/aside/header/footer ТОЛЬКО если внутри нет цены
    drop_structural_force: bool = False  # удалять эти блоки безусловно (агрессивно, риск потери цены)
    remove_cookie_banner: bool = True  # удалять cookie/consent-баннеры (class*=cookie/consent, id*=…)
    link_density_prune: bool = False   # удалять div-«меню» без семантики: доля текста-в-ссылках >0.8 и нет цены
    max_chars: int = 60000             # верхний предел размера Markdown (страховка)


# Пресеты «сценария очистки» (от безопасного к максимальному). UI задаёт по имени, бэкенд разрешает.
CLEAN_PRESETS: dict[str, dict] = {
    "safe":     {"strip_link_urls": True, "drop_structural": False, "drop_structural_force": False,
                 "remove_cookie_banner": True, "link_density_prune": False},
    "balanced": {"strip_link_urls": True, "drop_structural": True, "drop_structural_force": False,
                 "remove_cookie_banner": True, "link_density_prune": False},
    "max":      {"strip_link_urls": True, "drop_structural": True, "drop_structural_force": True,
                 "remove_cookie_banner": True, "link_density_prune": True},
}


# Зависимости фильтров очистки: флаг → что должно быть включено, иначе он ничего не делает.
# «…даже если внутри есть цена» — уточнение к «удалять меню/шапку/подвал»: без него сам обход
# структурных блоков не выполняется (см. fetch/clean.py: проверка идёт ВНУТРИ drop_structural).
CLEAN_DEPS: dict[str, list[str]] = {
    "drop_structural_force": ["drop_structural"],
}


def normalize_clean(cfg: "CleanConfig") -> "CleanConfig":
    """Погасить фильтры, у которых снята предпосылка (см. CLEAN_DEPS)."""
    data = cfg.model_dump()
    for flag, needs in CLEAN_DEPS.items():
        if data.get(flag) and not all(data.get(n) for n in needs):
            data[flag] = False
            log.info("Очистка: «%s» отключено — не включено «%s»", flag, "», «".join(needs))
    return CleanConfig(**data)


def resolve_clean(base: "CleanConfig", opts: dict | None) -> "CleanConfig":
    """Собрать эффективный CleanConfig: дефолт → пресет (opts['preset']) → точечные поля opts.

    В конце — нормализация зависимостей: интерфейс блокирует несочетаемое, но запрос может
    прийти и от стороннего клиента или из устаревшего localStorage.
    """
    data = base.model_dump()
    opts = dict(opts or {})
    preset = opts.pop("preset", None)
    if preset and preset in CLEAN_PRESETS:
        data.update(CLEAN_PRESETS[preset])
    for k, v in opts.items():                     # точечные переопределения («Свой»)
        if k in CleanConfig.model_fields and v is not None:
            data[k] = v
    return normalize_clean(CleanConfig(**data))


# Пресеты «режима открытия» (от лёгкого к максимальному). UI задаёт по имени, бэкенд разрешает.
FETCH_PRESETS: dict[str, dict] = {
    "fast":   {"backend": "http", "anti_detect": False, "headed": False, "persistent_profile": False,
               "warmup": False, "human_input": False, "delay_range": [2.0, 6.0]},
    "basic":  {"backend": "browser", "real_chrome": True, "anti_detect": False, "headed": False,
               "persistent_profile": False, "warmup": True, "human_input": False,
               "http_first": True, "delay_range": [8.0, 25.0]},
    "strong": {"backend": "browser", "real_chrome": True, "anti_detect": True, "headed": False,
               "persistent_profile": True, "warmup": True, "human_input": True,
               "http_first": True, "delay_range": [8.0, 25.0]},
    # На максимуме лёгкий путь ВЫКЛЮЧЕН намеренно: режим выбирают ради куки-профиля и реального
    # окна, а httpx-запрос идёт мимо них — на тяжёлых сайтах он только подсветит нас лишним
    # обращением без куков.
    "max":    {"backend": "browser", "real_chrome": True, "anti_detect": True, "headed": True,
               "persistent_profile": True, "warmup": True, "human_input": True,
               "http_first": False, "delay_range": [12.0, 30.0]},
    # Два «слоя» параллельности (ортогональны лесенке fast→max — выбираются по задаче):
    # swarm  — пачка вкладок РАЗНЫХ доменов разом, БЕЗ курсора (быстро; мышь не имитируем, чтобы
    #          широкая параллель не ломала человекоподобность). В один домен — всё равно по одному.
    "swarm":  {"backend": "browser", "real_chrome": True, "anti_detect": True, "headed": True,
               "persistent_profile": False, "warmup": False, "human_input": False,
               "http_first": True, "concurrency": 8, "delay_range": [2.0, 6.0]},
    # cursor — по ОДНОЙ вкладке за раз, С имитацией движений мыши/скролла (один «человек» = один
    #          курсор). Максимум человекоподобности для тяжёлых сайтов; медленно.
    "cursor": {"backend": "browser", "real_chrome": True, "anti_detect": True, "headed": True,
               "persistent_profile": True, "warmup": True, "human_input": True,
               "http_first": False, "concurrency": 1, "delay_range": [12.0, 30.0]},
}


# Зависимости приёмов «режима открытия»: приём → что обязано быть включено, иначе он не работает
# ВООБЩЕ. Псевдо-флаг "browser" означает backend == "browser": все семь приёмов ниже читаются
# только в fetch/browser.py, то есть при httpx-бэкенде они не более чем галочки в интерфейсе.
# Направление одно: снятие предпосылки гасит зависимые (обратное неверно — выключить, например,
# прогрев не значит выключить браузер).
FETCH_DEPS: dict[str, list[str]] = {
    "real_chrome": ["browser"],          # channel=chrome — параметр запуска браузера
    "anti_detect": ["browser"],          # patchright вместо playwright
    "headed": ["browser"],               # реальное окно
    "persistent_profile": ["browser"],   # профиль с куки живёт в браузере
    "warmup": ["browser"],               # заход на главную домена после блока
    "human_input": ["browser"],          # движения мыши/скролл
    "wait_networkidle": ["browser"],     # ожидание дорисовки JS
    "http_first": ["browser"],           # эскалация «лёгкий путь → браузер»: без браузера некуда
}


def normalize_fetch(cfg: "FetchConfig") -> "FetchConfig":
    """Свести режим открытия к самосогласованному: погасить приёмы без предпосылки.

    Зачем на бэкенде, если интерфейс и так блокирует контролы: настройки приходят из localStorage
    (может быть устаревшим) и из внешних вызовов /api/find. Молча выполнять несогласованный режим
    нельзя — пользователь считал бы, что анти-детект работает, а он не применяется.
    """
    data = cfg.model_dump()
    have = {"browser": data.get("backend") == "browser"}
    for flag, needs in FETCH_DEPS.items():
        if not data.get(flag):
            continue
        missing = [n for n in needs if not have.get(n, bool(data.get(n)))]
        if missing:
            data[flag] = False
            log.info("Режим открытия: «%s» отключено — работает только с браузером "
                     "(выбран бэкенд %s)", flag, data.get("backend"))
    # Один «человек» — один курсор: имитация мыши несовместима с пачкой одновременных вкладок.
    # Раньше это молча делалось при запуске, и выставленный в интерфейсе параллелизм пропадал.
    if data.get("human_input") and int(data.get("concurrency") or 1) != 1:
        log.info("Режим открытия: вкладок одновременно 1 (было %s) — включён человеческий ввод",
                 data.get("concurrency"))
        data["concurrency"] = 1
    return FetchConfig(**data)


def resolve_fetch(base: "FetchConfig", opts: dict | None) -> "FetchConfig":
    """Собрать эффективный FetchConfig: дефолт → пресет (opts['preset']) → точечные поля opts.

    В конце — нормализация зависимостей (см. `normalize_fetch`).
    """
    data = base.model_dump()
    opts = dict(opts or {})
    preset = opts.pop("preset", None)
    if preset and preset in FETCH_PRESETS:
        data.update(FETCH_PRESETS[preset])
    for k, v in opts.items():                     # точечные переопределения («Свой»)
        if k in FetchConfig.model_fields and v is not None:
            data[k] = v
    return normalize_fetch(FetchConfig(**data))


class ExtractConfig(BaseModel):
    # --- извлечение цены моделью со страницы ---
    max_chars: int = 24000             # символов Markdown в промпт: покрывает карточку целиком (приоритет
                                       # качества). Требует контекст модели ~16k+ токенов; для 8k — снизить
                                       # до ~10000. Длинные страницы дороже по времени (см. бенчмарк Qwen).
    min_confidence: float = 0.0        # порог отбраковки записей модели по уверенности (0 = не отбраковываем)
    include_structured_hint: bool = False  # НЕ подавать метки в промпт: на листингах они провоцируют
                                       # галлюцинацию (модель берёт число из jsonld, а не из текста).
                                       # Структурные метки остаются в коде — для деградации без модели.
    concurrency: int = 8               # ВЕРХНИЙ уровень адаптивного лимита вызовов модели (llm/orchestrator.py):
                                       # старт 8 параллельных прямых запросов; при таймауте (>180 с) лимит
                                       # ступенчато падает 8→4→1 и запрос повторяется, при серии успехов —
                                       # растёт обратно. Ступени выводятся из этого числа: (n, n//2, 1).
    orch_timeout: float = 175.0        # с; таймаут одного вызова модели (сервер сбрасывает при >180 с)
    orch_up_after: int = 8             # успехов подряд до повышения лимита вверх (авто-восстановление)
    retry_on_suspected_miss: bool = True  # пустой ответ на странице, где цена искомого по тексту
                                       # ЕСТЬ, — переспросить один раз по короткому фрагменту
    retry_max_chars: int = 3000        # размер фрагмента для повторного запроса
    verify_currency_on_page: bool = True  # сверять валюту с маркерами страницы: модель по
                                       # умолчанию пишет RUB даже там, где цена в сумах/тенге
    verify_price_on_page: bool = True  # отбрасывать цену, которой нет в отправленном модели тексте:
                                       # на карточках, где цена не отрисовалась, модель берёт число
                                       # из соседних блоков и метит его точным совпадением
    strict_analog_judge: bool = True   # код-судья строгости аналога: отсеивать позиции с доказанным
                                       # измеримым расхождением (фасовка/объём/размер), см. extract/judge.py
    analog_spec_tolerance: float = 0.02  # относительный допуск сравнения характеристик (2%)
    focus_pricing: bool = True         # умный отбор ценовых блоков вместо тупой обрезки markdown[:max_chars]:
                                       # окна вокруг цен + релевантные запросу блоки. Быстрее (короче вход),
                                       # без потери нужного. Универсально. См. extract/focus.py.
    focus_before: int = 20             # строк контекста ВЫШЕ цены (там обычно название товара)
    focus_after: int = 20              # строк контекста НИЖЕ цены. Симметрия не случайна: замер
                                       # 2236 ответов модели показал, что название бывает и под
                                       # ценой (8% случаев), причём как раз в самых дальних. 20/20
                                       # захватывает название в 98.9% случаев; шире — доплата за
                                       # десятые доли процента (см. extract/focus.py).
    max_note_chars: int = 1200         # предел комментария модели по источнику. Пользователь читает его
                                       # целиком в колонке «Комментарий» — обрезка на полуслове недопустима.
    skip_model_without_prices: bool = True  # не звать модель на странице, где цены нет НИ В ОДНОМ виде
                                       # (триаж без ценовых меток + нет структурных цен + ценовых блоков
                                       # не нашлось): ответ там гарантированно пуст, а генерация платная
                                       # и долгая. Выключить, если замер покажет потерю цен.
    cache_model_answers: bool = True   # кэшировать ответ модели по (страница + товар + модель + промпт):
                                       # повторный прогон того же файла не переспрашивает и не ждёт
                                       # заново (см. storage/extract_cache.py)


class AdjudicateConfig(BaseModel):
    # --- правила выбора ИТОГОВОЙ цены из всех найденных (extract/adjudicate.py) ---
    # Лежат в конфиге, а не в коде, потому что это бизнес-правила: по ним объясняют заказчику,
    # почему в отчёте стоит именно это число. Пороги ОТНОСИТЕЛЬНЫЕ (доля от медианы) — работают
    # на любом товаре без привязки к категории.
    k_low: float = 0.15                # ниже этой доли медианы → приманка/цена за часть/образец
    k_high: float = 6.0                # выше стольких медиан → опт (за тонну/поддон), опечатка
    mad_k: float = 4.0                 # порог устойчивой z-оценки (MAD) для выброса
    min_n_for_mad: int = 4             # MAD включаем только при достаточном числе цен
    min_n_for_corridor: int = 4        # коридор по перцентилям осмыслен только при достаточном N
    corroborate_rel: float = 0.05      # цены в пределах ±5% считаем «той же» (подтверждение доменами)
    # Веса скоринга кандидата (сумма ~1). Каждый показывается в отчёте отдельным слагаемым, чтобы
    # выбор читался без исходников: «надёжность сайта 8.4 + уверенность 11.3 + …».
    w_tier: float = 0.28               # тир источника (официальные/крупные надёжнее)
    w_conf: float = 0.15               # уверенность извлечения моделью
    w_corr: float = 0.20               # подтверждение независимыми доменами
    w_match: float = 0.12              # точное > аналог
    w_stock: float = 0.03              # есть в наличии — небольшой плюс
    w_median: float = 0.12             # близость к медиане: одиночная крайность подозрительнее
    w_vendor: float = 0.10             # цена с сайта самого производителя защищена от накрутки
    non_offer_penalty: float = 0.6     # обзор/выдача: цена настоящая, но это не предложение к покупке
    analog_only_penalty: float = 0.7   # итог собран только по аналогам — доверие ниже
    tie_penalty: float = 0.85          # балл победителя совпал с соперником — выбор доопределён правилом
    # Цена, взятую из структурной разметки при недоступной модели, соответствие товару не
    # сверял НИКТО. Вес «типа совпадения» у неё ниже, чем у аналога (0.5), а итоговое доверие
    # ограничено сверху — она не имеет права выглядеть надёжнее проверенной.
    unverified_match: float = 0.25     # вклад в балл вместо 1.0 («точное») / 0.5 («аналог»)
    unverified_confidence: float = 0.3  # потолок доверия к итогу, который модель не проверяла
    # Единица товара — та, за которую продают САМ товар, а не его аналоги. Поэтому доминирующую
    # единицу считаем по точным совпадениям, если они есть. Второй предохранитель против случая,
    # когда отсев по единице выбрасывает единственную точную цену (LTV RTM-085: 25500 ₽ за «шт»
    # проиграли двум аналогам за «шт.» и выпали из отчёта).
    unit_by_exact: bool = True


@lru_cache(maxsize=1)
def default_adjudicate_config() -> AdjudicateConfig:
    """Правила отбора из `config/adjudicate.yaml` без сборки всех настроек.

    `adjudicate()` — чистая функция, её зовут из шести мест (движок, конвейер, ReAct-скил, CLI),
    и тащить через все из них весь Settings ради семи весов незачем. Здесь читается только своя
    секция; результат кэшируется на процесс.
    """
    y = _load_yaml("adjudicate.yaml")
    return AdjudicateConfig(**{k: v for k, v in y.items() if k in AdjudicateConfig.model_fields})


class ReportConfig(BaseModel):
    # --- итоговый Excel-отчёт (Товар/Номер/Цена/Диапазон/Комментарий модели) ---
    use_model: bool = True             # комментарий по источникам пишет модель; иначе — детерминированная сводка
    max_offers_in_comment: int = 8     # сколько предложений передавать модели/перечислять в фолбэке
    include_excluded: bool = True      # упоминать отсеянные значения (приманки/выбросы) в комментарии
    concurrency: int = 4               # параллельных комментариев
    max_chars: int = 700               # предел длины комментария (обрезаем длинный ответ модели)
    # --- промежуточные сохранения по ходу прогона (report/checkpoint.py) ---
    checkpoint_every: int = 10         # через сколько закрытых позиций перезаписывать файлы на
                                       # диске. 0 — выключить (не рекомендуется: на длинном файле
                                       # сбой обнулит часы работы)
    checkpoint_dir: str = "results"    # папка в корне инструмента; внутри — подпапка на прогон
    rationale_timeout: float = 60.0    # сколько ждать комментарий модели ПО ХОДУ ПРОГОНА. Дольше
                                       # ждать нечего: комментарий — не цена, при таймауте берётся
                                       # детерминированная сводка, а прогон идёт дальше.


class StorageConfig(BaseModel):
    # --- история прогонов на SQLite (M5): храним все цены + своды + dead-letter ---
    enabled: bool = True               # писать историю по завершении прогона
    db_path: str = str(ROOT / "runs" / "history.sqlite")   # файл БД (по умолчанию в runs/)


class ReActConfig(BaseModel):
    # --- ReAct-агент (M6): модель ведёт шаги Мысль→Действие поверх реестра скилов ---
    max_steps: int = 14                # предел шагов на товар (защита от зацикливания)
    source_budget: int = 2             # сколько раз можно звать find_sources на товар
    fetch_budget: int = 6              # сколько загрузок страниц (fetch_page) на товар
    extract_budget: int = 6            # сколько извлечений цены (extract_prices) на товар
    escalate_budget: int = 3           # сколько эскалаций антибота на товар
    obs_max_chars: int = 800           # предел длины наблюдения, отдаваемого модели
    concurrency: int = 1               # товаров параллельно (модель серийна → обычно 1)


class AgentConfig(BaseModel):
    # --- движок «Найти»: сколько ПОЗИЦИЙ одновременно на каждом этапе конвейера ---
    # Лимит стоит на КАЖДОМ этапе отдельно (поиск, загрузка сайтов, триаж, извлечение моделью):
    # больше N позиций внутрь этапа не входит, а очередь на вход — по НОМЕРУ СТРОКИ, не по времени
    # прихода. Иначе (как было раньше) позиция №10 успевала получить цену, пока №2 и №3 стояли в
    # очереди за вкладкой браузера. См. agent/gates.py.
    stage_positions: int = 5
    # Точечные переопределения по этапу (пусто = у всех stage_positions). Пример: если замер
    # покажет, что модель простаивает — {"extract": 8}, чтобы исполнителю модели хватало работы.
    stage_positions_by_kind: dict[str, int] = Field(default_factory=dict)
    model_lanes: int = 1               # полос модельной очереди оркестратора (модель серийна)


class ResearchConfig(BaseModel):
    # --- чат-исследователь (M9): модель ведёт диалог и сама ищет/читает/проверяет источники ---
    # Бюджеты — на ОДИН ход диалога (одно сообщение пользователя), а не на весь чат: иначе
    # длинная беседа упиралась бы в потолок и агент замолкал бы посреди разговора.
    max_steps: int = 16                # предел шагов на ход (защита от зацикливания и от счёта)
    search_budget: int = 5             # web_search + find_sources
    read_budget: int = 8               # чтение страниц (read_page)
    probe_budget: int = 6              # аттестация источников (probe_source)
    extract_budget: int = 6            # извлечение цен со страницы
    escalate_budget: int = 3           # обходы антибота
    probe_links: int = 3               # внутренних ссылок обойти при deep-разведке источника
    obs_max_chars: int = 1200          # предел наблюдения, отдаваемого модели
    excerpt_max_chars: int = 3000      # предел выжимки страницы (read_page/probe_source)
    max_sources: int = 20              # потолок реестра источников на диалог
    keep_steps: int = 12               # сколько последних шагов держим дословно (остальные — свёрткой)
    verify_with_model: bool = True     # разведку подтверждает модель, а не пересказ выдачи
    fix_citations: bool = True         # одна корректирующая итерация, если утверждения без ссылок


class Settings(BaseModel):
    llm: LLMConfig
    discovery: DiscoveryConfig
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    clean: CleanConfig = Field(default_factory=CleanConfig)
    extract: ExtractConfig = Field(default_factory=ExtractConfig)
    adjudicate: AdjudicateConfig = Field(default_factory=AdjudicateConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    react: ReActConfig = Field(default_factory=ReActConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    sources: dict = Field(default_factory=dict)
    env_present: bool = False
    serper_api_key: str = ""           # ключ Serper.dev (из .env: SERPER_API_KEY или API_SEARCH)
    serper_region: str = "ru"          # gl
    serper_lang: str = "ru"            # hl


def _default_model() -> str:
    """Модель по умолчанию из каталога (config/models.yaml); фолбэк — константа Foundation."""
    try:
        from search_agent.llm.catalog import default_model
        return default_model()
    except Exception:  # noqa: BLE001  # каталог может отсутствовать/битый — не валим загрузку настроек
        return FOUNDATION_DEFAULT_MODEL


def load_settings() -> Settings:
    """Собрать настройки из .env и config/*.yaml (окружение процесса приоритетнее)."""
    dotenv = _load_dotenv(ENV_PATH)

    def get(name: str, default: str | None = None) -> str | None:
        return os.environ.get(name) or dotenv.get(name) or default

    # Лимиты и политика на отказ — из config/llm.yaml; подключение и секреты — из .env.
    llm_yaml = {k: v for k, v in _load_yaml("llm.yaml").items() if k in LLMConfig.model_fields}
    llm = LLMConfig(
        **llm_yaml,
        base_url=get("BASE_URL", FOUNDATION_BASE_URL),
        model=get("MODEL_AGENT") or _default_model(),   # выбор пользователя (.env) → дефолт каталога
        api_key=get("FOUNDATION_KEY", "") or "",         # токен прямого API
    )

    dc_yaml = _load_yaml("discovery.yaml")
    discovery = DiscoveryConfig(**{k: v for k, v in dc_yaml.items()
                                   if k in DiscoveryConfig.model_fields})

    fx_yaml = _load_yaml("fetch.yaml")
    fetch = FetchConfig(**{k: v for k, v in fx_yaml.items() if k in FetchConfig.model_fields})

    cl_yaml = _load_yaml("clean.yaml")
    clean = CleanConfig(**{k: v for k, v in cl_yaml.items() if k in CleanConfig.model_fields})

    ex_yaml = _load_yaml("extract.yaml")
    extract = ExtractConfig(**{k: v for k, v in ex_yaml.items() if k in ExtractConfig.model_fields})

    ad_yaml = _load_yaml("adjudicate.yaml")
    adjudicate = AdjudicateConfig(**{k: v for k, v in ad_yaml.items()
                                     if k in AdjudicateConfig.model_fields})

    rp_yaml = _load_yaml("report.yaml")
    report = ReportConfig(**{k: v for k, v in rp_yaml.items() if k in ReportConfig.model_fields})

    st_yaml = _load_yaml("storage.yaml")
    st_yaml["db_path"] = get("HISTORY_DB", st_yaml.get("db_path", StorageConfig.model_fields["db_path"].default))
    storage = StorageConfig(**{k: v for k, v in st_yaml.items() if k in StorageConfig.model_fields})

    ra_yaml = _load_yaml("react.yaml")
    react = ReActConfig(**{k: v for k, v in ra_yaml.items() if k in ReActConfig.model_fields})

    ag_yaml = _load_yaml("agent.yaml")
    agent = AgentConfig(**{k: v for k, v in ag_yaml.items() if k in AgentConfig.model_fields})

    rs_yaml = _load_yaml("research.yaml")
    research = ResearchConfig(**{k: v for k, v in rs_yaml.items()
                                 if k in ResearchConfig.model_fields})

    return Settings(
        llm=llm,
        discovery=discovery,
        fetch=fetch,
        clean=clean,
        extract=extract,
        adjudicate=adjudicate,
        report=report,
        storage=storage,
        react=react,
        agent=agent,
        research=research,
        sources=_load_yaml("sources.yaml"),
        env_present=ENV_PATH.exists(),
        serper_api_key=(get("SERPER_API_KEY") or get("API_SEARCH") or ""),
        serper_region=get("SERPER_REGION", "ru"),
        serper_lang=get("SERPER_LANG", "ru"),
    )
