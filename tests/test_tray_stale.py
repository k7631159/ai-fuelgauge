"""Tests for tray stale-bar fallback (multi-line tooltip + menu labels +
icon dot), covering scenarios 1-4 of the last-known-good design."""
from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("pystray")
pytest.importorskip("PIL")

import ai_fuelgauge as afg
from tray import (
    TrayApp,
    _summary_line,
    _max_pct,
    _claude_stale_menu_label,
)


def _seed_last_good(isolated_paths, primary_pct=29, secondary_pct=15,
                    primary_reset_at=None, secondary_reset_at=None,
                    age_seconds=3 * 3600):
    """Write a last-good record with controllable per-bar reset_at and age."""
    now = int(time.time())
    if primary_reset_at is None:
        primary_reset_at = now + 5400
    if secondary_reset_at is None:
        secondary_reset_at = now + 56000
    afg._save_last_good_claude({
        "status": 200,
        "primary": {
            "window_minutes": 300,
            "used_percent": primary_pct,
            "reset_at": primary_reset_at,
            "reset_in_seconds": primary_reset_at - now,
        },
        "secondary": {
            "window_minutes": 10080,
            "used_percent": secondary_pct,
            "reset_at": secondary_reset_at,
            "reset_in_seconds": secondary_reset_at - now,
        },
    })
    path = isolated_paths["last_good"]
    record = json.loads(path.read_text(encoding="utf-8"))
    record["_probed_at"] = int(time.time()) - age_seconds
    path.write_text(json.dumps(record))


# --- Scenario 1: both healthy (baseline tooltip) -------------------------

class TestTooltipHealthy:
    def test_multi_line_format(self):
        snap = {
            "codex": {
                "primary": {"used_percent": 29.0},
                "secondary": {"used_percent": 25.0},
            },
            "claude": {
                "primary": {"used_percent": 29.0},
                "secondary": {"used_percent": 15.0},
            },
        }
        result = _summary_line(snap)
        # Two lines, separated by \r\n. Codex on the first, Claude on the second.
        lines = result.split("\r\n")
        assert len(lines) == 2
        assert lines[0].startswith("Codex")
        assert "5h: 29%" in lines[0] and "week: 25%" in lines[0]
        assert lines[1].startswith("Claude")
        assert "5h: 29%" in lines[1] and "week: 15%" in lines[1]


# --- Scenario 2: stale bars, both still in window ------------------------

class TestTooltipStaleBothValid:
    def test_tooltip_shows_stale_bars_with_age(self, isolated_paths):
        _seed_last_good(isolated_paths, age_seconds=3 * 3600)
        snap = {
            "codex": {
                "primary": {"used_percent": 29.0},
                "secondary": {"used_percent": 25.0},
            },
            "claude": {"error": "auth-expired-no-refresh"},
        }
        result = _summary_line(snap)
        lines = result.split("\r\n")
        assert lines[0].startswith("Codex")
        # Claude line must include both stale bars + a stale/expired tag.
        assert "5h: 29%" in lines[1]
        assert "week: 15%" in lines[1]
        assert "3h stale" in lines[1]
        assert "expired" in lines[1]
        # Should NOT mention any window having reset (both still in window).
        assert "window reset" not in lines[1]


# --- Scenario 3: 5h rolled over, week valid (and reverse) ----------------

class TestTooltipPartialStale:
    def test_5h_rolled_over_week_valid(self, isolated_paths):
        _seed_last_good(
            isolated_paths,
            primary_reset_at=int(time.time()) - 600,
            secondary_reset_at=int(time.time()) + 30000,
            age_seconds=6 * 3600,
        )
        snap = {"codex": {}, "claude": {"error": "auth-expired-no-refresh"}}
        result = _summary_line(snap)
        claude_line = result.split("\r\n")[1]
        # 5h omitted, week present.
        assert "5h:" not in claude_line
        assert "week: 15%" in claude_line
        # Rollover annotation, with the wording Codex preferred.
        assert "5h window reset" in claude_line

    def test_week_rolled_over_5h_valid(self, isolated_paths):
        _seed_last_good(
            isolated_paths,
            primary_reset_at=int(time.time()) + 4000,
            secondary_reset_at=int(time.time()) - 600,
            age_seconds=2 * 3600,
        )
        snap = {"codex": {}, "claude": {"error": "auth-expired-no-refresh"}}
        result = _summary_line(snap)
        claude_line = result.split("\r\n")[1]
        assert "5h: 29%" in claude_line
        assert "week:" not in claude_line
        assert "week window reset" in claude_line


