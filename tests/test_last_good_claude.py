"""Tests for last-known-good Claude stale-bar fallback."""
from __future__ import annotations

import io
import json
import time
from contextlib import redirect_stdout

import ai_fuelgauge as afg


# --- _format_stale_age ---------------------------------------------------

class TestFormatStaleAge:
    def test_zero_seconds(self):
        assert afg._format_stale_age(0) == "0m"

    def test_negative_clamped(self):
        # Negative arrives only on logic error, but should not raise nor
        # produce "-1m" (would look like a bug surfacing through the UI).
        assert afg._format_stale_age(-5) == "0m"

    def test_under_a_minute(self):
        assert afg._format_stale_age(30) == "0m"

    def test_minute_boundary(self):
        assert afg._format_stale_age(60) == "1m"

    def test_minutes_round_down(self):
        assert afg._format_stale_age(45 * 60 + 59) == "45m"

    def test_hour_boundary(self):
        assert afg._format_stale_age(3600) == "1h"

    def test_three_hours(self):
        assert afg._format_stale_age(3 * 3600 + 30 * 60) == "3h"

    def test_almost_a_day(self):
        assert afg._format_stale_age(86400 - 1) == "23h"

    def test_day_boundary(self):
        assert afg._format_stale_age(86400) == "1d"


# --- _stale_bar_status ---------------------------------------------------

class TestStaleBarStatus:
    def test_none(self):
        assert afg._stale_bar_status(None) == "no_data"

    def test_empty_dict(self):
        assert afg._stale_bar_status({}) == "no_data"

    def test_no_pct_no_reset(self):
        assert afg._stale_bar_status({"foo": 1}) == "no_data"

    def test_valid_far_future(self):
        future = int(time.time()) + 3600
        assert afg._stale_bar_status({"used_percent": 29, "reset_at": future}) == "valid"

    def test_rolled_over_past(self):
        past = int(time.time()) - 60
        assert afg._stale_bar_status({"used_percent": 29, "reset_at": past}) == "rolled_over"

    def test_rolled_over_at_now(self):
        now = int(time.time())
        assert afg._stale_bar_status({"used_percent": 29, "reset_at": now}) == "rolled_over"

    def test_rolled_over_within_guard(self):
        # 60s guard: a window resetting in 30s is treated as already rolled over.
        soon = int(time.time()) + 30
        assert afg._stale_bar_status({"used_percent": 29, "reset_at": soon}) == "rolled_over"

    def test_boundary_exactly_at_guard(self):
        # `now == reset_at - guard` → reset_at = now + 60 → still rolled_over
        # (the guard inequality is `reset_at <= now + guard`).
        boundary = int(time.time()) + afg.LAST_GOOD_BAR_RESET_GUARD_SECONDS
        assert afg._stale_bar_status({"used_percent": 29, "reset_at": boundary}) == "rolled_over"

    def test_just_past_guard(self):
        just_past = int(time.time()) + afg.LAST_GOOD_BAR_RESET_GUARD_SECONDS + 1
        assert afg._stale_bar_status({"used_percent": 29, "reset_at": just_past}) == "valid"

    def test_reset_only_no_pct_is_no_data(self):
        """Codex review: a window with reset_at in the future but no
        used_percent must not classify as 'valid' — the renderer would
        print a stale header and then nothing, leaving the user staring
        at an empty bar region."""
        future = int(time.time()) + 3600
        assert afg._stale_bar_status({"reset_at": future}) == "no_data"

    def test_nan_pct_is_no_data(self):
        future = int(time.time()) + 3600
        assert afg._stale_bar_status({"used_percent": float("nan"), "reset_at": future}) == "no_data"

    def test_inf_pct_is_no_data(self):
        future = int(time.time()) + 3600
        assert afg._stale_bar_status({"used_percent": float("inf"), "reset_at": future}) == "no_data"

    def test_non_numeric_pct_is_no_data(self):
        future = int(time.time()) + 3600
        assert afg._stale_bar_status({"used_percent": "twenty", "reset_at": future}) == "no_data"

    def test_missing_reset_at_is_no_data(self):
        """Codex round-5 catch: a saved window with utilization but no
        `reset_at` cannot be classified — we can't tell if the original
        window has rolled over. Refuse to display rather than risk
        showing stale numbers up to 24h past the actual rollover."""
        assert afg._stale_bar_status({"used_percent": 29}) == "no_data"

    def test_none_reset_at_is_no_data(self):
        assert afg._stale_bar_status({"used_percent": 29, "reset_at": None}) == "no_data"

    def test_non_numeric_reset_at_is_no_data(self):
        assert afg._stale_bar_status({"used_percent": 29, "reset_at": "tomorrow"}) == "no_data"

    def test_bool_reset_at_is_no_data(self):
        """`bool` subclasses `int`; True passing as reset_at would
        classify as 'rolled_over' (True == 1 → in the past) and silently
        omit a window the saved record actually meant to keep."""
        assert afg._stale_bar_status({"used_percent": 29, "reset_at": True}) == "no_data"


