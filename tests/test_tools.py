"""
Tests de schemas y dispatch de tools.

Las dependencias HTTP a Google Calendar las mockeamos parcheando los
handlers en app.ai.tools. Verifica que execute_tool:
- Despacha al handler correcto.
- Pasa pool y wa_id correctamente.
- Maneja args faltantes y errores sin tirar excepción.
"""
import json

import pytest

import app.ai.tools as tools_module
from app.ai.tools import REALTIME_TOOLS, ToolContext, execute_tool


def test_realtime_tools_use_flat_schema():
    """Realtime no usa el wrapper {"function": {...}} de Chat Completions."""
    for t in REALTIME_TOOLS:
        assert t["type"] == "function"
        assert "name" in t  # nombre al nivel del item, no anidado
        assert "function" not in t
        assert "description" in t
        assert "parameters" in t


def test_all_expected_tools_present():
    names = {t["name"] for t in REALTIME_TOOLS}
    assert names == {
        "get_available_slots", "book_meeting", "list_my_meetings",
        "cancel_meeting", "reschedule_meeting",
    }


def test_book_meeting_required_args():
    tool = next(t for t in REALTIME_TOOLS if t["name"] == "book_meeting")
    required = set(tool["parameters"]["required"])
    assert required == {"start_iso", "end_iso", "attendee_email", "attendee_name"}


@pytest.mark.asyncio
async def test_execute_tool_dispatches_get_available_slots(monkeypatch):
    captured = {}

    async def fake_slots(pool, days_ahead, target_date=None):
        captured["pool"] = pool
        captured["days_ahead"] = days_ahead
        captured["target_date"] = target_date
        return [{"start_iso": "2026-05-10T15:00:00+00:00"}]

    monkeypatch.setattr(tools_module, "get_available_slots", fake_slots)

    ctx = ToolContext(pool="POOL_SENTINEL", wa_id="523131088881")
    result = await execute_tool("get_available_slots", {"days_ahead": 7}, ctx)
    parsed = json.loads(result)

    assert captured == {"pool": "POOL_SENTINEL", "days_ahead": 7, "target_date": None}
    assert parsed["slots"][0]["start_iso"] == "2026-05-10T15:00:00+00:00"


@pytest.mark.asyncio
async def test_execute_tool_get_slots_with_target_date(monkeypatch):
    captured = {}

    async def fake_slots(pool, days_ahead, target_date=None):
        captured["target_date"] = target_date
        return [{"start_iso": "2026-05-15T15:00:00+00:00"}]

    monkeypatch.setattr(tools_module, "get_available_slots", fake_slots)
    ctx = ToolContext(pool=None, wa_id="x")
    result = json.loads(await execute_tool(
        "get_available_slots", {"target_date": "2026-05-15"}, ctx,
    ))
    assert captured["target_date"] == "2026-05-15"
    assert result["slots"][0]["start_iso"] == "2026-05-15T15:00:00+00:00"


@pytest.mark.asyncio
async def test_execute_tool_get_slots_default_days(monkeypatch):
    async def fake_slots(pool, days_ahead, target_date=None):
        return []
    monkeypatch.setattr(tools_module, "get_available_slots", fake_slots)
    ctx = ToolContext(pool=None, wa_id="x")
    result = json.loads(await execute_tool("get_available_slots", {}, ctx))
    assert result["slots"] == []
    assert "message" in result


@pytest.mark.asyncio
async def test_execute_tool_get_slots_target_date_empty_message(monkeypatch):
    async def fake_slots(pool, days_ahead, target_date=None):
        return []
    monkeypatch.setattr(tools_module, "get_available_slots", fake_slots)
    ctx = ToolContext(pool=None, wa_id="x")
    result = json.loads(await execute_tool(
        "get_available_slots", {"target_date": "2026-05-15"}, ctx,
    ))
    assert result["slots"] == []
    assert "2026-05-15" in result["message"]


@pytest.mark.asyncio
async def test_execute_tool_book_passes_wa_id(monkeypatch):
    captured = {}

    async def fake_book(pool, **kw):
        captured.update(kw)
        captured["pool"] = pool
        return {"success": True, "event_id": "evt_1"}

    monkeypatch.setattr(tools_module, "book_meeting", fake_book)

    ctx = ToolContext(pool="P", wa_id="523131088881")
    args = {
        "start_iso": "2026-05-10T15:00:00+00:00",
        "end_iso": "2026-05-10T15:30:00+00:00",
        "attendee_email": "lead@example.com",
        "attendee_name": "Juan",
        "clinic_name": "Clinica X",
    }
    result = json.loads(await execute_tool("book_meeting", args, ctx))

    assert result["success"] is True
    assert captured["wa_id"] == "523131088881"
    assert captured["clinic_name"] == "Clinica X"
    assert captured["attendee_email"] == "lead@example.com"


@pytest.mark.asyncio
async def test_execute_tool_missing_required_arg_returns_error(monkeypatch):
    async def fake_book(pool, **kw):
        raise AssertionError("should not be called when args missing")
    monkeypatch.setattr(tools_module, "book_meeting", fake_book)

    ctx = ToolContext(pool=None, wa_id="x")
    result = json.loads(await execute_tool("book_meeting", {"start_iso": "x"}, ctx))
    assert "error" in result
    assert "Missing required argument" in result["error"]


@pytest.mark.asyncio
async def test_execute_tool_handler_exception_returns_error(monkeypatch):
    async def boom(pool, **kw):
        raise RuntimeError("calendar exploded")
    monkeypatch.setattr(tools_module, "list_user_meetings", boom)

    ctx = ToolContext(pool=None, wa_id="x")
    result = json.loads(await execute_tool(
        "list_my_meetings", {"attendee_email": "x@y.com"}, ctx,
    ))
    assert "error" in result
    assert "Tool execution failed" in result["error"]


@pytest.mark.asyncio
async def test_execute_tool_unknown_name():
    ctx = ToolContext(pool=None, wa_id="x")
    result = json.loads(await execute_tool("nonexistent", {}, ctx))
    assert "error" in result
    assert "Unknown tool" in result["error"]
