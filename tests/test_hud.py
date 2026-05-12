import json
import time

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


def test_format_hud_rows_surfaces_rate_limited_claude():
    rows = hud.format_hud_rows(
        {
            "codex": {"primary": {"used_percent": 10}},
            "claude": {"status": 429},
        },
        hud.MODE_COMPACT,
    )

    assert rows == [
        "Codex  5h    10%      -",
        "Claude rate limited",
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


def test_save_and_load_visibility(tmp_path):
    path = tmp_path / "hud.json"

    assert hud.load_visibility(path) is False

    hud.save_visibility(True, path)
    assert hud.load_visibility(path) is True

    hud.save_visibility(False, path)
    assert hud.load_visibility(path) is False


def test_hud_state_writes_preserve_position_opacity_and_visibility(tmp_path):
    path = tmp_path / "hud.json"

    hud.save_position(123, 456, path)
    hud.save_opacity(0.75, path)
    hud.save_visibility(True, path)
    hud.save_position(321, 654, path)

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "x": 321,
        "y": 654,
        "opacity": 0.75,
        "visible": True,
    }
    assert hud.load_position(path) == (321, 654)
    assert hud.load_opacity(path) == 0.75
    assert hud.load_visibility(path) is True


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


def test_reassert_window_attributes_applies_topmost_opacity_and_lift():
    class FakeRoot:
        def __init__(self):
            self.attributes_calls = []
            self.lift_count = 0

        def attributes(self, *args):
            self.attributes_calls.append(args)

        def winfo_ismapped(self):
            return True

        def lift(self):
            self.lift_count += 1

    app = hud.HudApp.__new__(hud.HudApp)
    app.root = FakeRoot()
    app.opacity = 0.7

    app._reassert_window_attributes()

    assert ("-topmost", True) in app.root.attributes_calls
    assert ("-alpha", 0.7) in app.root.attributes_calls
    assert app.root.lift_count == 1


def test_reassert_window_attributes_does_not_lift_over_open_menu():
    class FakeRoot:
        def __init__(self):
            self.attributes_calls = []
            self.lift_count = 0

        def attributes(self, *args):
            self.attributes_calls.append(args)

        def winfo_ismapped(self):
            return True

        def lift(self):
            self.lift_count += 1

    app = hud.HudApp.__new__(hud.HudApp)
    app.root = FakeRoot()
    app.opacity = 0.7
    app._menu_open = True

    app._reassert_window_attributes()

    assert ("-alpha", 0.7) in app.root.attributes_calls
    assert ("-topmost", True) not in app.root.attributes_calls
    assert app.root.lift_count == 0


def test_reassert_window_attributes_waits_until_window_is_mapped():
    class FakeRoot:
        def __init__(self):
            self.attributes_calls = []
            self.lift_count = 0

        def attributes(self, *args):
            self.attributes_calls.append(args)

        def winfo_ismapped(self):
            return False

        def lift(self):
            self.lift_count += 1

    app = hud.HudApp.__new__(hud.HudApp)
    app.root = FakeRoot()
    app.opacity = 0.7
    app._menu_open = False

    app._reassert_window_attributes()

    assert ("-alpha", 0.7) in app.root.attributes_calls
    assert ("-topmost", True) not in app.root.attributes_calls
    assert app.root.lift_count == 0


def test_show_menu_temporarily_releases_topmost_and_watches_close():
    class FakeRoot:
        def __init__(self):
            self.attributes_calls = []
            self.after_calls = []

        def attributes(self, *args):
            self.attributes_calls.append(args)

        def after(self, delay, callback):
            self.after_calls.append((delay, callback))

    class FakeMenu:
        def __init__(self):
            self.popup_args = None

        def tk_popup(self, x, y):
            self.popup_args = (x, y)

    class FakeEvent:
        x_root = 10
        y_root = 20

    app = hud.HudApp.__new__(hud.HudApp)
    app.root = FakeRoot()
    app.menu = FakeMenu()
    app._refresh_opacity_menu = lambda: None

    assert app._show_menu(FakeEvent()) == "break"

    assert app._menu_open is True
    assert ("-topmost", False) in app.root.attributes_calls
    assert app.menu.popup_args == (10, 20)
    assert app.root.after_calls == [(150, app._watch_menu_close)]


