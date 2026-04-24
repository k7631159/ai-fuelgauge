"""Tests for the claude-auth-status based OAuth refresh delegation."""
import subprocess
from unittest.mock import MagicMock, patch

from ai_fuelgauge import _trigger_claude_auth_refresh


class TestTriggerClaudeAuthRefresh:
    def test_returns_false_if_claude_bin_missing(self):
        with patch("ai_fuelgauge.shutil.which", return_value=None):
            assert _trigger_claude_auth_refresh() is False

    def test_returns_true_on_exit_zero(self):
        fake_result = MagicMock(returncode=0)
        with patch("ai_fuelgauge.shutil.which", return_value="/fake/claude"):
            with patch(
                "ai_fuelgauge.subprocess.run", return_value=fake_result
            ) as mock_run:
                assert _trigger_claude_auth_refresh() is True
        # Sanity: we invoked `<claude> auth status`, not `-p` or anything else
        args = mock_run.call_args.args[0]
        assert args == ["/fake/claude", "auth", "status"]

    def test_returns_false_on_nonzero_exit(self):
        fake_result = MagicMock(returncode=1)
        with patch("ai_fuelgauge.shutil.which", return_value="/fake/claude"):
            with patch(
                "ai_fuelgauge.subprocess.run", return_value=fake_result
            ):
                assert _trigger_claude_auth_refresh() is False

    def test_returns_false_on_timeout(self):
        with patch("ai_fuelgauge.shutil.which", return_value="/fake/claude"):
            with patch(
                "ai_fuelgauge.subprocess.run",
                side_effect=subprocess.TimeoutExpired("claude", 10),
            ):
                assert _trigger_claude_auth_refresh() is False

    def test_returns_false_on_oserror(self):
        with patch("ai_fuelgauge.shutil.which", return_value="/fake/claude"):
            with patch("ai_fuelgauge.subprocess.run", side_effect=OSError("boom")):
                assert _trigger_claude_auth_refresh() is False

    def test_uses_devnull_for_io(self):
        """The subprocess should not leak stdout/stderr into the caller's terminal."""
        fake_result = MagicMock(returncode=0)
        with patch("ai_fuelgauge.shutil.which", return_value="/fake/claude"):
            with patch(
                "ai_fuelgauge.subprocess.run", return_value=fake_result
            ) as mock_run:
                _trigger_claude_auth_refresh()
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("stdin") == subprocess.DEVNULL
        assert kwargs.get("stdout") == subprocess.DEVNULL
        assert kwargs.get("stderr") == subprocess.DEVNULL
