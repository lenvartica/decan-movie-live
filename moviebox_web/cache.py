"""Small JSON disk cache with per-read TTLs and atomic writes."""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from .config import atomic_write

HOUR = 3600
TTL_MANIFEST = 24 * HOUR
TTL_CATALOG = 1 * HOUR
TTL_STREAMS = 2 * HOUR
TTL_SEARCH = 1 * HOUR
TTL_DETAILS = 6 * HOUR
TTL_PLAYLIST = 24 * HOUR


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()


class DiskCache:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, namespace: str, key: str) -> Path:
        return self.root / namespace / f"{md5_hex(key)}.json"

    def get(self, namespace: str, key: str, ttl: float) -> Any | None:
        path = self._path(namespace, key)
        try:
            entry = json.loads(path.read_text("utf-8"))
            if time.time() - float(entry["t"]) <= ttl:
                return entry["v"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def set(self, namespace: str, key: str, value: Any) -> None:
        try:
            atomic_write(self._path(namespace, key), json.dumps({"t": time.time(), "v": value}))
        except (OSError, TypeError, ValueError):
            pass

    def clear(self, namespace: str | None = None) -> int:
        """Delete cached files. Returns the number of files removed."""
        target = self.root / namespace if namespace else self.root
        count = sum(1 for _ in target.rglob("*") if _.is_file()) if target.exists() else 0
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
        return count
