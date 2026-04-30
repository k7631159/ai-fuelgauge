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
from unittest.mock import patch

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
