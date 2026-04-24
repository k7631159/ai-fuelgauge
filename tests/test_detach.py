"""Tests for the Windows tray auto-detach re-exec logic."""
import sys
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("pystray")
pytest.importorskip("PIL")


class TestReexecDetachedCrossPlatform:
    """Tests that run on any platform — exercise the non-Windows early returns."""

    def test_returns_false_on_darwin(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        from tray import _reexec_detached_on_windows

        assert _reexec_detached_on_windows(["s.py"]) is False

    def test_returns_false_on_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        from tray import _reexec_detached_on_windows

        assert _reexec_detached_on_windows(["s.py"]) is False


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

        from tray import _reexec_detached_on_windows

        assert _reexec_detached_on_windows(["s.py"]) is False

    def test_skips_when_executable_is_frozen(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "win32")
        fake_frozen = tmp_path / "my_app.exe"
        fake_frozen.write_text("")
        monkeypatch.setattr(sys, "executable", str(fake_frozen))

        from tray import _reexec_detached_on_windows

        assert _reexec_detached_on_windows(["s.py"]) is False

    def test_skips_when_pythonw_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "win32")
        fake_python = tmp_path / "python.exe"
        fake_python.write_text("")
        # pythonw.exe is intentionally not created
        monkeypatch.setattr(sys, "executable", str(fake_python))

        from tray import _reexec_detached_on_windows

        assert _reexec_detached_on_windows(["s.py"]) is False

    def test_spawns_pythonw_when_all_conditions_met(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "win32")
        fake_python = tmp_path / "python.exe"
        fake_pythonw = tmp_path / "pythonw.exe"
        fake_python.write_text("")
        fake_pythonw.write_text("")
        monkeypatch.setattr(sys, "executable", str(fake_python))

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            from tray import _reexec_detached_on_windows

            result = _reexec_detached_on_windows(["s.py", "--tray"])

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
            from tray import _reexec_detached_on_windows

            assert _reexec_detached_on_windows(["s.py"]) is False
