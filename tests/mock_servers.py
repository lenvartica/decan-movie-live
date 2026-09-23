"""A local stand-in for a Stremio addon and a media host, so tests need no network.

Serves the addon protocol (manifest, catalogs, meta, streams), a byte-range
capable media file, a header-gated media file, an HLS playlist and an M3U
playlist. Requests are recorded in ``self.requests`` for assertions.
"""
from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

HERE = os.path.dirname(__file__)
SAMPLE = os.path.join(HERE, "fixtures", "sample.webm")

MOVIES = [
    {"id": "tt0000001", "type": "movie", "name": "Blade Runner", "releaseInfo": "1982", "poster": "https://img.test/br.jpg", "imdbRating": "8.1"},
    {"id": "tt0000002", "type": "movie", "name": "Blade Runner 2049", "releaseInfo": 2017, "poster": "https://img.test/br49.jpg"},
]
SERIES = [
    {"id": "tt0000003", "type": "series", "name": "Severance", "releaseInfo": "2022–", "poster": "https://img.test/sev.jpg"},
]


def meta_for(mid: str) -> dict | None:
    if mid == "tt0000001":
        return {**MOVIES[0], "description": "A blade runner must pursue replicants.", "runtime": "117 min", "genres": ["Sci-Fi", "Thriller"],
                "cast": ["Harrison Ford", "Rutger Hauer"], "director": ["Ridley Scott"], "background": "https://img.test/br-bg.jpg"}
    if mid == "tt0000002":
        return {**MOVIES[1], "description": "Sequel.", "runtime": "2h 44min", "genres": "Sci-Fi, Drama"}
    if mid == "tt0000003":
        return {
            **SERIES[0], "description": "Office drama.", "runtime": "50 min", "genres": ["Drama"],
            "videos": [
                {"id": "tt0000003:0:1", "season": 0, "episode": 1, "title": "Special"},
                {"id": "tt0000003:1:1", "season": 1, "episode": 1, "title": "Good News About Hell", "overview": "Pilot.", "thumbnail": "https://img.test/e1.jpg"},
                {"id": "tt0000003:1:2", "season": 1, "episode": 2, "title": "Half Loop"},
                {"id": "tt0000003:2:1", "season": "2", "episode": "1", "name": "Hello, Ms. Cobel"},
            ],
        }
    return None


