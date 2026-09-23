"""Streaming proxy (browser replacement for the terminal app's loopback sidecar).

A browser ``<video>`` element cannot set ``Referer`` / ``User-Agent`` / cookies,
and hls.js is bound by CORS. The proxy fetches upstream on the server with the
headers a stream requires, forwards ``Range`` so seeking works, and rewrites HLS
playlists so segments and keys flow through it too.

It only ever fetches URLs contained in a token this server signed, so it is not
an open proxy.
"""
from __future__ import annotations

import mimetypes
import re
from typing import Iterator
from urllib.parse import quote, unquote, urljoin, urlsplit

import requests
from flask import Request, Response, jsonify

from .security import HttpPolicy, TokenSigner, UnsafeURL

MAX_PLAYLIST_BYTES = 8 * 1024 * 1024
CHUNK = 64 * 1024
_FORWARD_REQUEST_HEADERS = ("Range", "If-Range", "If-None-Match", "If-Modified-Since")
_PASS_RESPONSE_HEADERS = ("Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag")
_EXT_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
    ".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t", ".m4s": "video/iso.segment",
    ".aac": "audio/aac", ".mp3": "audio/mpeg", ".vtt": "text/vtt",
}
_GENERIC_TYPES = ("application/octet-stream", "binary/octet-stream", "")


def is_hls_url(url: str) -> bool:
    return ".m3u8" in urlsplit(url).path.lower()


def _guess_type(url: str) -> str | None:
    path = urlsplit(url).path.lower()
    for ext, mime in _EXT_TYPES.items():
        if path.endswith(ext):
            return mime
    return mimetypes.guess_type(path)[0]


def _safe_filename(name: str, url: str) -> str:
    name = unquote(name or "").strip() or urlsplit(url).path.rsplit("/", 1)[-1] or "download"
    name = re.sub(r"[^\w .()\[\]-]", "_", name, flags=re.UNICODE).strip(" .")[:120] or "download"
    if "." not in name:
        ext = re.search(r"\.[A-Za-z0-9]{2,4}$", urlsplit(url).path)
        name += ext.group(0) if ext else ".mp4"
    return name


class StreamProxy:
    def __init__(self, http: HttpPolicy, signer: TokenSigner):
        self.http = http
        self.signer = signer

    # -- building proxy URLs
    def url_for(self, url: str, headers: list[tuple[str, str]] | None = None, name: str | None = None, ttl: int = 24 * 3600) -> str:
        payload: dict = {"u": url}
        if headers:
            payload["h"] = [[k, v] for k, v in headers]
        token = self.signer.sign(payload, ttl)
        return f"/proxy/{token}" + (f"/{quote(name, safe='')}" if name else "")

    # -- serving
    def handle(self, token: str, request: Request) -> Response:
        payload = self.signer.verify(token)
        if not payload or not isinstance(payload.get("u"), str):
            return _error("This playback link is invalid or has expired. Reload the title to get a fresh one.", 403)

        url: str = payload["u"]
        stream_headers = [(str(k), str(v)) for k, v in payload.get("h", []) if isinstance(k, str)]
        upstream_headers = {"Accept-Encoding": "identity"}
        if ua := request.headers.get("User-Agent"):
            upstream_headers["User-Agent"] = ua
        for name in _FORWARD_REQUEST_HEADERS:
            if value := request.headers.get(name):
                upstream_headers[name] = value
        for k, v in stream_headers:  # stream-required headers win over the browser's
            if k.lower() not in ("range", "host", "content-length"):
                upstream_headers[k] = v

        try:
            resp = self.http.request(url, headers=upstream_headers, stream=True, timeout=(10, 30))
        except UnsafeURL as exc:
            return _error(str(exc), 403)
        except requests.RequestException as exc:
            return _error(f"Could not reach the stream host ({exc.__class__.__name__}).", 502)

        ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if resp.status_code < 400 and ("mpegurl" in ctype or (is_hls_url(resp.url) and ctype in _GENERIC_TYPES + ("text/plain",))):
            return self._serve_playlist(resp, stream_headers)

        headers: dict[str, str] = {k: resp.headers[k] for k in _PASS_RESPONSE_HEADERS if k in resp.headers}
        if ctype in _GENERIC_TYPES:
            guessed = _guess_type(resp.url)
            if guessed:
                ctype = guessed
        if ctype:
            headers["Content-Type"] = ctype
        headers["Cache-Control"] = "private, no-store" if resp.status_code >= 400 else "private, max-age=3600"
        if request.args.get("download"):
            fname = _safe_filename(request.args.get("name", ""), resp.url)
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(fname)}"
        if resp.status_code >= 400:
            headers.pop("Content-Length", None)
        return Response(_iterate(resp), status=resp.status_code, headers=headers)

    def _serve_playlist(self, resp: requests.Response, stream_headers: list[tuple[str, str]]) -> Response:
        try:
            raw = bytearray()
            for chunk in resp.iter_content(CHUNK):
                raw.extend(chunk)
                if len(raw) > MAX_PLAYLIST_BYTES:
                    return _error("Playlist is too large to proxy.", 502)
            base = resp.url
        finally:
            resp.close()
        text = raw.decode("utf-8", errors="replace")
        rewritten = rewrite_hls(text, base, lambda u: self.url_for(u, stream_headers))
        return Response(
            rewritten,
            status=200,
            headers={"Content-Type": "application/vnd.apple.mpegurl", "Cache-Control": "no-store"},
        )


def _iterate(resp: requests.Response) -> Iterator[bytes]:
    try:
        for chunk in resp.iter_content(CHUNK):
            if chunk:
                yield chunk
    finally:
        resp.close()


def _error(message: str, status: int) -> Response:
    r = jsonify({"error": message})
    r.status_code = status
    return r


_URI_ATTR = re.compile(r'URI="([^"]+)"')


def rewrite_hls(text: str, base_url: str, wrap) -> str:
    """Point every URI in an HLS playlist (segments, variants, keys, maps) at the proxy."""
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            if "URI=" in stripped:
                line = _URI_ATTR.sub(lambda m: m.group(0) if m.group(1).startswith("data:") else f'URI="{wrap(urljoin(base_url, m.group(1)))}"', line)
            out.append(line)
        else:
            out.append(wrap(urljoin(base_url, stripped)))
    return "\n".join(out) + "\n"
