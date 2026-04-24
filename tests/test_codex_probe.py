"""Tests for Codex probe tolerance — _normalize_codex_window + main render guard.

Covers robustness review items H2, M2, M3.
"""
from unittest.mock import patch

import pytest

from ai_fuelgauge import _normalize_codex_window


class TestNormalizeCodexWindow:
    """H2 + M2 regression tests."""

    def test_valid_int_resetsAt(self):
        result = _normalize_codex_window(
            {
                "usedPercent": 25,
                "windowDurationMins": 300,
                "resetsAt": 1_800_000_000,
            },
            now=1_700_000_000,
        )
        assert result["used_percent"] == 25
        assert result["window_minutes"] == 300
        assert result["reset_at"] == 1_800_000_000
        assert result["reset_in_seconds"] == 100_000_000

    def test_resetsAt_equal_to_zero_preserved(self):
        """Regression for M2: `resetsAt: 0` was being silently dropped by
        `w.get("resetsAt") or w.get("reset_at")` because `0` is falsy."""
        result = _normalize_codex_window(
            {"usedPercent": 0, "resetsAt": 0},
            now=1_700_000_000,
        )
        assert result["reset_at"] == 0
        assert result["reset_in_seconds"] == -1_700_000_000

    def test_resetsAt_as_string_does_not_crash(self):
        """Regression for H2: non-numeric `resetsAt` (e.g. ISO string from a
        future plan-tier variant) used to raise ValueError."""
        result = _normalize_codex_window(
            {"usedPercent": 25, "resetsAt": "2026-05-01T10:00:00Z"},
            now=1_700_000_000,
        )
        assert result["reset_at"] == "2026-05-01T10:00:00Z"
        assert result["reset_in_seconds"] is None  # countdown unknown, not error

    def test_resetsAt_none_yields_none(self):
        result = _normalize_codex_window(
            {"usedPercent": 25, "resetsAt": None},
            now=1_700_000_000,
        )
        assert result["reset_at"] is None
        assert result["reset_in_seconds"] is None

    def test_missing_resetsAt_falls_back_to_snake_case(self):
        """Backward-compat with older sqlite snapshot shape (reset_at)."""
        result = _normalize_codex_window(
            {"usedPercent": 25, "reset_at": 1_800_000_000},
            now=1_700_000_000,
        )
        assert result["reset_at"] == 1_800_000_000

    def test_explicit_null_resetsAt_falls_back_to_snake_case(self):
        """Per Codex pre-commit review: when `resetsAt` is present but None
        AND `reset_at` has a value, fallback should still take the snake-case
        value, not stick at None."""
        result = _normalize_codex_window(
            {"resetsAt": None, "reset_at": 1_800_000_000},
            now=1_700_000_000,
        )
        assert result["reset_at"] == 1_800_000_000
        assert result["reset_in_seconds"] == 100_000_000

    def test_non_dict_input_returns_empty_dict(self):
        assert _normalize_codex_window(None, now=1_700_000_000) == {}
        assert _normalize_codex_window([], now=1_700_000_000) == {}
        assert _normalize_codex_window("not a dict", now=1_700_000_000) == {}
        assert _normalize_codex_window(42, now=1_700_000_000) == {}

    def test_empty_dict_returns_all_none(self):
        assert _normalize_codex_window({}, now=1_700_000_000) == {
            "used_percent": None,
            "window_minutes": None,
            "reset_at": None,
            "reset_in_seconds": None,
        }

    def test_window_duration_snake_case_fallback(self):
        result = _normalize_codex_window(
            {"window_minutes": 10080},
            now=1_700_000_000,
        )
        assert result["window_minutes"] == 10080

    def test_dict_with_float_resetsAt_accepted(self):
        result = _normalize_codex_window(
            {"resetsAt": 1_800_000_000.5},
            now=1_700_000_000,
        )
        # int(float) truncates — that's fine, countdown still populated
        assert result["reset_at"] == 1_800_000_000.5
        assert result["reset_in_seconds"] == 100_000_000


class TestMainRenderStaleTimestampGuard:
    """M3 regression: main() must tolerate non-numeric `as_of` without crashing."""

    def test_non_numeric_as_of_does_not_crash_render(self, capsys, isolated_paths):
        """sqlite `ts` column can hold whatever got logged — a malformed row
        must not break the whole CLI after we've already started rendering."""
        import ai_fuelgauge as afg

        broken_data = {
            "codex": {
                "plan": "plus",
                "as_of": "not-a-timestamp",  # <-- the poison value
                "primary": {
                    "used_percent": 10,
                    "reset_in_seconds": 300,
                    "window_minutes": 300,
                    "reset_at": None,
                },
                "secondary": {
                    "used_percent": 5,
                    "reset_in_seconds": 500,
                    "window_minutes": 10080,
                    "reset_at": None,
                },
                "_source": "sqlite-snapshot",
            },
            "claude": None,
            "_from_cache": True,
        }

        with patch("ai_fuelgauge.load_cache", return_value=broken_data):
            exit_code = afg.main(["--no-color"])

        assert exit_code == 0
        captured = capsys.readouterr()
        # Codex section should still render — the guard prevents the crash
        assert "Codex" in captured.out
        # The 10% utilization line should appear
        assert "10%" in captured.out

    def test_none_as_of_is_fine(self, capsys, isolated_paths):
        """as_of absent / None is the normal path — must not regress."""
        import ai_fuelgauge as afg

        data = {
            "codex": {
                "as_of": None,
                "primary": {
                    "used_percent": 20,
                    "reset_in_seconds": 300,
                    "window_minutes": 300,
                    "reset_at": None,
                },
                "secondary": None,
            },
            "claude": None,
            "_from_cache": True,
        }

        with patch("ai_fuelgauge.load_cache", return_value=data):
            exit_code = afg.main(["--no-color"])

        assert exit_code == 0

    def test_bool_as_of_is_rejected(self, capsys, isolated_paths):
        """Per Codex pre-commit review: bool is int in Python (`int(False)==0`),
        which would render as a 1970-epoch "stale 56 years ago" — obviously
        wrong. The guard must exclude bool before int()."""
        import ai_fuelgauge as afg

        data = {
            "codex": {
                "as_of": False,  # <-- boolean sneaks past int() guard
                "primary": {
                    "used_percent": 15,
                    "reset_in_seconds": 300,
                    "window_minutes": 300,
                    "reset_at": None,
                },
                "secondary": None,
            },
            "claude": None,
            "_from_cache": True,
        }

        with patch("ai_fuelgauge.load_cache", return_value=data):
            exit_code = afg.main(["--no-color"])

        assert exit_code == 0
        # A bogus "ago" marker would look like "(as of 56y ago)" — make sure
        # we didn't render that
        captured = capsys.readouterr()
        assert "56y" not in captured.out
        assert "1970" not in captured.out
