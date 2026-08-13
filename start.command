#!/usr/bin/env bash
# Обёртка для macOS: двойной щелчок по .command открывает Терминал, у .sh такой ассоциации нет.
# Вся логика — в start.sh (он же для Linux).
cd "$(dirname "$0")" && exec ./start.sh "$@"
