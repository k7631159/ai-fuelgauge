"""Tests for the Windows GUI auto-detach re-exec logic."""
import sys
from unittest.mock import MagicMock, patch

import pytest


class TestHudDetachEntry:
    def test_hud_reexecs_detached_by_default(self, monkeypatch):
        import ai_fuelgauge as afg

        monkeypatch.setattr(sys, "argv", ["ai_fuelgauge.py", "--hud"])
        with patch("windows_detach.reexec_detached_on_windows", return_value=True) as reexec:
            assert afg.main(["--hud"]) == 0

        reexec.assert_called_once_with(["ai_fuelgauge.py", "--hud"])

    def test_hud_no_detach_runs_in_foreground(self):
        import ai_fuelgauge as afg

        with patch("windows_detach.reexec_detached_on_windows") as reexec:
            with patch("hud.run_hud", return_value=0) as run_hud:
                assert afg.main(["--hud", "--no-detach"]) == 0

        reexec.assert_not_called()
        run_hud.assert_called_once_with(interval=300)

    def test_hud_runs_foreground_when_reexec_not_needed(self, monkeypatch):
        import ai_fuelgauge as afg

        monkeypatch.setattr(sys, "argv", ["ai_fuelgauge.py", "--hud"])
        with patch("windows_detach.reexec_detached_on_windows", return_value=False) as reexec:
            with patch("hud.run_hud", return_value=0) as run_hud:
                assert afg.main(["--hud"]) == 0

        reexec.assert_called_once_with(["ai_fuelgauge.py", "--hud"])
        run_hud.assert_called_once_with(interval=300)


class TestReexecDetachedCrossPlatform:
    """Tests that run on any platform — exercise the non-Windows early returns."""

    def test_returns_false_on_darwin(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        from windows_detach import reexec_detached_on_windows

        assert reexec_detached_on_windows(["s.py"]) is False

    def test_returns_false_on_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        from windows_detach import reexec_detached_on_windows

        assert reexec_detached_on_windows(["s.py"]) is False


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-specific branch references subprocess.DETACHED_PROCESS",
)
class TestReexecDetachedOnWindows:
    def test_skips_when_already_pythonw(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "win32")
        fake_pythonw = tmp_path / "pythonw.exe"
        fake_pythonw.write_text("")
        monkeypatch.setattr(sys, "executable", str(fake_pythonw))

        from windows_detach import reexec_detached_on_windows

        assert reexec_detached_on_windows(["s.py"]) is False

    def test_skips_when_executable_is_frozen(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "win32")
        fake_frozen = tmp_path / "my_app.exe"
        fake_frozen.write_text("")
        monkeypatch.setattr(sys, "executable", str(fake_frozen))

        from windows_detach import reexec_detached_on_windows

        assert reexec_detached_on_windows(["s.py"]) is False

    def test_skips_when_pythonw_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "win32")
        fake_python = tmp_path / "python.exe"
        fake_python.write_text("")
        # pythonw.exe is intentionally not created
        monkeypatch.setattr(sys, "executable", str(fake_python))

        from windows_detach import reexec_detached_on_windows

        assert reexec_detached_on_windows(["s.py"]) is False

    def test_spawns_pythonw_when_all_conditions_met(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "win32")
        fake_python = tmp_path / "python.exe"
        fake_pythonw = tmp_path / "pythonw.exe"
        fake_python.write_text("")
        fake_pythonw.write_text("")
        monkeypatch.setattr(sys, "executable", str(fake_python))

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            from windows_detach import reexec_detached_on_windows

            result = reexec_detached_on_windows(["s.py", "--tray"])

        assert result is True
        mock_popen.assert_called_once()
        spawned_argv = mock_popen.call_args.args[0]
        assert str(fake_pythonw) in spawned_argv[0]
        assert "s.py" in spawned_argv
        assert "--tray" in spawned_argv

    def test_returns_false_on_popen_exception(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "win32")
        fake_python = tmp_path / "python.exe"
        fake_pythonw = tmp_path / "pythonw.exe"
        fake_python.write_text("")
        fake_pythonw.write_text("")
        monkeypatch.setattr(sys, "executable", str(fake_python))

        with patch("subprocess.Popen", side_effect=OSError("spawn failed")):
            from windows_detach import reexec_detached_on_windows

            assert reexec_detached_on_windows(["s.py"]) is False
