"""Community Stremio HTTP addons (``providers/addons`` in the terminal app).

Speaks the standard addon protocol: ``/manifest.json``, ``/catalog``, ``/meta``
and ``/stream``. Cinemeta is installed by default as the locked core metadata
source. Only direct HTTP(S) streams are playable; torrent-only results are
reported as "blocked" instead of silently dropped.
"""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from ..cache import TTL_CATALOG, TTL_DETAILS, TTL_MANIFEST, TTL_SEARCH, TTL_STREAMS, DiskCache
from ..config import load_json, save_json
from ..models import (
    MOVIE,
    SERIES,
    CatalogItem,
    Episode,
    MediaDetails,
    Release,
    Season,
    Shelf,
    SourceMirror,
    StreamResult,
    clean_stream_text,
    extract_4digit_year,
)
from ..security import HttpPolicy, UnsafeURL
from .base import ProviderCapabilities, ProviderError, ReleaseProvider

CINEMETA_MANIFEST = "https://v3-cinemeta.strem.io/manifest.json"
_SERIES_TYPES = ("series", "tv", "anime")
_TYPE_PREFIXES = ("movie", "series", "tv", "anime", "other")
_ALLOWED_STREAM_HEADERS = {"user-agent", "referer", "origin", "range", "x-forwarded-for", "accept", "accept-language"}


# --------------------------------------------------------------------------- tolerant JSON coercion
# Addons in the wild disagree about types (year as number or string, genres as a
# list or "A, B"). These replace the serde deserializers in the Rust models.