def test_watch_menu_close_restores_window_attributes_after_submenus_close():
    class FakeRoot:
        def __init__(self):
            self.after_calls = []

        def after(self, delay, callback):
            self.after_calls.append((delay, callback))

    app = hud.HudApp.__new__(hud.HudApp)
    app.root = FakeRoot()
    app._menu_open = True
    app._menu_is_mapped = lambda: False
    restored = []
    app._reassert_window_attributes = lambda: restored.append(True)

    app._watch_menu_close()

    assert app._menu_open is False
    assert restored == [True]
    assert app.root.after_calls == []


def test_watch_menu_close_waits_while_submenu_is_open():
    class FakeRoot:
        def __init__(self):
            self.after_calls = []

        def after(self, delay, callback):
            self.after_calls.append((delay, callback))

    app = hud.HudApp.__new__(hud.HudApp)
    app.root = FakeRoot()
    app._menu_open = True
    app._menu_is_mapped = lambda: True
    app._reassert_window_attributes = lambda: (_ for _ in ()).throw(
        AssertionError("must not reassert while menu is open")
    )

    app._watch_menu_close()

    assert app._menu_open is True
    assert app.root.after_calls == [(150, app._watch_menu_close)]


def test_window_reassert_is_scheduled_periodically():
    class FakeRoot:
        def __init__(self):
            self.after_calls = []

        def after(self, delay, callback):
            self.after_calls.append((delay, callback))

    app = hud.HudApp.__new__(hud.HudApp)
    app.root = FakeRoot()
    reasserted = []
    app._reassert_window_attributes = lambda: reasserted.append(True)

    app._schedule_window_reassert()

    assert reasserted == [True]
    assert app.root.after_calls == [
        (hud.WINDOW_REASSERT_INTERVAL_MS, app._schedule_window_reassert)
    ]


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


def test_save_current_position_uses_last_requested_position(monkeypatch):
    class FakeRoot:
        def update_idletasks(self):
            pass

        def winfo_x(self):
            return 0

        def winfo_y(self):
            return 0

        def winfo_screenwidth(self):
            return 1920

        def winfo_screenheight(self):
            return 1080

    saved = []
    app = hud.HudApp.__new__(hud.HudApp)
    app.root = FakeRoot()
    app._last_position = (321, 654)

    monkeypatch.setattr(hud, "save_position", lambda x, y: saved.append((x, y)))

    app._save_current_position()

    assert saved == [(321, 654)]
    assert app._last_position == (321, 654)


def test_close_save_preserves_last_position_when_winfo_reports_zero(monkeypatch):
    """Regression: on the close path, winfo_x/y can report (0, 0) for a
    Windows overrideredirect toplevel mid-teardown. The save must trust
    the last requested/dragged position, not the bogus winfo readback."""

    class FakeRoot:
        def update_idletasks(self):
            pass

        def winfo_x(self):
            return 0

        def winfo_y(self):
            return 0

        def winfo_screenwidth(self):
            return 1920

        def winfo_screenheight(self):
            return 1080

    saved = []
    app = hud.HudApp.__new__(hud.HudApp)
    app.root = FakeRoot()
    app._last_position = (321, 654)
    app._close_position_saved = False

    monkeypatch.setattr(hud, "save_position", lambda x, y: saved.append((x, y)))

    app._save_close_position_once()

    assert saved == [(321, 654)]
    assert app._last_position == (321, 654)


def test_quit_saves_close_position_once_before_destroy(monkeypatch):
    """quit() must save the user's last requested position exactly once and
    a second redundant call to _save_close_position_once must be a no-op."""

    class FakeRoot:
        def __init__(self):
            self.destroy_count = 0

        def update_idletasks(self):
            pass

        def winfo_x(self):
            # Simulate Windows close-time stale readback.
            return 0

        def winfo_y(self):
            return 0

        def winfo_screenwidth(self):
            return 1920

        def winfo_screenheight(self):
            return 1080

        def destroy(self):
            self.destroy_count += 1

    saved = []
    app = hud.HudApp.__new__(hud.HudApp)
    app.root = FakeRoot()
    app._last_position = (321, 654)
    app._close_position_saved = False

    monkeypatch.setattr(hud, "save_position", lambda x, y: saved.append((x, y)))

    app.quit()
    app._save_close_position_once()

    assert saved == [(321, 654)]
    assert app.root.destroy_count == 1
    assert app._close_position_saved is True


