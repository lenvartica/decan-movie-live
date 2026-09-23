"""Paths, atomic file writes and the persisted user configuration.

Port of ``src/config.rs``. File names and JSON shapes for ``favorites.json``,
``history.json``, ``addons_config.json`` and ``tv_config.json`` match the
terminal app, so you can point ``MOVIEBOX_CONFIG_DIR`` / ``MOVIEBOX_DATA_DIR`` at
its directories and share the same library.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("moviebox_web")

APP_NAME = "moviebox-web"


@dataclass(frozen=True)
class Paths:
    config: Path
    data: Path
    cache: Path

    @classmethod
    def resolve(cls, home: str | os.PathLike | None = None) -> "Paths":
        """Resolve directories.

        Precedence: explicit ``home`` argument, then the per-directory env vars
        (``MOVIEBOX_CONFIG_DIR`` etc., same names as the terminal app), then
        ``MOVIEBOX_WEB_HOME``, then ``~/.moviebox-web``.
        """
        root = Path(home or os.environ.get("MOVIEBOX_WEB_HOME") or Path.home() / ".moviebox-web")

        def pick(env: str, sub: str) -> Path:
            if home is None and os.environ.get(env):
                return Path(os.environ[env])
            return root / sub

        paths = cls(pick("MOVIEBOX_CONFIG_DIR", "config"), pick("MOVIEBOX_DATA_DIR", "data"), pick("MOVIEBOX_CACHE_DIR", "cache"))
        for p in (paths.config, paths.data, paths.cache):
            p.mkdir(parents=True, exist_ok=True)
        return paths


def atomic_write(path: Path, content: str | bytes) -> None:
    """Write via a temp file in the same directory, then rename over the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8") if isinstance(content, str) else content
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json(path: Path, default: Any) -> Any:
    """Read JSON; a corrupt file is rotated to ``<name>.corrupt.<ts>`` (never deleted)."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        stamp = int(time.time())
        corrupt = path.with_name(f"{path.stem}.corrupt.{stamp}{path.suffix}")
        log.error("failed to parse %s (%s); rotating to %s", path.name, exc, corrupt.name)
        try:
            os.replace(path, corrupt)
        except OSError:
            pass
        return default


def save_json(path: Path, value: Any, *, pretty: bool = False) -> None:
    try:
        atomic_write(path, json.dumps(value, indent=2 if pretty else None, ensure_ascii=False))
    except OSError as exc:
        log.warning("failed to save %s: %s", path.name, exc)


DEFAULT_CONFIG: dict[str, Any] = {
    "active_mode": "streaming",
    "active_provider": "addons",
    "active_theme": "Mocha",
    "streaming_enabled": True,
    "tv_enabled": True,
    "addons_enabled": True,
    # "auto": play https streams directly, proxy the rest. "always": proxy everything.
    "proxy_mode": "auto",
}


class ConfigStore:
    """``config.json``. Unknown keys written by other apps are preserved on save."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        raw = load_json(path, {})
        self._raw: dict[str, Any] = raw if isinstance(raw, dict) else {}

    def get(self) -> dict[str, Any]:
        with self._lock:
            merged = dict(DEFAULT_CONFIG)
            merged.update({k: v for k, v in self._raw.items() if k in DEFAULT_CONFIG})
            return merged

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            for key, value in changes.items():
                if key not in DEFAULT_CONFIG:
                    continue
                if isinstance(value, type(DEFAULT_CONFIG[key])):
                    self._raw[key] = value
            self._raw["active_mode"] = self._raw.get("active_mode", "streaming")
            if self._raw["active_mode"] not in ("streaming", "tv"):
                self._raw["active_mode"] = "streaming"
            if self._raw.get("proxy_mode", "auto") not in ("auto", "always"):
                self._raw["proxy_mode"] = "auto"
            save_json(self.path, self._raw, pretty=True)
            return self.get()
