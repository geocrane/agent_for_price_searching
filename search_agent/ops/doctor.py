# -*- coding: utf-8 -*-
"""doctor — проверка готовности окружения БЕЗ вызова модели.

Проверяет: загрузку конфига, наличие .env, параметры модели и ключ поиска, число
зарегистрированных скилов. Реальный вызов модели инициирует пользователь, поэтому здесь
LLM НЕ дёргаем — только сообщаем готовность транспорта.
"""
from ..config import Settings, load_settings
from ..tools import registry


class Check:
    def __init__(self, name: str, ok: bool, detail: str = "") -> None:
        self.name, self.ok, self.detail = name, ok, detail


def run_checks(settings: Settings | None = None) -> list[Check]:
    s = settings or load_settings()
    checks: list[Check] = []

    checks.append(Check("Конфиг", True,
                        "sources: %d тир(ов)" % len((s.sources or {}).get("tiers", {}))))

    checks.append(Check(".env", s.env_present,
                        "найден" if s.env_present else "нет .env (создаётся при первом запуске)"))

    ok = bool(s.llm.api_key)
    checks.append(Check("Модель", ok, "%s, модель %s — токен %s"
                        % (s.llm.base_url, s.llm.model or "—",
                           "задан" if ok else "НЕ задан (FOUNDATION_KEY)")))

    ok = bool(s.serper_api_key)
    checks.append(Check("Поиск", ok, "serper (Google, gl=%s) — ключ %s"
                        % (s.serper_region, "задан" if ok else "НЕ задан (SERPER_API_KEY/API_SEARCH)")))

    checks.append(Check("Скилы (реестр)", True,
                        "%d зарегистрировано: %s"
                        % (len(registry.all()), ", ".join(t.name for t in registry.all()) or "—")))
    return checks
