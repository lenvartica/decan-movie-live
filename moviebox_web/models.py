"""Domain entities shared by every provider, plus the text helpers they rely on."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

MOVIE = "movie"
SERIES = "series"


# --------------------------------------------------------------------------- text helpers

def extract_4digit_year(raw: str | None) -> str:
    """First 4-digit run that starts with 1 or 2 (``providers/models.rs``)."""
    if not raw:
        return ""
    for i in range(len(raw) - 3):
        chunk = raw[i : i + 4]
        if chunk.isascii() and chunk.isdigit() and chunk[0] in "12":
            return chunk
    return ""


def strip_emojis(text: str) -> str:
    def bad(c: str) -> bool:
        u = ord(c)
        return (
            0x1F000 <= u <= 0x1FAFF
            or 0x2600 <= u <= 0x27BF
            or 0x2300 <= u <= 0x23FF
            or 0x2B00 <= u <= 0x2BFF
            or 0xFE00 <= u <= 0xFE0F
            or u == 0x200D
        )

    return "".join(c for c in text if not bad(c))


def clean_stream_text(text: str) -> str:
    """Strip emojis and collapse whitespace."""
    return " ".join(strip_emojis(text).split())


_LANGUAGE_TAGS = (
    "hindi", "tamil", "telugu", "kannada", "malayalam", "bengali", "marathi", "punjabi",
    "gujarati", "urdu", "english", "spanish", "french", "german", "italian", "japanese",
    "korean", "chinese", "russian", "portuguese", "turkish", "arabic", "dub", "audio",
    "multi", "season",
)


def clean_title(raw: str | None) -> str:
    """Strip release-tag noise from a title (``clean_moviebox_title``).

    Used to match the same title across sources, so favorites and history stay
    linked even when a provider decorates names with ``[Hindi]`` or ``S01``.
    """
    original = (raw or "").strip()
    title = original
    if not title:
        return ""

    while title.startswith("["):
        close = title.find("]")
        if close == -1:
            break
        remainder = title[close + 1 :].strip()
        if remainder:
            title = remainder
        else:
            break

    pos = title.find("[")
    if pos > 0:
        title = title[:pos].strip()

    pos = title.find("(")
    if pos > 0:
        inside = title[pos + 1 :].split(")")[0].strip()
        is_year = len(inside) == 4 and inside.isascii() and inside.isdigit() and 1900 <= int(inside) <= 2099
        if not is_year:
            title = title[:pos].strip()

    pos = title.rfind(" - ")
    if pos != -1:
        suffix = title[pos + 3 :]
        low = suffix.lower()
        season_like = suffix[:1] in ("s", "S") and all(c.isdigit() or c == "-" for c in suffix[1:])
        if any(tag in low for tag in _LANGUAGE_TAGS) or season_like:
            title = title[:pos].strip()

    s_idx = title.rfind(" S")
    if s_idx != -1:
        suffix = title[s_idx + 2 :]
        if suffix and suffix[0].isdigit() and all(c.isdigit() or c in "-S" for c in suffix):
            title = title[:s_idx].strip()

    s_idx = title.lower().rfind(" season ")
    if s_idx != -1:
        title = title[:s_idx].strip()

    for sep in "_ .-":
        pos = title.rfind(sep)
        if pos != -1:
            suffix = title[pos + 1 :]
            if len(suffix) > 1 and suffix[-1] in "pP" and suffix[:-1].isascii() and suffix[:-1].isdigit():
                if 144 <= int(suffix[:-1]) <= 8640:
                    title = title[:pos].strip()

    cleaned = title.rstrip("-:_. ").strip()
    return cleaned or original


def parse_duration_seconds(text: str | None) -> int | None:
    """``1:32:10``, ``92:05``, ``2h 5m``, ``148 min`` -> seconds."""
    s = (text or "").strip()
    if not s or s.lower() == "n/a":
        return None
    if ":" in s:
        parts = s.split(":")
        try:
            nums = [int(p.strip()) for p in parts]
        except ValueError:
            return None
        if len(nums) == 2:
            return nums[0] * 60 + nums[1]
        if len(nums) == 3:
            return nums[0] * 3600 + nums[1] * 60 + nums[2]
    units = re.findall(r"(\d+)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])", s.lower())
    if units:
        scale = {"h": 3600, "m": 60, "s": 1}
        return sum(int(n) * scale[u[0]] for n, u in units)
    # A bare number is treated as minutes (Cinemeta reports ``runtime: "148"``).
    return int(s) * 60 if s.isascii() and s.isdigit() else None


def format_duration(secs: int) -> str:
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_file_size(size: float) -> str:
    mb = size / 1024 / 1024
    return f"{mb / 1024:.1f}GB" if mb >= 1024 else f"{mb:.0f}MB"


# --------------------------------------------------------------------------- entities

@dataclass
class CatalogItem:
    provider: str
    id: str
    title: str
    media_type: str = MOVIE
    year: str | None = None
    poster_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["stype"] = 2 if self.media_type == SERIES else 1
        return d


@dataclass
class Episode:
    season: int
    number: int
    title: str | None = None
    overview: str | None = None
    thumbnail: str | None = None
    released: str | None = None


@dataclass
class Season:
    number: int
    episodes: list[Episode] = field(default_factory=list)


@dataclass
class MediaDetails:
    provider: str
    id: str
    title: str
    media_type: str = MOVIE
    year: str | None = None
    description: str | None = None
    imdb_rating: str | None = None
    director: str | None = None
    stars: str | None = None
    poster_url: str | None = None
    background_url: str | None = None
    duration: str | None = None
    genres: list[str] = field(default_factory=list)
    seasons: list[Season] = field(default_factory=list)

    @property
    def is_series(self) -> bool:
        return self.media_type == SERIES

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["stype"] = 2 if self.is_series else 1
        d["duration_seconds"] = parse_duration_seconds(self.duration)
        return d


@dataclass
class SourceMirror:
    label: str
    resolver_url: str
    headers: list[tuple[str, str]] = field(default_factory=list)
    direct_file: bool = True
    # Stremio ``behaviorHints.notWebReady``: the file may not decode in a browser
    # (MKV/HEVC/multi-audio); the UI steers those to an external player.
    web_ready: bool = True


@dataclass
class Release:
    provider: str
    filename: str
    quality: str | None = None
    codec: str | None = None
    language: str | None = None
    size_bytes: int | None = None
    season: int | None = None
    episode: int | None = None
    mirrors: list[SourceMirror] = field(default_factory=list)
    resource_id: str | None = None

    def resolution(self) -> int:
        q = (self.quality or "").strip()
        if q.lower() in ("4k", "uhd"):
            return 2160
        try:
            return int(q.rstrip("pP"))
        except ValueError:
            return 1080

    def direct_url(self) -> str | None:
        return self.mirrors[0].resolver_url if self.mirrors else None


@dataclass
class StreamResult:
    releases: list[Release] = field(default_factory=list)
    # Addons that answered but offered nothing playable over HTTP (torrent-only etc.).
    blocked: list[str] = field(default_factory=list)


@dataclass
class Shelf:
    label: str
    items: list[CatalogItem] = field(default_factory=list)
    error: str | None = None
