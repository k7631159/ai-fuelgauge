"""Tests for NaN / infinity handling in the CLI and tray renderers.

`json.loads` accepts NaN and Infinity via `parse_constant`, so a hostile or
misbehaving endpoint returning `{"utilization": NaN}` used to propagate
through to `bar()` and raise ValueError at `int(round(NaN))`. Those values
must be treated as "no data" at the render layer.
"""
import math

import pytest

pytest.importorskip("pystray")
pytest.importorskip("PIL")

import ai_fuelgauge as afg  # noqa: E402
import tray  # noqa: E402


class TestPrintBlockWithNaN:
    def test_nan_utilization_is_skipped_not_crashed(self, capsys):
        """print_block must NOT crash when primary.used_percent is NaN."""
        afg.print_block(
            name="Claude",
            plan=None,
            primary={"used_percent": float("nan"), "reset_in_seconds": 300},
            secondary={"used_percent": 20.0, "reset_in_seconds": 600},
            use_color=False,
        )
        captured = capsys.readouterr()
        # The NaN 5h line is skipped (no data) but the week line still prints.
        assert "Claude" in captured.out
        assert "20%" in captured.out
        # No "nan" token anywhere (neither lowercase nor %)
        assert "nan" not in captured.out.lower()

    def test_infinity_utilization_is_skipped(self, capsys):
        afg.print_block(
            name="Claude",
            plan=None,
            primary={"used_percent": float("inf"), "reset_in_seconds": 300},
            secondary={"used_percent": 10.0, "reset_in_seconds": 600},
            use_color=False,
        )
        captured = capsys.readouterr()
        assert "inf" not in captured.out.lower()
        assert "10%" in captured.out

    def test_string_utilization_does_not_crash(self, capsys):
        """Non-numeric types (strings from an evolved endpoint) should skip
        the row instead of raising."""
        afg.print_block(
            name="Codex",
            plan=None,
            primary={"used_percent": "forty-two", "reset_in_seconds": 300},
            secondary={"used_percent": 15.0, "reset_in_seconds": 600},
            use_color=False,
        )
        captured = capsys.readouterr()
        assert "15%" in captured.out

    def test_all_nan_shows_no_data_hint(self, capsys):
        """If every window value is NaN, show the no-data hint (not crash)."""
        afg.print_block(
            name="Claude",
            plan=None,
            primary={"used_percent": float("nan"), "reset_in_seconds": 300},
            secondary={"used_percent": float("nan"), "reset_in_seconds": 600},
            use_color=False,
            no_data_hint="no quota data",
        )
        captured = capsys.readouterr()
        assert "no quota data" in captured.out


class TestPctOrNoneNaN:
    def test_nan_returns_none(self):
        assert tray._pct_or_none({"primary": {"used_percent": float("nan")}}, "primary") is None

    def test_positive_inf_returns_none(self):
        assert tray._pct_or_none({"primary": {"used_percent": float("inf")}}, "primary") is None

    def test_negative_inf_returns_none(self):
        assert tray._pct_or_none({"primary": {"used_percent": float("-inf")}}, "primary") is None

    def test_normal_value_still_works(self):
        assert tray._pct_or_none({"primary": {"used_percent": 42.5}}, "primary") == 42.5

    def test_string_returns_none(self):
        assert tray._pct_or_none({"primary": {"used_percent": "abc"}}, "primary") is None


class TestMaxPctNaN:
    def test_nan_in_snap_does_not_propagate(self):
        """_max_pct must not return NaN just because one window has it —
        that would feed into icon threshold comparison (70/90) unpredictably."""
        snap = {
            "codex": {
                "primary": {"used_percent": float("nan")},
                "secondary": {"used_percent": 25.0},
            },
            "claude": {
                "primary": {"used_percent": 45.0},
                "secondary": {"used_percent": 10.0},
            },
        }
        result = tray._max_pct(snap)
        assert not math.isnan(result)
        assert result == 45.0

    def test_all_nan_returns_zero(self):
        snap = {
            "codex": {"primary": {"used_percent": float("nan")}},
            "claude": {"primary": {"used_percent": float("inf")}},
        }
        result = tray._max_pct(snap)
        assert result == 0.0


class TestSummaryLineNaN:
    def test_nan_renders_as_question_mark_not_nan_percent(self):
        """Before the fix, `f"{nan:.0f}%"` rendered literal 'nan%' in the tray
        title. Now _summary_line routes through _pct_or_none so NaN → '?'."""
        snap = {
            "codex": {
                "primary": {"used_percent": 30.0},
                "secondary": {"used_percent": 20.0},
            },
            "claude": {
                "primary": {"used_percent": float("nan")},
                "secondary": {"used_percent": float("nan")},
            },
        }
        result = tray._summary_line(snap)
        assert "nan" not in result.lower()
        # Multi-line tooltip: each provider on its own line, with explicit
        # `5h:` / `week:` labels. NaN values surface as `?`.
        assert "5h: 30%" in result and "week: 20%" in result
        assert "5h: ?" in result and "week: ?" in result


class TestBoolUsedPercent:
    """`bool` is a subclass of `int` in Python; `float(True)` is 1.0.
    Without explicit guards, an evolved endpoint emitting boolean
    utilization would render as 1% / 0% across the live CLI/tray paths
    (the stale path closed this in commit 8f9aa1c)."""

    def test_print_block_skips_bool_pct(self, capsys):
        from ai_fuelgauge import print_block
        print_block(
            "Test", None,
            {"used_percent": True, "reset_in_seconds": 300},
            None,
            use_color=False,
        )
        out = capsys.readouterr().out
        assert "1%" not in out
        assert "True" not in out

    def test_pct_or_none_rejects_bool(self):
        d = {"primary": {"used_percent": True}}
        assert tray._pct_or_none(d, "primary") is None

    def test_max_pct_skips_bool_in_live_path(self):
        snap = {
            "codex": {"primary": {"used_percent": True}},
            "claude": {"primary": {"used_percent": False}},
        }
        assert tray._max_pct(snap) == 0.0
