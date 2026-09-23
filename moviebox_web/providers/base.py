"""The provider contract (Python port of the Rust ``Provider`` / ``ReleaseProvider`` traits).

Every content source implements ``Provider``. Sources that resolve playable
releases (as opposed to only browsing metadata) also implement
``ReleaseProvider``. See ``providers/custom/_template.py`` for a starting point.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

from ..models import CatalogItem, MediaDetails, Shelf, StreamResult


class ProviderError(Exception):
    """Standard error boundary. ``kind`` is one of the ``KIND_*`` constants."""

    KIND_NETWORK = "network"
    KIND_RATE_LIMITED = "rate_limited"
    KIND_NOT_FOUND = "not_found"
    KIND_PARSING = "parsing"
    KIND_UNAVAILABLE = "unavailable"

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind
        self.message = message

    def user_message(self, provider_label: str) -> str:
        return {
            self.KIND_NETWORK: f"{provider_label} is unreachable. Check the connection and try again.",
            self.KIND_RATE_LIMITED: f"{provider_label} is rate limiting requests. Wait a moment and retry.",
            self.KIND_NOT_FOUND: f"Nothing found on {provider_label}.",
            self.KIND_PARSING: f"{provider_label} returned data that could not be read.",
        }.get(self.kind, self.message or f"{provider_label} is unavailable.")

    @property
    def http_status(self) -> int:
        return {
            self.KIND_NETWORK: 502,
            self.KIND_RATE_LIMITED: 429,
            self.KIND_NOT_FOUND: 404,
            self.KIND_PARSING: 502,
        }.get(self.kind, 503)


@dataclass(frozen=True)
class ProviderCapabilities:
    supports_search: bool = True
    supports_pagination: bool = False
    supports_series: bool = True
    supports_subtitles: bool = False
    supports_homepage: bool = False


@dataclass
class ProviderContext:
    """What the app hands to plugins: policy-enforced HTTP, a cache and a data dir.

    Plugins should use ``ctx.http`` for all requests so they inherit the app's
    private-address policy, timeouts and redirect handling.
    """

    http: Any
    cache: Any
    data_dir: Any
    log: Any
    env: Callable[[str, str | None], str | None]


class Provider(ABC):
    id: str = ""
    label: str = ""

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...

    @abstractmethod
    def search(self, query: str, page: int = 1) -> list[CatalogItem]: ...

    @abstractmethod
    def details(self, media_id: str) -> MediaDetails: ...

    def browse(self) -> list[Shelf]:
        """Home-page shelves. Only called when ``supports_homepage`` is true."""
        return []


class ReleaseProvider(Provider):
    @abstractmethod
    def episode_streams(self, media_id: str, season: int = 0, episode: int = 0, is_series: bool = False) -> StreamResult:
        """Playable releases for a movie (season/episode 0) or one episode."""