# --- Scenario 4: full fallback (no usable stale) -------------------------

class TestTooltipFullFallback:
    def test_both_rolled_over_falls_back_to_expired(self, isolated_paths):
        _seed_last_good(
            isolated_paths,
            primary_reset_at=int(time.time()) - 3600,
            secondary_reset_at=int(time.time()) - 3600,
            age_seconds=8 * 86400,  # also past 24h cap, but rolled-over check fires first
        )
        snap = {"codex": {}, "claude": {"error": "auth-expired-no-refresh"}}
        result = _summary_line(snap)
        claude_line = result.split("\r\n")[1]
        # No stale numbers should appear.
        assert "29%" not in claude_line and "15%" not in claude_line
        assert "expired" in claude_line.lower()

    def test_cache_miss_falls_back(self, isolated_paths):
        # No last-good written.
        snap = {"codex": {}, "claude": {"error": "auth-expired-no-refresh"}}
        result = _summary_line(snap)
        claude_line = result.split("\r\n")[1]
        assert "29%" not in claude_line
        assert "expired" in claude_line.lower()


# --- Menu labels ---------------------------------------------------------

class TestMenuStaleLabels:
    def test_5h_stale_label_when_valid(self, isolated_paths):
        _seed_last_good(isolated_paths, age_seconds=3 * 3600)
        app = TrayApp(interval=300)
        app.snapshot = {"codex": {}, "claude": {"error": "auth-expired-no-refresh"}}
        assert app._claude_5h(None) == "Claude 5h: 29% (3h stale)"

    def test_week_stale_label_when_valid(self, isolated_paths):
        _seed_last_good(isolated_paths, age_seconds=3 * 3600)
        app = TrayApp(interval=300)
        app.snapshot = {"codex": {}, "claude": {"error": "auth-expired-no-refresh"}}
        assert app._claude_week(None) == "Claude week: 15% (3h stale)"

    def test_5h_window_reset_label(self, isolated_paths):
        _seed_last_good(
            isolated_paths,
            primary_reset_at=int(time.time()) - 600,
            secondary_reset_at=int(time.time()) + 30000,
            age_seconds=6 * 3600,
        )
        app = TrayApp(interval=300)
        app.snapshot = {"codex": {}, "claude": {"error": "auth-expired-no-refresh"}}
        assert app._claude_5h(None) == "Claude 5h: -- (window reset)"

    def test_week_window_reset_label(self, isolated_paths):
        _seed_last_good(
            isolated_paths,
            primary_reset_at=int(time.time()) + 4000,
            secondary_reset_at=int(time.time()) - 600,
            age_seconds=2 * 3600,
        )
        app = TrayApp(interval=300)
        app.snapshot = {"codex": {}, "claude": {"error": "auth-expired-no-refresh"}}
        assert app._claude_week(None) == "Claude week: -- (window reset)"

    def test_double_dash_when_no_last_good(self, isolated_paths):
        """Cache miss with proactive-expiry: both menu rows show plain `--`."""
        app = TrayApp(interval=300)
        app.snapshot = {"codex": {}, "claude": {"error": "auth-expired-no-refresh"}}
        assert app._claude_5h(None) == "Claude 5h: --"
        assert app._claude_week(None) == "Claude week: --"

    def test_envtok_uses_stale_too(self, isolated_paths):
        _seed_last_good(isolated_paths, age_seconds=2 * 3600)
        app = TrayApp(interval=300)
        app.snapshot = {"codex": {}, "claude": {"error": "env-token-expired"}}
        assert "29%" in app._claude_5h(None)
        assert "stale" in app._claude_5h(None)


