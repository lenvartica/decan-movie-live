"""Live TV from M3U playlists (``providers/tv`` in the terminal app).

Playlists come from an ``https://`` URL or a file. Remote playlists are cached
for 24 hours, both kinds are capped at 15 MB, and channels are de-duplicated by
stream URL across all playlists.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

from ..cache import TTL_PLAYLIST, md5_hex
from ..config import atomic_write
from ..security import HttpPolicy, UnsafeURL

MAX_PLAYLIST_BYTES = 15 * 1024 * 1024


@dataclass
class Channel:
    id: str = ""
    name: str = ""
    logo: str = ""
    group: str = ""
    stream_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def is_http_url(source: str) -> bool:
    return source.strip().lower().startswith(("http://", "https://"))


def _extract_attr(line: str, name: str) -> str:
    """Value of ``name="..."`` or ``name='...'`` inside an #EXTINF line."""
    needle = name + "="
    start = 0
    while True:
        i = line.find(needle, start)
        if i == -1:
            return ""
        # must be a whole attribute name, not the tail of another one
        if i == 0 or not (line[i - 1].isalnum() or line[i - 1] in "-_"):
            q = i + len(needle)
            if q < len(line) and line[q] in "\"'":
                end = line.find(line[q], q + 1)
                if end != -1:
                    return line[q + 1 : end]
        start = i + 1


def _title_comma(line: str) -> int:
    """Index of the comma that starts the display title (ignoring commas inside quotes)."""
    quote: str | None = None
    for idx, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == ",":
            return idx
    return line.find(",")


def parse_m3u(content: str) -> list[Channel]:
    channels: list[Channel] = []
    current = Channel()
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            if tvg := _extract_attr(line, "tvg-id"):
                current.id = tvg
            if logo := _extract_attr(line, "tvg-logo"):
                current.logo = logo
            if group := _extract_attr(line, "group-title"):
                current.group = group
            idx = _title_comma(line)
            if idx != -1:
                current.name = line[idx + 1 :].strip()
        elif not line.startswith("#"):
            current.stream_url = line
            if not current.id:
                current.id = current.name
            channels.append(current)
            current = Channel()
    return channels


class TVService:
    def __init__(self, cache_dir: Path, http: HttpPolicy, allow_local_files: bool, upload_dir: Path):
        self.cache_dir = cache_dir / "tv_playlists"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.http = http
        self.allow_local_files = allow_local_files
        self.upload_dir = upload_dir
        self.upload_dir.mkdir(parents=True, exist_ok=True)

    # -- sources
    def save_upload(self, filename: str, data: bytes) -> str:
        if len(data) > MAX_PLAYLIST_BYTES:
            raise ValueError("Playlist exceeds the 15 MB size limit")
        safe = "".join(c for c in Path(filename).stem if c.isalnum() or c in "-_ ")[:60].strip() or "playlist"
        path = self.upload_dir / f"{safe}-{md5_hex(filename + str(len(data)))[:8]}.m3u"
        atomic_write(path, data)
        return str(path)

    def _is_own_upload(self, source: str) -> bool:
        try:
            return Path(source).resolve().parent == self.upload_dir.resolve()
        except OSError:
            return False

    def _read_remote(self, url: str) -> str:
        cache_file = self.cache_dir / f"{md5_hex(url)}.m3u"
        try:
            if time.time() - cache_file.stat().st_mtime < TTL_PLAYLIST:
                return cache_file.read_text("utf-8", errors="replace")
        except OSError:
            pass
        resp = self.http.get(url, stream=True, timeout=(10, 30))
        try:
            resp.raise_for_status()
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_PLAYLIST_BYTES:
                raise ValueError("remote playlist exceeds the 15 MB size limit")
            body = bytearray()
            for chunk in resp.iter_content(64 * 1024):
                body.extend(chunk)
                if len(body) > MAX_PLAYLIST_BYTES:
                    raise ValueError("remote playlist exceeds the 15 MB size limit")
        finally:
            resp.close()
        text = body.decode("utf-8", errors="replace")
        if parse_m3u(text):
            try:
                atomic_write(cache_file, text)
            except OSError:
                pass
        return text

    def _read_local(self, source: str) -> str:
        path = Path(source).expanduser()
        if not (self.allow_local_files or self._is_own_upload(source)):
            raise PermissionError("Local file playlists are disabled on this server. Upload the file instead.")
        size = path.stat().st_size
        if size > MAX_PLAYLIST_BYTES:
            raise ValueError("local playlist exceeds the 15 MB size limit")
        return path.read_text("utf-8", errors="replace")

    def fetch_playlist(self, source: str) -> list[Channel]:
        source = source.strip()
        text = self._read_remote(source) if is_http_url(source) else self._read_local(source)
        return parse_m3u(text)

    def load_all(self, sources: list[str]) -> tuple[list[Channel], list[dict]]:
        """Load every playlist concurrently. Returns (deduped channels, failures)."""

        def load(src: str) -> tuple[str, list[Channel] | str]:
            try:
                return src, self.fetch_playlist(src)
            except (requests.RequestException, UnsafeURL, ValueError, PermissionError, OSError) as exc:
                return src, f"{exc.__class__.__name__}: {exc}"[:200]

        if not sources:
            return [], []
        with ThreadPoolExecutor(max_workers=min(6, len(sources))) as pool:
            results = list(pool.map(load, sources))

        seen: set[str] = set()
        channels: list[Channel] = []
        failed: list[dict] = []
        for src, res in results:
            if isinstance(res, str):
                failed.append({"source": src, "error": res})
                continue
            for ch in res:
                if ch.stream_url and ch.stream_url not in seen:
                    seen.add(ch.stream_url)
                    channels.append(ch)
        return channels, failed

    def forget(self, source: str) -> None:
        """Drop cached data for a removed source."""
        if is_http_url(source):
            try:
                (self.cache_dir / f"{md5_hex(source.strip())}.m3u").unlink()
            except OSError:
                pass
        elif self._is_own_upload(source):
            try:
                Path(source).unlink()
            except OSError:
                pass
