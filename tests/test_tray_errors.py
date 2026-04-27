"""Tests for the tray error classification (new commit 6 — Codex + Claude UX).

Before this commit, the tray showed `Claude ?/?` or `Codex ?/?` for every
failure regardless of cause — which several users (including the author)
read as "the tool is broken" rather than "server said 429" / "token expired".

The classifier maps probe-result dicts to (short_label, menu_text). Short
labels go in the tray title (≤8 chars); menu_text is the right-click
explanation.
"""
import pytest

pytest.importorskip("pystray")
pytest.importorskip("PIL")

from tray import TrayApp, _classify_probe_error, _summary_line


class TestClassifyClaudeErrors:
    def test_no_token(self):
        label, msg = _classify_probe_error({"error": "no-token-found"}, "Claude")
        assert label == "login"
        assert "not logged in" in msg
        assert "claude" in msg.lower()

    def test_probe_failed_network(self):
        label, msg = _classify_probe_error(
            {"error": "probe-failed: [Errno 11001] getaddrinfo failed"}, "Claude"
        )
        assert label == "offline"
        assert "offline" in msg.lower() or "probe" in msg.lower()

    def test_rate_limited_429(self):
        label, msg = _classify_probe_error({"status": 429}, "Claude")
        assert label == "429"
        assert "rate" in msg.lower() and "limit" in msg.lower()

    def test_auth_expired_401(self):
        label, msg = _classify_probe_error({"status": 401}, "Claude")
        assert label == "auth"
        assert "auth" in msg.lower() or "expired" in msg.lower()

    def test_proactive_token_expired_label(self):
        """Proactive skip (no HTTP call): use `expired` label so user knows
        to run `claude`, not wait."""
        label, msg = _classify_probe_error(
            {"error": "auth-expired-no-refresh"}, "Claude"
        )
        assert label == "expired"
        assert "claude" in msg.lower()

    def test_env_token_expired_label(self):
        label, msg = _classify_probe_error(
            {"error": "env-token-expired"}, "Claude"
        )
        assert label == "envtok"
        assert "env" in msg.lower() or "$claude" in msg.lower()

    def test_401_with_env_token_mode_marker(self):
        """Reactive 401 when env-token mode: same `envtok` label so the
        message points at the env var, not at running `claude`."""
        label, msg = _classify_probe_error(
            {"status": 401, "_env_token_mode": True}, "Claude"
        )
        assert label == "envtok"
        assert "env" in msg.lower() or "$claude" in msg.lower()

    def test_401_with_refresh_attempted_marker(self):
        """Reactive 401 after auto-refresh tried but didn't help — surface
        a more specific 'auto-refresh failed' explanation."""
        label, msg = _classify_probe_error(
            {"status": 401, "_refresh_attempted": True}, "Claude"
        )
        assert label == "auth"
        assert "auto-refresh" in msg.lower() or "refresh" in msg.lower()

    def test_generic_4xx(self):
        label, msg = _classify_probe_error({"status": 403}, "Claude")
        assert label == "403"
        assert "403" in msg

    def test_generic_5xx(self):
        label, msg = _classify_probe_error({"status": 502}, "Claude")
        assert label == "502"

    def test_success_returns_none(self):
        """When probe succeeded (primary has utilization), classifier returns None
        so the caller renders the actual numbers."""
        result = _classify_probe_error(
            {
                "status": 200,
                "primary": {"used_percent": 50.0, "reset_in_seconds": 300},
                "secondary": {"used_percent": 10.0, "reset_in_seconds": 600},
            },
            "Claude",
        )
        assert result is None


class TestClassifyCodexErrors:
    def test_not_in_path(self):
        label, msg = _classify_probe_error({"error": "codex-not-in-path"}, "Codex")
        assert label == "no-cli"
        assert "codex" in msg.lower()

    def test_spawn_failed(self):
        label, msg = _classify_probe_error(
            {"error": "spawn-failed: [WinError 2] file not found"}, "Codex"
        )
        assert label == "spawn"
        assert "spawn" in msg.lower()

    def test_no_response(self):
        label, msg = _classify_probe_error(
            {"error": "no-response-from-app-server"}, "Codex"
        )
        assert label == "no-resp"
        assert "responding" in msg.lower() or "response" in msg.lower()

    def test_jsonrpc_error(self):
        label, msg = _classify_probe_error(
            {"error": "jsonrpc-error: {'code': -32600}"}, "Codex"
        )
        assert label == "rpc-err"
        assert "json-rpc" in msg.lower() or "rpc" in msg.lower()

    def test_empty_rate_limits(self):
        label, msg = _classify_probe_error({"error": "empty-rateLimits"}, "Codex")
        assert label == "empty"

    def test_sqlite_error(self):
        label, msg = _classify_probe_error(
            {"error": "codex sqlite: database locked"}, "Codex"
        )
        assert label == "db-err"
        assert "sqlite" in msg.lower()

    def test_success_returns_none(self):
        result = _classify_probe_error(
            {"primary": {"used_percent": 30.0}, "secondary": {"used_percent": 15.0}},
            "Codex",
        )
        assert result is None


