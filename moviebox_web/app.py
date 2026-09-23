"""Flask app: JSON API + static single-page UI."""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from urllib.parse import urlsplit

import requests
from flask import Flask, Response, abort, jsonify, request, send_from_directory

from . import __version__
from .cache import DiskCache
from .config import ConfigStore, Paths
from .models import CatalogItem, Release, clean_title
from .providers import ProviderContext, ProviderError, ProviderRegistry
from .providers.addons import AddonClient, AddonsProvider, AddonsStore, InstalledAddon
from .providers.tv import TVService
from .proxy import StreamProxy, is_hls_url
from .security import (
    HttpPolicy,
    TokenSigner,
    UnsafeURL,
    host_header_ok,
    is_loopback_bind,
    load_secret,
    origin_ok,
    password_ok,
)
from .store import (
    FavoritesManager,
    HistoryManager,
    TVSources,
    build_favorite,
    build_history_item,
    identity_matches,
    is_in_progress,
)

log = logging.getLogger("moviebox_web")
STATIC_DIR = Path(__file__).parent / "static"
THEMES = json.loads((Path(__file__).parent / "themes.json").read_text("utf-8"))
DEFAULT_THEME = "Mocha"
TV_PAGE_MAX = 500


def theme_css(name: str) -> str:
    palette = THEMES.get(name) or THEMES[DEFAULT_THEME]
    decls = "".join(f"--c-{k.replace('_', '-')}:{v};" for k, v in palette.items() if k != "is_light")
    scheme = "light" if palette.get("is_light") else "dark"
    return f":root{{{decls}color-scheme:{scheme};}}"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _url_name(url: str) -> str | None:
    tail = urlsplit(url).path.rsplit("/", 1)[-1]
    return tail[:80] if tail and "." in tail else None


class Services:
    """Everything a request handler needs, built once per app."""

    def __init__(self, paths: Paths, allow_private: bool, allow_local_files: bool, plugin_dirs: list[Path]):
        self.paths = paths
        self.cache = DiskCache(paths.cache)
        self.config = ConfigStore(paths.config / "config.json")
        self.http = HttpPolicy(allow_private)
        self.signer = TokenSigner(load_secret(paths.data / "secret.key"))
        self.proxy = StreamProxy(self.http, self.signer)
        self.addons = AddonsStore(paths.config / "addons_config.json")
        self.addon_client = AddonClient(self.http, self.cache)
        self.favorites = FavoritesManager(paths.data / "favorites.json")
        self.history = HistoryManager(paths.data / "history.json")
        self.tv_sources = TVSources(paths.config / "tv_config.json")
        self.tv = TVService(paths.cache, self.http, allow_local_files, paths.data / "playlists")
        self.allow_local_files = allow_local_files

        self.registry = ProviderRegistry()
        self.registry.register(AddonsProvider(self.addon_client, self.addons))
        ctx = ProviderContext(http=self.http, cache=self.cache, data_dir=paths.data, log=log, env=lambda k, d=None: os.environ.get(k, d))
        self.registry.load_plugins(ctx, plugin_dirs)

        self._tv_lock = threading.Lock()
        self._tv_state: dict | None = None

    # -- TV channel snapshot (parsed once, reused across searches and pages)
    def tv_snapshot(self, force: bool = False) -> dict:
        with self._tv_lock:
            sources = self.tv_sources.snapshot()
            if force or self._tv_state is None or self._tv_state["sources"] != sources:
                channels, failed = self.tv.load_all(sources)
                groups: dict[str, int] = {}
                for ch in channels:
                    groups[ch.group or "Ungrouped"] = groups.get(ch.group or "Ungrouped", 0) + 1
                self._tv_state = {
                    "sources": sources,
                    "channels": channels,
                    "failed": failed,
                    "groups": [{"name": n, "count": c} for n, c in sorted(groups.items(), key=lambda kv: kv[0].lower())],
                }
            return self._tv_state

    def tv_invalidate(self) -> None:
        with self._tv_lock:
            self._tv_state = None

    # -- playback URL shaping
    def mirror_json(self, mirror) -> dict:
        url = mirror.resolver_url
        hls = is_hls_url(url)
        proxied = self.proxy.url_for(url, mirror.headers, _url_name(url))
        mode = self.config.get().get("proxy_mode", "auto")
        direct_ok = mode == "auto" and url.lower().startswith("https://") and not mirror.headers and not hls
        return {
            "label": mirror.label,
            "kind": "hls" if hls else "file",
            "web_ready": mirror.web_ready,
            "direct_url": url,
            "headers": {k: v for k, v in mirror.headers},
            "play_url": url if direct_ok else proxied,
            "proxy_url": proxied,
            "download_url": self.proxy.url_for(url, mirror.headers, _url_name(url)) + "?download=1",
        }

    def release_json(self, rel: Release) -> dict:
        return {
            "provider": rel.provider,
            "filename": rel.filename,
            "quality": rel.quality,
            "codec": rel.codec,
            "language": rel.language,
            "size_bytes": rel.size_bytes,
            "season": rel.season,
            "episode": rel.episode,
            "resolution": rel.resolution(),
            "resource_id": rel.resource_id,
            "mirrors": [self.mirror_json(m) for m in rel.mirrors],
        }


