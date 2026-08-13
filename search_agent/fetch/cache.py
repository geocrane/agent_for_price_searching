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


def put(url: str, data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / (_key(url) + ".json")
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