class MockHost(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.requests: list[dict] = []
        self.stream_calls: list[str] = []
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def start(self) -> "MockHost":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class Handler(BaseHTTPRequestHandler):
    server: MockHost

    def log_message(self, *args):  # silence
        pass

    def _send(self, status: int, body: bytes, ctype: str = "application/json", extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj).encode())

    def _media_bytes(self) -> bytes:
        if os.path.exists(SAMPLE):
            with open(SAMPLE, "rb") as fh:
                return fh.read()
        return bytes(range(256)) * 4096  # 1 MiB of predictable bytes

    def _serve_ranged(self, data: bytes, ctype: str):
        rng = self.headers.get("Range")
        if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
            start = int(m.group(1) or 0)
            end = int(m.group(2)) if m.group(2) else len(data) - 1
            end = min(end, len(data) - 1)
            if start >= len(data):
                return self._send(416, b"", ctype, {"Content-Range": f"bytes */{len(data)}"})
            chunk = data[start : end + 1]
            return self._send(206, chunk, ctype, {"Content-Range": f"bytes {start}-{end}/{len(data)}", "Accept-Ranges": "bytes"})
        return self._send(200, data, ctype, {"Accept-Ranges": "bytes"})

    do_HEAD = lambda self: self.do_GET()  # noqa: E731

    def do_GET(self):  # noqa: N802
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        self.server.requests.append({"path": path, "headers": {k.lower(): v for k, v in self.headers.items()}})
        base = self.server.base

        # ---- addon protocol (also mounted under /cinemeta/ to act as the core addon)
        for prefix in ("", "/cinemeta"):
            if not path.startswith(prefix):
                continue
            rel = path[len(prefix):]
            if rel == "/manifest.json":
                name = "Cinemeta" if prefix else "MockStreams"
                res = ["catalog", "meta"] if prefix else ["stream"]
                return self._json({"id": f"org.mock.{name.lower()}", "name": name, "version": "1.0.0", "description": "mock", "resources": res, "types": ["movie", "series"],
                                   "catalogs": [{"type": "movie", "id": "top"}] if prefix else [], "idPrefixes": ["tt"]})
            if m := re.fullmatch(r"/catalog/(movie|series)/(top|imdbRating)(?:/search=(.*))?\.json", rel):
                kind, cat, query = m.groups()
                pool = MOVIES if kind == "movie" else SERIES
                if query is not None:
                    q = unquote(query).lower()
                    pool = [x for x in pool if q in x["name"].lower()]
                    # duplicate on purpose to test de-duplication
                    pool = pool + pool[:1]
                return self._json({"metas": pool})
            if m := re.fullmatch(r"/meta/(\w+)/(.+)\.json", rel):
                meta = meta_for(m.group(2))
                if not meta:
                    return self._json({}, 404)
                return self._json({"meta": meta})
            if m := re.fullmatch(r"/stream/(movie|series)/(.+)\.json", rel):
                self.server.stream_calls.append(m.group(2))
                return self._json({"streams": self._streams(m.group(1), m.group(2), base)})

        # ---- media host
        if path == "/media/sample.webm":
            return self._serve_ranged(self._media_bytes(), "video/webm")
        if path == "/media/octet.mp4":
            return self._serve_ranged(self._media_bytes(), "application/octet-stream")
        if path == "/media/needs-referer.webm":
            if self.headers.get("Referer") != "https://allowed.example/":
                return self._send(403, b"forbidden", "text/plain")
            return self._serve_ranged(self._media_bytes(), "video/webm")
        if path == "/media/gone.webm":
            return self._send(404, b"nope", "text/plain")
        if path == "/hls/live.m3u8":
            body = (
                "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:0\n"
                '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
                "#EXTINF:4.0,\nseg0.ts\n#EXTINF:4.0,\n/hls/seg1.ts\n#EXT-X-ENDLIST\n"
            )
            return self._send(200, body.encode(), "application/vnd.apple.mpegurl")
        if path == "/hls/master.m3u8":
            return self._send(200, b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nlive.m3u8\n", "application/x-mpegURL")
        if path in ("/hls/seg0.ts", "/hls/seg1.ts", "/hls/key.bin"):
            return self._send(200, b"\x47" + b"\x00" * 187, "video/mp2t")
        if path == "/redirect-to-media":
            return self._send(302, b"", "text/plain", {"Location": "/media/sample.webm"})
        if path == "/playlist.m3u":
            body = (
                "#EXTM3U\n"
                '#EXTINF:-1 tvg-id="news1" tvg-logo="https://img.test/n1.png" group-title="News, World",News One\n'
                f"{base}/hls/live.m3u8\n"
                '#EXTINF:-1 tvg-id="sport1" group-title="Sports",Sports One\n'
                "https://cdn.example/sports1.m3u8\n"
                '#EXTINF:-1 group-title="News, World",Duplicate of News One\n'
                f"{base}/hls/live.m3u8\n"
                "#EXTINF:-1,No Group Channel\n"
                "https://cdn.example/nogroup.mp4\n"
            )
            return self._send(200, body.encode(), "audio/x-mpegurl")
        return self._json({"error": "nope"}, 404)

    def _streams(self, kind: str, ident: str, base: str) -> list[dict]:
        if kind == "movie" and ident == "tt0000001":
            return [
                {"name": "MockStreams\n1080p", "title": "Blade.Runner.1982.1080p.BluRay.x264.English 8.4 GB", "url": f"{base}/media/sample.webm"},
                {"name": "MockStreams\n4K", "title": "Blade.Runner.1982.2160p.UHD.HEVC.HDR.Hindi.English", "url": f"{base}/media/sample.webm?q=4k",
                 "behaviorHints": {"videoSize": 13314398617, "notWebReady": True}},
                {"name": "Gated\n720p", "title": "Blade Runner 720p", "url": f"{base}/media/needs-referer.webm",
                 "behaviorHints": {"headers": {"Referer": "https://allowed.example/", "X-Secret": "dropme", "User-Agent": "MockUA/1.0"}}},
                {"name": "Torrent", "title": "Blade Runner torrent", "infoHash": "0123456789abcdef"},
                {"name": "HLS", "title": "Blade Runner live 480p", "url": f"{base}/hls/live.m3u8"},
                {"name": "Dup", "title": "Same 1080p as first", "url": f"{base}/media/sample.webm"},
            ]
        if kind == "series" and ident == "tt0000003:1:2":
            return [
                {"name": "MockStreams", "title": "Severance.S01E02.1080p.WEB-DL", "url": f"{base}/media/sample.webm?e=2"},
                {"name": "MockStreams", "title": "Severance.S01E03.1080p.WEB-DL", "url": f"{base}/media/sample.webm?e=3"},
                {"name": "MockStreams", "title": "Severance 1x02 720p", "url": f"{base}/media/sample.webm?e=2b"},
            ]
        if kind == "movie" and ident == "tt0000002":
            return [{"name": "Torrent", "title": "only torrents", "infoHash": "abc"}]
        return []
