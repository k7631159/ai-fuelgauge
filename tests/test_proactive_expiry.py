"""Tests for the proactive token expiry check (commit 3 — A + L).

Root-cause defense: never send an expired Bearer to /api/oauth/usage,
because Cloudflare treats expired-bearer requests as abuse and locks
the IP for ~30 min. Local `expiresAt` reads avoid that entirely.
"""
import json
from unittest.mock import patch

import pytest

import ai_fuelgauge as afg
from ai_fuelgauge import (
    CLAUDE_TOKEN_EXPIRY_BUFFER_SECONDS,
    _extract_expires_at_from_cred_json,
    _is_token_expired_or_expiring,
    probe_claude_quota,
    read_claude_creds_with_meta,
)


class TestExtractExpiresAt:
    def test_finds_nested_expires_at(self):
        data = {"claudeAiOauth": {"accessToken": "x", "expiresAt": 1234567890000}}
        assert _extract_expires_at_from_cred_json(data) == 1234567890000

    def test_finds_top_level_expires_at(self):
        assert _extract_expires_at_from_cred_json({"expiresAt": 999}) == 999

    def test_accepts_snake_case(self):
        assert _extract_expires_at_from_cred_json({"expires_at": 555}) == 555

    def test_missing_returns_none(self):
        assert _extract_expires_at_from_cred_json({"claudeAiOauth": {"accessToken": "x"}}) is None

    def test_non_numeric_returns_none(self):
        # Server bug or hand-edit could put a string here — must not crash.
        assert _extract_expires_at_from_cred_json({"expiresAt": "tomorrow"}) is None

    def test_float_coerced_to_int(self):
        assert _extract_expires_at_from_cred_json({"expiresAt": 12345.7}) == 12345


class TestIsTokenExpiredOrExpiring:
    def test_far_future_is_not_expired(self):
        # 10**13 ms ≈ year 2286 — comfortably outside any buffer.
        assert _is_token_expired_or_expiring(10**13) is False

    def test_past_is_expired(self):
        assert _is_token_expired_or_expiring(0) is True

    def test_within_buffer_is_expired(self):
        import time
        now_ms = int(time.time() * 1000)
        # Expires 30 seconds from now → within 60s buffer → treat as expired.
        assert _is_token_expired_or_expiring(now_ms + 30 * 1000) is True

    def test_just_outside_buffer_is_fresh(self):
        import time
        now_ms = int(time.time() * 1000)
        # Expires 5 minutes from now → outside 60s buffer → fresh.
        assert _is_token_expired_or_expiring(now_ms + 5 * 60 * 1000) is False

    def test_none_returns_false(self):
        """When we can't read expiry (env token, keychain-bare), don't
        proactively skip — let the reactive 401 path handle it."""
        assert _is_token_expired_or_expiring(None) is False


class TestReadClaudeCredsWithMeta:
    def test_file_with_expires_at(self, valid_creds_file):
        creds = read_claude_creds_with_meta()
        assert creds is not None
        assert creds["source"] == "file"
        assert creds["access_token"].startswith("sk-ant-")
        assert creds["expires_at_ms"] == 9999999999000

    def test_env_token_has_no_expires_at(self, valid_creds_file, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-env-test")
        creds = read_claude_creds_with_meta()
        assert creds["source"] == "env"
        assert creds["access_token"] == "sk-ant-env-test"
        assert creds["expires_at_ms"] is None

    def test_no_creds_returns_none(self, isolated_paths):
        assert read_claude_creds_with_meta() is None

    def test_file_without_expires_at(self, isolated_paths):
        # Old-format credentials with just an access token, no expiresAt.
        isolated_paths["creds"].write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "sk-ant-old-format"}
        }))
        creds = read_claude_creds_with_meta()
        assert creds["access_token"] == "sk-ant-old-format"
        assert creds["expires_at_ms"] is None  # missing → None, not assumed expired