# --- _save_last_good_claude / _load_last_good_claude ---------------------

def _good_probe(primary_pct=29, secondary_pct=15, primary_reset_at=None, secondary_reset_at=None):
    now = int(time.time())
    return {
        "status": 200,
        "primary": {
            "window_minutes": 300,
            "used_percent": primary_pct,
            "reset_at": primary_reset_at if primary_reset_at is not None else now + 5400,
            "reset_in_seconds": 5400,
        },
        "secondary": {
            "window_minutes": 10080,
            "used_percent": secondary_pct,
            "reset_at": secondary_reset_at if secondary_reset_at is not None else now + 56000,
            "reset_in_seconds": 56000,
        },
    }


class TestSaveLoad:
    def test_round_trip(self, isolated_paths):
        afg._save_last_good_claude(_good_probe())
        loaded = afg._load_last_good_claude()
        assert loaded is not None
        assert loaded["primary"]["used_percent"] == 29
        assert loaded["secondary"]["used_percent"] == 15

    def test_save_skips_when_error(self, isolated_paths):
        afg._save_last_good_claude({"error": "auth-expired-no-refresh"})
        assert afg._load_last_good_claude() is None

    def test_save_skips_when_4xx(self, isolated_paths):
        afg._save_last_good_claude({"status": 401})
        assert afg._load_last_good_claude() is None

    def test_save_skips_when_no_windows(self, isolated_paths):
        afg._save_last_good_claude({"status": 200})
        assert afg._load_last_good_claude() is None

    def test_save_skips_non_dict(self, isolated_paths):
        afg._save_last_good_claude(None)
        afg._save_last_good_claude("oops")
        assert afg._load_last_good_claude() is None

    def test_save_drops_debug_metadata(self, isolated_paths):
        probe = _good_probe()
        probe["response_body"] = {"sensitive": "do-not-persist"}
        probe["_refresh_attempted"] = True
        afg._save_last_good_claude(probe)
        raw = isolated_paths["last_good"].read_text(encoding="utf-8")
        assert "sensitive" not in raw
        assert "_refresh_attempted" not in raw

    def test_load_missing_file(self, isolated_paths):
        assert afg._load_last_good_claude() is None

    def test_load_corrupt_file(self, isolated_paths):
        isolated_paths["last_good"].parent.mkdir(parents=True, exist_ok=True)
        isolated_paths["last_good"].write_text("{ not json")
        assert afg._load_last_good_claude() is None

    def test_load_non_dict_json(self, isolated_paths):
        isolated_paths["last_good"].parent.mkdir(parents=True, exist_ok=True)
        isolated_paths["last_good"].write_text("[]")
        assert afg._load_last_good_claude() is None

    def test_load_schema_mismatch(self, isolated_paths):
        isolated_paths["last_good"].parent.mkdir(parents=True, exist_ok=True)
        isolated_paths["last_good"].write_text(json.dumps({
            "_schema": 99,
            "_probed_at": int(time.time()),
            "primary": {"used_percent": 29},
        }))
        assert afg._load_last_good_claude() is None

    def test_load_age_within_24h(self, isolated_paths):
        afg._save_last_good_claude(_good_probe())
        # Rewrite probed_at to 23h ago
        record = json.loads(isolated_paths["last_good"].read_text(encoding="utf-8"))
        record["_probed_at"] = int(time.time()) - 23 * 3600
        isolated_paths["last_good"].write_text(json.dumps(record))
        assert afg._load_last_good_claude() is not None

    def test_load_age_at_24h_boundary(self, isolated_paths):
        """Exactly 24h old is still acceptable; one tick past the boundary
        is rejected. The cap is inclusive of 24h, exclusive of 24h+1s."""
        afg._save_last_good_claude(_good_probe())
        path = isolated_paths["last_good"]
        record = json.loads(path.read_text(encoding="utf-8"))
        record["_probed_at"] = int(time.time()) - afg.LAST_GOOD_CLAUDE_TTL_SECONDS
        path.write_text(json.dumps(record))
        assert afg._load_last_good_claude() is not None

        record["_probed_at"] = int(time.time()) - afg.LAST_GOOD_CLAUDE_TTL_SECONDS - 1
        path.write_text(json.dumps(record))
        assert afg._load_last_good_claude() is None

    def test_load_clock_skew_future(self, isolated_paths):
        """A cached probe time slightly in the future means the system clock
        moved backwards or someone tampered with the cache. Either way we
        can't trust the staleness calc, so reject."""
        afg._save_last_good_claude(_good_probe())
        path = isolated_paths["last_good"]
        record = json.loads(path.read_text(encoding="utf-8"))
        record["_probed_at"] = int(time.time()) + 5
        path.write_text(json.dumps(record))
        assert afg._load_last_good_claude() is None

    def test_load_non_numeric_probed_at(self, isolated_paths):
        isolated_paths["last_good"].parent.mkdir(parents=True, exist_ok=True)
        isolated_paths["last_good"].write_text(json.dumps({
            "_schema": afg.LAST_GOOD_CLAUDE_SCHEMA,
            "_probed_at": "yesterday",
            "primary": {"used_percent": 29},
        }))
        assert afg._load_last_good_claude() is None

    def test_save_uses_atomic_replace(self, isolated_paths):
        """Codex review: write must be atomic so a tray reader during a
        CLI save sees old-or-new, never partial JSON. Verify by ensuring
        the .tmp side file is gone after a successful save (rename
        atomically replaced it)."""
        afg._save_last_good_claude(_good_probe())
        path = isolated_paths["last_good"]
        tmp = path.with_name(path.name + ".tmp")
        assert path.exists()
        assert not tmp.exists(), "temp file leaked — write was not atomic"

    def test_load_bool_probed_at(self, isolated_paths):
        """`bool` is a subclass of `int` in Python — guard against `True`
        being silently accepted as `1` (1970-epoch nonsense)."""
        isolated_paths["last_good"].parent.mkdir(parents=True, exist_ok=True)
        isolated_paths["last_good"].write_text(json.dumps({
            "_schema": afg.LAST_GOOD_CLAUDE_SCHEMA,
            "_probed_at": True,
            "primary": {"used_percent": 29},
        }))
        assert afg._load_last_good_claude() is None


