"""Favorites, watch history and TV playlist sources.

Ports of ``favorites.rs`` and ``history.rs`` (same JSON shapes, same matching
rules) so a library created by the terminal app keeps working.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from .config import load_json, save_json
from .models import clean_title

MAX_RECENT = 100
IN_PROGRESS_MIN_SECONDS = 30
COMPLETED_FRACTION = 0.90


def canon_provider(provider: str | None) -> str:
    p = (provider or "").strip().lower()
    return {"addon": "addons", "stremio": "addons"}.get(p, p)


def now() -> int:
    return int(time.time())


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _opt_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def identity_matches(a: dict, b: dict) -> bool:
    """``SubjectIdentity::matches``: same provider + id, else same cleaned title (+ year)."""
    if _int(a.get("stype"), 1) != _int(b.get("stype"), 1):
        return False
    if canon_provider(a.get("provider")) != canon_provider(b.get("provider")):
        return False
    id_a, id_b = a.get("subject_id") or "", b.get("subject_id") or ""
    if id_a and id_b:
        return id_a == id_b
    ta, tb = clean_title(a.get("title")), clean_title(b.get("title"))
    if ta and ta.lower() == tb.lower():
        ya, yb = (a.get("release_year") or "").strip(), (b.get("release_year") or "").strip()
        if ya and yb:
            return ya == yb
        return True
    return False


def _clean_str(value: Any, limit: int = 500) -> str:
    return str(value or "")[:limit]


def _opt_url(value: Any) -> str | None:
    v = str(value or "").strip()
    return v[:2000] if v.startswith(("http://", "https://")) else None


# --------------------------------------------------------------------------- favorites

def build_favorite(payload: dict) -> dict:
    title = _clean_str(payload.get("title"))
    return {
        "provider": canon_provider(payload.get("provider")),
        "subject_id": _clean_str(payload.get("subject_id")),
        "title": clean_title(title) or title,
        "cover_url": _opt_url(payload.get("cover_url")),
        "stype": 2 if _int(payload.get("stype"), 1) == 2 else 1,
        "release_year": _clean_str(payload.get("release_year"), 16),
        "added_at": now(),
    }


class FavoritesManager:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        raw = load_json(path, {})
        items = raw.get("items", []) if isinstance(raw, dict) else []
        self.items: list[dict] = [i for i in items if isinstance(i, dict) and i.get("title")]

    def _save(self) -> None:
        save_json(self.path, {"items": self.items})

    def is_favorite(self, identity: dict) -> bool:
        with self._lock:
            return any(identity_matches(item, identity) for item in self.items)

    def toggle(self, item: dict) -> bool:
        """Add or remove. Returns True when the title is now a favorite."""
        with self._lock:
            for pos, existing in enumerate(self.items):
                if identity_matches(existing, item):
                    del self.items[pos]
                    self._save()
                    return False
            self.items.append(item)
            self._save()
            return True

    def remove(self, identity: dict) -> None:
        with self._lock:
            self.items = [i for i in self.items if not identity_matches(i, identity)]
            self._save()

    def clear(self) -> None:
        with self._lock:
            self.items = []
            self._save()

    def snapshot(self) -> list[dict]:
        with self._lock:
            return sorted(self.items, key=lambda i: i.get("added_at", 0), reverse=True)


# --------------------------------------------------------------------------- history

def build_history_item(payload: dict) -> dict:
    title = _clean_str(payload.get("title"))
    return {
        "provider": canon_provider(payload.get("provider")),
        "subject_id": _clean_str(payload.get("subject_id")),
        "title": clean_title(title) or title,
        "cover_url": _opt_url(payload.get("cover_url")),
        "stype": 2 if _int(payload.get("stype"), 1) == 2 else 1,
        "release_year": _clean_str(payload.get("release_year"), 16),
        "season": max(0, _int(payload.get("season"))),
        "episode": max(0, _int(payload.get("episode"))),
        "timestamp": now(),
        "duration_seconds": _opt_int(payload.get("duration_seconds")),
        "progress_seconds": max(0, _int(payload.get("progress_seconds"))),
        "completed": bool(payload.get("completed", False)),
    }


def is_in_progress(item: dict) -> bool:
    """Shown in Continue Watching: started (>=30s), not finished (<90%), finite length."""
    if item.get("completed"):
        return False
    progress = _int(item.get("progress_seconds"))
    if progress < IN_PROGRESS_MIN_SECONDS:
        return False
    duration = item.get("duration_seconds")
    if not duration or progress >= int(duration * COMPLETED_FRACTION):
        return False
    return True


class HistoryManager:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        raw = load_json(path, {})
        if not isinstance(raw, dict):
            raw = {}
        self.recent: list[dict] = [self._normalize(i) for i in raw.get("recent", []) if isinstance(i, dict)]
        self.watched: set[str] = {str(k) for k in raw.get("watched", [])}
        self._hydrate()

    @staticmethod
    def _normalize(item: dict) -> dict:
        base = build_history_item(item)
        base["timestamp"] = _int(item.get("timestamp"), now())
        base["title"] = _clean_str(item.get("title"))
        return base

    @staticmethod
    def key(provider: str, subject_id: str, season: int, episode: int) -> str:
        return f"{canon_provider(provider)}::{subject_id}::{season}::{episode}"

    def _save(self) -> None:
        save_json(self.path, {"watched": sorted(self.watched), "recent": self.recent})

    def _hydrate(self) -> None:
        for item in self.recent:
            k = self.key(item["provider"], item["subject_id"], item["season"], item["episode"])
            if k in self.watched:
                item["completed"] = True
            elif item["completed"]:
                self.watched.add(k)
        # consolidate: keep the newest entry per show
        merged: list[dict] = []
        for item in sorted(self.recent, key=lambda i: i["timestamp"]):
            existing = next((m for m in merged if identity_matches(m, item)), None)
            if existing is None:
                merged.append(item)
            elif item["timestamp"] >= existing["timestamp"]:
                cover = item.get("cover_url") or existing.get("cover_url")
                existing.clear()
                existing.update(item)
                existing["cover_url"] = cover
        changed = len(merged) != len(self.recent)
        self.recent = merged
        if changed:
            self._save()

    def _push(self, item: dict) -> None:
        self.recent = [i for i in self.recent if not identity_matches(i, item)]
        self.recent.append(item)
        if len(self.recent) > MAX_RECENT:
            self.recent = self.recent[-MAX_RECENT:]
        self._save()

    def _existing(self, item: dict) -> dict | None:
        return next((i for i in self.recent if identity_matches(i, item)), None)

    def get_item(self, provider: str, subject_id: str, season: int, episode: int, title: str | None = None) -> dict | None:
        with self._lock:
            prov = canon_provider(provider)
            for i in self.recent:
                if canon_provider(i["provider"]) == prov and i["subject_id"] == subject_id:
                    if i["stype"] == 1 or (i["season"] == season and i["episode"] == episode):
                        return dict(i)
                if title:
                    ci, ct = clean_title(i["title"]), clean_title(title)
                    if ci and ci.lower() == ct.lower() and (i["stype"] == 1 or (i["season"] == season and i["episode"] == episode)):
                        return dict(i)
            return None

    def mark_watched(self, item: dict) -> None:
        with self._lock:
            item = dict(item, completed=True)
            self.watched.add(self.key(item["provider"], item["subject_id"], item["season"], item["episode"]))
            existing = self._existing(item)
            if not item.get("cover_url") and existing:
                item["cover_url"] = existing.get("cover_url")
            self._push(item)

    def update_progress(self, item: dict, progress: int, duration: int | None, completed: bool) -> None:
        with self._lock:
            item = dict(item, progress_seconds=max(0, progress), duration_seconds=duration, completed=completed, timestamp=now())
            key = self.key(item["provider"], item["subject_id"], item["season"], item["episode"])
            if completed:
                self.watched.add(key)
            else:
                self.watched.discard(key)
            existing = self._existing(item)
            if existing:
                if not item.get("cover_url"):
                    item["cover_url"] = existing.get("cover_url")
                same_episode = existing["season"] == item["season"] and existing["episode"] == item["episode"]
                if (
                    same_episode
                    and existing["progress_seconds"] >= progress
                    and existing["completed"] == completed
                    and (existing["timestamp"] >= item["timestamp"] or item["timestamp"] - existing["timestamp"] < 60)
                ):
                    return
            self._push(item)

    def record_start(self, item: dict, start_pos: int = 0) -> None:
        """Register a playback session immediately so it shows up even if the tab closes."""
        with self._lock:
            new = dict(item)
            existing = self._existing(new)
            if existing:
                if not new.get("cover_url"):
                    new["cover_url"] = existing.get("cover_url")
                if existing["season"] == new["season"] and existing["episode"] == new["episode"]:
                    new["progress_seconds"] = max(existing["progress_seconds"], start_pos)
                    new["duration_seconds"] = existing.get("duration_seconds") or new.get("duration_seconds")
                    new["completed"] = existing["completed"]
                else:
                    new["progress_seconds"] = start_pos
            else:
                new["progress_seconds"] = start_pos
            new["timestamp"] = now()
            self._push(new)

    def remove(self, provider: str, subject_id: str, season: int, episode: int) -> None:
        with self._lock:
            self.watched.discard(self.key(provider, subject_id, season, episode))
            prov = canon_provider(provider)
            self.recent = [
                i
                for i in self.recent
                if not (canon_provider(i["provider"]) == prov and i["subject_id"] == subject_id and i["season"] == season and i["episode"] == episode)
            ]
            self._save()

    def clear(self) -> None:
        with self._lock:
            self.watched.clear()
            self.recent = []
            self._save()

    def is_watched(self, provider: str, subject_id: str, season: int, episode: int) -> bool:
        with self._lock:
            if self.key(provider, subject_id, season, episode) in self.watched:
                return True
            found = self.get_item(provider, subject_id, season, episode)
            return bool(found and found["completed"])

    def watched_episodes(self, provider: str, subject_id: str) -> list[str]:
        prefix = f"{canon_provider(provider)}::{subject_id}::"
        with self._lock:
            out = []
            for k in self.watched:
                if k.startswith(prefix):
                    s, e = k[len(prefix):].split("::")
                    out.append(f"{s}:{e}")
            return sorted(out)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return sorted((dict(i) for i in self.recent), key=lambda i: i["timestamp"], reverse=True)

    def continue_watching(self) -> list[dict]:
        return [i for i in self.snapshot() if is_in_progress(i)]


# --------------------------------------------------------------------------- TV sources

class TVSources:
    """``tv_config.json``: a JSON array of playlist URLs or file paths."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        raw = load_json(path, [])
        seen: set[str] = set()
        self.sources: list[str] = []
        for item in raw if isinstance(raw, list) else []:
            s = str(item).strip()
            if s and s not in seen:
                seen.add(s)
                self.sources.append(s)

    def add(self, source: str) -> bool:
        with self._lock:
            source = source.strip()
            if not source or source in self.sources:
                return False
            self.sources.append(source)
            save_json(self.path, self.sources, pretty=True)
            return True

    def remove(self, source: str) -> bool:
        with self._lock:
            if source not in self.sources:
                return False
            self.sources.remove(source)
            save_json(self.path, self.sources, pretty=True)
            return True

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self.sources)
