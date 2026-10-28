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
PORT_TRIES = 30                # сколько соседних портов перебрать, если основной занят
WAIT_SECONDS = 90.0            # столько ждём открытия порта, прежде чем сдаться
PAGE_MARK = b"search_agent"    # по этой строке в <title> узнаём свой же уже запущенный сервер


def _port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _port_free(host: str, port: int) -> bool:
    """Можно ли занять порт. SO_REUSEADDR — как у uvicorn, чтобы TIME_WAIT не считался занятостью."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
        return True


def _is_our_server(host: str, port: int) -> bool:
    """Отвечает ли на порту уже запущенный интерфейс агента (второй экземпляр не нужен)."""
    import urllib.request
    try:
        with urllib.request.urlopen("http://%s:%d/" % (host, port), timeout=2) as r:
            return PAGE_MARK in r.read(4096)
    except Exception:
        return False


def _pick_port() -> tuple:
    """(порт, уже_запущен): основной порт или ближайший свободный после него.

    Порт может держать не только наш прошлый запуск, но и чужой процесс — на macOS, например,
    системный sharingd (AirDrop/Handoff) иногда садится на 8770, и его не остановить. Если на
    одном из портов уже отвечает наш интерфейс, второй экземпляр не поднимаем.
    """
    for port in range(PORT, PORT + PORT_TRIES):
        if _port_free(HOST, port):
            if port != PORT:
                print("Порт %d занят другой программой — беру %d" % (PORT, port), flush=True)
            return port, False
        if _is_our_server(HOST, port):
            return port, True
    sys.exit("Нет свободного порта в диапазоне %d–%d. Задайте другой через WEBUI_PORT."
             % (PORT, PORT + PORT_TRIES - 1))


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
    global PORT
    PORT, running = _pick_port()
    if running:
        url = "http://%s:%d/" % (HOST, PORT)
        print("Интерфейс уже запущен в другом окне — открываю его: %s" % url, flush=True)
        webbrowser.open(url)
        return
    print("Запуск сервера, ждите — браузер откроется сам (http://%s:%d/)" % (HOST, PORT), flush=True)
    threading.Thread(target=_open_browser_when_ready, daemon=True).start()
    uvicorn.run("webui.server:app", host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