def test_destroy_event_saves_close_position_once(monkeypatch):
    """The <Destroy> handler must save _last_position exactly once. A
    second <Destroy> event during teardown must not re-save (and must not
    overwrite with bogus winfo readback)."""

    class FakeRoot:
        def update_idletasks(self):
            pass

        def winfo_x(self):
            return 0

        def winfo_y(self):
            return 0

        def winfo_screenwidth(self):
            return 1920

        def winfo_screenheight(self):
            return 1080

    class FakeEvent:
        pass

    saved = []
    app = hud.HudApp.__new__(hud.HudApp)
    app.root = FakeRoot()
    app._last_position = (200, 300)
    app._close_position_saved = False
    event = FakeEvent()
    event.widget = app.root

    monkeypatch.setattr(hud, "save_position", lambda x, y: saved.append((x, y)))

    app._on_destroy(event)
    app._on_destroy(event)

    assert saved == [(200, 300)]
    assert app._close_position_saved is True


def test_drag_release_does_not_save_stale_winfo_origin(monkeypatch):
    class FakeRoot:
        def __init__(self):
            self.geometries = []

        def geometry(self, value):
            self.geometries.append(value)

        def winfo_x(self):
            return 0

        def winfo_y(self):
            return 0

        def winfo_screenwidth(self):
            return 1920

        def winfo_screenheight(self):
            return 1080

    class FakeEvent:
        def __init__(self, x_root, y_root):
            self.x_root = x_root
            self.y_root = y_root

    saved = []
    app = hud.HudApp.__new__(hud.HudApp)
    app.root = FakeRoot()
    app._press_root = (20, 30)
    app._press_pointer = (100, 100)
    app._dragged = False
    app._last_position = (20, 30)

    monkeypatch.setattr(hud, "save_position", lambda x, y: saved.append((x, y)))

    assert app._on_drag(FakeEvent(160, 175)) == "break"
    assert app._on_release(FakeEvent(160, 175)) == "break"

    assert app.root.geometries == ["+80+105"]
    assert saved == [(80, 105)]


def test_close_save_survives_tray_quit_with_zero_winfo_readback(monkeypatch):
    """Full close-path regression: user drags HUD to (500, 300), tray sends
    a quit request, HUD consumes it via _schedule_command_poll and quits.
    Even though the FakeRoot reports winfo_x/y = (0, 0) (mimicking the
    Windows mid-teardown readback we hit in the field), the saved position
    must remain (500, 300)."""

    class FakeRoot:
        def __init__(self):
            self.destroy_count = 0
            self.geometries = []

        def update_idletasks(self):
            pass

        def geometry(self, value):
            self.geometries.append(value)

        def winfo_x(self):
            return 0

        def winfo_y(self):
            return 0

        def winfo_screenwidth(self):
            return 1920

        def winfo_screenheight(self):
            return 1080

        def destroy(self):
            self.destroy_count += 1

    class FakeDragEvent:
        def __init__(self, x_root, y_root):
            self.x_root = x_root
            self.y_root = y_root

    saved = []
    app = hud.HudApp.__new__(hud.HudApp)
    app.root = FakeRoot()
    app._press_root = (50, 50)
    app._press_pointer = (200, 200)
    app._dragged = False
    app._last_position = (50, 50)
    app._close_position_saved = False

    monkeypatch.setattr(hud, "save_position", lambda x, y: saved.append((x, y)))

    # User drags the HUD: press already recorded, drag moves +450/+250.
    assert app._on_drag(FakeDragEvent(650, 450)) == "break"
    assert app._on_release(FakeDragEvent(650, 450)) == "break"

    assert app._last_position == (500, 300)
    assert saved == [(500, 300)]

    # Now tray-driven quit: HudApp.quit() saves close position before destroy.
    hud.HudApp.quit(app)

    assert app.root.destroy_count == 1
    # Final saved entry must still be the user's dragged position, NOT (0, 0).
    assert saved[-1] == (500, 300)
    assert app._last_position == (500, 300)


def test_clamp_position_to_bounds_supports_negative_virtual_origin():
    """Multi-monitor setups can place the virtual desktop origin at a
    negative coordinate (e.g., a left-of-primary secondary monitor). The
    clamp must keep saved positions reachable there instead of forcing
    them back to (0, 0)."""

    pos = (-1500, -200)
    clamped = hud.clamp_position_to_bounds(pos, -1920, -1080, 3840, 2160)
    assert clamped == (-1500, -200)

    too_far_left = hud.clamp_position_to_bounds((-5000, -2000), -1920, -1080, 3840, 2160)
    assert too_far_left == (-1920, -1080)

    too_far_right = hud.clamp_position_to_bounds((10000, 10000), -1920, -1080, 3840, 2160)
    assert too_far_right == (-1920 + 3840 - 24, -1080 + 2160 - 24)


