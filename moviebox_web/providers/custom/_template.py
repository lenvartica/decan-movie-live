"""Template for a custom provider.

Copy this file, remove the leading underscore from the name (files starting with
``_`` are never loaded), and fill in the three methods. Restart the server and the
provider appears in the provider picker.

Use this for sources you are licensed or authorised to use. Keep credentials in
environment variables, never in the file:

    export EXAMPLE_API_KEY="the key your provider issued to you"
    export EXAMPLE_BASE_URL="https://api.example.com"

Everything the app needs from a provider is in ``providers/base.py``. In short:

* ``search(query, page)``   -> list[CatalogItem]
* ``details(media_id)``     -> MediaDetails (include ``seasons`` for series)
* ``episode_streams(...)``  -> StreamResult(releases=[Release(mirrors=[SourceMirror(...)])])
* ``browse()``              -> optional home-page shelves (set supports_homepage=True)

Ids you return are opaque to the app; it hands them back to ``details`` and
``episode_streams`` unchanged. Raise ``ProviderError`` for failures so the UI
shows a clear message.

Playback goes through the app's signed proxy automatically. If your API needs
request headers (Referer, Authorization, cookies...) put them in
``SourceMirror.headers`` and the proxy will send them; the browser never sees
your key.
"""
from __future__ import annotations

from moviebox_web.models import CatalogItem, MediaDetails, Release, SourceMirror, StreamResult
from moviebox_web.providers.base import ProviderCapabilities, ProviderContext, ProviderError, ReleaseProvider


class ExampleProvider(ReleaseProvider):
    id = "example"
    label = "Example"

    def __init__(self, ctx: ProviderContext, base_url: str, api_key: str):
        self.ctx = ctx
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_search=True, supports_series=True, supports_homepage=False)

    def _get(self, path: str, **params):
        resp = self.ctx.http.get(f"{self.base_url}{path}", headers={"Authorization": f"Bearer {self.api_key}"}, timeout=(8, 15))
        if resp.status_code == 404:
            raise ProviderError(ProviderError.KIND_NOT_FOUND)
        if resp.status_code == 429:
            raise ProviderError(ProviderError.KIND_RATE_LIMITED)
        resp.raise_for_status()
        return resp.json()

    def search(self, query: str, page: int = 1) -> list[CatalogItem]:
        data = self._get(f"/search?q={query}&page={page}")
        return [
            CatalogItem(provider=self.id, id=str(r["id"]), title=r["title"], media_type="movie", year=str(r.get("year") or "") or None, poster_url=r.get("poster"))
            for r in data.get("results", [])
        ]

    def details(self, media_id: str) -> MediaDetails:
        d = self._get(f"/title/{media_id}")
        return MediaDetails(provider=self.id, id=media_id, title=d["title"], media_type="movie", year=str(d.get("year") or "") or None, description=d.get("overview"), poster_url=d.get("poster"))

    def episode_streams(self, media_id: str, season: int = 0, episode: int = 0, is_series: bool = False) -> StreamResult:
        d = self._get(f"/title/{media_id}/streams")
        releases = [
            Release(
                provider=self.id,
                filename=s.get("name", "Stream"),
                quality=s.get("quality"),
                size_bytes=s.get("size"),
                mirrors=[SourceMirror(label=self.label, resolver_url=s["url"], headers=[("Referer", self.base_url + "/")])],
            )
            for s in d.get("streams", [])
        ]
        return StreamResult(releases=releases)


def create_provider(ctx: ProviderContext):
    key = ctx.env("EXAMPLE_API_KEY", None)
    base = ctx.env("EXAMPLE_BASE_URL", None)
    if not key or not base:
        return None  # not configured: skip silently
    return ExampleProvider(ctx, base, key)
