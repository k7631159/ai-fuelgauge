"""Tests for TrayApp._do_fetch exception handling.

The previous implementation placed `_apply_to_icon()` in a `try/else`
branch outside the exception handler. If Pillow or pystray raised during
the icon update, the exception propagated out of the poller / refresh
thread. Under the detached `pythonw.exe` tray, stderr is DEVNULL, so
the user saw no signal and the tray showed stale data forever.

The fix: widen the `try` to cover all of `_snapshot` + thresholds +
apply-to-icon, so any error is logged and the fetch lock is always
released cleanly.
"""
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("pystray")
pytest.importorskip("PIL")

from tray import TrayApp


class TestDoFetchExceptionHandling:
    def test_apply_to_icon_raising_does_not_propagate(self):
        """Regression H3: Pillow/pystray error in _apply_to_icon must NOT
        kill the poller thread — it must be caught so the next poll can
        try again."""
        app = TrayApp(interval=300)
        snap = {
            "codex": {"primary": {"used_percent": 50}, "secondary": None},
            "claude": None,
        }

        with patch("tray._snapshot", return_value=snap):
            with patch.object(
                app,
                "_apply_to_icon",
                side_effect=RuntimeError("pystray backend hiccup"),
            ):
                # Should not raise
                app._do_fetch()

    def test_apply_to_icon_raising_releases_lock(self):
        """Regression H3: the fetch lock must be released even when a
        downstream step after _snapshot raises."""
        app = TrayApp(interval=300)
        snap = {"codex": {"primary": {"used_percent": 1}}, "claude": None}

        with patch("tray._snapshot", return_value=snap):
            with patch.object(
                app,
                "_apply_to_icon",
                side_effect=OSError("pillow load failed"),
            ):
                app._do_fetch()

        # A second _do_fetch must be able to acquire the lock — proving
        # the previous invocation released it.
        assert app._fetch_lock.acquire(blocking=False) is True
        app._fetch_lock.release()

    def test_snapshot_raising_still_caught(self):
        """Preserve the pre-existing behavior: _snapshot() raising is caught
        and logged. (Regression guard for the H3 fix not breaking the
        existing path.)"""
        app = TrayApp(interval=300)

        with patch(
            "tray._snapshot", side_effect=ConnectionError("network dead")
        ):
            app._do_fetch()  # must not raise

        assert app._fetch_lock.acquire(blocking=False) is True
        app._fetch_lock.release()

    def test_snapshot_raised_does_not_overwrite_snapshot(self):
        """If _snapshot raises, the old self.snapshot must remain intact —
        we don't want transient network blips to wipe good data."""
        app = TrayApp(interval=300)
        app.snapshot = {"codex": {"primary": {"used_percent": 42}}}

        with patch("tray._snapshot", side_effect=RuntimeError("kaboom")):
            app._do_fetch()

        # Old snapshot still there
        assert app.snapshot == {"codex": {"primary": {"used_percent": 42}}}

    def test_three_consecutive_apply_failures_do_not_deadlock(self):
        """Cumulative test: lock is released on each failure so the next
        call can still acquire."""
        app = TrayApp(interval=300)
        snap = {"codex": {"primary": {"used_percent": 10}}, "claude": None}

        with patch("tray._snapshot", return_value=snap):
            with patch.object(
                app, "_apply_to_icon", side_effect=RuntimeError("boom")
            ):
                for _ in range(3):
                    app._do_fetch()

        # Still acquirable
        assert app._fetch_lock.acquire(blocking=False) is True
        app._fetch_lock.release()

    def test_successful_fetch_still_works(self):
        """Sanity: happy path still assigns snapshot and calls apply."""
        app = TrayApp(interval=300)
        snap = {"codex": {"primary": {"used_percent": 25}}, "claude": None}

        apply_calls = {"count": 0}

        def fake_apply():
            apply_calls["count"] += 1

        with patch("tray._snapshot", return_value=snap):
            with patch.object(app, "_apply_to_icon", side_effect=fake_apply):
                app._do_fetch()

        assert app.snapshot == snap
        assert apply_calls["count"] == 1

    def test_snapshot_writes_shared_cache_for_hud(self):
        codex = {"primary": {"used_percent": 25}}
        claude = {"primary": {"used_percent": 35}}

        with patch("tray.afg.probe_codex_fresh", return_value=codex):
            with patch("tray.afg.probe_claude_quota", return_value=claude):
                with patch("tray.afg._save_last_good_claude"):
                    with patch("tray.afg.save_cache") as save_cache:
                        snap = __import__("tray")._snapshot()

        assert snap == {"codex": codex, "claude": claude}
        save_cache.assert_called_once_with(snap)

    def test_threshold_notification_names_codex_provider(self):
        app = TrayApp(interval=300)
        snap = {
            "codex": {"primary": {"used_percent": 82}},
            "claude": {"primary": {"used_percent": 17}},
        }

        with patch("tray._notify") as notify:
            app._check_thresholds(snap)

        notify.assert_called_once_with("Codex quota warning", "5-hour window at 82%")

    def test_threshold_notification_names_claude_provider(self):
        app = TrayApp(interval=300)
        snap = {
            "codex": {"secondary": {"used_percent": 30}},
            "claude": {"secondary": {"used_percent": 91}},
        }

        with patch("tray._notify") as notify:
            app._check_thresholds(snap)

        notify.assert_called_once_with("Claude quota warning", "Weekly window at 91%")

    def test_threshold_notification_names_both_providers_on_tie(self):
        app = TrayApp(interval=300)
        snap = {
            "codex": {"primary": {"used_percent": 80}},
            "claude": {"primary": {"used_percent": 80}},
        }

        with patch("tray._notify") as notify:
            app._check_thresholds(snap)

        notify.assert_called_once_with(
            "Codex + Claude quota warning",
            "5-hour window at 80%",
        )

    def test_stderr_none_does_not_crash_on_error(self, monkeypatch):
        """Regression: under `pythonw.exe`, `sys.stderr` can be None.
        The except branch's `stderr.write(None)` would itself raise
        AttributeError and kill the thread — defeating the purpose."""
        import sys as _sys

        monkeypatch.setattr(_sys, "stderr", None)

        app = TrayApp(interval=300)
        snap = {"codex": {"primary": {"used_percent": 10}}, "claude": None}

        with patch("tray._snapshot", return_value=snap):
            with patch.object(
                app, "_apply_to_icon", side_effect=RuntimeError("boom")
            ):
                # Must not raise — stderr is None AND apply_to_icon errors
                app._do_fetch()

        assert app._fetch_lock.acquire(blocking=False) is True
        app._fetch_lock.release()

    def test_hud_label_reflects_process_state(self):
        app = TrayApp(interval=300)

        assert app._hud_label(None) == "Show HUD"

        class RunningThread:
            def is_alive(self):
                return True

        app._hud_thread = RunningThread()
        assert app._hud_label(None) == "Hide HUD"

    def test_start_hud_runs_in_process_thread(self):
        app = TrayApp(interval=123)

        class FakeThread:
            def __init__(self, target, daemon):
                self.target = target
                self.daemon = daemon
                self.started = False

            def is_alive(self):
                return False

            def start(self):
                self.started = True

        created = []

        def make_thread(*args, **kwargs):
            thread = FakeThread(*args, **kwargs)
            created.append(thread)
            return thread

        with patch("tray.threading.Thread", side_effect=make_thread):
            app._start_hud()

        assert created
        assert created[0].target == app._run_hud
        assert created[0].daemon is True
        assert created[0].started is True
        assert app._hud_thread is created[0]

    def test_stop_hud_schedules_in_process_window_quit(self):
        app = TrayApp(interval=300)
        root = MagicMock()
        fake_app = MagicMock(root=root, quit=MagicMock())
        app._hud_app = fake_app

        app._stop_hud()

        root.after.assert_called_once_with(0, fake_app.quit)

    def test_stop_hud_before_window_exists_sets_pending_stop(self):
        app = TrayApp(interval=300)

        class StartingThread:
            def is_alive(self):
                return True

        app._hud_thread = StartingThread()
        app._hud_app = None

        app._stop_hud()

        assert app._hud_stop_requested is True

    def test_snapshot_for_hud_uses_tray_snapshot_before_cache(self):
        app = TrayApp(interval=300)
        app.snapshot = {"codex": {"primary": {"used_percent": 22}}}

        with patch("tray.afg.load_cache") as load_cache:
            result = app._snapshot_for_hud()

        assert result == app.snapshot
        assert result is not app.snapshot

        load_cache.assert_not_called()

    def test_snapshot_for_hud_falls_back_to_shared_cache(self):
        app = TrayApp(interval=300)
        cache = {"codex": {"primary": {"used_percent": 33}}}

        with patch("tray.afg.load_cache", return_value=cache):
            assert app._snapshot_for_hud() == {
                "codex": {"primary": {"used_percent": 33}},
                "_from_cache": True,
            }

    def test_refresh_snapshot_for_hud_triggers_tray_fetch(self):
        app = TrayApp(interval=300)
        snap = {"codex": {"primary": {"used_percent": 44}}}

        def fake_fetch(**_kwargs):
            app.snapshot = snap

        with patch.object(app, "_do_fetch", side_effect=fake_fetch) as do_fetch:
            assert app._refresh_snapshot_for_hud() == snap

        do_fetch.assert_called_once_with(blocking=True)

    def test_run_hud_passes_tray_snapshot_loaders(self):
        app = TrayApp(interval=123)
        captured = {}

        class FakeHudApp:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.root = MagicMock()

            def run(self):
                pass

        with patch("hud.HudApp", FakeHudApp):
            app._run_hud()

        assert captured["interval"] == 123
        assert captured["cache_only"] is True
        assert captured["snapshot_loader"] == app._snapshot_for_hud
        assert captured["refresh_loader"] == app._refresh_snapshot_for_hud
