# -*- coding: utf-8 -*-
"""Запуск локального интерфейса агента.

    python -m webui.run

Поднимает uvicorn на 127.0.0.1:8770 и открывает браузер. Локальный режим: загрузка/разбор
Excel и логи работают сразу, подключение к модели — по кнопке в интерфейсе.
"""
import os
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

HOST = os.environ.get("WEBUI_HOST", "127.0.0.1")
PORT = int(os.environ.get("WEBUI_PORT", "8770"))
WAIT_SECONDS = 90.0            # столько ждём открытия порта, прежде чем сдаться


def _port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _open_browser_when_ready() -> None:
    """Открыть браузер ТОЛЬКО когда порт реально принимает соединения.

    По таймеру открывать нельзя: на свежем окружении импорт fastapi и пакета агента занимает
    заметно больше секунды, браузер стучится в ещё закрытый порт и показывает «не удалось
    подключиться» — сам он не повторяет, и человек видит ошибку при работающем сервере.
    """
    url = "http://%s:%d/" % (HOST, PORT)
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if _port_open(HOST, PORT):
            print("Интерфейс готов: %s" % url, flush=True)
            webbrowser.open(url)
            return
        time.sleep(0.2)
    print("Сервер не открыл порт за %d с. Откройте вручную: %s" % (WAIT_SECONDS, url), flush=True)


def main():
    if __package__ in (None, ""):
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    print("Запуск сервера, ждите — браузер откроется сам (http://%s:%d/)" % (HOST, PORT), flush=True)
    threading.Thread(target=_open_browser_when_ready, daemon=True).start()
    uvicorn.run("webui.server:app", host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
