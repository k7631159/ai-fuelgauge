import quota_state


def test_load_or_probe_uses_cache_when_available(monkeypatch):
    monkeypatch.setattr(
        quota_state.afg,
        "load_cache",
        lambda ttl: {"codex": {"primary": {"used_percent": 11}}},
    )

    def fail_probe():
        raise AssertionError("cache hit should not probe")

    monkeypatch.setattr(quota_state, "probe_snapshot", fail_probe)

    assert quota_state.load_or_probe_snapshot() == {
        "codex": {"primary": {"used_percent": 11}},
        "_from_cache": True,
    }


def test_force_refresh_bypasses_cache(monkeypatch):
    monkeypatch.setattr(
        quota_state.afg,
        "load_cache",
        lambda ttl: {"codex": {"primary": {"used_percent": 11}}},
    )
    monkeypatch.setattr(
        quota_state,
        "probe_snapshot",
        lambda: {"codex": {"primary": {"used_percent": 55}}, "_from_cache": False},
    )

    assert quota_state.load_or_probe_snapshot(force_refresh=True) == {
        "codex": {"primary": {"used_percent": 55}},
        "_from_cache": False,
    }


def test_cache_only_miss_returns_empty_without_probe(monkeypatch):
    monkeypatch.setattr(quota_state.afg, "load_cache", lambda ttl: None)

    def fail_probe():
        raise AssertionError("cache-only service must not probe")

    monkeypatch.setattr(quota_state, "probe_snapshot", fail_probe)

    assert quota_state.load_or_probe_snapshot(cache_only=True) == {
        "codex": {},
        "claude": {},
        "_from_cache": False,
    }


def test_cached_snapshot_reads_shared_cache_without_probe(monkeypatch):
    monkeypatch.setattr(
        quota_state.afg,
        "load_cache",
        lambda ttl: {"codex": {"primary": {"used_percent": 82}}},
    )

    def fail_probe():
        raise AssertionError("cache sync must not probe providers")

    monkeypatch.setattr(quota_state, "probe_snapshot", fail_probe)

    assert quota_state.cached_snapshot() == {
        "codex": {"primary": {"used_percent": 82}},
        "_from_cache": True,
    }


def test_probe_snapshot_saves_cache_without_runtime_cache_marker(monkeypatch):
    codex = {"primary": {"used_percent": 22}}
    claude = {"primary": {"used_percent": 33}}
    saved = []

    monkeypatch.setattr(quota_state.afg, "probe_codex_fresh", lambda: codex)
    monkeypatch.setattr(quota_state.afg, "read_codex_quota", lambda: None)
    monkeypatch.setattr(quota_state.afg, "probe_claude_quota", lambda: claude)
    monkeypatch.setattr(quota_state.afg, "_save_last_good_claude", lambda _claude: None)
    monkeypatch.setattr(quota_state.afg, "save_cache", saved.append)

    snapshot = quota_state.probe_snapshot()

    assert snapshot == {
        "codex": {"primary": {"used_percent": 22}, "_source": "fresh-api"},
        "claude": claude,
        "_from_cache": False,
    }
    assert saved == [
        {
            "codex": {"primary": {"used_percent": 22}, "_source": "fresh-api"},
            "claude": claude,
        }
    ]


def test_probe_snapshot_preserves_codex_error_when_no_stale_fallback(monkeypatch):
    codex_error = {"error": "codex-not-in-path"}
    claude = {"primary": {"used_percent": 33}}
    saved = []

    monkeypatch.setattr(quota_state.afg, "probe_codex_fresh", lambda: codex_error)
    monkeypatch.setattr(quota_state.afg, "read_codex_quota", lambda: None)
    monkeypatch.setattr(quota_state.afg, "probe_claude_quota", lambda: claude)
    monkeypatch.setattr(quota_state.afg, "_save_last_good_claude", lambda _claude: None)
    monkeypatch.setattr(quota_state.afg, "save_cache", saved.append)

    snapshot = quota_state.probe_snapshot()

    assert snapshot["codex"] == {"error": "codex-not-in-path"}
    assert snapshot["_from_cache"] is False
    assert saved == [{"codex": {"error": "codex-not-in-path"}, "claude": claude}]


def test_service_current_snapshot_returns_copy_and_cache_fallback(monkeypatch):
    service = quota_state.QuotaStateService()
    service._snapshot = {"codex": {"primary": {"used_percent": 44}}}

    current = service.current_snapshot()

    assert current == service._snapshot
    assert current is not service._snapshot

    service = quota_state.QuotaStateService()
    monkeypatch.setattr(
        quota_state.afg,
        "load_cache",
        lambda ttl: {"codex": {"primary": {"used_percent": 55}}},
    )

    assert service.current_snapshot() == {
        "codex": {"primary": {"used_percent": 55}},
        "_from_cache": True,
    }


def test_service_refresh_updates_owned_snapshot(monkeypatch):
    service = quota_state.QuotaStateService()
    monkeypatch.setattr(
        quota_state,
        "load_or_probe_snapshot",
        lambda **_kwargs: {"codex": {"primary": {"used_percent": 66}}},
    )

    refreshed = service.refresh_snapshot(force_refresh=True)

    assert refreshed == {"codex": {"primary": {"used_percent": 66}}}
    assert service.current_snapshot() == refreshed
    assert service.current_snapshot() is not refreshed