# --- CLI render scenarios -------------------------------------------------

def _seed_last_good(isolated_paths, primary_reset_at=None, secondary_reset_at=None,
                    age_seconds=3 * 3600):
    """Write a last-good record, age it, and customize per-bar reset_at."""
    probe = _good_probe(
        primary_reset_at=primary_reset_at,
        secondary_reset_at=secondary_reset_at,
    )
    afg._save_last_good_claude(probe)
    path = isolated_paths["last_good"]
    record = json.loads(path.read_text(encoding="utf-8"))
    record["_probed_at"] = int(time.time()) - age_seconds
    path.write_text(json.dumps(record))


class TestCliRenderExpired:
    def test_scenario_2_both_bars_valid(self, isolated_paths):
        _seed_last_good(
            isolated_paths,
            primary_reset_at=int(time.time()) + 5400,
            secondary_reset_at=int(time.time()) + 56000,
            age_seconds=3 * 3600,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            afg._render_claude(
                {"error": "auth-expired-no-refresh"},
                use_color=False, debug=False,
            )
        out = buf.getvalue()
        assert "cached 3h ago; token expired" in out
        assert "5h" in out and "29%" in out
        assert "week" in out and "15%" in out
        assert "(stale)" in out
        assert "open `claude`" in out
        assert "cached window already reset" not in out

    def test_scenario_3_5h_rolled_over(self, isolated_paths):
        # 5h reset already passed (rolled over), week still valid.
        _seed_last_good(
            isolated_paths,
            primary_reset_at=int(time.time()) - 300,
            secondary_reset_at=int(time.time()) + 30000,
            age_seconds=6 * 3600,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            afg._render_claude(
                {"error": "auth-expired-no-refresh"},
                use_color=False, debug=False,
            )
        out = buf.getvalue()
        assert "cached 6h ago; token expired" in out
        assert "cached window already reset" in out
        assert "week" in out and "15%" in out

    def test_scenario_3_reverse_week_rolled_over(self, isolated_paths):
        """Reverse partial-invalidity: weekly rolled, 5h still valid."""
        _seed_last_good(
            isolated_paths,
            primary_reset_at=int(time.time()) + 4000,
            secondary_reset_at=int(time.time()) - 600,
            age_seconds=2 * 3600,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            afg._render_claude(
                {"error": "auth-expired-no-refresh"},
                use_color=False, debug=False,
            )
        out = buf.getvalue()
        assert "5h" in out and "29%" in out
        # week line should be the omitted-marker, not a percent.
        assert "cached window already reset" in out
        # Make sure we didn't print a stale week percent.
        week_lines = [ln for ln in out.splitlines() if ln.strip().startswith("week")]
        assert all("%" not in ln for ln in week_lines)

    def test_scenario_4_both_rolled_over_falls_back(self, isolated_paths):
        _seed_last_good(
            isolated_paths,
            primary_reset_at=int(time.time()) - 3600,
            secondary_reset_at=int(time.time()) - 3600,
            age_seconds=8 * 86400,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            afg._render_claude(
                {"error": "auth-expired-no-refresh", "_expires_at_ms": int(time.time() * 1000) - 3600 * 1000},
                use_color=False, debug=False,
            )
        out = buf.getvalue()
        assert "auth token expired" in out
        assert "29%" not in out  # no stale data should leak through
        assert "open `claude`" in out

    def test_scenario_4_cache_miss_falls_back(self, isolated_paths):
        # No last-good written at all.
        buf = io.StringIO()
        with redirect_stdout(buf):
            afg._render_claude(
                {"error": "auth-expired-no-refresh"},
                use_color=False, debug=False,
            )
        out = buf.getvalue()
        assert "auth token expired" in out
        assert "(stale)" not in out

    def test_env_token_expired_with_stale(self, isolated_paths):
        _seed_last_good(
            isolated_paths,
            primary_reset_at=int(time.time()) + 5000,
            secondary_reset_at=int(time.time()) + 50000,
            age_seconds=2 * 3600,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            afg._render_claude(
                {"error": "env-token-expired"},
                use_color=False, debug=False,
            )
        out = buf.getvalue()
        assert "$CLAUDE_CODE_OAUTH_TOKEN expired" in out
        assert "Replace $CLAUDE_CODE_OAUTH_TOKEN" in out
        assert "29%" in out  # stale bar still shown
        # Crucially the env-var path should NOT recommend `claude` /exit.
        assert "open `claude`" not in out

    def test_env_token_expired_no_stale_falls_back(self, isolated_paths):
        buf = io.StringIO()
        with redirect_stdout(buf):
            afg._render_claude(
                {"error": "env-token-expired"},
                use_color=False, debug=False,
            )
        out = buf.getvalue()
        assert "$CLAUDE_CODE_OAUTH_TOKEN appears expired" in out
        assert "(stale)" not in out

    def test_reactive_envtok_401_uses_stale_path(self, isolated_paths):
        """Codex round-3 catch: a reactive env-token 401 (env token without
        local `expires_at_ms` that the server later 401s) arrives as
        `{status: 401, _env_token_mode: True}` with no `error` key. Tray
        already routes this to stale via the classifier; CLI must follow,
        otherwise the same probe state renders inconsistently across the
        two surfaces."""
        _seed_last_good(
            isolated_paths,
            primary_reset_at=int(time.time()) + 5000,
            secondary_reset_at=int(time.time()) + 50000,
            age_seconds=2 * 3600,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            afg._render_claude(
                {"status": 401, "_env_token_mode": True},
                use_color=False, debug=False,
            )
        out = buf.getvalue()
        # Stale bars + env-var replacement hint (matches the proactive
        # `error: env-token-expired` rendering).
        assert "29%" in out
        assert "(stale)" in out
        assert "Replace $CLAUDE_CODE_OAUTH_TOKEN" in out
        # The legacy "Claude probe HTTP 401" line must not appear — that
        # was the old behaviour we're replacing.
        assert "HTTP 401" not in out

    def test_reactive_envtok_401_no_stale_falls_back(self, isolated_paths):
        """No last-known-good seeded: same reactive envtok 401 still
        produces the env-var replacement hint, just without bars."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            afg._render_claude(
                {"status": 401, "_env_token_mode": True},
                use_color=False, debug=False,
            )
        out = buf.getvalue()
        assert "$CLAUDE_CODE_OAUTH_TOKEN appears expired" in out
        assert "(stale)" not in out
        assert "HTTP 401" not in out

    def test_reactive_envtok_401_debug_dumps_body(self, isolated_paths):
        """Codex round-4 catch: routing the reactive env-token 401 into
        the expiry branch must not eat the `--debug` body dump. The
        documented `--debug` contract is to print raw API responses,
        and 401 from /api/oauth/usage carries a body the user may
        want to inspect."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            afg._render_claude(
                {
                    "status": 401,
                    "_env_token_mode": True,
                    "response_body": {"error": {"type": "authentication_error",
                                                "message": "expired"}},
                },
                use_color=False, debug=True,
            )
        out = buf.getvalue()
        assert "/api/oauth/usage response" in out
        assert "authentication_error" in out

    def test_stale_with_only_reset_at_no_pct_falls_back(self, isolated_paths):
        """Codex review regression: last-good written with reset_at but
        no used_percent (e.g. an evolved API shape that drops the field)
        must NOT render a stale header followed by an empty bar region.
        Should collapse to the plain expired actionable error."""
        path = isolated_paths["last_good"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "_schema": afg.LAST_GOOD_CLAUDE_SCHEMA,
            "_probed_at": int(time.time()) - 600,
            "primary": {"reset_at": int(time.time()) + 5400},
            "secondary": {"reset_at": int(time.time()) + 50000},
        }))
        buf = io.StringIO()
        with redirect_stdout(buf):
            afg._render_claude(
                {"error": "auth-expired-no-refresh"},
                use_color=False, debug=False,
            )
        out = buf.getvalue()
        # Must not display the stale header — every "valid" check failed
        # because no displayable percent existed.
        assert "cached" not in out.lower()
        assert "auth token expired" in out
