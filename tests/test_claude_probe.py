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
        """`HTTPError` wraps a file-like body too. Without `close()` we
        leak fds during 4xx storms — exactly the 429 rate-limit
        scenario we're trying to survive."""
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
    """Refresh logic now uses token-fingerprint detection instead of trusting
    subprocess returncode. Tests must simulate credential rewrites — that's
    what `claude auth status` actually does on a real refresh."""

    @staticmethod
    def _rewrite_creds(creds_path, new_token):
        creds_path.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": new_token,
                "refreshToken": "sk-ant-test-refresh-xyz",
                "expiresAt": 9999999999000,
                "scopes": ["user:inference"],
                "subscriptionType": "max",
            }
        }))

    def test_401_with_refresh_rewriting_creds_then_retries(self, valid_creds_file, valid_usage_json):
        """First urlopen raises 401; refresh rewrites creds (token fp changes);
        second urlopen returns data. New contract: token actually changing
        triggers retry, not subprocess exit==0."""
        call_count = {"n": 0}
        creds_path = valid_creds_file

        def fake_urlopen(req, timeout=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _mk_http_error(401)
            return FakeResponse(valid_usage_json)

        def fake_refresh():
            self._rewrite_creds(creds_path, "sk-ant-test-NEW-token")
            return True

        with patch("ai_fuelgauge.urllib.request.urlopen", side_effect=fake_urlopen):
            with patch(
                "ai_fuelgauge._trigger_claude_auth_refresh", side_effect=fake_refresh
            ) as mock_refresh:
                result = probe_claude_quota()

        assert mock_refresh.call_count == 1
        assert call_count["n"] == 2
        assert result["status"] == 200
        assert result["primary"]["used_percent"] == 42.5

    def test_401_subprocess_ok_but_token_unchanged_returns_401(self, valid_creds_file):
        """New corner case: `claude auth status` exits 0 but doesn't rewrite
        creds (passive check, network fail, etc.). Old code wrongly retried
        with the same dead token; new code detects the unchanged fingerprint
        and surfaces 401 + a marker explaining auto-refresh was tried."""
        def fake_urlopen(req, timeout=None):
            raise _mk_http_error(401)

        with patch("ai_fuelgauge.urllib.request.urlopen", side_effect=fake_urlopen):
            with patch("ai_fuelgauge._trigger_claude_auth_refresh", return_value=True):
                result = probe_claude_quota()

        assert result["status"] == 401
        assert result.get("_refresh_attempted") is True
        assert result.get("_refresh_subprocess_ok") is True

    def test_401_with_failed_refresh_returns_401(self, valid_creds_file):
        """Refresh subprocess returns False AND token didn't change."""
        def fake_urlopen(req, timeout=None):
            raise _mk_http_error(401)

        with patch("ai_fuelgauge.urllib.request.urlopen", side_effect=fake_urlopen):
            with patch("ai_fuelgauge._trigger_claude_auth_refresh", return_value=False):
                result = probe_claude_quota()

        assert result["status"] == 401
        assert result.get("_refresh_attempted") is True
        assert result.get("_refresh_subprocess_ok") is False

    def test_401_skip_spawn_when_other_process_already_refreshed(self, valid_creds_file, valid_usage_json):
        """Race A: another process refreshed credentials between our request
        being sent and the 401 coming back. We re-read the token, see it
        already changed, retry directly WITHOUT spawning `claude auth status`."""
        call_count = {"n": 0}
        creds_path = valid_creds_file

        def fake_urlopen(req, timeout=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Simulate other process having refreshed AFTER our request
                # was sent but BEFORE we processed the 401.
                self._rewrite_creds(creds_path, "sk-ant-test-RACE-WIN-token")
                raise _mk_http_error(401)
            return FakeResponse(valid_usage_json)

        with patch("ai_fuelgauge.urllib.request.urlopen", side_effect=fake_urlopen):
            with patch("ai_fuelgauge._trigger_claude_auth_refresh") as mock_refresh:
                result = probe_claude_quota()

        # Critical: refresh subprocess must NOT have been spawned.
        assert mock_refresh.call_count == 0
        assert call_count["n"] == 2
        assert result["status"] == 200

    def test_401_with_env_token_skips_refresh(self, valid_creds_file, monkeypatch):
        """When token came from $CLAUDE_CODE_OAUTH_TOKEN, refresh is impossible
        (env vars are static), so the spawn must be skipped. Result carries
        `_env_token_mode` so UI can give the right 'replace your env var'
        message instead of the generic 'run claude' hint."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-env-token-xyz")

        def fake_urlopen(req, timeout=None):
            raise _mk_http_error(401)

        with patch("ai_fuelgauge.urllib.request.urlopen", side_effect=fake_urlopen):
            with patch("ai_fuelgauge._trigger_claude_auth_refresh") as mock_refresh:
                result = probe_claude_quota()

        assert mock_refresh.call_count == 0
        assert result["status"] == 401
        assert result.get("_env_token_mode") is True

    def test_401_refresh_does_not_recurse(self, valid_creds_file):
        """Recursion guard: even if refresh changes token to one that ALSO
        gets 401, we only retry once — never spawn refresh on the retry."""
        refresh_count = {"n": 0}
        creds_path = valid_creds_file

        def fake_urlopen(req, timeout=None):
            raise _mk_http_error(401)

        def counting_refresh():
            refresh_count["n"] += 1
            # Each refresh actually changes the token (so retry is triggered),
            # but the new token also 401s. The recursion guard must still
            # cap total refresh attempts at 1.
            self._rewrite_creds(creds_path, f"sk-ant-test-NEW-{refresh_count['n']}")
            return True

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
