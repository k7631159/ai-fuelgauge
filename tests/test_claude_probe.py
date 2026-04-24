"""Tests for probe_claude_quota — the /api/oauth/usage consumer."""
import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from ai_fuelgauge import probe_claude_quota


class FakeResponse:
    """Minimal fake of `urllib.request.urlopen`'s return value.

    Supports context-manager protocol so the M7 fix (`with urlopen(...)`)
    works under test. The `closed` attribute records whether __exit__ ran,
    which a M7 regression test asserts.
    """

    def __init__(self, body, status=200):
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self.closed = False

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.closed = True
        return False


def _mk_http_error(code, url="https://api.anthropic.com/api/oauth/usage"):
    err = urllib.error.HTTPError(url, code, "HTTP error", {}, io.BytesIO(b""))
    err.read = lambda: b""  # type: ignore[method-assign]
    return err


@pytest.fixture
def valid_usage_json():
    return json.dumps(
        {
            "five_hour": {
                "utilization": 42.5,
                "resets_at": "2026-05-01T10:00:00+00:00",
            },
            "seven_day": {
                "utilization": 15.0,
                "resets_at": "2026-05-05T10:00:00+00:00",
            },
            "seven_day_sonnet": {"utilization": 0.0, "resets_at": None},
            "extra_usage": {"is_enabled": False},
        }
    )


class TestProbeClaudeQuotaHappyPath:
    def test_parses_valid_response(self, valid_creds_file, valid_usage_json):
        with patch("ai_fuelgauge.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = FakeResponse(valid_usage_json)
            result = probe_claude_quota()

        assert result["status"] == 200
        assert result["primary"]["used_percent"] == 42.5
        assert result["primary"]["window_minutes"] == 300
        assert result["secondary"]["used_percent"] == 15.0
        assert result["secondary"]["window_minutes"] == 10080
        assert result["primary"]["reset_at"] is not None
        assert result["primary"]["reset_in_seconds"] is not None

    def test_debug_mode_includes_parsed_body(self, valid_creds_file, valid_usage_json):
        with patch("ai_fuelgauge.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = FakeResponse(valid_usage_json)
            result = probe_claude_quota(debug=True)

        assert "response_body" in result
        assert isinstance(result["response_body"], dict)
        assert result["response_body"]["five_hour"]["utilization"] == 42.5

    def test_response_closed_via_context_manager(self, valid_creds_file, valid_usage_json):
        """Regression M7: the urlopen response was leaking file descriptors
        because it was never explicitly closed. Now wrapped in `with` — this
        test verifies __exit__ ran after probe completed."""
        fake = FakeResponse(valid_usage_json)
        with patch("ai_fuelgauge.urllib.request.urlopen", return_value=fake):
            probe_claude_quota()

        assert fake.closed is True, "urlopen response must be closed via context manager"

    def test_http_error_body_is_closed(self, valid_creds_file):
        """M7 extension (caught by Codex pre-commit review): HTTPError wraps
        a file-like body too. Without close() we leak fds during 4xx storms —
        exactly the 429 rate-limit scenario we're trying to survive."""
        close_counter = {"calls": 0}

        def fake_urlopen(req, timeout=None):
            err = urllib.error.HTTPError(
                req.full_url, 429, "Rate limited", {}, io.BytesIO(b"{}")
            )
            err.read = lambda: b"{}"
            original_close = err.close

            def tracked_close():
                close_counter["calls"] += 1
                original_close()

            err.close = tracked_close
            raise err

        with patch("ai_fuelgauge.urllib.request.urlopen", side_effect=fake_urlopen):
            # Disable auto-refresh recursion to keep this focused on close().
            probe_claude_quota(_allow_refresh=False)

        assert close_counter["calls"] == 1, "HTTPError body must be closed exactly once"


class TestProbeClaudeQuotaErrors:
    def test_no_token_returns_error(self, isolated_paths):
        result = probe_claude_quota()
        assert result == {"error": "no-token-found"}

    def test_network_error_returns_error_dict(self, valid_creds_file):
        with patch("ai_fuelgauge.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = ConnectionError("network dead")
            result = probe_claude_quota()

        assert "error" in result
        assert "probe-failed" in result["error"]

    def test_invalid_json_body_returns_status_only(self, valid_creds_file):
        with patch("ai_fuelgauge.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = FakeResponse("not json at all")
            result = probe_claude_quota()

        assert result["status"] == 200
        # No primary/secondary parsed (body wasn't JSON), but status is surfaced
        assert "primary" not in result
        assert "secondary" not in result


class TestProbeClaudeQuotaAuthRefresh:
    def test_401_triggers_refresh_then_retries(self, valid_creds_file, valid_usage_json):
        """First urlopen raises 401; refresh succeeds; second urlopen returns data."""
        call_count = {"n": 0}

        def fake_urlopen(req, timeout=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _mk_http_error(401)
            return FakeResponse(valid_usage_json)

        with patch("ai_fuelgauge.urllib.request.urlopen", side_effect=fake_urlopen):
            with patch(
                "ai_fuelgauge._trigger_claude_auth_refresh", return_value=True
            ) as mock_refresh:
                result = probe_claude_quota()

        assert mock_refresh.call_count == 1
        assert call_count["n"] == 2
        assert result["status"] == 200
        assert result["primary"]["used_percent"] == 42.5

    def test_401_with_failed_refresh_returns_401(self, valid_creds_file):
        """If refresh delegation returns False, do not retry; surface the 401."""
        def fake_urlopen(req, timeout=None):
            raise _mk_http_error(401)

        with patch("ai_fuelgauge.urllib.request.urlopen", side_effect=fake_urlopen):
            with patch(
                "ai_fuelgauge._trigger_claude_auth_refresh", return_value=False
            ):
                result = probe_claude_quota()

        assert result["status"] == 401

    def test_401_refresh_does_not_recurse(self, valid_creds_file):
        """The recursion guard (_allow_refresh=False on retry) must cap refresh at 1."""
        refresh_count = {"n": 0}

        def fake_urlopen(req, timeout=None):
            raise _mk_http_error(401)

        def counting_refresh():
            refresh_count["n"] += 1
            return True  # pretend refresh always succeeds — retry should still cap

        with patch("ai_fuelgauge.urllib.request.urlopen", side_effect=fake_urlopen):
            with patch(
                "ai_fuelgauge._trigger_claude_auth_refresh",
                side_effect=counting_refresh,
            ):
                result = probe_claude_quota()

        assert refresh_count["n"] == 1
        assert result["status"] == 401

    def test_non_401_error_does_not_trigger_refresh(self, valid_creds_file):
        """500 / 429 / other HTTP errors should NOT call refresh — it's 401-specific."""
        def fake_urlopen(req, timeout=None):
            raise _mk_http_error(500)

        with patch("ai_fuelgauge.urllib.request.urlopen", side_effect=fake_urlopen):
            with patch(
                "ai_fuelgauge._trigger_claude_auth_refresh", return_value=True
            ) as mock_refresh:
                result = probe_claude_quota()

        assert mock_refresh.call_count == 0
        assert result["status"] == 500