def _json_body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(_err("Expected a JSON object body.", 400))
    return data


def _err(message: str, status: int = 400) -> Response:
    r = jsonify({"error": message})
    r.status_code = status
    return r


def _int_arg(name: str, default: int = 0, lo: int = 0, hi: int = 100000) -> int:
    try:
        return max(lo, min(hi, int(request.args.get(name, default))))
    except (TypeError, ValueError):
        return default


def create_app(
    home: str | os.PathLike | None = None,
    *,
    host: str = "127.0.0.1",
    allow_private: bool | None = None,
    password: str | None = None,
    plugin_dirs: list[Path] | None = None,
) -> Flask:
    paths = Paths.resolve(home)
    loopback = is_loopback_bind(host)
    if allow_private is None:
        allow_private = _env_flag("MOVIEBOX_ALLOW_PRIVATE") or loopback
    password = password if password is not None else os.environ.get("MOVIEBOX_PASSWORD") or None
    allow_local_files = loopback or _env_flag("MOVIEBOX_ALLOW_LOCAL_PLAYLISTS")
    dirs = plugin_dirs if plugin_dirs is not None else [Path(__file__).parent / "providers" / "custom", paths.data / "plugins"]

    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
    app.json.sort_keys = False
    svc = Services(paths, allow_private, allow_local_files, dirs)
    app.extensions["moviebox"] = svc

    allowed_hosts = {"localhost", "127.0.0.1", "[::1]"} | {h.strip().lower() for h in os.environ.get("MOVIEBOX_ALLOWED_HOSTS", "").split(",") if h.strip()}

    # ------------------------------------------------------------------ request guards
    @app.before_request
    def guard():
        if loopback and not host_header_ok(request.host, allowed_hosts):
            return _err("Unrecognised Host header. Open the app via http://localhost.", 400)
        if password and not password_ok(request.headers.get("Authorization"), password):
            r = _err("Authentication required.", 401)
            r.headers["WWW-Authenticate"] = 'Basic realm="MovieBox Web"'
            return r
        if request.method not in ("GET", "HEAD", "OPTIONS") and not origin_ok(request.headers.get("Origin"), request.host):
            return _err("Cross-origin request blocked.", 403)
        return None

    @app.after_request
    def headers(resp: Response):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        if not request.path.startswith("/proxy/"):
            resp.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src * data: blob:; media-src 'self' blob: https: http:; "
                "script-src 'self' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src https://fonts.gstatic.com; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
        return resp

    # ------------------------------------------------------------------ errors
    @app.errorhandler(ProviderError)
    def provider_error(exc: ProviderError):
        label = "The provider"
        pid = request.args.get("provider")
        try:
            label = svc.registry.get(pid).label
        except ProviderError:
            pass
        r = jsonify({"error": exc.user_message(label), "kind": exc.kind, "detail": exc.message})
        r.status_code = exc.http_status
        return r

    @app.errorhandler(404)
    def not_found(_exc):
        if request.path.startswith(("/api/", "/proxy/")):
            return _err("Not found.", 404)
        return send_index()

    @app.errorhandler(413)
    def too_large(_exc):
        return _err("That upload is too large (16 MB max).", 413)

    # ------------------------------------------------------------------ pages
    def bootstrap() -> dict:
        cfg = svc.config.get()
        return {
            "version": __version__,
            "config": cfg,
            "themes": THEMES,
            "providers": [{"id": p.id, "label": p.label, **vars(p.capabilities())} for p in svc.registry.all()],
            "addons": [a.to_dict() for a in svc.addons.list()],
            "allow_local_files": svc.allow_local_files,
        }

    def send_index() -> Response:
        html = (STATIC_DIR / "index.html").read_text("utf-8")
        boot = json.dumps(bootstrap()).replace("<", "\\u003c")
        html = html.replace("<!--THEME-->", f"<style id=\"theme-vars\">{theme_css(svc.config.get()['active_theme'])}</style>")
        html = html.replace("<!--BOOT-->", f'<script id="boot" type="application/json">{boot}</script>')
        r = Response(html, mimetype="text/html")
        r.headers["Cache-Control"] = "no-cache"
        return r

    @app.get("/")
    def index():
        return send_index()

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "version": __version__})

    # ------------------------------------------------------------------ proxy
    @app.get("/proxy/<token>")
    @app.get("/proxy/<token>/<path:_name>")
    def proxy(token: str, _name: str = ""):
        return svc.proxy.handle(token, request)

    # ------------------------------------------------------------------ config / bootstrap
    @app.get("/api/bootstrap")
    def api_bootstrap():
        return jsonify(bootstrap())

    @app.put("/api/config")
    def api_config():
        body = _json_body()
        if "active_theme" in body and body["active_theme"] not in THEMES:
            return _err("Unknown theme.", 400)
        if "active_provider" in body:
            try:
                body["active_provider"] = svc.registry.get(body["active_provider"]).id
            except ProviderError:
                return _err("Unknown provider.", 400)
        return jsonify(svc.config.update(body))

    @app.get("/api/theme.css")
    def api_theme_css():
        name = request.args.get("name", DEFAULT_THEME)
        return Response(theme_css(name), mimetype="text/css")

    # ------------------------------------------------------------------ catalog
    @app.get("/api/browse")
    def api_browse():
        prov = svc.registry.get(request.args.get("provider"))
        if not prov.capabilities().supports_homepage:
            return jsonify({"shelves": [], "supported": False})
        shelves = [
            {"label": s.label, "error": s.error, "items": [i.to_dict() for i in s.items]}
            for s in prov.browse()
        ]
        return jsonify({"shelves": shelves, "supported": True})

    @app.get("/api/search")
    def api_search():
        q = (request.args.get("q") or "").strip()
        if not q:
            return _err("Type something to search for.", 400)
        if len(q) > 200:
            return _err("Search text is too long.", 400)
        prov = svc.registry.get(request.args.get("provider"))
        items: list[CatalogItem] = prov.search(q, _int_arg("page", 1, 1, 1000))
        return jsonify({"query": q, "provider": prov.id, "results": [i.to_dict() for i in items]})

    def _identity(details) -> dict:
        return {"provider": details.provider, "subject_id": details.id, "title": details.title, "stype": 2 if details.is_series else 1, "release_year": details.year or ""}

    @app.get("/api/details")
    def api_details():
        media_id = (request.args.get("id") or "").strip()
        if not media_id:
            return _err("Missing title id.", 400)
        prov = svc.registry.get(request.args.get("provider"))
        details = prov.details(media_id)
        ident = _identity(details)
        resume = next((i for i in svc.history.snapshot() if identity_matches(i, ident)), None)
        return jsonify(
            {
                "details": details.to_dict(),
                "favorite": svc.favorites.is_favorite(ident),
                "watched": svc.history.watched_episodes(prov.id, details.id),
                "resume": resume,
            }
        )

    @app.get("/api/streams")
    def api_streams():
        media_id = (request.args.get("id") or "").strip()
        if not media_id:
            return _err("Missing title id.", 400)
        prov = svc.registry.get(request.args.get("provider"))
        if not hasattr(prov, "episode_streams"):
            return _err(f"{prov.label} does not provide playable streams.", 400)
        season, episode = _int_arg("season"), _int_arg("episode")
        is_series = request.args.get("type") == "series" or media_id.startswith("series:")
        result = prov.episode_streams(media_id, season, episode, is_series)
        return jsonify({"releases": [svc.release_json(r) for r in result.releases], "blocked": result.blocked})

    # ------------------------------------------------------------------ addons
    def _addons_json() -> list[dict]:
        return [a.to_dict() for a in svc.addons.list()]

    @app.get("/api/addons")
    def api_addons():
        return jsonify({"addons": _addons_json()})

    @app.post("/api/addons")
    def api_addons_install():
        url = str(_json_body().get("manifest_url", "")).strip()
        if not url:
            return _err("Paste an addon manifest URL.", 400)
        manifest_url = svc.addon_client.normalize_manifest_url(url)
        try:
            manifest = svc.addon_client.fetch_manifest(manifest_url)
        except ProviderError as exc:
            return _err(f"Could not install that addon: {exc.message or exc.user_message('The addon')}", 400)
        addon = InstalledAddon.from_manifest(manifest_url, manifest)
        if not (addon.provides_catalog or addon.provides_meta or addon.provides_stream):
            return _err("That addon does not provide catalogs, metadata or streams.", 400)
        try:
            svc.addons.add(addon)
        except ValueError as exc:
            return _err(str(exc), 409)
        return jsonify({"addon": addon.to_dict(), "addons": _addons_json()}), 201

    @app.patch("/api/addons")
    def api_addons_toggle():
        body = _json_body()
        try:
            svc.addons.set_enabled(str(body.get("manifest_url", "")), bool(body.get("enabled")))
        except KeyError:
            return _err("Addon not found.", 404)
        except PermissionError as exc:
            return _err(str(exc), 403)
        return jsonify({"addons": _addons_json()})

    @app.delete("/api/addons")
    def api_addons_delete():
        try:
            svc.addons.remove(request.args.get("manifest_url", ""))
        except KeyError:
            return _err("Addon not found.", 404)
        except PermissionError as exc:
            return _err(str(exc), 403)
        return jsonify({"addons": _addons_json()})

    # ------------------------------------------------------------------ favorites
    @app.get("/api/favorites")
    def api_favorites():
        return jsonify({"items": svc.favorites.snapshot()})

    @app.post("/api/favorites/toggle")
    def api_favorites_toggle():
        item = build_favorite(_json_body())
        if not item["subject_id"] or not item["title"]:
            return _err("A title id and name are required.", 400)
        return jsonify({"favorite": svc.favorites.toggle(item)})

    @app.delete("/api/favorites")
    def api_favorites_clear():
        svc.favorites.clear()
        return jsonify({"items": []})

    # ------------------------------------------------------------------ history
    def _hist_item(body: dict) -> dict:
        item = build_history_item(body.get("item") if isinstance(body.get("item"), dict) else {})
        if not item["subject_id"] or not item["title"]:
            abort(_err("A title id and name are required.", 400))
        return item

    @app.get("/api/history")
    def api_history():
        return jsonify({"recent": svc.history.snapshot(), "continue": svc.history.continue_watching()})

    @app.post("/api/history/start")
    def api_history_start():
        body = _json_body()
        svc.history.record_start(_hist_item(body), max(0, int(body.get("start") or 0)))
        return jsonify({"ok": True})

    @app.post("/api/history/progress")
    def api_history_progress():
        body = _json_body()
        item = _hist_item(body)
        try:
            progress = max(0, int(float(body.get("progress") or 0)))
            duration = body.get("duration")
            duration = int(float(duration)) if duration not in (None, "") and float(duration) > 0 else None
        except (TypeError, ValueError):
            return _err("Invalid progress values.", 400)
        completed = bool(body.get("completed")) or bool(duration and progress >= int(duration * 0.90))
        svc.history.update_progress(item, progress, duration, completed)
        return jsonify({"ok": True, "completed": completed})

    @app.post("/api/history/watched")
    def api_history_watched():
        svc.history.mark_watched(_hist_item(_json_body()))
        return jsonify({"ok": True})

    @app.delete("/api/history")
    def api_history_remove():
        svc.history.remove(request.args.get("provider", ""), request.args.get("id", ""), _int_arg("season"), _int_arg("episode"))
        return jsonify({"ok": True})

    @app.delete("/api/history/all")
    def api_history_clear():
        svc.history.clear()
        return jsonify({"ok": True})

    # ------------------------------------------------------------------ TV
    def _sources_json() -> list[dict]:
        return [
            {"source": s, "kind": "url" if s.lower().startswith(("http://", "https://")) else "file", "label": s if s.lower().startswith("http") else Path(s).name}
            for s in svc.tv_sources.snapshot()
        ]

    @app.get("/api/tv/playlists")
    def api_tv_playlists():
        return jsonify({"sources": _sources_json(), "allow_local_files": svc.allow_local_files})

    @app.post("/api/tv/playlists")
    def api_tv_add():
        if request.files.get("file"):
            f = request.files["file"]
            try:
                source = svc.tv.save_upload(f.filename or "playlist.m3u", f.read())
            except ValueError as exc:
                return _err(str(exc), 400)
        else:
            source = str(_json_body().get("source", "")).strip()
            if not source:
                return _err("Enter a playlist URL or choose a file.", 400)
            if not source.lower().startswith(("http://", "https://")) and not svc.allow_local_files:
                return _err("Local file paths are disabled on this server. Upload the file instead.", 400)
        if not svc.tv_sources.add(source):
            return _err("That playlist is already added.", 409)
        svc.tv_invalidate()
        return jsonify({"sources": _sources_json()}), 201

    @app.delete("/api/tv/playlists")
    def api_tv_remove():
        source = request.args.get("source", "")
        if not svc.tv_sources.remove(source):
            return _err("Playlist not found.", 404)
        svc.tv.forget(source)
        svc.tv_invalidate()
        return jsonify({"sources": _sources_json()})

    @app.get("/api/tv/channels")
    def api_tv_channels():
        snap = svc.tv_snapshot(force=request.args.get("refresh") == "1")
        q = (request.args.get("q") or "").strip().lower()
        group = request.args.get("group") or ""
        matches = [
            c
            for c in snap["channels"]
            if (not group or (c.group or "Ungrouped") == group) and (not q or q in c.name.lower() or q in c.group.lower())
        ]
        limit = _int_arg("limit", 120, 1, TV_PAGE_MAX)
        offset = _int_arg("offset", 0, 0, 10_000_000)
        page = []
        for c in matches[offset : offset + limit]:
            hls = is_hls_url(c.stream_url)
            proxied = svc.proxy.url_for(c.stream_url, None, _url_name(c.stream_url))
            direct_ok = svc.config.get().get("proxy_mode") == "auto" and c.stream_url.lower().startswith("https://") and not hls
            page.append(
                {
                    "id": c.id,
                    "name": c.name,
                    "logo": c.logo if c.logo.lower().startswith(("http://", "https://")) else "",
                    "group": c.group or "Ungrouped",
                    "kind": "hls" if hls else "file",
                    "direct_url": c.stream_url,
                    "play_url": c.stream_url if direct_ok else proxied,
                    "proxy_url": proxied,
                }
            )
        return jsonify(
            {
                "channels": page,
                "total": len(matches),
                "all_total": len(snap["channels"]),
                "groups": snap["groups"],
                "failed": snap["failed"],
                "sources": len(snap["sources"]),
            }
        )

    # ------------------------------------------------------------------ maintenance
    @app.post("/api/cache/clear")
    def api_cache_clear():
        removed = svc.cache.clear()
        svc.tv_invalidate()
        return jsonify({"removed": removed})

    return app
