from datetime import datetime

import pytest

from app.models.meetings import _calendar_local_naive, update_meeting_schedule


def test_calendar_local_naive_converts_utc_to_mexico_city():
    assert _calendar_local_naive("2026-05-15T21:00:00+00:00") == datetime(2026, 5, 15, 15, 0)


def test_calendar_local_naive_leaves_naive_values_untouched():
    assert _calendar_local_naive("2026-05-15T15:00:00") == datetime(2026, 5, 15, 15, 0)


class _FakeConn:
    def __init__(self):
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append((query, args))


class _AcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self):
        self.conn = _FakeConn()

    def acquire(self):
        return _AcquireCtx(self.conn)


@pytest.mark.asyncio
async def test_update_meeting_schedule_keeps_caller_contact_and_local_time():
    pool = _FakePool()

    await update_meeting_schedule(
        pool,
        event_id="evt_juan",
        wa_id="5213139617442",
        attendee_email="jbarraganacevedo@gmail.com",
        start_iso="2026-05-18T17:00:00+00:00",
        end_iso="2026-05-18T17:30:00+00:00",
        meet_link="https://meet.google.com/qxa-yszd-xwc",
    )

    _, args = pool.conn.calls[0]
    assert args[0] == "evt_juan"
    assert args[1] == "5213139617442"
    assert args[2] == "jbarraganacevedo@gmail.com"
    assert args[3] == datetime(2026, 5, 18, 11, 0)
    assert args[4] == datetime(2026, 5, 18, 11, 30)
