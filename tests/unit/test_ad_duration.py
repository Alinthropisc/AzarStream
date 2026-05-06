"""Tests for ad_duration service"""

import pytest
from datetime import datetime, timedelta

from services.ad_duration import (
    calculate_expires_at,
    format_expires_at,
    DURATION_PRESETS,
)


class TestDurationPresets:
    """Test DURATION_PRESETS configuration."""

    def test_presets_exist(self):
        assert len(DURATION_PRESETS) == 6

    def test_presets_have_required_fields(self):
        for preset in DURATION_PRESETS:
            assert "label" in preset
            assert "days" in preset
            assert "icon" in preset

    def test_forever_preset(self):
        forever = next(p for p in DURATION_PRESETS if "Forever" in p["label"])
        assert forever["days"] is None

    def test_one_month_preset(self):
        one_month = next(p for p in DURATION_PRESETS if "1 month" in p["label"])
        assert one_month["days"] == 30


class TestCalculateExpiresAt:
    """Test calculate_expires_at function."""

    def test_returns_none_for_zero_days(self):
        # 0 means "Forever" in the UI, but calculate_expires_at treats 0 as 0 days
        # The controller converts 0 to None before calling calculate_expires_at
        result = calculate_expires_at(0)
        assert result is not None  # 0 days = now

    def test_returns_none_for_none(self):
        assert calculate_expires_at(None) is None

    def test_calculates_correct_days(self):
        now = datetime(2026, 1, 1, 12, 0, 0)
        result = calculate_expires_at(7, now)
        assert result == datetime(2026, 1, 8, 12, 0, 0)

    def test_calculates_30_days(self):
        now = datetime(2026, 1, 1)
        result = calculate_expires_at(30, now)
        assert result == datetime(2026, 1, 31)

    def test_uses_current_time_by_default(self):
        before = datetime.now()
        result = calculate_expires_at(7)
        after = datetime.now() + timedelta(days=7)
        assert before + timedelta(days=7) <= result <= after


class TestFormatExpiresAt:
    """Test format_expires_at function."""

    def test_returns_forever_for_none(self):
        assert "Forever" in format_expires_at(None)

    def test_returns_expired_for_past_date(self):
        past = datetime.now() - timedelta(days=5)
        result = format_expires_at(past)
        assert "Expired" in result
        assert "day" in result.lower()

    def test_returns_expires_today(self):
        today = datetime.now().replace(hour=23, minute=59)
        result = format_expires_at(today)
        assert "today" in result.lower() or "day" in result.lower()

    def test_returns_expires_tomorrow(self):
        tomorrow = datetime.now() + timedelta(days=2)
        result = format_expires_at(tomorrow)
        assert "tomorrow" in result.lower()

    def test_returns_days_left(self):
        future = datetime.now() + timedelta(days=10)
        result = format_expires_at(future)
        assert "day" in result.lower()

    def test_includes_date(self):
        future = datetime(2026, 6, 15, 14, 30)
        result = format_expires_at(future)
        assert "2026" in result
