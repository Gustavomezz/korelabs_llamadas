from datetime import datetime, time, timedelta, timezone
import json

import pytest

import app.ai.tools as tools_module
import app.integrations.google_calendar as cal
from app.ai.tools import ToolContext, execute_tool
from app.integrations.google_calendar import CalendarUnavailableError


def _next_business_slot() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    for offset in range(1, 30):
        day = (now + timedelta(days=offset)).date()
        if day.weekday() >= 5:
            continue
        start = datetime.combine(day, time(15, 0), tzinfo=timezone.utc)
        if start > now:
            end = start + timedelta(minutes=30)
            return start.isoformat(), end.isoformat()
    raise AssertionError("no future weekday found")


@pytest.mark.asyncio
async def test_get_available_slots_fails_closed_when_freebusy_fails(monkeypatch):
    async def fake_request(*args, **kwargs):
        return None

    monkeypatch.setattr(cal, "google_calendar_request", fake_request)

    with pytest.raises(CalendarUnavailableError):
        await cal.get_available_slots(pool=None)


@pytest.mark.asyncio
async def test_execute_tool_reports_calendar_unavailable(monkeypatch):
    async def unavailable(*args, **kwargs):
        raise CalendarUnavailableError("freeBusy failed")

    monkeypatch.setattr(tools_module, "get_available_slots", unavailable)
    result = json.loads(await execute_tool("get_available_slots", {}, ToolContext(pool=None, wa_id="x")))

    assert result["error"] == "calendar_unavailable"
    assert "No pude confirmar disponibilidad" in result["message"]


@pytest.mark.asyncio
async def test_book_meeting_rejects_busy_slot_before_creating_event(monkeypatch):
    start_iso, end_iso = _next_business_slot()
    event_posts = []

    async def fake_request(pool, method, path, json_data=None, params=None):
        if path == "/freeBusy":
            return {"calendars": {"primary": {"busy": [{"start": start_iso, "end": end_iso}]}}}
        if path == "/calendars/primary/events":
            event_posts.append(json_data)
            return {"id": "evt_should_not_exist"}
        return None

    monkeypatch.setattr(cal, "google_calendar_request", fake_request)

    result = await cal.book_meeting(
        None,
        start_iso=start_iso,
        end_iso=end_iso,
        attendee_email="lead@example.com",
        attendee_name="Lead",
        wa_id="5213312345678",
    )

    assert result["success"] is False
    assert "ya no está libre" in result["error"]
    assert event_posts == []


@pytest.mark.asyncio
async def test_book_meeting_rejects_non_30_minute_window():
    start_iso, _ = _next_business_slot()
    bad_end = (datetime.fromisoformat(start_iso) + timedelta(minutes=45)).isoformat()

    result = await cal.book_meeting(
        None,
        start_iso=start_iso,
        end_iso=bad_end,
        attendee_email="lead@example.com",
        attendee_name="Lead",
        wa_id="5213312345678",
    )

    assert result["success"] is False
    assert "30 minutos" in result["error"]
