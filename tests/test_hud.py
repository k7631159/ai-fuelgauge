import json

import hud


def test_toggle_mode_is_two_state_cycle():
    assert hud.toggle_mode(hud.MODE_COMPACT) == hud.MODE_EXPANDED
    assert hud.toggle_mode(hud.MODE_EXPANDED) == hud.MODE_COMPACT
    assert hud.toggle_mode("anything-else") == hud.MODE_COMPACT


def test_is_drag_respects_threshold():
    assert hud.is_drag((10, 10), (14, 14), threshold=4) is False
    assert hud.is_drag((10, 10), (15, 10), threshold=4) is True
    assert hud.is_drag((10, 10), (10, 5), threshold=4) is True


def test_format_hud_rows_compact_shows_only_5h(monkeypatch):
    monkeypatch.setattr(hud.time, "time", lambda: 1_700_000_000)
    snap = {
        "codex": {
            "primary": {"used_percent": 30, "reset_at": 1_700_003_600},
            "secondary": {"used_percent": 25, "reset_at": 1_700_086_400},
        },
        "claude": {
            "primary": {"used_percent": 29, "reset_at": 1_700_005_400},
            "secondary": {"used_percent": 15, "reset_at": 1_700_172_800},
        },
    }

    rows = hud.format_hud_rows(snap, hud.MODE_COMPACT)

    assert rows == [
        "Codex  5h    30%  1h00m",
        "Claude 5h    29%  1h30m",
    ]


def test_format_hud_rows_expanded_includes_week_rows(monkeypatch):
    monkeypatch.setattr(hud.time, "time", lambda: 1_700_000_000)
    snap = {
        "codex": {
            "primary": {"used_percent": 30, "reset_at": 1_700_003_600},
            "secondary": {"used_percent": 25, "reset_at": 1_700_086_400},
        },
        "claude": {
            "primary": {"used_percent": 29, "reset_at": 1_700_005_400},
            "secondary": {"used_percent": 15, "reset_at": 1_700_172_800},
        },
    }

    rows = hud.format_hud_rows(snap, hud.MODE_EXPANDED)

    assert rows == [
        "Codex  5h    30%  1h00m",
        "Codex  week  25%  1d00h",
        "Claude 5h    29%  1h30m",
        "Claude week  15%  2d00h",
    ]


def test_format_hud_rows_uses_valid_stale_claude(monkeypatch):
    monkeypatch.setattr(hud.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(
        hud.afg,
        "_load_last_good_claude",
        lambda: {
            "_schema": 1,
            "_probed_at": 1_699_999_000,
            "primary": {"used_percent": 77, "reset_at": 1_700_003_600},
            "secondary": {"used_percent": 44, "reset_at": 1_700_086_400},
        },
    )

    rows = hud.format_hud_rows(
        {
            "codex": {"primary": {"used_percent": 10, "reset_at": 1_700_003_600}},
            "claude": {"error": "auth-expired-no-refresh"},
        },
        hud.MODE_EXPANDED,
    )

    assert rows == [
        "Codex  5h    10%  1h00m",
        "Codex  week   --      -",
        "Claude 5h    77%  1h00m stale",
        "Claude week  44%  1d00h stale",
    ]


def test_load_position_rejects_malformed_file(tmp_path):
    path = tmp_path / "hud.json"
    path.write_text("{not-json", encoding="utf-8")

    assert hud.load_position(path) is None


def test_save_and_load_position(tmp_path):
    path = tmp_path / "nested" / "hud.json"

    hud.save_position(123, 456, path)

    assert json.loads(path.read_text(encoding="utf-8")) == {"x": 123, "y": 456}
    assert hud.load_position(path) == (123, 456)


def test_clamp_position_keeps_hud_on_screen():
    assert hud.clamp_position((-50, -10), 1920, 1080) == (0, 0)
    assert hud.clamp_position((5000, 2000), 1920, 1080) == (1896, 1056)
    assert hud.clamp_position((100, 200), 1920, 1080) == (100, 200)


def test_load_opacity_defaults_and_clamps(tmp_path):
    path = tmp_path / "hud.json"

    assert hud.load_opacity(path) == hud.DEFAULT_OPACITY

    path.write_text(json.dumps({"opacity": 5}), encoding="utf-8")
    assert hud.load_opacity(path) == hud.MAX_OPACITY

    path.write_text(json.dumps({"opacity": 0.1}), encoding="utf-8")
    assert hud.load_opacity(path) == hud.MIN_OPACITY


def test_adjust_opacity_value_steps_and_clamps():
    assert hud.adjust_opacity_value(0.75, 1) == 0.85
    assert hud.adjust_opacity_value(0.75, -2) == 0.55
    assert hud.adjust_opacity_value(0.95, 1) == hud.MAX_OPACITY
    assert hud.adjust_opacity_value(0.26, -1) == hud.MIN_OPACITY


def test_opacity_presets_cover_visible_range():
    assert hud.OPACITY_PRESETS == (1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3)
    assert min(hud.OPACITY_PRESETS) >= hud.MIN_OPACITY
    assert max(hud.OPACITY_PRESETS) == hud.MAX_OPACITY


def test_hud_release_toggles_even_if_release_coordinates_jump():
    class FakeHud:
        _press_root = (0, 0)
        _press_pointer = (100, 100)
        _dragged = False
        toggled = False
        saved = False

        def toggle(self):
            self.toggled = True

        def _save_current_position(self):
            self.saved = True

    class FakeEvent:
        x_root = 500
        y_root = 500

    app = FakeHud()

    result = hud.HudApp._on_release(app, FakeEvent())

    assert app.toggled is True
    assert app.saved is False
    assert app._press_root is None
    assert app._press_pointer is None
    assert app._dragged is False
    assert result == "break"


def test_hud_release_ignores_duplicate_bindtag_release():
    class FakeHud:
        _press_root = (0, 0)
        _press_pointer = (100, 100)
        _dragged = False
        toggle_count = 0
        save_count = 0

        def toggle(self):
            self.toggle_count += 1

        def _save_current_position(self):
            self.save_count += 1

    class FakeEvent:
        x_root = 100
        y_root = 100

    app = FakeHud()

    assert hud.HudApp._on_release(app, FakeEvent()) == "break"
    assert hud.HudApp._on_release(app, FakeEvent()) == "break"

    assert app.toggle_count == 1
    assert app.save_count == 0


def test_hud_drag_motion_marks_dragged_and_release_saves():
    class FakeRoot:
        def __init__(self):
            self.geometries = []

        def geometry(self, value):
            self.geometries.append(value)

    class FakeHud:
        _press_root = (20, 30)
        _press_pointer = (100, 100)
        _dragged = False
        root = FakeRoot()
        toggled = False
        saved = False

        def toggle(self):
            self.toggled = True

        def _save_current_position(self):
            self.saved = True

    class FakeEvent:
        def __init__(self, x_root, y_root):
            self.x_root = x_root
            self.y_root = y_root

    app = FakeHud()

    assert hud.HudApp._on_drag(app, FakeEvent(103, 103)) == "break"

    assert app._dragged is False
    assert app.root.geometries == []

    assert hud.HudApp._on_drag(app, FakeEvent(110, 105)) == "break"
    assert hud.HudApp._on_release(app, FakeEvent(110, 105)) == "break"

    assert app.root.geometries == ["+30+35"]
    assert app.saved is True
    assert app.toggled is False


def test_save_opacity_preserves_position(tmp_path):
    path = tmp_path / "hud.json"
    hud.save_position(123, 456, path)

    hud.save_opacity(0.75, path)

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "x": 123,
        "y": 456,
        "opacity": 0.75,
    }
    assert hud.load_position(path) == (123, 456)
    assert hud.load_opacity(path) == 0.75


