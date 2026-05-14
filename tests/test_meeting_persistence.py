from datetime import datetime

from app.models.meetings import _calendar_local_naive


def test_calendar_local_naive_converts_utc_to_mexico_city():
    assert _calendar_local_naive("2026-05-15T21:00:00+00:00") == datetime(2026, 5, 15, 15, 0)


def test_calendar_local_naive_leaves_naive_values_untouched():
    assert _calendar_local_naive("2026-05-15T15:00:00") == datetime(2026, 5, 15, 15, 0)
