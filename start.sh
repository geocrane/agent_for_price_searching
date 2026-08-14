#!/usr/bin/env bash
# Установка и запуск агента поиска цен — macOS и Linux.
#
# Что делает: проверяет Python, создаёт .venv при отсутствии, ставит зависимости (только если они
# менялись), докачивает Chromium для браузерного режима, готовит .env и поднимает интерфейс.
# Повторный запуск ничего лишнего не переустанавливает.
#
# Запуск: ./start.sh в терминале. На macOS можно двойным щелчком по start.command — это обёртка
# над этим же файлом (у .sh нет ассоциации с Терминалом, а у .command есть).
set -euo pipefail

cd "$(dirname "$0")"                       # работаем из корня репозитория, откуда бы ни запустили

VENV="${VENV:-.venv}"                      # переопределяется переменной окружения (нужно для проверок)
STAMP="$VENV/.deps-stamp"                  # отпечаток requirements.txt: по нему решаем, ставить ли
BROWSER_STAMP="$VENV/.browser-stamp"       # признак, что Chromium для playwright/patchright скачан
PY_MIN_MAJOR=3
PY_MIN_MINOR=11                            # ниже 3.11 код не запустится (синтаксис типов)
PY_DEV_MINOR=14                            # на этой версии разрабатывалось

say()  { printf "\033[1;36m▸ %s\033[0m\n" "$*"; }
warn() { printf "\033[1;33m! %s\033[0m\n" "$*"; }
die()  { printf "\033[1;31m✖ %s\033[0m\n" "$*" >&2; exit 1; }

# ---- 1. Python ---------------------------------------------------------------
# Берём первый подходящий интерпретатор: сначала свежие явные версии, потом общие имена.
find_python() {
  for cand in python3.14 python3.13 python3.12 python3.11 python3 python; do
    command -v "$cand" >/dev/null 2>&1 || continue
    if "$cand" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= ($PY_MIN_MAJOR, $PY_MIN_MINOR) else 1)" 2>/dev/null; then
      echo "$cand"; return 0
    fi
  done
  return 1
}

PY="$(find_python)" || die "Нужен Python $PY_MIN_MAJOR.$PY_MIN_MINOR или новее (рекомендуется $PY_MIN_MAJOR.$PY_DEV_MINOR).
   macOS:  brew install python@3.14   либо  https://www.python.org/downloads/
   Linux:  sudo apt install python3.14 python3.14-venv   (или пакет вашего дистрибутива)"

PY_VER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
say "Python $PY_VER ($PY)"
"$PY" -c "import sys; raise SystemExit(0 if sys.version_info[1] >= $PY_DEV_MINOR else 1)" 2>/dev/null \
  || warn "Разрабатывалось на Python 3.$PY_DEV_MINOR; на $PY_VER возможны неожиданности."

# ---- 2. Виртуальное окружение ------------------------------------------------
# Существующее окружение переиспользуем только если оно исправно: сделано подходящим Python и
# не побито. Иначе запуск доходил до конца и падал на «No module named uvicorn» — сообщение,
# по которому причину (старый .venv от прежней версии Python) не угадать.
venv_ok() {
  [ -x "$VENV/bin/python" ] || return 1
  "$VENV/bin/python" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= ($PY_MIN_MAJOR, $PY_MIN_MINOR) else 1)" 2>/dev/null
}

if ! venv_ok; then
  if [ -e "$VENV" ]; then
    warn "Окружение $VENV непригодно (нет python или версия ниже $PY_MIN_MAJOR.$PY_MIN_MINOR) — пересоздаю"
    rm -rf "$VENV"
  fi
  say "Создаю виртуальное окружение $VENV"
  "$PY" -m venv "$VENV" || die "Не удалось создать $VENV. На Debian/Ubuntu поставьте пакет python3-venv."
else
  say "Виртуальное окружение уже есть"
fi
VPY="$VENV/bin/python"

# ---- 3. Зависимости (только при изменении requirements.txt) -------------------
# Проверяем не отпечаток, а сам факт: импортируются ли ключевые пакеты. Отпечаток совпадает и
# тогда, когда установка прервалась на середине или окружение почистили, — и тогда пропуск
# установки означал падение при запуске.
deps_ok() { "$VPY" -c "import uvicorn, fastapi, pydantic, openai, playwright" >/dev/null 2>&1; }

want="$("$VPY" - <<'EOF'
import hashlib, pathlib
print(hashlib.sha256(pathlib.Path("requirements.txt").read_bytes()).hexdigest())
EOF
)"
have="$(cat "$STAMP" 2>/dev/null || echo "")"
# Пропуск только при трёх условиях сразу: отпечаток посчитан, совпал со штампом и пакеты
# импортируются. Пустой отпечаток совпал бы с пустым штампом свежего клона — и установка
# пропустилась бы целиком (именно так это ломалось на Windows).
if [ -z "$want" ] || [ "$want" != "$have" ] || ! deps_ok; then
  [ -z "$want" ] && warn "Не удалось посчитать отпечаток requirements.txt — ставлю зависимости"
  [ -n "$want" ] && [ "$want" = "$have" ] && warn "Отпечаток совпал, но пакеты не импортируются — переустанавливаю"
  say "Устанавливаю зависимости (это займёт несколько минут при первом запуске)"
  "$VPY" -m pip install --upgrade pip >/dev/null
  "$VPY" -m pip install -r requirements.txt || die "Установка зависимостей не удалась (см. вывод выше)."
  deps_ok || die "Зависимости установились, но не импортируются. Покажите вывод выше — там причина."
  # Штамп пишем только с посчитанным отпечатком: пустой означал бы «всё поставлено» на следующем
  # запуске, ничего на деле не подтверждая.
  [ -n "$want" ] && printf "%s" "$want" > "$STAMP"
else
  say "Зависимости на месте — пропускаю установку"
fi

# ---- 4. Chromium для браузерного режима --------------------------------------
# Это отдельная загрузка (~150 МБ на движок), в pip-пакете браузера нет. Без него работает только
# быстрый http-режим, а защищённые сайты (Ozon и подобные) не открываются.
if [ ! -f "$BROWSER_STAMP" ]; then
  say "Скачиваю Chromium для playwright и patchright"
  "$VPY" -m playwright install chromium || warn "playwright: Chromium не скачался — браузерный режим будет недоступен"
  "$VPY" -m patchright install chromium || warn "patchright: Chromium не скачался — анти-детект будет недоступен"
  date > "$BROWSER_STAMP"
else
  say "Chromium уже скачан"
fi

# ---- 5. Файл настроек -----------------------------------------------------------
# Готовит .env (создаёт при отсутствии, чистит пояснения, гасит заглушки) и НИЧЕГО не спрашивает:
# токены вводятся в интерфейсе кнопкой «Модель» и применяются без перезапуска. Логика одна на обе
# системы — в scripts/setup_env.py (в cmd её пришлось бы писать строковой хирургией).
say "Проверяю настройки (.env)"
"$VPY" scripts/setup_env.py || warn "Не удалось подготовить .env — задайте ключи вручную"

# ---- 6. Запуск ---------------------------------------------------------------
say "Запускаю интерфейс: http://127.0.0.1:${WEBUI_PORT:-8770}/  (браузер откроется сам)"
say "Остановить — Ctrl+C в этом окне"
exec "$VPY" -m webui.run
