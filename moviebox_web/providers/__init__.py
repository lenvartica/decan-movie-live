"""Provider registry.

Built in: ``addons`` (Stremio HTTP addons). Anything else is loaded as a plugin
from ``providers/custom/*.py`` or from ``<data dir>/plugins/*.py``: a module
that defines ``create_provider(ctx) -> Provider | None``. Return ``None`` when
the plugin is not configured (for example its API key env var is unset) and it
is simply skipped.

Plugins are ordinary Python running with your permissions. Only install ones
you wrote or trust.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

from .base import Provider, ProviderCapabilities, ProviderContext, ProviderError, ReleaseProvider

__all__ = ["Provider", "ReleaseProvider", "ProviderCapabilities", "ProviderContext", "ProviderError", "ProviderRegistry"]

log = logging.getLogger("moviebox_web.providers")


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}

    def register(self, provider: Provider) -> None:
        pid = (provider.id or "").strip().lower()
        if not pid:
            raise ValueError("provider has no id")
        if pid in self._providers:
            raise ValueError(f"provider id '{pid}' is already registered")
        self._providers[pid] = provider

    def get(self, provider_id: str | None) -> Provider:
        pid = (provider_id or "addons").strip().lower()
        if pid in ("addon", "stremio"):
            pid = "addons"
        try:
            return self._providers[pid]
        except KeyError:
            raise ProviderError(ProviderError.KIND_NOT_FOUND, f"Unknown provider '{provider_id}'") from None

    def all(self) -> list[Provider]:
        return list(self._providers.values())

    def load_plugins(self, ctx: ProviderContext, dirs: list[Path]) -> list[str]:
        """Import plugin modules. Returns the ids that were registered."""
        loaded: list[str] = []
        for directory in dirs:
            if not directory.is_dir():
                continue
            for file in sorted(directory.glob("*.py")):
                if file.name.startswith("_"):
                    continue
                try:
                    spec = importlib.util.spec_from_file_location(f"moviebox_web_plugin_{file.stem}", file)
                    if spec is None or spec.loader is None:
                        continue
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[spec.name] = module
                    spec.loader.exec_module(module)
                    factory = getattr(module, "create_provider", None)
                    if factory is None:
                        log.warning("plugin %s has no create_provider(ctx); skipped", file.name)
                        continue
                    provider = factory(ctx)
                    if provider is None:
                        log.info("plugin %s is not configured; skipped", file.name)
                        continue
                    if not isinstance(provider, Provider):
                        log.warning("plugin %s returned %r, not a Provider; skipped", file.name, type(provider).__name__)
                        continue
                    self.register(provider)
                    loaded.append(provider.id)
                    log.info("loaded provider plugin '%s' from %s", provider.id, file.name)
                except Exception:  # a broken plugin must never take the app down
                    log.exception("failed to load plugin %s", file.name)
        return loaded