def test_desktop_bounds_uses_virtual_root_when_available():
    class FakeRoot:
        def winfo_vrootx(self):
            return -1920

        def winfo_vrooty(self):
            return -100

        def winfo_vrootwidth(self):
            return 3840

        def winfo_vrootheight(self):
            return 2160

        def winfo_screenwidth(self):
            return 1920

        def winfo_screenheight(self):
            return 1080

    app = hud.HudApp.__new__(hud.HudApp)
    app.root = FakeRoot()

    assert app._desktop_bounds() == (-1920, -100, 3840, 2160)


def test_desktop_bounds_falls_back_to_screen_when_vroot_is_zero_size():
    class FakeRoot:
        def winfo_vrootx(self):
            return 0

        def winfo_vrooty(self):
            return 0

        def winfo_vrootwidth(self):
            return 0

        def winfo_vrootheight(self):
            return 0

        def winfo_screenwidth(self):
            return 1920

        def winfo_screenheight(self):
            return 1080

    app = hud.HudApp.__new__(hud.HudApp)
    app.root = FakeRoot()

    assert app._desktop_bounds() == (0, 0, 1920, 1080)


def test_desktop_bounds_falls_back_when_vroot_methods_missing():
    """Some Tk builds / X11 fallbacks may not expose winfo_vroot* at all."""

    class FakeRoot:
        def winfo_screenwidth(self):
            return 1280

        def winfo_screenheight(self):
            return 720

    app = hud.HudApp.__new__(hud.HudApp)
    app.root = FakeRoot()

    assert app._desktop_bounds() == (0, 0, 1280, 720)


def test_close_save_clamps_to_virtual_desktop_bounds(monkeypatch):
    """If _last_position drifted off the (now-smaller) virtual desktop,
    the close save must clamp it back inside rather than write garbage."""

    class FakeRoot:
        def update_idletasks(self):
            pass

        def winfo_x(self):
            return 0

        def winfo_y(self):
            return 0

        def winfo_vrootx(self):
            return 0

        def winfo_vrooty(self):
            return 0

        def winfo_vrootwidth(self):
            return 1920

        def winfo_vrootheight(self):
            return 1080

        def winfo_screenwidth(self):
            return 1920

        def winfo_screenheight(self):
            return 1080

    saved = []
    app = hud.HudApp.__new__(hud.HudApp)
    app.root = FakeRoot()
    app._last_position = (5000, 4000)  # left over from a previous monitor layout
    app._close_position_saved = False

    monkeypatch.setattr(hud, "save_position", lambda x, y: saved.append((x, y)))

    app._save_close_position_once()

    assert saved == [(1920 - 24, 1080 - 24)]
    assert app._last_position == (1920 - 24, 1080 - 24)


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


def test_hud_lock_allows_only_one_instance(tmp_path):
    path = tmp_path / "hud.lock"

    fd = hud.acquire_hud_lock(path)
    try:
        assert fd is not None
        assert hud.acquire_hud_lock(path) is None
    finally:
        if fd is not None:
            hud.release_hud_lock(fd)

    fd2 = hud.acquire_hud_lock(path)
    try:
        assert fd2 is not None
    finally:
        if fd2 is not None:
            hud.release_hud_lock(fd2)


def test_run_hud_exits_when_another_hud_is_running(monkeypatch):
    monkeypatch.setattr(hud, "acquire_hud_lock", lambda: None)

    def fail_app(*_args, **_kwargs):
        raise AssertionError("run_hud must not create a second HUD")

    monkeypatch.setattr(hud, "HudApp", fail_app)

    assert hud.run_hud() == 0


def test_run_hud_releases_lock(monkeypatch):
    released = []

    class FakeHudApp:
        def __init__(self, **_kwargs):
            pass

        def run(self):
            pass

    monkeypatch.setattr(hud, "acquire_hud_lock", lambda: 123)
    monkeypatch.setattr(hud, "release_hud_lock", released.append)
    monkeypatch.setattr(hud, "HudApp", FakeHudApp)

    assert hud.run_hud() == 0
    assert released == [123]


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
    monkeypatch.setattr(hud.quota_state, "probe_snapshot", lambda: {"codex": {"ok": True}})

    assert hud._load_or_probe_snapshot(cache_only=False) == {"codex": {"ok": True}}