def _text(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip() or None
    return None


def _text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [str(value)]
    out: list[str] = []
    if isinstance(value, list):
        for v in value:
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
            elif isinstance(v, dict) and isinstance(v.get("name"), str) and v["name"].strip():
                out.append(v["name"].strip())
    return out


def _uint(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _first(*values: Any) -> str | None:
    for v in values:
        t = _text(v)
        if t:
            return t
    return None


# --------------------------------------------------------------------------- installed addons

@dataclass
class InstalledAddon:
    manifest_url: str
    name: str
    version: str | None = None
    description: str | None = None
    enabled: bool = True
    provides_catalog: bool = False
    provides_meta: bool = False
    provides_stream: bool = False
    id_prefixes: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)

    @classmethod
    def cinemeta(cls) -> "InstalledAddon":
        return cls(
            manifest_url=CINEMETA_MANIFEST,
            name="Cinemeta",
            version="3.0.14",
            description="Official Catalog and Metadata",
            provides_catalog=True,
            provides_meta=True,
            id_prefixes=["tt"],
            types=["movie", "series"],
        )

    @classmethod
    def from_dict(cls, raw: dict) -> "InstalledAddon | None":
        if not isinstance(raw, dict) or not raw.get("manifest_url") or not raw.get("name"):
            return None
        return cls(
            manifest_url=str(raw["manifest_url"]),
            name=str(raw["name"]),
            version=_text(raw.get("version")),
            description=_text(raw.get("description")),
            enabled=bool(raw.get("enabled", True)),
            provides_catalog=bool(raw.get("provides_catalog", False)),
            provides_meta=bool(raw.get("provides_meta", False)),
            provides_stream=bool(raw.get("provides_stream", False)),
            id_prefixes=_text_list(raw.get("id_prefixes")),
            types=_text_list(raw.get("types")),
        )

    @classmethod
    def from_manifest(cls, manifest_url: str, manifest: dict) -> "InstalledAddon":
        resources = {
            (r if isinstance(r, str) else (r or {}).get("name", "")).lower()
            for r in manifest.get("resources", [])
            if isinstance(r, (str, dict))
        }
        return cls(
            manifest_url=manifest_url,
            name=str(manifest["name"]),
            version=_text(manifest.get("version")),
            description=_text(manifest.get("description")),
            provides_catalog="catalog" in resources or bool(manifest.get("catalogs")),
            provides_meta="meta" in resources,
            provides_stream="stream" in resources,
            id_prefixes=_text_list(manifest.get("idPrefixes")),
            types=_text_list(manifest.get("types")),
        )

    @property
    def is_core(self) -> bool:
        return self.name.lower() == "cinemeta" or "cinemeta" in self.manifest_url.lower()

    def to_dict(self) -> dict:
        return asdict(self)


class AddonsStore:
    """``addons_config.json``: list of installed addons; Cinemeta is always present and enabled."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        raw = load_json(path, [])
        loaded = [a for a in (InstalledAddon.from_dict(r) for r in raw) if a] if isinstance(raw, list) else []
        if not any(a.is_core for a in loaded):
            loaded.insert(0, InstalledAddon.cinemeta())
        for a in loaded:
            if a.is_core:
                a.enabled = True
        self._addons = loaded
        self._save()

    def _save(self) -> None:
        save_json(self.path, [a.to_dict() for a in self._addons], pretty=True)

    def list(self) -> list[InstalledAddon]:
        with self._lock:
            return [InstalledAddon(**a.to_dict()) for a in self._addons]

    def add(self, addon: InstalledAddon) -> None:
        with self._lock:
            key = AddonClient.base_addon_url(addon.manifest_url)
            if any(AddonClient.base_addon_url(a.manifest_url) == key for a in self._addons):
                raise ValueError(f"{addon.name} is already installed")
            self._addons.append(addon)
            self._save()

    def set_enabled(self, manifest_url: str, enabled: bool) -> InstalledAddon:
        with self._lock:
            addon = self._find(manifest_url)
            if addon.is_core and not enabled:
                raise PermissionError("Cinemeta is the core metadata source and cannot be disabled")
            addon.enabled = enabled
            self._save()
            return addon

    def remove(self, manifest_url: str) -> None:
        with self._lock:
            addon = self._find(manifest_url)
            if addon.is_core:
                raise PermissionError("Cinemeta is the core metadata source and cannot be removed")
            self._addons.remove(addon)
            self._save()

    def _find(self, manifest_url: str) -> InstalledAddon:
        key = AddonClient.base_addon_url(manifest_url)
        for a in self._addons:
            if a.manifest_url == manifest_url or AddonClient.base_addon_url(a.manifest_url) == key:
                return a
        raise KeyError(manifest_url)


# --------------------------------------------------------------------------- protocol client

class AddonClient:
    def __init__(self, http: HttpPolicy, cache: DiskCache):
        self.http = http
        self.cache = cache

    # -- url helpers
    @staticmethod
    def normalize_manifest_url(raw: str) -> str:
        url = raw.strip()
        if url.startswith("stremio://"):
            url = "https://" + url[len("stremio://"):]
        elif not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        if not url.endswith("/manifest.json") and "/manifest.json?" not in url:
            url = url + "manifest.json" if url.endswith("/") else url + "/manifest.json"
        return url

    @staticmethod
    def base_addon_url(manifest_url: str) -> str:
        url = AddonClient.normalize_manifest_url(manifest_url)
        pos = url.rfind("/manifest.json")
        return url[:pos] if pos != -1 else url.rstrip("/")

    # -- transport
    def _get_json(self, url: str, what: str, cache_ns: str | None = None, ttl: float = 0, cache_if=None) -> Any:
        if cache_ns:
            hit = self.cache.get(cache_ns, url, ttl)
            if hit is not None:
                return hit
        try:
            resp = self.http.get(url, timeout=(8, 12))
        except UnsafeURL as exc:
            raise ProviderError(ProviderError.KIND_UNAVAILABLE, f"{what}: {exc}") from exc
        except requests.RequestException as exc:
            raise ProviderError(ProviderError.KIND_NETWORK, f"{what} failed: {exc.__class__.__name__}") from exc
        try:
            if resp.status_code == 429:
                raise ProviderError(ProviderError.KIND_RATE_LIMITED, f"{what}: HTTP 429")
            if resp.status_code == 404:
                raise ProviderError(ProviderError.KIND_NOT_FOUND, f"{what}: HTTP 404")
            if resp.status_code >= 400:
                raise ProviderError(ProviderError.KIND_NETWORK, f"{what}: HTTP {resp.status_code}")
            try:
                data = resp.json()
            except ValueError as exc:
                raise ProviderError(ProviderError.KIND_PARSING, f"{what}: invalid JSON") from exc
        finally:
            resp.close()
        if cache_ns and (cache_if is None or cache_if(data)):
            self.cache.set(cache_ns, url, data)
        return data

    def fetch_manifest(self, manifest_url: str) -> dict:
        url = self.normalize_manifest_url(manifest_url)
        manifest = self._get_json(url, "Manifest request", "addon_manifest", TTL_MANIFEST)
        if not isinstance(manifest, dict) or not str(manifest.get("name", "")).strip():
            raise ProviderError(ProviderError.KIND_PARSING, "Addon manifest missing a valid name")
        return manifest

    @staticmethod
    def _metas(raw: Any) -> list[dict]:
        if isinstance(raw, dict):
            for key in ("metas", "items", "results"):
                if isinstance(raw.get(key), list):
                    raw = raw[key]
                    break
            else:
                return []
        return [m for m in raw if isinstance(m, dict) and m.get("id")] if isinstance(raw, list) else []

    def fetch_catalog_search(self, base: str, media_type: str, catalog_id: str, query: str) -> list[dict]:
        encoded = quote(query.strip(), safe="")
        url = f"{base}/catalog/{media_type}/{catalog_id}/search={encoded}.json"
        return self._metas(self._get_json(url, "Catalog search", "addon_search", TTL_SEARCH))

    def fetch_catalog(self, base: str, media_type: str, catalog_id: str, extra: str | None = None) -> list[dict]:
        tail = f"/{extra}.json" if extra and extra.strip() else ".json"
        url = f"{base}/catalog/{media_type}/{catalog_id}{tail}"
        return self._metas(self._get_json(url, "Catalog request", "addon_catalog", TTL_CATALOG))

    def fetch_meta(self, base: str, media_type: str, media_id: str) -> dict:
        url = f"{base}/meta/{media_type}/{quote(media_id, safe=':')}.json"
        raw = self._get_json(url, "Metadata request", "addon_meta", TTL_DETAILS)
        meta = raw.get("meta") if isinstance(raw, dict) and isinstance(raw.get("meta"), dict) else raw
        if not isinstance(meta, dict) or not meta.get("id"):
            raise ProviderError(ProviderError.KIND_NOT_FOUND, "Metadata response had no meta object")
        return meta

    def fetch_streams(self, base: str, media_type: str, stream_id: str) -> list[dict]:
        url = f"{base}/stream/{media_type}/{quote(stream_id, safe=':')}.json"
        # Empty answers are not cached: many addons fill in results after a short delay.
        has_streams = lambda d: bool(d.get("streams") if isinstance(d, dict) else d)  # noqa: E731
        raw = self._get_json(url, "Streams request", "addon_streams", TTL_STREAMS, cache_if=has_streams)
        streams = raw.get("streams") if isinstance(raw, dict) else raw
        return [s for s in streams if isinstance(s, dict)] if isinstance(streams, list) else []


# --------------------------------------------------------------------------- adapters

def _is_series_type(kind: Any) -> bool:
    return str(kind or "").lower() in _SERIES_TYPES


def meta_to_catalog_item(meta: dict, provider: str = "addons") -> CatalogItem:
    is_series = _is_series_type(meta.get("type"))
    year = extract_4digit_year(_first(meta.get("releaseInfo"), meta.get("year"), meta.get("released"))) or None
    title = _text(meta.get("name")) or _text(meta.get("title")) or "Unknown"
    prefix = SERIES if is_series else MOVIE
    return CatalogItem(
        provider=provider,
        id=f"{prefix}:{meta['id']}",
        title=title,
        media_type=prefix,
        year=year,
        poster_url=_first(meta.get("poster"), meta.get("cover")),
    )


def meta_to_media_details(meta: dict, provider: str = "addons") -> MediaDetails:
    videos = [v for v in meta.get("videos", []) if isinstance(v, dict)] if isinstance(meta.get("videos"), list) else []
    is_series = _is_series_type(meta.get("type")) or bool(videos)

    seasons: dict[int, dict[int, Episode]] = {}
    for v in videos:
        s = _uint(v.get("season"))
        s = 1 if s is None else s
        e = _uint(v.get("episode"))
        if e is None:
            e = _uint(v.get("number"))
        e = 1 if e is None else e
        seasons.setdefault(s, {}).setdefault(
            e,
            Episode(
                season=s,
                number=e,
                title=_first(v.get("title"), v.get("name")),
                overview=_first(v.get("overview"), v.get("description")),
                thumbnail=_first(v.get("thumbnail")),
                released=_first(v.get("released")),
            ),
        )

    year_raw = _first(meta.get("releaseInfo"), meta.get("year"), meta.get("released")) or ""
    year = extract_4digit_year(year_raw) or (year_raw or None)
    prefix = SERIES if is_series else MOVIE

    def joined(*keys: str) -> str | None:
        for key in keys:
            items = _text_list(meta.get(key))
            if items:
                return ", ".join(items)
        return None

    return MediaDetails(
        provider=provider,
        id=f"{prefix}:{meta['id']}",
        title=_text(meta.get("name")) or _text(meta.get("title")) or "Unknown",
        media_type=prefix,
        year=year,
        description=_first(meta.get("description"), meta.get("overview"), meta.get("synopsis")),
        imdb_rating=_first(meta.get("imdbRating"), meta.get("rating")),
        director=joined("director", "directors", "writers", "writer"),
        stars=joined("cast", "stars"),
        poster_url=_first(meta.get("poster"), meta.get("cover"), meta.get("background")),
        background_url=_first(meta.get("background"), meta.get("poster")),
        duration=_first(meta.get("runtime")),
        genres=_text_list(meta.get("genres")) or _text_list(meta.get("genre")),
        seasons=[Season(number=n, episodes=[eps[k] for k in sorted(eps)]) for n, eps in sorted(seasons.items())],
    )


# -- release parsing heuristics (ports of parse_quality / parse_codec / ... in adapter.rs)

def parse_quality(text: str) -> str | None:
    up = text.upper()
    words = set(re.split(r"[^A-Z0-9]+", up))
    if "2160P" in up or "4K" in up or "UHD" in up:
        return "2160p"
    if "1080P" in up or "FHD" in up or "FULL HD" in up or "FULLHD" in up:
        return "1080p"
    if "720P" in up or "HD" in words:
        return "720p"
    if "480P" in up or "SD" in words:
        return "480p"
    return None


def parse_codec(text: str) -> str | None:
    up = text.upper()
    if any(t in up for t in ("HEVC", "X265", "H.265", "H265")):
        return "HEVC/x265"
    if any(t in up for t in ("X264", "H.264", "H264", "AVC")):
        return "AVC/x264"
    if "AV1" in up:
        return "AV1"
    return None


_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(gib|gb|mib|mb)(?![a-z])|(\d+(?:\.\d+)?)\s+(g|m)(?![a-z])", re.I)


def parse_size_bytes(text: str) -> int | None:
    m = _SIZE_RE.search(text)
    if not m:
        return None
    num = float(m.group(1) or m.group(3))
    unit = (m.group(2) or m.group(4)).lower()
    return int(num * (1_073_741_824 if unit.startswith("g") else 1_048_576))


_AUDIO_CANDIDATES = [
    ("HINDI", "Hindi"), ("ENGLISH", "English"), ("ENG", "English"), ("HIN", "Hindi"), ("TAMIL", "Tamil"),
    ("TELUGU", "Telugu"), ("BENGALI", "Bengali"), ("BEN", "Bengali"), ("MALAYALAM", "Malayalam"),
    ("KANNADA", "Kannada"), ("MARATHI", "Marathi"), ("PUNJABI", "Punjabi"), ("GUJARATI", "Gujarati"),
    ("URDU", "Urdu"), ("SPANISH", "Spanish"), ("FRENCH", "French"), ("GERMAN", "German"), ("ITALIAN", "Italian"),
    ("JAPANESE", "Japanese"), ("JAP", "Japanese"), ("KOREAN", "Korean"), ("KOR", "Korean"), ("RUSSIAN", "Russian"),
    ("CHINESE", "Chinese"), ("DUAL", "Dual Audio"), ("MULTI", "Multi Audio"),
]


def parse_audio_tracks(text: str) -> str | None:
    up = text.upper()
    langs: list[str] = []
    for needle, label in _AUDIO_CANDIDATES:
        # Short language codes need letter boundaries so "ENGINE" is not English.
        pattern = rf"(?<![A-Z]){needle}(?![A-Z])" if len(needle) <= 3 else needle
        if re.search(pattern, up) and label not in langs:
            langs.append(label)
    return " + ".join(langs) if langs else None


_SE_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9])[sS](\d{1,3})[ ._-]*[eE](\d{1,4})"),
    re.compile(r"(?<![A-Za-z0-9])(\d{1,3})[xX](\d{1,4})(?!\d)"),
)


def parse_season_episode(text: str) -> tuple[int, int] | None:
    best: tuple[int, tuple[int, int]] | None = None
    for i, pat in enumerate(_SE_PATTERNS):
        m = pat.search(text)
        if not m:
            continue
        s, e = int(m.group(1)), int(m.group(2))
        if i == 1 and not (0 < s < 100 and 0 < e < 10000):
            continue
        if best is None or m.start() < best[0]:
            best = (m.start(), (s, e))
    if best:
        return best[1]
    up = text.upper()
    ep = re.search(r"EPISODE\s+(\d+)", up)
    if ep:
        season = re.search(r"SEASON\s+(\d+)", up)
        return (int(season.group(1)) if season else 1, int(ep.group(1)))
    return None


def _domain_label(url: str) -> str | None:
    host = re.sub(r"^https?://", "", url.strip(), flags=re.I)
    host = re.split(r"[/:?#]", host, maxsplit=1)[0].strip()
    if not host or re.fullmatch(r"[\d.]+|[0-9a-fA-F:]+", host):
        return None
    parts = host.split(".")
    if len(parts) < 2:
        return None
    main = parts[-3] if parts[-2] in ("co", "com") and len(parts) >= 3 else parts[-2]
    if not main or main in ("www", "api", "cdn"):
        return None
    return " ".join(w[:1].upper() + w[1:] for w in re.split(r"[-_]", main))


def detect_stream_host(addon_name: str, stream_name: str, url: str) -> str:
    labels: list[str] = []
    for line in stream_name.splitlines():
        t = line.strip().strip("[]() ")
        if t and t.lower() != addon_name.lower() and t not in labels:
            labels.append(t)
    domain = _domain_label(url)
    if domain and domain.lower() != addon_name.lower() and not any(l.lower() == domain.lower() for l in labels):
        labels.append(domain)
    return f"{addon_name} · {labels[0]}" if labels else addon_name


def stream_to_release(addon_name: str, stream: dict, season: int, episode: int) -> Release | None:
    url = _text(stream.get("url"))
    if not url or not url.lower().startswith(("http://", "https://")):
        return None

    name = _text(stream.get("name")) or ""
    title = _text(stream.get("title")) or ""
    desc = _text(stream.get("description")) or ""
    combined = f"{name} {title} {desc}"

    if season > 0 and episode > 0:
        found = parse_season_episode(combined)
        if found and found != (season, episode):
            return None

    hints = stream.get("behaviorHints") if isinstance(stream.get("behaviorHints"), dict) else {}
    size = _uint(hints.get("videoSize")) or parse_size_bytes(combined)

    raw_filename = _text(stream.get("title")) or _text(stream.get("description")) or _text(stream.get("name")) or f"{addon_name} Stream"
    filename = clean_stream_text(raw_filename.splitlines()[0] if raw_filename.strip() else raw_filename) or f"{addon_name} Stream"

    headers: list[tuple[str, str]] = []
    if isinstance(hints.get("headers"), dict):
        headers = [(str(k), str(v)) for k, v in hints["headers"].items() if str(k).lower() in _ALLOWED_STREAM_HEADERS]

    language = parse_audio_tracks(combined)
    return Release(
        provider="addons",
        filename=filename,
        quality=parse_quality(combined),
        codec=parse_codec(combined),
        language=clean_stream_text(language) if language else None,
        size_bytes=size,
        season=season if season > 0 else None,
        episode=episode if episode > 0 else None,
        mirrors=[
            SourceMirror(
                label=clean_stream_text(detect_stream_host(addon_name, name, url)),
                resolver_url=url,
                headers=headers,
                direct_file=True,
                web_ready=not bool(hints.get("notWebReady", False)),
            )
        ],
    )


def _quality_score(quality: str | None) -> int:
    return {"2160p": 40, "1080p": 30, "720p": 20, "480p": 10}.get(quality or "", 0)


def aggregate_streams(client: AddonClient, addons: list[InstalledAddon], subject_id: str, season: int, episode: int, is_series: bool) -> StreamResult:
    stream_addons = [a for a in addons if a.enabled and a.provides_stream]
    if not stream_addons:
        return StreamResult()

    clean_id = subject_id
    head, sep, rest = subject_id.partition(":")
    if sep and head.lower() in _TYPE_PREFIXES:
        clean_id = rest
    media_type = "series" if is_series else "movie"
    stream_id = f"{clean_id}:{season}:{episode}" if is_series and episode > 0 else clean_id

    def one(addon: InstalledAddon) -> tuple[list[Release], str | None]:
        base = client.base_addon_url(addon.manifest_url)
        try:
            items = client.fetch_streams(base, media_type, stream_id)
        except ProviderError:
            return [], None
        releases = [r for r in (stream_to_release(addon.name, s, season, episode) for s in items) if r]
        return releases, (addon.name if items and not releases else None)

    with ThreadPoolExecutor(max_workers=min(8, len(stream_addons))) as pool:
        results = list(pool.map(one, stream_addons))

    merged: list[Release] = []
    blocked: list[str] = []
    by_url: dict[str, Release] = {}
    for releases, blocked_name in results:
        if blocked_name:
            blocked.append(blocked_name)
        for rel in releases:
            direct = rel.direct_url() or ""
            existing = by_url.get(direct)
            if existing is None:
                by_url[direct] = rel
                merged.append(rel)
                continue
            for m in rel.mirrors:
                if not any(em.resolver_url == m.resolver_url and em.label == m.label for em in existing.mirrors):
                    existing.mirrors.append(m)

    merged.sort(key=lambda r: (-_quality_score(r.quality), -(r.size_bytes or 0), r.mirrors[0].label if r.mirrors else ""))
    return StreamResult(releases=merged, blocked=blocked)


# --------------------------------------------------------------------------- browse presets

@dataclass(frozen=True)
class CatalogTarget:
    label: str
    addon: InstalledAddon
    media_type: str
    catalog_id: str


def curated_catalog_presets(addons: list[InstalledAddon]) -> list[CatalogTarget]:
    enabled = [a for a in addons if a.enabled and a.provides_catalog]
    multi = len(enabled) > 1
    targets: list[CatalogTarget] = []
    for a in enabled:
        suffix = f" ({a.name})" if multi else ""
        has_movies = not a.types or any(t.lower() == "movie" for t in a.types)
        has_series = not a.types or any(t.lower() == "series" for t in a.types)
        if has_movies:
            targets.append(CatalogTarget(f"Top Movies{suffix}", a, "movie", "top"))
        if has_series:
            targets.append(CatalogTarget(f"Top Series{suffix}", a, "series", "top"))
        if has_movies and a.is_core:
            targets.append(CatalogTarget(f"Top Rated Movies{suffix}", a, "movie", "imdbRating"))
        if has_series and a.is_core:
            targets.append(CatalogTarget(f"Top Rated Series{suffix}", a, "series", "imdbRating"))
        if len(targets) >= 6:
            break
    if not targets:
        core = InstalledAddon.cinemeta()
        targets = [
            CatalogTarget("Top Movies", core, "movie", "top"),
            CatalogTarget("Top Series", core, "series", "top"),
            CatalogTarget("Top Rated Movies", core, "movie", "imdbRating"),
            CatalogTarget("Top Rated Series", core, "series", "imdbRating"),
        ]
    return targets[:6]


# --------------------------------------------------------------------------- provider

class AddonsProvider(ReleaseProvider):
    id = "addons"
    label = "Addons"
    BROWSE_LIMIT = 60

    def __init__(self, client: AddonClient, store: AddonsStore):
        self.client = client
        self.store = store

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_search=True, supports_pagination=False, supports_series=True, supports_subtitles=False, supports_homepage=True)

    def search(self, query: str, page: int = 1) -> list[CatalogItem]:
        addons = [a for a in self.store.list() if a.enabled and (a.provides_meta or a.provides_catalog)]
        if not addons:
            raise ProviderError(ProviderError.KIND_UNAVAILABLE, "No catalog or metadata addon is enabled. Open Settings to configure one.")

        combined: list[dict] = []
        errors: list[ProviderError] = []
        succeeded = False
        for addon in addons:
            base = self.client.base_addon_url(addon.manifest_url)
            for kind in ("movie", "series"):
                try:
                    combined.extend(self.client.fetch_catalog_search(base, kind, "top", query))
                    succeeded = True
                except ProviderError as exc:
                    errors.append(exc)
            if combined:
                break
        if not combined and errors and not succeeded:
            raise errors[-1]

        seen: set[str] = set()
        out: list[CatalogItem] = []
        for meta in combined:
            if meta["id"] in seen:
                continue
            seen.add(meta["id"])
            out.append(meta_to_catalog_item(meta))
        return out

    def details(self, media_id: str) -> MediaDetails:
        type_hint: str | None = None
        clean_id = media_id
        head, sep, rest = media_id.partition(":")
        if sep and head.lower() in _TYPE_PREFIXES:
            type_hint, clean_id = head.lower(), rest

        addons = self.store.list()
        targets = [a for a in addons if a.enabled and a.provides_meta]
        targets += [a for a in addons if a.enabled and a not in targets]

        types = ["series", "movie", "tv", "anime", "other"]
        if type_hint:
            types = [type_hint] + [t for t in types if t != type_hint]

        best: dict | None = None
        last_error: ProviderError | None = None
        for addon in targets:
            base = self.client.base_addon_url(addon.manifest_url)
            for kind in types:
                try:
                    meta = self.client.fetch_meta(base, kind, clean_id)
                except ProviderError as exc:
                    last_error = exc
                    continue
                valid = str(meta.get("id")) == clean_id and bool(_text(meta.get("name")) or _text(meta.get("title")))
                if not valid:
                    continue
                mtype = str(meta.get("type", "")).lower()
                if meta.get("videos") or mtype in ("series", "tv"):
                    return self._finish(meta, type_hint, clean_id)
                if type_hint == "movie" and mtype == "movie":
                    return self._finish(meta, "movie", clean_id)
                if best is None:
                    best = meta
        if best is not None:
            return self._finish(best, type_hint, clean_id)
        if last_error is not None and last_error.kind == ProviderError.KIND_NETWORK:
            raise last_error
        raise ProviderError(ProviderError.KIND_NOT_FOUND)

    @staticmethod
    def _finish(meta: dict, type_hint: str | None, clean_id: str) -> MediaDetails:
        details = meta_to_media_details(meta)
        if type_hint:
            details.id = f"{type_hint}:{clean_id}"
        return details

    def browse(self) -> list[Shelf]:
        targets = curated_catalog_presets(self.store.list())

        def load(target: CatalogTarget) -> Shelf:
            base = self.client.base_addon_url(target.addon.manifest_url)
            try:
                metas = self.client.fetch_catalog(base, target.media_type, target.catalog_id)
            except ProviderError as exc:
                return Shelf(label=target.label, error=exc.user_message(target.addon.name))
            return Shelf(label=target.label, items=[meta_to_catalog_item(m) for m in metas[: self.BROWSE_LIMIT]])

        with ThreadPoolExecutor(max_workers=6) as pool:
            return list(pool.map(load, targets))

    def episode_streams(self, media_id: str, season: int = 0, episode: int = 0, is_series: bool = False) -> StreamResult:
        return aggregate_streams(self.client, self.store.list(), media_id, season, episode, is_series)
