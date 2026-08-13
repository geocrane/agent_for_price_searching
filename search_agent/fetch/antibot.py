# -*- coding: utf-8 -*-
"""Детект блокировки/капчи: жёсткий блок → пропуск с причиной (прокси не используем)."""

_MARKERS = ("captcha", "recaptcha", "antirobot", "are you a robot", "доступ ограничен",
            "проверка браузера", "cf-challenge", "attention required", "just a moment",
            "проверка, что вы не робот", "подтвердите, что вы не робот")


def detect_block(status: int | None, html: str | None) -> str | None:
    """Причина блокировки или None. Проверяет статус и маркеры в начале тела."""
    if status in (401, 403, 407, 429, 503):
        return "http_%s" % status
    low = (html or "")[:6000].lower()
    for m in _MARKERS:
        if m in low:
            return "captcha/antibot"
    if len((html or "").strip()) < 200:
        return "empty"
    return None