def test_cache_only_snapshot_does_not_probe(monkeypatch):
    monkeypatch.setattr(hud.afg, "load_cache", lambda ttl: None)

    def fail_probe():
        raise AssertionError("cache-only HUD must not probe")

    monkeypatch.setattr(hud, "_probe_snapshot", fail_probe)

    assert hud._load_or_probe_snapshot(cache_only=True) == {
        "codex": {},
        "claude": {},
        "_from_cache": False,
    }


def test_non_cache_only_snapshot_probes_when_cache_missing(monkeypatch):
    monkeypatch.setattr(hud.afg, "load_cache", lambda ttl: None)
    monkeypatch.setattr(hud, "_probe_snapshot", lambda: {"codex": {"ok": True}})

    assert hud._load_or_probe_snapshot(cache_only=False) == {"codex": {"ok": True}}


def test_force_refresh_bypasses_cache_for_standalone_hud(monkeypatch):
    monkeypatch.setattr(
        hud.afg,
        "load_cache",
        lambda ttl: {"codex": {"primary": {"used_percent": 11}}},
    )
    monkeypatch.setattr(
        hud,
        "_probe_snapshot",
        lambda: {"codex": {"primary": {"used_percent": 55}}},
    )

    assert hud._load_or_probe_snapshot(cache_only=False, force_refresh=True) == {
        "codex": {"primary": {"used_percent": 55}},
    }


def test_force_refresh_respects_cache_only_mode(monkeypatch):
    monkeypatch.setattr(
        hud.afg,
        "load_cache",
        lambda ttl: {"codex": {"primary": {"used_percent": 11}}},
    )

    def fail_probe():
        raise AssertionError("cache-only HUD must not probe on forced refresh")

    monkeypatch.setattr(hud, "_probe_snapshot", fail_probe)

    assert hud._load_or_probe_snapshot(cache_only=True, force_refresh=True) == {
        "codex": {},
        "claude": {},
        "_from_cache": False,
    }


def test_hud_refresh_now_uses_refresh_loader(monkeypatch):
    class InlineThread:
        def __init__(self, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self):
            self.target()

    class FakeRoot:
        def after(self, _delay, callback):
            callback()

    applied = []
    refreshed = {"codex": {"primary": {"used_percent": 55}}}

    app = hud.HudApp.__new__(hud.HudApp)
    app._fetch_lock = hud.threading.Lock()
    app._cache_only = True
    app._snapshot_loader = lambda: {"codex": {"primary": {"used_percent": 11}}}
    app._refresh_loader = lambda: refreshed
    app.root = FakeRoot()
    app._apply_snapshot = applied.append

    monkeypatch.setattr(hud.threading, "Thread", InlineThread)

    app.refresh_now()

    assert applied == [refreshed]


def test_hud_poll_uses_snapshot_loader(monkeypatch):
    class InlineThread:
        def __init__(self, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self):
            self.target()

    class FakeRoot:
        def after(self, _delay, callback):
            callback()

    applied = []
    snapshot = {"codex": {"primary": {"used_percent": 11}}}
    refresh_called = False

    def refresh_loader():
        nonlocal refresh_called
        refresh_called = True
        return {"codex": {"primary": {"used_percent": 55}}}

    app = hud.HudApp.__new__(hud.HudApp)
    app._fetch_lock = hud.threading.Lock()
    app._cache_only = True
    app._snapshot_loader = lambda: snapshot
    app._refresh_loader = refresh_loader
    app.root = FakeRoot()
    app._apply_snapshot = applied.append

    monkeypatch.setattr(hud.threading, "Thread", InlineThread)

    app._start_fetch()

    assert applied == [snapshot]
    assert refresh_called is False
