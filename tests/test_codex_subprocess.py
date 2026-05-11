"""Tests for Codex app-server subprocess lifecycle.

Ensures the terminate → wait → kill cleanup sequence prevents zombie
processes on Unix / hung child processes on any platform.
"""
import subprocess
from unittest.mock import MagicMock, patch

import ai_fuelgauge


def _make_fake_proc(wait_side_effect):
    """Produce a fake Popen whose reader threads exit instantly and
    whose wait() behaves per `wait_side_effect`."""
    fake = MagicMock()
    # Reader threads iterate via `for line in iter(stream.readline, b"")`
    # — returning b"" makes the iter stop immediately so threads die.
    fake.stdout.readline.return_value = b""
    fake.stderr.readline.return_value = b""
    fake.stdin = MagicMock()
    fake.poll.return_value = None  # never reported as exited while loop runs
    fake.terminate = MagicMock()
    fake.kill = MagicMock()
    fake.wait = MagicMock(side_effect=wait_side_effect)
    return fake


class TestCodexSubprocessCleanup:
    def test_cooperative_shutdown_no_kill(self):
        """Normal path: terminate() is respected, wait() returns promptly,
        kill() is never needed."""
        fake_proc = _make_fake_proc(wait_side_effect=[None])

        with patch("ai_fuelgauge._find_codex_bin", return_value="/fake/codex"):
            with patch("ai_fuelgauge.subprocess.Popen", return_value=fake_proc):
                with patch("ai_fuelgauge.CODEX_APP_SERVER_TIMEOUT", 0.1):
                    ai_fuelgauge.probe_codex_fresh()

        fake_proc.terminate.assert_called_once()
        fake_proc.wait.assert_called_once_with(timeout=1)
        fake_proc.kill.assert_not_called()

    def test_hung_child_gets_killed(self):
        """M5 regression: child ignores terminate() → wait() TimeoutExpired
        → kill() is called so we don't leak a zombie."""
        fake_proc = _make_fake_proc(
            wait_side_effect=[
                subprocess.TimeoutExpired("codex", 1),  # after terminate
                None,  # after kill
            ]
        )

        with patch("ai_fuelgauge._find_codex_bin", return_value="/fake/codex"):
            with patch("ai_fuelgauge.subprocess.Popen", return_value=fake_proc):
                with patch("ai_fuelgauge.CODEX_APP_SERVER_TIMEOUT", 0.1):
                    ai_fuelgauge.probe_codex_fresh()

        fake_proc.terminate.assert_called_once()
        fake_proc.kill.assert_called_once()
        # wait is called twice — once after terminate, once after kill
        assert fake_proc.wait.call_count == 2

    def test_truly_stuck_child_does_not_raise(self):
        """Worst case: both terminate and kill fail to unblock the child.
        The probe must still return gracefully — a stuck child shouldn't
        propagate an exception into the caller (CLI or tray fetch thread)."""
        fake_proc = _make_fake_proc(
            wait_side_effect=[
                subprocess.TimeoutExpired("codex", 1),
                subprocess.TimeoutExpired("codex", 1),
            ]
        )

        with patch("ai_fuelgauge._find_codex_bin", return_value="/fake/codex"):
            with patch("ai_fuelgauge.subprocess.Popen", return_value=fake_proc):
                with patch("ai_fuelgauge.CODEX_APP_SERVER_TIMEOUT", 0.1):
                    result = ai_fuelgauge.probe_codex_fresh()

        fake_proc.terminate.assert_called_once()
        fake_proc.kill.assert_called_once()
        assert result is not None  # returned a dict, didn't raise


class TestCodexBinarySelection:
    def test_windows_prefers_exe_over_cmd_shim(self, monkeypatch):
        monkeypatch.setattr(ai_fuelgauge.sys, "platform", "win32")

        def fake_which(name):
            return {
                "codex.exe": r"C:\Codex\codex.exe",
                "codex.cmd": r"C:\Node\codex.cmd",
                "codex": r"C:\Node\codex.CMD",
            }.get(name)

        monkeypatch.setattr(ai_fuelgauge.shutil, "which", fake_which)

        assert ai_fuelgauge._find_codex_bin() == r"C:\Codex\codex.exe"

    def test_windows_falls_back_to_cmd_when_exe_missing(self, monkeypatch):
        monkeypatch.setattr(ai_fuelgauge.sys, "platform", "win32")

        def fake_which(name):
            return {"codex.cmd": r"C:\Node\codex.cmd"}.get(name)

        monkeypatch.setattr(ai_fuelgauge.shutil, "which", fake_which)

        assert ai_fuelgauge._find_codex_bin() == r"C:\Node\codex.cmd"

    def test_non_windows_prefers_plain_codex(self, monkeypatch):
        monkeypatch.setattr(ai_fuelgauge.sys, "platform", "linux")

        def fake_which(name):
            return {
                "codex": "/usr/local/bin/codex",
                "codex.exe": "/unexpected/codex.exe",
            }.get(name)

        monkeypatch.setattr(ai_fuelgauge.shutil, "which", fake_which)

        assert ai_fuelgauge._find_codex_bin() == "/usr/local/bin/codex"


class TestHiddenSubprocessStartupKwargs:
    def test_non_windows_returns_empty_kwargs(self, monkeypatch):
        monkeypatch.setattr(ai_fuelgauge.sys, "platform", "linux")

        assert ai_fuelgauge._hidden_subprocess_startup_kwargs() == {}

    def test_windows_includes_create_no_window(self, monkeypatch):
        monkeypatch.setattr(ai_fuelgauge.sys, "platform", "win32")

        kwargs = ai_fuelgauge._hidden_subprocess_startup_kwargs()

        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        if hasattr(subprocess, "STARTUPINFO"):
            assert "startupinfo" in kwargs
