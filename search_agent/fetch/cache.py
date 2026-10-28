# -*- coding: utf-8 -*-
"""Дисковый кэш загруженных/очищенных страниц (ключ — URL). Не грузим повторно."""
import hashlib
import json
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "runs" / "fetch_cache"


def _key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def get(url: str) -> dict | None:
    p = CACHE_DIR / (_key(url) + ".json")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None


def get_any(*urls: str | None) -> tuple[dict | None, str | None]:
    """Первая непустая страница из кэша по любому из адресов: (запись, причина отказа).

    Адресов несколько, потому что страница и кандидат не обязаны совпадать: человек мог прийти на
    карточку через редирект, и ручной забор кладёт запись под ФАКТИЧЕСКИМ адресом окна, тогда как
    кандидат хранит исходный URL из выдачи. Пробуем по очереди, начиная с самого точного.

    Причина отказа возвращается словами: страница, которую человек только что открыл руками, не
    имеет права молча исчезнуть из работы.
    """
    seen_blocked = None
    for url in urls:
        if not url:
            continue
        rec = get(url)
        if not rec:
            continue
        # Автозагрузка кладёт в кэш и отказы (fetcher.py::fetch_one), поэтому запись «нас не
        # пустили» здесь обычное дело. Она не страница — идём к следующему адресу, а причину
        # запоминаем: если годной так и не найдётся, человеку надо сказать именно её.
        if rec.get("blocked"):
            seen_blocked = seen_blocked or rec["blocked"]
            continue
        if not (rec.get("markdown") or "").strip():
            continue
        return rec, None
    if seen_blocked:
        return None, "страница ещё под защитой (%s) — пройдите проверку и заберите снова" % seen_blocked
    return None, "страницы нет в кэше — сначала откройте её вручную и дождитесь забора"


def put(url: str, data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / (_key(url) + ".json")
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
