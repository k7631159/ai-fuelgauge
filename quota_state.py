"""Shared quota state service for CLI-adjacent UI surfaces."""
from __future__ import annotations

import threading

import ai_fuelgauge as afg


def _copy_snapshot(snapshot: dict) -> dict:
    # Shallow by design: renderers must treat nested provider dicts as read-only.
    return dict(snapshot)


def empty_snapshot() -> dict:
    return {"codex": {}, "claude": {}, "_from_cache": False}


def _load_cached_snapshot() -> "dict | None":
    cache = afg.load_cache(afg.CACHE_TTL_SECONDS)
    if not isinstance(cache, dict):
        return None
    cache = dict(cache)
    cache["_from_cache"] = True
    return cache


def cached_snapshot() -> "dict | None":
    """Return the current shared cache without probing providers."""
    return _load_cached_snapshot()


def probe_snapshot() -> dict:
    """Fetch a fresh quota snapshot and update shared caches."""
    codex_fresh = afg.probe_codex_fresh()
    codex_stale = afg.read_codex_quota()
    if (
        codex_fresh
        and not codex_fresh.get("error")
        and codex_fresh.get("primary", {}).get("used_percent") is not None
    ):
        codex = codex_fresh
        codex["_source"] = "fresh-api"
    elif codex_stale:
        codex = codex_stale
        if isinstance(codex, dict):
            codex["_source"] = "sqlite-snapshot"
            codex["_fresh_probe_error"] = (
                codex_fresh.get("error") if isinstance(codex_fresh, dict) else "no-data"
            )
    else:
        codex = codex_fresh if isinstance(codex_fresh, dict) else {}
    claude = afg.probe_claude_quota()
    afg._save_last_good_claude(claude)
    data = {"codex": codex, "claude": claude, "_from_cache": False}
    cache_data = dict(data)
    cache_data.pop("_from_cache", None)
    afg.save_cache(cache_data)
    return data


def load_or_probe_snapshot(cache_only: bool = False,
                           force_refresh: bool = False) -> dict:
    if not force_refresh:
        cache = _load_cached_snapshot()
        if cache is not None:
            return cache
    if cache_only:
        return empty_snapshot()
    return probe_snapshot()


class QuotaStateService:
    """Single owner for quota snapshots shared by UI views in one process."""

    def __init__(self, cache_only: bool = False) -> None:
        self.cache_only = cache_only
        self._lock = threading.RLock()
        self._snapshot: dict = {}

    def current_snapshot(self) -> dict:
        with self._lock:
            if self._snapshot:
                return _copy_snapshot(self._snapshot)
        cache = _load_cached_snapshot()
        if cache is not None:
            return cache
        return empty_snapshot()

    def refresh_snapshot(self, force_refresh: bool = True) -> dict:
        snapshot = load_or_probe_snapshot(
            cache_only=self.cache_only,
            force_refresh=force_refresh,
        )
        with self._lock:
            self._snapshot = snapshot
            return _copy_snapshot(self._snapshot)
