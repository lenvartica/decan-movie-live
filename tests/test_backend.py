import base64
import io
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))

from mock_servers import MockHost  # noqa: E402

from moviebox_web import models  # noqa: E402
from moviebox_web.app import create_app  # noqa: E402
from moviebox_web.providers import addons as A  # noqa: E402
from moviebox_web.providers.tv import parse_m3u  # noqa: E402
from moviebox_web.security import TokenSigner, UnsafeURL, check_url  # noqa: E402
from moviebox_web.store import FavoritesManager, HistoryManager, identity_matches, is_in_progress  # noqa: E402


def make_home(base: str, with_streams: bool = True) -> str:
    """A fresh config dir whose core addon points at the mock server."""
    home = tempfile.mkdtemp()
    cfg = Path(home) / "config"
    cfg.mkdir(parents=True)
    addons = [{"manifest_url": f"{base}/cinemeta/manifest.json", "name": "Cinemeta", "enabled": True, "provides_catalog": True, "provides_meta": True, "types": ["movie", "series"]}]
    if with_streams:
        addons.append({"manifest_url": f"{base}/manifest.json", "name": "MockStreams", "enabled": True, "provides_stream": True})
    (cfg / "addons_config.json").write_text(json.dumps(addons))
    return home


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = MockHost().start()
        cls.base = cls.mock.base

    @classmethod
    def tearDownClass(cls):
        cls.mock.stop()

    def new_app(self, **kw):
        app = create_app(make_home(self.base), plugin_dirs=[], **kw)
        return app, app.test_client(), app.extensions["moviebox"]


# --------------------------------------------------------------------------- text + heuristics

class TextHelpers(unittest.TestCase):
    def test_clean_title(self):
        self.assertEqual(models.clean_title("[Hindi] Movie Name (2021) [1080p]"), "Movie Name (2021)")
        self.assertEqual(models.clean_title("Show Name S01"), "Show Name")
        self.assertEqual(models.clean_title("Movie - Hindi Dubbed"), "Movie")
        self.assertEqual(models.clean_title("Dune: Part Two"), "Dune: Part Two")
        self.assertEqual(models.clean_title("Movie (Extended Cut)"), "Movie")
        self.assertEqual(models.clean_title(""), "")

    def test_year_and_duration(self):
        self.assertEqual(models.extract_4digit_year("2019–2022"), "2019")
        self.assertEqual(models.extract_4digit_year("no year"), "")
        self.assertEqual(models.parse_duration_seconds("1:32:10"), 5530)
        self.assertEqual(models.parse_duration_seconds("2h 5m"), 7500)
        self.assertEqual(models.parse_duration_seconds("117 min"), 7020)
        self.assertEqual(models.parse_duration_seconds("2h28min"), 8880)
        self.assertIsNone(models.parse_duration_seconds("n/a"))
        self.assertEqual(models.format_duration(3725), "1:02:05")

    def test_clean_stream_text(self):
        self.assertEqual(models.clean_stream_text("  🔥 Hello   World "), "Hello World")


class Heuristics(unittest.TestCase):
    def test_quality(self):
        self.assertEqual(A.parse_quality("Movie 2160p HDR"), "2160p")
        self.assertEqual(A.parse_quality("4K UHD"), "2160p")
        self.assertEqual(A.parse_quality("Full HD"), "1080p")
        self.assertEqual(A.parse_quality("Movie HD"), "720p")
        self.assertEqual(A.parse_quality("480p"), "480p")
        self.assertIsNone(A.parse_quality("nothing"))

    def test_codec_size_audio(self):
        self.assertEqual(A.parse_codec("x265 10bit"), "HEVC/x265")
        self.assertEqual(A.parse_codec("H.264"), "AVC/x264")
        self.assertEqual(A.parse_codec("AV1"), "AV1")
        self.assertEqual(A.parse_size_bytes("File 2.5 GB here"), int(2.5 * 1073741824))
        self.assertEqual(A.parse_size_bytes("700MB"), 700 * 1048576)
        self.assertIsNone(A.parse_size_bytes("no size 1080p"))
        self.assertEqual(A.parse_audio_tracks("Hindi English DUAL"), "Hindi + English + Dual Audio")
        self.assertIsNone(A.parse_audio_tracks("ENGINE room"))  # short codes need letter boundaries

    def test_season_episode(self):
        self.assertEqual(A.parse_season_episode("Show.S01E02.1080p"), (1, 2))
        self.assertEqual(A.parse_season_episode("show s2 e10"), (2, 10))
        self.assertEqual(A.parse_season_episode("Show 3x07"), (3, 7))
        self.assertEqual(A.parse_season_episode("Season 4 Episode 9"), (4, 9))
        self.assertEqual(A.parse_season_episode("Episode 5"), (1, 5))
        self.assertIsNone(A.parse_season_episode("Movie 1080p"))
        self.assertIsNone(A.parse_season_episode("1920x1080"))

    def test_url_helpers(self):
        n = A.AddonClient.normalize_manifest_url
        self.assertEqual(n("stremio://example.com/x"), "https://example.com/x/manifest.json")
        self.assertEqual(n("example.com"), "https://example.com/manifest.json")
        self.assertEqual(n("https://a.b/manifest.json"), "https://a.b/manifest.json")
        self.assertEqual(A.AddonClient.base_addon_url("https://a.b/c/manifest.json"), "https://a.b/c")

    def test_host_label(self):
        self.assertEqual(A.detect_stream_host("Addon", "Addon\n4K", "https://cdn.fast-host.com/x.mp4"), "Addon · 4K")
        self.assertEqual(A.detect_stream_host("Addon", "Addon", "https://cdn.fast-host.com/x.mp4"), "Addon · Fast Host")


