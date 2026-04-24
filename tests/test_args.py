"""Tests for argparse validators."""
import argparse

import pytest

from ai_fuelgauge import interval_seconds


class TestIntervalSeconds:
    def test_accepts_positive_int(self):
        assert interval_seconds("300") == 300
        assert interval_seconds("1") == 1
        assert interval_seconds("86400") == 86400

    def test_rejects_zero(self):
        with pytest.raises(argparse.ArgumentTypeError, match=r">=\s*1"):
            interval_seconds("0")

    def test_rejects_negative(self):
        with pytest.raises(argparse.ArgumentTypeError, match=r">=\s*1"):
            interval_seconds("-5")

    def test_rejects_non_integer(self):
        with pytest.raises(argparse.ArgumentTypeError, match="must be an integer"):
            interval_seconds("not-a-number")

    def test_rejects_float_like(self):
        with pytest.raises(argparse.ArgumentTypeError, match="must be an integer"):
            interval_seconds("5.5")

    def test_rejects_empty(self):
        with pytest.raises(argparse.ArgumentTypeError):
            interval_seconds("")

    def test_accepts_leading_plus(self):
        # int("+5") is valid Python — this is a quirk, but document it
        assert interval_seconds("+5") == 5

    def test_error_messages_include_value(self):
        with pytest.raises(argparse.ArgumentTypeError) as exc_info:
            interval_seconds("bad-value")
        assert "bad-value" in str(exc_info.value)