class TestMenuHintRow:
    def test_hint_visible_on_proactive_expiry(self):
        app = TrayApp(interval=300)
        app.snapshot = {"codex": {}, "claude": {"error": "auth-expired-no-refresh"}}
        assert app._claude_hint_visible(None) is True
        assert "token expired" in app._claude_hint_text(None)
        assert "claude" in app._claude_hint_text(None).lower()

    def test_hint_visible_on_env_token_expired(self):
        app = TrayApp(interval=300)
        app.snapshot = {"codex": {}, "claude": {"error": "env-token-expired"}}
        assert app._claude_hint_visible(None) is True
        assert "$CLAUDE_CODE_OAUTH_TOKEN" in app._claude_hint_text(None)

    def test_hint_hidden_on_healthy(self):
        app = TrayApp(interval=300)
        app.snapshot = {
            "codex": {},
            "claude": {"primary": {"used_percent": 29.0}},
        }
        assert app._claude_hint_visible(None) is False

    def test_hint_hidden_on_non_expiry_auth_error(self):
        """Non-proactive auth errors (401 from refresh-failed, 429, login):
        the existing menu rows already carry the actionable text — adding
        the hint row would duplicate. Keep hidden."""
        app = TrayApp(interval=300)
        app.snapshot = {"codex": {}, "claude": {"status": 401}}
        assert app._claude_hint_visible(None) is False

    def test_hint_visible_on_reactive_envtok_401(self, isolated_paths):
        """Codex pre-push catch: when an env-mode token expires server-side,
        the result dict carries `status: 401, _env_token_mode: True` (no
        `error` key). The classifier still returns `envtok`, so stale-bar
        routing fires — the hint row must follow suit and surface the
        env-var replacement instruction. Without this, the user sees stale
        numbers with no recovery action."""
        _seed_last_good(isolated_paths, age_seconds=2 * 3600)
        app = TrayApp(interval=300)
        app.snapshot = {
            "codex": {},
            "claude": {"status": 401, "_env_token_mode": True},
        }
        assert app._claude_hint_visible(None) is True
        assert "$CLAUDE_CODE_OAUTH_TOKEN" in app._claude_hint_text(None)
        # And the stale bar still renders.
        assert "29%" in app._claude_5h(None)


# --- Icon dot color (max_pct) --------------------------------------------

class TestMaxPctStale:
    def test_includes_stale_valid_bars(self, isolated_paths):
        _seed_last_good(isolated_paths, primary_pct=85, secondary_pct=15, age_seconds=2 * 3600)
        snap = {
            "codex": {
                "primary": {"used_percent": 30.0},
                "secondary": {"used_percent": 25.0},
            },
            "claude": {"error": "auth-expired-no-refresh"},
        }
        # Stale 85% should drive the icon (orange) instead of getting dropped.
        assert _max_pct(snap) == 85.0

    def test_excludes_rolled_over_stale_bars(self, isolated_paths):
        _seed_last_good(
            isolated_paths,
            primary_pct=85,
            secondary_pct=15,
            primary_reset_at=int(time.time()) - 60,  # rolled over
            secondary_reset_at=int(time.time()) + 30000,  # still valid
            age_seconds=6 * 3600,
        )
        snap = {
            "codex": {
                "primary": {"used_percent": 30.0},
                "secondary": {"used_percent": 25.0},
            },
            "claude": {"error": "auth-expired-no-refresh"},
        }
        # The rolled-over 85% must NOT be picked up; the valid 15% week + 30%
        # codex max wins.
        assert _max_pct(snap) == 30.0

    def test_falls_back_to_zero_when_full_fallback(self, isolated_paths):
        # No last-good at all + claude proactive-expiry → claude contributes 0.
        snap = {
            "codex": {},
            "claude": {"error": "auth-expired-no-refresh"},
        }
        assert _max_pct(snap) == 0.0

    def test_includes_stale_on_reactive_envtok_401(self, isolated_paths):
        """Codex round-2 catch: reactive env-token 401 surfaces as
        `{status: 401, _env_token_mode: True}` (no `error` key). Menu /
        tooltip / hint already route stale via the classifier, but
        `_max_pct` previously checked the raw `error` field — leaving
        the icon dot inconsistent with the menu values. All four
        consumers now go through the classifier."""
        _seed_last_good(isolated_paths, primary_pct=85, secondary_pct=15, age_seconds=2 * 3600)
        snap = {
            "codex": {
                "primary": {"used_percent": 30.0},
                "secondary": {"used_percent": 25.0},
            },
            "claude": {"status": 401, "_env_token_mode": True},
        }
        # Stale 85% must drive the icon dot just like in the proactive case.
        assert _max_pct(snap) == 85.0


# --- _claude_stale_menu_label module helper ------------------------------

class TestClaudeStaleMenuLabel:
    def test_no_last_good(self, isolated_paths):
        assert _claude_stale_menu_label("primary", "5h") == "Claude 5h: --"

    def test_window_reset(self, isolated_paths):
        _seed_last_good(
            isolated_paths,
            primary_reset_at=int(time.time()) - 100,
        )
        assert _claude_stale_menu_label("primary", "5h") == "Claude 5h: -- (window reset)"

    def test_valid_bar_shows_age(self, isolated_paths):
        _seed_last_good(isolated_paths, age_seconds=45 * 60)
        assert _claude_stale_menu_label("primary", "5h") == "Claude 5h: 29% (45m stale)"