# --------------------------------------------------------------------------- catalog API

class CatalogApi(Base):
    def setUp(self):
        self.app, self.c, self.svc = self.new_app()

    def test_bootstrap(self):
        d = self.c.get("/api/bootstrap").get_json()
        self.assertEqual(len(d["themes"]), 9)
        self.assertEqual(d["providers"][0]["id"], "addons")
        self.assertTrue(d["providers"][0]["supports_homepage"])

    def test_index_embeds_theme_and_boot(self):
        html = self.c.get("/").get_data(as_text=True)
        self.assertIn('id="theme-vars"', html)
        self.assertIn('id="boot"', html)
        self.assertIn("--c-base:#1e1e2e", html)

    def test_search_dedupes_and_maps(self):
        r = self.c.get("/api/search?q=blade").get_json()
        ids = [x["id"] for x in r["results"]]
        self.assertEqual(ids, ["movie:tt0000001", "movie:tt0000002"])
        self.assertEqual(r["results"][1]["year"], "2017")  # numeric releaseInfo coerced
        s = self.c.get("/api/search?q=sever").get_json()["results"]
        self.assertEqual((s[0]["id"], s[0]["media_type"], s[0]["stype"], s[0]["year"]), ("series:tt0000003", "series", 2, "2022"))

    def test_search_validation(self):
        self.assertEqual(self.c.get("/api/search?q=").status_code, 400)
        self.assertEqual(self.c.get("/api/search?q=" + "x" * 300).status_code, 400)

    def test_browse_shelves(self):
        d = self.c.get("/api/browse").get_json()
        labels = [s["label"] for s in d["shelves"]]
        self.assertEqual(labels, ["Top Movies", "Top Series", "Top Rated Movies", "Top Rated Series"])
        self.assertEqual(len(d["shelves"][0]["items"]), 2)

    def test_details_movie(self):
        d = self.c.get("/api/details?id=movie:tt0000001").get_json()["details"]
        self.assertEqual(d["title"], "Blade Runner")
        self.assertEqual(d["id"], "movie:tt0000001")
        self.assertEqual(d["genres"], ["Sci-Fi", "Thriller"])
        self.assertEqual(d["director"], "Ridley Scott")
        self.assertEqual(d["stars"], "Harrison Ford, Rutger Hauer")
        self.assertEqual(d["duration_seconds"], 7020)
        self.assertEqual(d["background_url"], "https://img.test/br-bg.jpg")
        d2 = self.c.get("/api/details?id=movie:tt0000002").get_json()["details"]
        self.assertEqual(d2["genres"], ["Sci-Fi", "Drama"])  # comma string coerced
        self.assertEqual(d2["duration_seconds"], 2 * 3600 + 44 * 60)

    def test_details_series_seasons(self):
        d = self.c.get("/api/details?id=series:tt0000003").get_json()["details"]
        self.assertEqual(d["media_type"], "series")
        self.assertEqual([s["number"] for s in d["seasons"]], [0, 1, 2])
        self.assertEqual(d["seasons"][1]["episodes"][0]["title"], "Good News About Hell")
        self.assertEqual(d["seasons"][2]["episodes"][0]["title"], "Hello, Ms. Cobel")  # string season/episode + `name`

    def test_details_without_type_prefix_and_missing(self):
        d = self.c.get("/api/details?id=tt0000003").get_json()["details"]
        self.assertEqual(d["media_type"], "series")
        r = self.c.get("/api/details?id=tt9999999")
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.get_json())

    def test_unknown_provider(self):
        self.assertEqual(self.c.get("/api/search?q=x&provider=nope").status_code, 404)


# --------------------------------------------------------------------------- streams

