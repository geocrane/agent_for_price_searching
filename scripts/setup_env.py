# -*- coding: utf-8 -*-
"""Подготовка .env при запуске. Готовит файл и молчит — токены вводятся в интерфейсе.

Зовётся из start.sh и start.bat перед запуском сервера. Одна реализация на обе системы —
иначе логика разъезжается между bash и cmd (и в cmd её пришлось бы писать строковой хирургией).

Токены здесь БОЛЬШЕ НЕ СПРАШИВАЮТСЯ. Раньше скрипт ждал ввода через getpass, и это было хуже
интерфейса по всем статьям: запуск блокировался на вопросе (при двойном щелчке по start.command
человек его не видел и получал «программа не запускается»), правильность ключа не проверялась,
а ключ поиска кончается вообще посреди работы — в момент, когда скрипта запуска рядом нет.
Оба токена задаются в интерфейсе кнопкой «Модель», сохраняются в этот же .env и действуют
сразу, без перезапуска сервера (webui/server.py → api_model_select).

Что скрипт делает:
  • .env нет → создаём из шаблона TEMPLATE ниже (там текущие адреса API);
  • .env есть → сохраняем значения и порядок ключей;
  • заглушки («your_foundation_key_here») обнуляем: с мусорным ключом приложение попыталось бы
    авторизоваться и упало бы непонятной ошибкой вместо честного «ключ не задан»;
  • пояснения-комментарии из значений вычищаем (см. ниже) и ставим chmod 600.

В сам .env пояснения НЕ пишем — только строки «КЛЮЧ=значение»: файл читают и правят руками, и
комментарии там только мешают — пояснение после значения раньше попадало в значение
(см. config._strip_inline_comment).

Код возврата всегда 0: отсутствие ключа не повод не запускать интерфейс — именно там его и вводят.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"

# Значения-заглушки: считаем их «не заполнено».
PLACEHOLDERS = {"your_foundation_key_here", "your_serper_key_here", "changeme", "xxx", ""}

# Ключ → (как называется в интерфейсе, где взять, обязателен ли для работы).
SECRETS = [
    ("FOUNDATION_KEY", "Токен модели (Foundation Models)",
     "https://cloud.ru → Foundation Models → API-ключ", True),
    ("SERPER_API_KEY", "Токен поиска (Serper)",
     "https://serper.dev — есть бесплатный лимит", False),
]

# Шаблон нового .env: адреса API по умолчанию, значения токенов пустые (их вводят в интерфейсе).
TEMPLATE = """\
BASE_URL=https://foundation-models.api.cloud.ru/v1
MODEL_AGENT=zai-org/GLM-5.2
FOUNDATION_KEY=
SERPER_API_KEY=
"""


def parse(text: str) -> dict:
    """Значения KEY=VALUE из текста .env (комментарии и пустые строки пропускаем)."""
    out = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        # Пояснение через два пробела (осталось от прежних версий файла) — не часть токена.
        out[k.strip()] = v.split("  #")[0].split("\t#")[0].strip().strip('"').strip("'")
    return out


def strip_comments(text: str) -> str:
    """Оставить только строки «КЛЮЧ=значение»: ни строк-комментариев, ни пояснений после значения.

    Порядок ключей сохраняем — так файл остаётся узнаваемым после правок руками.
    """
    out = []
    seen = set()
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k = k.strip()
        if not k or k in seen:
            continue
        seen.add(k)
        # Пояснение — «#» после пробела; «#» внутри значения (пароли) сохраняем. Значение в
        # кавычках берём целиком. Логика та же, что в config._strip_inline_comment.
        v = v.strip()
        if v[:1] in ('"', "'"):
            end = v.find(v[0], 1)
            v = v[1:end] if end > 0 else v[1:]
        else:
            for sep in ("  #", " #", "\t#"):
                i = v.find(sep)
                if i >= 0:
                    v = v[:i].strip()
                    break
        out.append("%s=%s" % (k, v))
    return "\n".join(out) + "\n" if out else ""


def filled(value: str | None) -> bool:
    return bool(value) and value.strip().lower() not in PLACEHOLDERS


def set_key(text: str, key: str, value: str) -> str:
    """Заменить значение ключа, сохранив порядок строк; если ключа нет — добавить в конец."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        if line.partition("=")[0].strip() == key:
            lines[i] = "%s=%s" % (key, value)
            break
    else:
        lines.append("%s=%s" % (key, value))
    return "\n".join(lines) + "\n"


def main() -> int:
    raw = ENV.read_text(encoding="utf-8") if ENV.exists() else None
    created = raw is None
    if created:
        raw = TEMPLATE
    # В .env держим только «КЛЮЧ=значение»: пояснения (оставшиеся от прежних версий файла)
    # убираем всегда, значения при этом сохраняем.
    text = strip_comments(raw)

    current = parse(text)
    # Переменные окружения важнее файла: если ключ уже задан в системе, он считается заполненным.
    missing = [s for s in SECRETS
               if not filled(current.get(s[0])) and not filled(os.environ.get(s[0]))]
    # Заглушки шаблона обнуляем: пустое значение честнее «your_..._here», с которым приложение
    # пыталось бы авторизоваться мусорным ключом.
    for key, _label, _where, _required in missing:
        if current.get(key):                          # в файле стоит заглушка — стираем
            text = set_key(text, key, "")

    changed = text != raw
    if created or changed:
        ENV.write_text(text, encoding="utf-8")
    try:
        os.chmod(ENV, 0o600)                          # ключи читает только владелец (в Windows не действует)
    except OSError:
        pass

    if created:
        print("  Создан .env")
    elif changed:
        print("  Из .env убраны пояснения — остались только ключи и значения")

    if not missing:
        print("  Токены на месте")
        return 0

    # Вопросов не задаём: ключи вводятся в интерфейсе и применяются без перезапуска.
    print("")
    print("  Не заполнено — задайте в интерфейсе, кнопка «Модель» (перезапуск не нужен):")
    for key, label, where, required in missing:
        print("    • %s — %s%s" % (label, where, "" if required else "; можно позже"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