class TestClassifyPriority:
    """Ordering between status-based and error-key signals matters — the more
    specific HTTP code wins when both are present. Codex pre-commit guard."""

    def test_status_429_wins_over_probe_failed(self):
        """If both `status: 429` and `error: probe-failed` were ever set on
        the same result dict, the HTTP 429 is the more useful signal."""
        label, _ = _classify_probe_error(
            {"status": 429, "error": "probe-failed: some transport hiccup"},
            "Claude",
        )
        assert label == "429"

    def test_status_401_wins_over_no_token(self):
        label, _ = _classify_probe_error(
            {"status": 401, "error": "no-token-found"},
            "Claude",
        )
        assert label == "auth"


class TestClassifyEdgeCases:
    def test_empty_dict(self):
        label, msg = _classify_probe_error({}, "Claude")
        assert label == "?"
        assert "no data" in msg.lower()

    def test_none_input(self):
        label, msg = _classify_probe_error(None, "Claude")
        assert label == "?"

    def test_unclassified_error_truncated(self):
        long_err = "some-weird-error-with-a-very-long-description-that-overflows"
        label, msg = _classify_probe_error({"error": long_err}, "Claude")
        assert label == "err"
        # Should be truncated to avoid bloating the tray menu
        assert len(msg) <= 60


class TestSummaryLineWithErrors:
    def test_claude_429_renders_label_not_question_mark(self):
        snap = {
            "codex": {
                "primary": {"used_percent": 30.0},
                "secondary": {"used_percent": 20.0},
            },
            "claude": {"status": 429},
        }
        result = _summary_line(snap)
        assert "Codex 30%/20%" in result
        assert "Claude 429" in result
        assert "Claude ?/?" not in result

    def test_codex_not_installed_label(self):
        snap = {
            "codex": {"error": "codex-not-in-path"},
            "claude": {
                "primary": {"used_percent": 25.0},
                "secondary": {"used_percent": 10.0},
            },
        }
        result = _summary_line(snap)
        assert "Codex no-cli" in result
        assert "Claude 25%/10%" in result

    def test_both_error_labels_independent(self):
        """Each side's error label surfaces independently."""
        snap = {
            "codex": {"error": "no-response-from-app-server"},
            "claude": {"error": "no-token-found"},
        }
        result = _summary_line(snap)
        assert "Codex no-resp" in result
        assert "Claude login" in result


class TestMenuGetters:
    def test_claude_5h_menu_shows_full_error(self):
        app = TrayApp(interval=300)
        app.snapshot = {"codex": {}, "claude": {"status": 401}}
        label = app._claude_5h(None)
        assert "Claude" in label
        assert "auth" in label.lower() or "expired" in label.lower()

    def test_claude_week_menu_shows_dash_on_error(self):
        """To avoid duplicating the same error message on two adjacent menu
        rows, the week row shows a simple `-` when an error is detected."""
        app = TrayApp(interval=300)
        app.snapshot = {"codex": {}, "claude": {"status": 401}}
        label = app._claude_week(None)
        assert label == "Claude week: -"

    def test_codex_5h_menu_shows_not_found_message(self):
        app = TrayApp(interval=300)
        app.snapshot = {"codex": {"error": "codex-not-in-path"}, "claude": {}}
        label = app._codex_5h(None)
        assert "Codex" in label
        assert "PATH" in label or "not found" in label.lower()

    def test_menu_shows_percent_on_success(self):
        """Regression guard: happy path menu text unchanged."""
        app = TrayApp(interval=300)
        app.snapshot = {
            "codex": {"primary": {"used_percent": 42.0}},
            "claude": {"primary": {"used_percent": 17.5}},
        }
        assert app._codex_5h(None) == "Codex 5h: 42%"
        assert app._claude_5h(None) == "Claude 5h: 18%"