class StreamsApi(Base):
    def setUp(self):
        self.app, self.c, self.svc = self.new_app()

    def test_movie_streams_ranked_and_parsed(self):
        d = self.c.get("/api/streams?id=movie:tt0000001").get_json()
        rel = d["releases"]
        self.assertEqual(rel[0]["quality"], "2160p")
        self.assertEqual(rel[0]["codec"], "HEVC/x265")
        self.assertEqual(rel[0]["language"], "Hindi + English")
        self.assertEqual(rel[0]["size_bytes"], 13314398617)
        self.assertFalse(rel[0]["mirrors"][0]["web_ready"])
        qualities = [r["quality"] for r in rel]
        self.assertEqual(qualities, sorted(qualities, key=lambda q: -{"2160p": 4, "1080p": 3, "720p": 2, "480p": 1}.get(q or "", 0)))
        # torrent-only entry dropped; duplicate URL merged into one release with the 1080p one
        self.assertEqual(len(rel), 4)
        first_1080 = next(r for r in rel if r["quality"] == "1080p")
        self.assertEqual(len(first_1080["mirrors"]), 2)

    def test_header_allowlist_and_proxying(self):
        rel = self.c.get("/api/streams?id=movie:tt0000001").get_json()["releases"]
        gated = next(r for r in rel if "needs-referer" in r["mirrors"][0]["direct_url"])
        m = gated["mirrors"][0]
        self.assertEqual(m["headers"], {"Referer": "https://allowed.example/", "User-Agent": "MockUA/1.0"})  # X-Secret dropped
        self.assertTrue(m["play_url"].startswith("/proxy/"))
        self.assertIn("download=1", m["download_url"])
        hls = next(r for r in rel if r["mirrors"][0]["kind"] == "hls")
        self.assertTrue(hls["mirrors"][0]["play_url"].startswith("/proxy/"))

    def test_episode_isolation(self):
        d = self.c.get("/api/streams?id=series:tt0000003&season=1&episode=2").get_json()
        self.assertIn("tt0000003:1:2", self.mock.stream_calls)
        titles = [r["filename"] for r in d["releases"]]
        self.assertEqual(len(titles), 2)
        self.assertTrue(all("S01E03" not in t for t in titles))

    def test_blocked_addons_reported(self):
        d = self.c.get("/api/streams?id=movie:tt0000002").get_json()
        self.assertEqual(d["releases"], [])
        self.assertEqual(d["blocked"], ["MockStreams"])

    def test_empty_stream_responses_not_cached_but_results_are(self):
        self.c.get("/api/streams?id=movie:tt0000001")
        n = len(self.mock.stream_calls)
        self.c.get("/api/streams?id=movie:tt0000001")
        self.assertEqual(len(self.mock.stream_calls), n)  # cached
        self.c.get("/api/streams?id=movie:tt7777777")
        n = len(self.mock.stream_calls)
        self.c.get("/api/streams?id=movie:tt7777777")
        self.assertEqual(len(self.mock.stream_calls), n + 1)  # empty answers re-fetched

    def test_proxy_mode_always_vs_auto(self):
        class R:  # https mirror without headers
            pass

        from moviebox_web.models import SourceMirror

        m = SourceMirror(label="x", resolver_url="https://cdn.example/a.mp4")
        self.assertEqual(self.svc.mirror_json(m)["play_url"], "https://cdn.example/a.mp4")  # direct in auto
        self.c.put("/api/config", json={"proxy_mode": "always"})
        self.assertTrue(self.svc.mirror_json(m)["play_url"].startswith("/proxy/"))


# --------------------------------------------------------------------------- addons management