class TestProactiveSkipsProbe:
    """The critical guarantee: when local check says token is expired and
    refresh can't fix it, we MUST NOT make the HTTP call. Otherwise we
    re-trigger the CF abuse heuristic."""

    def test_expired_token_with_failed_refresh_skips_probe(self, valid_creds_file):
        # Make the credential expired.
        valid_creds_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-ant-expired",
                "refreshToken": "sk-ant-refresh-dead",
                "expiresAt": 1000,  # epoch ms = 1970-01-01
            }
        }))

        with patch("ai_fuelgauge.urllib.request.urlopen") as mock_urlopen:
            with patch("ai_fuelgauge._trigger_claude_auth_refresh", return_value=False):
                result = probe_claude_quota()

        # Critical: no HTTP call must have been made.
        assert mock_urlopen.call_count == 0
        assert result["error"] == "auth-expired-no-refresh"
        assert result["_proactive_skip"] is True
        assert result["_refresh_attempted"] is True

    def test_expired_token_with_successful_refresh_proceeds(self, valid_creds_file):
        """Proactive refresh that actually rewrites credentials → probe
        proceeds normally with the fresh token."""
        creds_path = valid_creds_file
        # Start expired.
        creds_path.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-ant-expired",
                "refreshToken": "sk-ant-refresh",
                "expiresAt": 1000,
            }
        }))

        def fake_refresh():
            # Simulate `claude auth status` rewriting with a fresh token.
            creds_path.write_text(json.dumps({
                "claudeAiOauth": {
                    "accessToken": "sk-ant-fresh",
                    "refreshToken": "sk-ant-refresh",
                    "expiresAt": 9999999999000,
                }
            }))
            return True

        from tests.test_claude_probe import FakeResponse
        usage_json = json.dumps({
            "five_hour": {"utilization": 12.0, "resets_at": "2099-01-01T00:00:00+00:00"},
            "seven_day": {"utilization": 5.0, "resets_at": "2099-01-08T00:00:00+00:00"},
        })

        with patch("ai_fuelgauge.urllib.request.urlopen", return_value=FakeResponse(usage_json)) as mock_urlopen:
            with patch("ai_fuelgauge._trigger_claude_auth_refresh", side_effect=fake_refresh):
                result = probe_claude_quota()

        # Probe DID happen — and with the fresh token.
        assert mock_urlopen.call_count == 1
        assert result["status"] == 200
        assert result["primary"]["used_percent"] == 12.0

    def test_env_token_expired_returns_local_error_no_probe(self, valid_creds_file, monkeypatch):
        """Env token can't be refreshed by `claude auth status`. When the
        env token has no expires_at, we CAN'T proactively detect expiry —
        but if some upstream (CI?) sets both, we honor it. Test the latter
        case via a mock."""
        # Force env mode AND make the local check think it's expired by
        # patching the helper directly. Realistic env tokens lack expires_at,
        # but if one has it, we should still skip the probe.
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-env-expired")

        with patch("ai_fuelgauge._is_token_expired_or_expiring", return_value=True):
            with patch("ai_fuelgauge.urllib.request.urlopen") as mock_urlopen:
                with patch("ai_fuelgauge._trigger_claude_auth_refresh") as mock_refresh:
                    result = probe_claude_quota()

        assert mock_urlopen.call_count == 0
        assert mock_refresh.call_count == 0  # env mode skips spawn
        assert result["error"] == "env-token-expired"
        assert result["_proactive_skip"] is True

    def test_fresh_token_proceeds_normally(self, valid_creds_file):
        """When token is comfortably fresh (expires far in the future),
        proactive check passes silently and probe runs as before."""
        from tests.test_claude_probe import FakeResponse
        usage_json = json.dumps({
            "five_hour": {"utilization": 8.0, "resets_at": "2099-01-01T00:00:00+00:00"},
            "seven_day": {"utilization": 3.0, "resets_at": "2099-01-08T00:00:00+00:00"},
        })

        with patch("ai_fuelgauge.urllib.request.urlopen", return_value=FakeResponse(usage_json)) as mock_urlopen:
            with patch("ai_fuelgauge._trigger_claude_auth_refresh") as mock_refresh:
                result = probe_claude_quota()

        # Network call did happen; refresh was NOT triggered.
        assert mock_urlopen.call_count == 1
        assert mock_refresh.call_count == 0
        assert result["status"] == 200

    def test_reactive_401_retry_with_stale_refreshed_token_skips_network(self, valid_creds_file):
        """The reactive 401 retry path uses `_allow_refresh=False`,
        which previously also disabled the proactive expiry guard.
        If `claude auth status` rewrote credentials with a DIFFERENT
        but ALSO-EXPIRED token (clock skew / refresh bug), the retry
        would still send the stale bearer upstream — violating the
        README's 'no network request if refresh doesn't produce a
        new non-expired token' contract.

        The expiry guard is now unconditional (only the refresh
        ATTEMPT is gated by `_allow_refresh`), so the retry detects
        the stale-but-changed token and bails with
        `auth-expired-no-refresh` before any second HTTP call."""
        from tests.test_claude_probe import _mk_http_error
        creds_path = valid_creds_file

        # Start with a HEALTHY token so the first probe gets through to the
        # network and can return 401.
        creds_path.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-ant-healthy-but-server-rejects",
                "refreshToken": "sk-ant-refresh",
                "expiresAt": 9999999999000,  # passes proactive guard
            }
        }))

        urlopen_call_count = {"n": 0}
        def fake_urlopen(req, timeout=None):
            urlopen_call_count["n"] += 1
            raise _mk_http_error(401)

        # Refresh writes a NEW token but with a stale expiresAt — simulates
        # a clock-skew / refresh-bug scenario. Token fingerprint changes,
        # which the old code took as proof of recovery.
        def stale_refresh():
            creds_path.write_text(json.dumps({
                "claudeAiOauth": {
                    "accessToken": "sk-ant-DIFFERENT-but-also-stale",
                    "refreshToken": "sk-ant-refresh",
                    "expiresAt": 1000,  # already expired
                }
            }))
            return True

        with patch("ai_fuelgauge.urllib.request.urlopen", side_effect=fake_urlopen):
            with patch("ai_fuelgauge._trigger_claude_auth_refresh", side_effect=stale_refresh):
                result = probe_claude_quota()

        # Critical: the retry must NOT have made a second network call.
        assert urlopen_call_count["n"] == 1, (
            "stale refreshed token must not be sent — expected exactly 1 "
            "network call (the original), got "
            f"{urlopen_call_count['n']}"
        )
        assert result["error"] == "auth-expired-no-refresh"
        assert result["_proactive_skip"] is True
        assert result["_refresh_attempted"] is True
