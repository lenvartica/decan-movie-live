"""Security helpers.

The terminal app talks to the network from your own machine only. A web server
is reachable by more than one program, so this module adds:

* signed, expiring proxy tokens, so ``/proxy`` can only fetch URLs that came out
  of your own catalogs or playlists (it is not an open proxy),
* an SSRF policy for every server-side fetch (private ranges are blocked unless
  you run locally or opt in),
* Host / Origin validation, which stops other websites in your browser from
  driving a server bound to localhost (DNS rebinding, cross-site POSTs),
* optional HTTP Basic auth for when you host it beyond localhost.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from .config import atomic_write


class UnsafeURL(Exception):
    """Raised when a URL is not allowed by the fetch policy."""


# --------------------------------------------------------------------------- tokens

def load_secret(path: Path) -> bytes:
    try:
        data = path.read_bytes()
        if len(data) >= 32:
            return data
    except OSError:
        pass
    data = secrets.token_bytes(32)
    atomic_write(path, data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return data


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class TokenSigner:
    def __init__(self, secret: bytes):
        self._secret = secret

    def sign(self, payload: dict, ttl: int = 24 * 3600) -> str:
        body = dict(payload)
        body["x"] = int(time.time()) + ttl
        blob = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        sig = hmac.new(self._secret, blob.encode("ascii"), hashlib.sha256).digest()[:18]
        return f"{blob}.{_b64(sig)}"

    def verify(self, token: str) -> dict | None:
        try:
            blob, sig = token.split(".", 1)
            expect = hmac.new(self._secret, blob.encode("ascii"), hashlib.sha256).digest()[:18]
            if not hmac.compare_digest(_unb64(sig), expect):
                return None
            body = json.loads(_unb64(blob))
            if not isinstance(body, dict) or int(body.get("x", 0)) < time.time():
                return None
            return body
        except (ValueError, TypeError, UnicodeError):
            return None


# --------------------------------------------------------------------------- SSRF policy

def _blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not ip.is_global


def check_url(url: str, allow_private: bool) -> None:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("Only http:// and https:// URLs are allowed")
    host = parts.hostname
    if not host:
        raise UnsafeURL("URL has no host")
    if allow_private:
        return
    if host.lower() in ("localhost", "localhost.localdomain") or host.lower().endswith(".localhost"):
        raise UnsafeURL("Private and loopback addresses are blocked")
    try:
        literals = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror:
            return  # unresolvable: the fetch itself will fail with a network error
        literals = [ipaddress.ip_address(info[4][0]) for info in infos]
    if any(_blocked_ip(ip) for ip in literals):
        raise UnsafeURL("Private and loopback addresses are blocked (set MOVIEBOX_ALLOW_PRIVATE=1 to allow)")


_REDIRECTS = (301, 302, 303, 307, 308)
_CROSS_ORIGIN_STRIP = ("authorization", "cookie")


class HttpPolicy:
    """One shared ``requests`` session plus the fetch policy."""

    def __init__(self, allow_private: bool, user_agent: str = "MovieBox-Web/1.0"):
        self.allow_private = allow_private
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        stream: bool = False,
        timeout: tuple[float, float] = (8, 20),
        max_redirects: int = 5,
    ) -> requests.Response:
        hdrs = dict(headers or {})
        current = url
        for _ in range(max_redirects + 1):
            check_url(current, self.allow_private)
            resp = self.session.request(method, current, headers=hdrs, stream=stream, timeout=timeout, allow_redirects=False)
            location = resp.headers.get("Location")
            if resp.status_code in _REDIRECTS and location:
                nxt = urljoin(current, location)
                resp.close()
                if urlsplit(nxt).netloc != urlsplit(current).netloc:
                    hdrs = {k: v for k, v in hdrs.items() if k.lower() not in _CROSS_ORIGIN_STRIP}
                current = nxt
                continue
            return resp
        raise UnsafeURL("Too many redirects")

    def get(self, url: str, **kw) -> requests.Response:
        return self.request(url, **kw)


# --------------------------------------------------------------------------- request checks

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def is_loopback_bind(host: str) -> bool:
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def host_header_ok(host_header: str, allowed: set[str]) -> bool:
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        name = host.split("]")[0] + "]"
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return name in allowed


def origin_ok(origin: str | None, host_header: str) -> bool:
    """Same-origin check for state-changing requests."""
    if not origin:
        return True
    if origin == "null":
        return False
    return urlsplit(origin).netloc.lower() == (host_header or "").lower()


def password_ok(header: str | None, password: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
    except (ValueError, UnicodeError):
        return False
    supplied = decoded.split(":", 1)[-1]
    return hmac.compare_digest(supplied.encode("utf-8"), password.encode("utf-8"))