class AddonsApi(Base):
    def setUp(self):
        self.app, self.c, self.svc = self.new_app()

    def test_core_protected(self):
        core = f"{self.base}/cinemeta/manifest.json"
        self.assertEqual(self.c.patch("/api/addons", json={"manifest_url": core, "enabled": False}).status_code, 403)
        self.assertEqual(self.c.delete("/api/addons", query_string={"manifest_url": core}).status_code, 403)

    def test_toggle_remove_reinstall(self):
        url = f"{self.base}/manifest.json"
        r = self.c.patch("/api/addons", json={"manifest_url": url, "enabled": False}).get_json()
        self.assertFalse(next(a for a in r["addons"] if a["name"] == "MockStreams")["enabled"])
        self.assertEqual(self.c.get("/api/streams?id=movie:tt0000001").get_json()["releases"], [])
        self.assertEqual(self.c.delete("/api/addons", query_string={"manifest_url": url}).status_code, 200)
        r = self.c.post("/api/addons", json={"manifest_url": self.base})  # bare base URL; /manifest.json is appended
        self.assertEqual(r.status_code, 201)
        self.assertTrue(r.get_json()["addon"]["provides_stream"])
        self.assertEqual(self.c.post("/api/addons", json={"manifest_url": url}).status_code, 409)

    def test_install_failures(self):
        r = self.c.post("/api/addons", json={"manifest_url": "http://127.0.0.1:9/"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.c.post("/api/addons", json={"manifest_url": ""}).status_code, 400)
        self.assertEqual(self.c.patch("/api/addons", json={"manifest_url": "https://nope/", "enabled": True}).status_code, 404)

    def test_persisted_in_tui_compatible_shape(self):
        raw = json.loads((Path(self.svc.paths.config) / "addons_config.json").read_text())
        self.assertIsInstance(raw, list)
        self.assertEqual({"manifest_url", "name", "enabled", "provides_stream"} <= set(raw[1]), True)


# --------------------------------------------------------------------------- proxy

class ProxyApi(Base):
    def setUp(self):
        self.app, self.c, self.svc = self.new_app()

    def url(self, path: str, headers=None, name=None) -> str:
        return self.svc.proxy.url_for(f"{self.base}{path}", headers, name)

    def test_full_and_range(self):
        u = self.url("/media/sample.webm", name="sample.webm")
        full = self.c.get(u)
        self.assertEqual(full.status_code, 200)
        total = len(full.data)
        self.assertEqual(full.headers["Content-Type"], "video/webm")
        part = self.c.get(u, headers={"Range": "bytes=10-109"})
        self.assertEqual(part.status_code, 206)
        self.assertEqual(len(part.data), 100)
        self.assertEqual(part.headers["Content-Range"], f"bytes 10-109/{total}")
        self.assertEqual(part.data, full.data[10:110])
        self.assertEqual(self.c.get(u, headers={"Range": f"bytes={total + 5}-"}).status_code, 416)

    def test_required_headers_injected_server_side(self):
        bare = self.c.get(self.url("/media/needs-referer.webm"))
        self.assertEqual(bare.status_code, 403)
        ok = self.c.get(self.url("/media/needs-referer.webm", [("Referer", "https://allowed.example/")]))
        self.assertEqual(ok.status_code, 200)
        last = [r for r in self.mock.requests if r["path"] == "/media/needs-referer.webm"][-1]
        self.assertEqual(last["headers"]["referer"], "https://allowed.example/")
        self.assertEqual(last["headers"]["accept-encoding"], "identity")

    def test_stream_headers_win_over_browser_ua(self):
        self.c.get(self.url("/media/sample.webm", [("User-Agent", "MockUA/1.0")]), headers={"User-Agent": "Browser/9"})
        last = [r for r in self.mock.requests if r["path"] == "/media/sample.webm"][-1]
        self.assertEqual(last["headers"]["user-agent"], "MockUA/1.0")

    def test_octet_stream_gets_video_type_from_extension(self):
        r = self.c.get(self.url("/media/octet.mp4"))
        self.assertEqual(r.headers["Content-Type"], "video/mp4")

    def test_download_disposition(self):
        r = self.c.get(self.url("/media/sample.webm") + "?download=1&name=My%20Movie:2024.webm")
        self.assertEqual(r.status_code, 200)
        cd = r.headers["Content-Disposition"]
        self.assertTrue(cd.startswith("attachment;"))
        self.assertNotIn(":", cd.split("''")[1])

    def test_upstream_errors_pass_status(self):
        self.assertEqual(self.c.get(self.url("/media/gone.webm")).status_code, 404)

    def test_redirects_followed(self):
        r = self.c.get(self.url("/redirect-to-media"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["Content-Type"], "video/webm")

    def test_token_tamper_and_expiry(self):
        u = self.url("/media/sample.webm")
        token = u.split("/")[2]
        bad = token[:-3] + ("AAA" if not token.endswith("AAA") else "BBB")
        self.assertEqual(self.c.get(f"/proxy/{bad}").status_code, 403)
        self.assertEqual(self.c.get("/proxy/garbage").status_code, 403)
        expired = self.svc.signer.sign({"u": f"{self.base}/media/sample.webm"}, ttl=-5)
        self.assertEqual(self.c.get(f"/proxy/{expired}").status_code, 403)
        other = TokenSigner(b"x" * 32).sign({"u": f"{self.base}/media/sample.webm"})
        self.assertEqual(self.c.get(f"/proxy/{other}").status_code, 403)

    def test_hls_playlist_rewritten(self):
        body = self.c.get(self.url("/hls/live.m3u8")).get_data(as_text=True)
        lines = [l for l in body.splitlines() if l and not l.startswith("#")]
        self.assertTrue(lines and all(l.startswith("/proxy/") for l in lines), body)
        self.assertIn('URI="/proxy/', body)  # key line rewritten
        self.assertNotIn("seg0.ts", body)
        seg = self.c.get(lines[0])
        self.assertEqual(seg.status_code, 200)
        self.assertEqual(seg.headers["Content-Type"], "video/mp2t")

    def test_hls_master_content_type_variant(self):
        r = self.c.get(self.url("/hls/master.m3u8"))
        self.assertEqual(r.headers["Content-Type"], "application/vnd.apple.mpegurl")
        variant = [l for l in r.get_data(as_text=True).splitlines() if l.startswith("/proxy/")][0]
        inner = self.c.get(variant).get_data(as_text=True)
        self.assertIn("#EXTINF", inner)
        self.assertIn("/proxy/", inner)

    def test_nested_proxy_url_carries_headers(self):
        u = self.url("/hls/live.m3u8", [("Referer", "https://r.example/")])
        seg = [l for l in self.c.get(u).get_data(as_text=True).splitlines() if l.startswith("/proxy/")][0]
        payload = self.svc.signer.verify(seg.split("/")[2])
        self.assertEqual(payload["h"], [["Referer", "https://r.example/"]])


# --------------------------------------------------------------------------- security

class Security(Base):
    def test_ssrf_policy(self):
        for bad in ("http://127.0.0.1/x", "http://localhost/x", "http://169.254.169.254/latest", "http://10.0.0.5/", "http://[::1]/", "http://192.168.1.1/", "file:///etc/passwd", "ftp://example.com/x", "http://[::ffff:127.0.0.1]/"):
            with self.assertRaises(UnsafeURL, msg=bad):
                check_url(bad, allow_private=False)
        check_url("http://127.0.0.1/x", allow_private=True)
        check_url("https://1.1.1.1/", allow_private=False)

    def test_proxy_blocked_for_private_in_hosted_mode(self):
        app = create_app(make_home(self.base), host="0.0.0.0", plugin_dirs=[])
        c, svc = app.test_client(), app.extensions["moviebox"]
        self.assertFalse(svc.http.allow_private)
        self.assertEqual(c.get(svc.proxy.url_for(f"{self.base}/media/sample.webm")).status_code, 403)
        self.assertEqual(c.get(svc.proxy.url_for("http://169.254.169.254/latest/meta-data")).status_code, 403)
        # adding a private addon URL is refused too
        r = c.post("/api/addons", json={"manifest_url": f"{self.base}/manifest.json"})
        self.assertEqual(r.status_code, 400)

    def test_host_header_rejected_on_loopback(self):
        _, c, _ = self.new_app()
        self.assertEqual(c.get("/api/bootstrap", headers={"Host": "evil.example"}).status_code, 400)
        self.assertEqual(c.get("/api/bootstrap", headers={"Host": "localhost:8787"}).status_code, 200)
        self.assertEqual(c.get("/api/bootstrap", headers={"Host": "127.0.0.1:8787"}).status_code, 200)

    def test_cross_origin_writes_blocked(self):
        _, c, _ = self.new_app()
        r = c.post("/api/favorites/toggle", json={"provider": "addons", "subject_id": "x", "title": "T"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        r = c.post("/api/favorites/toggle", json={"provider": "addons", "subject_id": "x", "title": "T"}, headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 200)
        r = c.post("/api/favorites/toggle", json={"provider": "addons", "subject_id": "y", "title": "T"}, headers={"Origin": "null"})
        self.assertEqual(r.status_code, 403)

    def test_password(self):
        app = create_app(make_home(self.base), password="s3cret", plugin_dirs=[])
        c = app.test_client()
        self.assertEqual(c.get("/api/bootstrap").status_code, 401)
        self.assertIn("Basic", c.get("/").headers["WWW-Authenticate"])
        good = "Basic " + base64.b64encode(b"me:s3cret").decode()
        bad = "Basic " + base64.b64encode(b"me:wrong").decode()
        self.assertEqual(c.get("/api/bootstrap", headers={"Authorization": good}).status_code, 200)
        self.assertEqual(c.get("/api/bootstrap", headers={"Authorization": bad}).status_code, 401)

    def test_security_headers(self):
        _, c, _ = self.new_app()
        h = c.get("/").headers
        self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")

    def test_secret_persists_so_links_survive_restart(self):
        home = make_home(self.base)
        a1 = create_app(home, plugin_dirs=[])
        tok = a1.extensions["moviebox"].proxy.url_for(f"{self.base}/media/sample.webm")
        a2 = create_app(home, plugin_dirs=[])
        self.assertEqual(a2.test_client().get(tok).status_code, 200)

    def test_config_validation(self):
        _, c, _ = self.new_app()
        self.assertEqual(c.put("/api/config", json={"active_theme": "Nope"}).status_code, 400)
        r = c.put("/api/config", json={"active_theme": "Nord", "bogus": 1, "proxy_mode": "always"}).get_json()
        self.assertEqual((r["active_theme"], r["proxy_mode"]), ("Nord", "always"))
        self.assertNotIn("bogus", r)
        self.assertEqual(c.get("/api/bootstrap").get_json()["config"]["active_theme"], "Nord")
        self.assertIn("--c-base:#2e3440", c.get("/").get_data(as_text=True))


# --------------------------------------------------------------------------- favorites + history

class Library(Base):
    def setUp(self):
        self.app, self.c, self.svc = self.new_app()

    fav = {"provider": "addons", "subject_id": "movie:tt1", "title": "Some Movie", "stype": 1, "release_year": "2020", "cover_url": "https://i.test/p.jpg"}

    def test_favorites_toggle_and_persist(self):
        self.assertTrue(self.c.post("/api/favorites/toggle", json=self.fav).get_json()["favorite"])
        self.assertEqual(len(self.c.get("/api/favorites").get_json()["items"]), 1)
        home = self.svc.paths.data / "favorites.json"
        self.assertEqual(len(FavoritesManager(home).items), 1)
        self.assertFalse(self.c.post("/api/favorites/toggle", json=self.fav).get_json()["favorite"])
        self.c.post("/api/favorites/toggle", json=self.fav)
        self.c.delete("/api/favorites")
        self.assertEqual(self.c.get("/api/favorites").get_json()["items"], [])

    def test_favorite_flag_in_details(self):
        ident = {"provider": "addons", "subject_id": "movie:tt0000001", "title": "Blade Runner", "stype": 1, "release_year": "1982"}
        self.c.post("/api/favorites/toggle", json=ident)
        self.assertTrue(self.c.get("/api/details?id=movie:tt0000001").get_json()["favorite"])
        self.assertFalse(self.c.get("/api/details?id=movie:tt0000002").get_json()["favorite"])

    def test_identity_matching_rules(self):
        a = {"provider": "addons", "subject_id": "1", "title": "X", "stype": 1}
        self.assertTrue(identity_matches(a, {**a, "title": "Y"}))  # same id wins
        self.assertFalse(identity_matches(a, {**a, "subject_id": "2"}))
        self.assertFalse(identity_matches(a, {**a, "stype": 2}))
        self.assertFalse(identity_matches(a, {**a, "provider": "other"}))
        no_id_a = {"provider": "addons", "subject_id": "", "title": "[Hindi] Movie (2020)", "stype": 1, "release_year": "2020"}
        no_id_b = {"provider": "Addon", "subject_id": "", "title": "Movie (2020) [1080p]", "stype": 1, "release_year": "2020"}
        self.assertTrue(identity_matches(no_id_a, no_id_b))
        self.assertFalse(identity_matches(no_id_a, {**no_id_b, "release_year": "1999"}))

    def item(self, **kw):
        base = {"provider": "addons", "subject_id": "series:tt0000003", "title": "Severance", "stype": 2, "release_year": "2022", "season": 1, "episode": 2, "cover_url": "https://i.test/s.jpg"}
        return {**base, **kw}

    def post_progress(self, progress, duration=3000, **kw):
        return self.c.post("/api/history/progress", json={"item": self.item(**kw), "progress": progress, "duration": duration})

    def test_continue_watching_rules(self):
        self.post_progress(10)
        self.assertEqual(self.c.get("/api/history").get_json()["continue"], [])  # under 30s
        self.post_progress(1200)
        h = self.c.get("/api/history").get_json()
        self.assertEqual(len(h["continue"]), 1)
        self.assertEqual(h["continue"][0]["progress_seconds"], 1200)
        r = self.post_progress(2800).get_json()  # >= 90% completes
        self.assertTrue(r["completed"])
        self.assertEqual(self.c.get("/api/history").get_json()["continue"], [])
        d = self.c.get("/api/details?id=series:tt0000003").get_json()
        self.assertIn("1:2", d["watched"])
        self.assertEqual(d["resume"]["episode"], 2)

    def test_one_entry_per_show_watched_set_per_episode(self):
        self.post_progress(3000, episode=1)
        self.post_progress(500, episode=2)
        h = self.c.get("/api/history").get_json()["recent"]
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["episode"], 2)
        d = self.c.get("/api/details?id=series:tt0000003").get_json()
        self.assertEqual(d["watched"], ["1:1"])

    def test_live_length_never_in_continue(self):
        self.c.post("/api/history/progress", json={"item": self.item(), "progress": 900, "duration": None})
        self.assertEqual(self.c.get("/api/history").get_json()["continue"], [])
        self.assertFalse(is_in_progress({"progress_seconds": 900, "duration_seconds": None, "completed": False}))

    def test_completion_saved_even_if_progress_unchanged(self):
        self.post_progress(2000)
        self.post_progress(2000)
        self.c.post("/api/history/progress", json={"item": self.item(), "progress": 2000, "duration": 3000, "completed": True})
        d = self.c.get("/api/details?id=series:tt0000003").get_json()
        self.assertIn("1:2", d["watched"])
        self.assertTrue(HistoryManager(self.svc.paths.data / "history.json").is_watched("addons", "series:tt0000003", 1, 2))

    def test_start_watched_remove_clear(self):
        self.c.post("/api/history/start", json={"item": self.item(), "start": 45})
        self.assertEqual(self.c.get("/api/history").get_json()["recent"][0]["progress_seconds"], 45)
        self.c.post("/api/history/watched", json={"item": self.item(episode=3)})
        self.assertIn("1:3", self.c.get("/api/details?id=series:tt0000003").get_json()["watched"])
        self.c.delete("/api/history", query_string={"provider": "addons", "id": "series:tt0000003", "season": 1, "episode": 3})
        self.assertNotIn("1:3", self.c.get("/api/details?id=series:tt0000003").get_json()["watched"])
        self.c.delete("/api/history/all")
        self.assertEqual(self.c.get("/api/history").get_json()["recent"], [])

    def test_history_validation_and_limit(self):
        self.assertEqual(self.c.post("/api/history/progress", json={"item": {}, "progress": 1}).status_code, 400)
        self.assertEqual(self.c.post("/api/history/progress", json={"item": self.item(), "progress": "abc"}).status_code, 400)
        for i in range(105):
            self.c.post("/api/history/start", json={"item": self.item(subject_id=f"movie:tt{i}", title=f"T{i}", stype=1, season=0, episode=0), "start": 0})
        self.assertEqual(len(self.c.get("/api/history").get_json()["recent"]), 100)

    def test_tui_history_file_loads(self):
        p = self.svc.paths.data / "history.json"
        p.write_text(json.dumps({"watched": ["addons::series:tt9::1::1"], "recent": [
            {"provider": "Addons", "subject_id": "series:tt9", "title": "Old Show", "cover_url": None, "stype": 2, "release_year": "2010", "season": 1, "episode": 1, "timestamp": 1700000000, "duration_seconds": 2400, "progress_seconds": 600, "completed": False}]}))
        h = HistoryManager(p)
        self.assertEqual(h.recent[0]["provider"], "addons")
        self.assertTrue(h.recent[0]["completed"])  # reconciled from the watched index
        self.assertEqual(h.watched_episodes("addons", "series:tt9"), ["1:1"])

    def test_corrupt_files_are_rotated_not_lost(self):
        p = self.svc.paths.data / "history.json"
        p.write_text("{not json")
        HistoryManager(p)
        self.assertTrue(any(f.name.startswith("history.corrupt.") for f in p.parent.iterdir()))


# --------------------------------------------------------------------------- TV

class TV(Base):
    def setUp(self):
        self.app, self.c, self.svc = self.new_app()

    def add(self, source):
        return self.c.post("/api/tv/playlists", json={"source": source})

    def test_parse_m3u_details(self):
        ch = parse_m3u('#EXTM3U\n#EXTINF:-1 tvg-id=\'a\' group-title="Music, Pop" tvg-logo="http://l/x.png",Chan, With Comma\nhttp://s/1.m3u8\n')
        self.assertEqual((ch[0].id, ch[0].group, ch[0].logo, ch[0].name), ("a", "Music, Pop", "http://l/x.png", "Chan, With Comma"))
        self.assertEqual(parse_m3u("#EXTINF:-1,Solo\nhttp://s/2\n")[0].id, "Solo")  # id falls back to name
        self.assertEqual(parse_m3u("#EXTM3U\n#EXTVLCOPT:x=y\n"), [])

    def test_load_group_search_paginate(self):
        self.assertEqual(self.add(f"{self.base}/playlist.m3u").status_code, 201)
        d = self.c.get("/api/tv/channels").get_json()
        self.assertEqual((d["total"], d["all_total"]), (3, 3))  # duplicate stream URL removed
        self.assertEqual({g["name"]: g["count"] for g in d["groups"]}, {"News, World": 1, "Sports": 1, "Ungrouped": 1})
        news = next(c for c in d["channels"] if c["name"] == "News One")
        self.assertEqual(news["logo"], "https://img.test/n1.png")
        self.assertEqual(news["kind"], "hls")
        self.assertTrue(news["play_url"].startswith("/proxy/"))
        self.assertEqual(self.c.get("/api/tv/channels?q=sport").get_json()["total"], 1)
        self.assertEqual(self.c.get("/api/tv/channels?q=world").get_json()["total"], 1)  # matches group name
        self.assertEqual(self.c.get("/api/tv/channels?group=Ungrouped").get_json()["total"], 1)
        p = self.c.get("/api/tv/channels?limit=2&offset=2").get_json()
        self.assertEqual((len(p["channels"]), p["total"]), (1, 3))

    def test_duplicates_and_removal(self):
        self.add(f"{self.base}/playlist.m3u")
        self.assertEqual(self.add(f"{self.base}/playlist.m3u").status_code, 409)
        self.assertEqual(self.c.delete("/api/tv/playlists", query_string={"source": f"{self.base}/playlist.m3u"}).status_code, 200)
        self.assertEqual(self.c.get("/api/tv/channels").get_json()["all_total"], 0)
        self.assertEqual(json.loads((self.svc.paths.config / "tv_config.json").read_text()), [])

    def test_failed_playlists_reported_not_fatal(self):
        self.add(f"{self.base}/playlist.m3u")
        self.add(f"{self.base}/missing.m3u")
        d = self.c.get("/api/tv/channels").get_json()
        self.assertEqual(d["all_total"], 3)
        self.assertEqual(len(d["failed"]), 1)

    def test_upload_and_local_path(self):
        data = b'#EXTM3U\n#EXTINF:-1 group-title="Up",Uploaded\nhttps://x.test/u.m3u8\n'
        r = self.c.post("/api/tv/playlists", data={"file": (io.BytesIO(data), "My List.m3u")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.c.get("/api/tv/channels").get_json()["channels"][0]["name"], "Uploaded")
        f = Path(tempfile.mkdtemp()) / "mine.m3u"
        f.write_bytes(b"#EXTINF:-1,Local\nhttps://x.test/l.m3u8\n")
        self.assertEqual(self.add(str(f)).status_code, 201)  # loopback: local paths allowed
        self.assertEqual(self.c.get("/api/tv/channels").get_json()["all_total"], 2)

    def test_local_paths_refused_when_hosted(self):
        app = create_app(make_home(self.base), host="0.0.0.0", plugin_dirs=[])
        c = app.test_client()
        self.assertEqual(c.post("/api/tv/playlists", json={"source": "/etc/passwd"}).status_code, 400)

    def test_size_cap(self):
        big = b"#EXTM3U\n" + b"x" * (15 * 1024 * 1024 + 10)
        r = self.c.post("/api/tv/playlists", data={"file": (io.BytesIO(big), "big.m3u")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)


# --------------------------------------------------------------------------- plugins

class Plugins(Base):
    def test_plugin_lifecycle(self):
        d = Path(tempfile.mkdtemp())
        (d / "good.py").write_text(textwrap.dedent('''
            from moviebox_web.models import CatalogItem, MediaDetails, Release, SourceMirror, StreamResult
            from moviebox_web.providers.base import ProviderCapabilities, ReleaseProvider

            class Good(ReleaseProvider):
                id = "good"; label = "Good Source"
                def capabilities(self): return ProviderCapabilities(supports_homepage=False)
                def search(self, query, page=1): return [CatalogItem(provider="good", id="g1", title="Hit " + query, year="2020")]
                def details(self, media_id): return MediaDetails(provider="good", id=media_id, title="Detail")
                def episode_streams(self, media_id, season=0, episode=0, is_series=False):
                    return StreamResult(releases=[Release(provider="good", filename="f", quality="1080p", mirrors=[SourceMirror(label="G", resolver_url="https://x.test/v.mp4", headers=[("Referer", "https://x.test/")])])])

            def create_provider(ctx):
                return Good() if ctx.env("GOOD_KEY", "yes") else None
        '''))
        (d / "broken.py").write_text("raise RuntimeError('boom')")
        (d / "unconfigured.py").write_text("def create_provider(ctx): return None")
        (d / "_hidden.py").write_text("raise SystemExit('should never load')")
        app = create_app(make_home(self.base), plugin_dirs=[d])
        c = app.test_client()
        ids = [p["id"] for p in c.get("/api/bootstrap").get_json()["providers"]]
        self.assertEqual(ids, ["addons", "good"])
        r = c.get("/api/search?q=abc&provider=good").get_json()
        self.assertEqual(r["results"][0]["title"], "Hit abc")
        s = c.get("/api/streams?id=g1&provider=good").get_json()["releases"][0]["mirrors"][0]
        self.assertEqual(s["headers"], {"Referer": "https://x.test/"})
        self.assertTrue(s["play_url"].startswith("/proxy/"))  # headers force the proxy
        self.assertEqual(c.get("/api/browse?provider=good").get_json()["supported"], False)

    def test_template_not_loaded_without_credentials(self):
        app = create_app(make_home(self.base))  # default dirs include providers/custom with _template.py
        ids = [p["id"] for p in app.test_client().get("/api/bootstrap").get_json()["providers"]]
        self.assertEqual(ids, ["addons"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
