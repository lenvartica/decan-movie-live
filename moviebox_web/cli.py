"""Command line entry point: ``python run.py`` or ``python -m moviebox_web``."""
from __future__ import annotations

import argparse
import logging
import os
import threading
import webbrowser

from . import __version__
from .app import create_app
from .security import is_loopback_bind


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="moviebox-web", description="Browser front end for MovieBox-TUI")
    parser.add_argument("--host", default=os.environ.get("MOVIEBOX_HOST", "127.0.0.1"), help="interface to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("MOVIEBOX_PORT", "8787")), help="port (default 8787)")
    parser.add_argument("--open", action="store_true", help="open the app in your browser")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--version", action="version", version=f"moviebox-web {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app(host=args.host)

    loopback = is_loopback_bind(args.host)
    url = f"http://{'localhost' if loopback else args.host}:{args.port}"
    if not loopback and not os.environ.get("MOVIEBOX_PASSWORD"):
        logging.warning(
            "Binding to %s without MOVIEBOX_PASSWORD: anyone who can reach this port can use your library "
            "and play streams through this server. Set MOVIEBOX_PASSWORD, or bind to 127.0.0.1.",
            args.host,
        )
    print(f"\n  MovieBox Web {__version__}\n  {url}\n")
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:  # waitress handles many concurrent streams better than the dev server
        from waitress import serve

        serve(app, host=args.host, port=args.port, threads=32, channel_timeout=120)
    except ImportError:
        app.run(host=args.host, port=args.port, threaded=True, debug=False)
    return 0