def test_force_refresh_bypasses_cache_for_standalone_hud(monkeypatch):
    monkeypatch.setattr(
        hud.afg,
        "load_cache",
        lambda ttl: {"codex": {"primary": {"used_percent": 11}}},
    )
    monkeypatch.setattr(
        hud.quota_state,
        "probe_snapshot",
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

    monkeypatch.setattr(hud.quota_state, "probe_snapshot", fail_probe)

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


def test_hud_view_sync_uses_snapshot_loader_without_refresh(monkeypatch):
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
    snapshot = {"codex": {"primary": {"used_percent": 22}}}
    refresh_called = False

    def refresh_loader():
        nonlocal refresh_called
        refresh_called = True
        return {"codex": {"primary": {"used_percent": 55}}}

    app = hud.HudApp.__new__(hud.HudApp)
    app._fetch_lock = hud.threading.Lock()
    app._snapshot_loader = lambda: snapshot
    app._refresh_loader = refresh_loader
    app.root = FakeRoot()
    app._apply_snapshot = applied.append

    monkeypatch.setattr(hud.threading, "Thread", InlineThread)

    app._start_view_sync()

    assert applied == [snapshot]
    assert refresh_called is False


def test_standalone_hud_view_sync_prefers_shared_cache(monkeypatch):
    class InlineThread:
        def __init__(self, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self):
            self.target()

    class FakeRoot:
        def after(self, _delay, callback):
            callback()

    class FakeQuotaState:
        def current_snapshot(self):
            return {"codex": {"primary": {"used_percent": 11}}}

    cached = {
        "codex": {"primary": {"used_percent": 82}},
        "claude": {"primary": {"used_percent": 0}},
        "_from_cache": True,
    }
    applied = []

    app = hud.HudApp.__new__(hud.HudApp)
    app._fetch_lock = hud.threading.Lock()
    app._snapshot_loader = None
    app._refresh_loader = None
    app._quota_state = FakeQuotaState()
    app.root = FakeRoot()
    app._apply_snapshot = applied.append

    monkeypatch.setattr(hud.threading, "Thread", InlineThread)
    monkeypatch.setattr(hud.quota_state, "cached_snapshot", lambda: cached)

    app._start_view_sync()

    assert applied == [cached]


def test_tray_launched_hud_does_not_create_standalone_quota_service(monkeypatch):
    def fail_service(**_kwargs):
        raise AssertionError("tray-launched HUD must use tray loaders, not own state")

    monkeypatch.setattr(hud.quota_state, "QuotaStateService", fail_service)

    app = hud.HudApp(
        interval=999,
        cache_only=True,
        snapshot_loader=lambda: {},
        refresh_loader=lambda: {},
    )
    try:
        assert app._quota_state is None
    finally:
        app.root.destroy()


def test_hud_loaders_must_be_provided_together():
    try:
        app = hud.HudApp(interval=999, snapshot_loader=lambda: {})
    except ValueError as exc:
        assert "snapshot_loader and refresh_loader" in str(exc)
    else:
        app.root.destroy()
        raise AssertionError("partial loader configuration must fail")


def test_hud_close_request_is_consumed(tmp_path):
    path = tmp_path / "close.json"

    hud.request_hud_close(path)

    assert hud.consume_hud_close_request(time.time() - 10, path) is True
    assert not path.exists()


def test_stale_hud_close_request_is_ignored(tmp_path):
    path = tmp_path / "close.json"
    path.write_text(
        json.dumps({"command": "quit", "requested_at": time.time() - 60}),
        encoding="utf-8",
    )

    assert hud.consume_hud_close_request(time.time(), path) is False
    assert not path.exists()


def test_hud_command_poll_quits_on_close_request(monkeypatch, tmp_path):
    path = tmp_path / "close.json"
    path.write_text(
        json.dumps({"command": "quit", "requested_at": time.time()}),
        encoding="utf-8",
    )

    class FakeRoot:
        def after(self, *_args):
            raise AssertionError("quit request should not schedule another poll")

    app = hud.HudApp.__new__(hud.HudApp)
    app._started_at = time.time() - 1
    app.root = FakeRoot()
    quit_called = []
    app.quit = lambda: quit_called.append(True)
    consume_hud_close_request = hud.consume_hud_close_request

    monkeypatch.setattr(
        hud,
        "consume_hud_close_request",
        lambda started_at: consume_hud_close_request(started_at, path),
    )

    app._schedule_command_poll()

    assert quit_called == [True]
