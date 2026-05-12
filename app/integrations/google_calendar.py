"""
Google Calendar: refresh de tokens, freeBusy, slots, creación, cancelación
y reagendamiento.

Adaptado del bot de WhatsApp pero parametrizado por pool (cada tenant tiene
su BD donde viven los `google_tokens`). El OAuth inicial NO se hace acá:
asumimos que el bot WhatsApp del tenant ya lo hizo y los tokens están en
la tabla `google_tokens`. Acá solo refrescamos.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
import httpx

from app.config import logger, settings
from app.models.google_tokens import get_latest_token, save_google_tokens
from app.models.meetings import save_meeting, save_meeting_action


# ============================================
# OAuth - access token con refresh automático
# ============================================

async def get_valid_google_token(pool: asyncpg.Pool) -> tuple[Optional[str], Optional[str]]:
    """Retorna (access_token, owner_email). Refresca si está por expirar."""
    row = await get_latest_token(pool)
    if not row:
        return None, None

    expires_at = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        return row["access_token"], row["owner_email"]

    if not (settings.google_client_id and settings.google_client_secret):
        logger.error("google client_id/secret not configured; cannot refresh token")
        return None, None

    logger.info("refreshing google access token")
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "refresh_token": row["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
        if response.status_code != 200:
            logger.error("failed to refresh google token: %s", response.text)
            return None, None
        data = response.json()
        new_access = data["access_token"]
        expires_in = data.get("expires_in", 3600)
        await save_google_tokens(
            pool, owner_email=row["owner_email"], access_token=new_access,
            refresh_token=row["refresh_token"], expires_in=expires_in,
        )
        return new_access, row["owner_email"]


# ============================================
# Calendar API
# ============================================

async def google_calendar_request(
    pool: asyncpg.Pool,
    method: str,
    path: str,
    json_data: Optional[dict] = None,
    params: Optional[dict] = None,
):
    access_token, _ = await get_valid_google_token(pool)
    if not access_token:
        logger.error("no valid google token available")
        return None

    url = f"https://www.googleapis.com/calendar/v3{path}"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            if method == "GET":
                response = await client.get(url, headers=headers, params=params)
            elif method == "POST":
                response = await client.post(url, headers=headers, json=json_data, params=params)
            elif method == "PATCH":
                response = await client.patch(url, headers=headers, json=json_data, params=params)
            elif method == "DELETE":
                response = await client.delete(url, headers=headers, params=params)
            else:
                return None

            if method == "DELETE" and response.status_code in (200, 204):
                return {"deleted": True}
            if response.status_code in (200, 201):
                return response.json()

            logger.error(
                "google calendar %s %s failed: %s - %s",
                method, path, response.status_code, response.text,
            )
            return None
        except Exception:
            logger.exception("google calendar request error")
            return None


async def get_busy_periods(pool: asyncpg.Pool, start_iso: str, end_iso: str) -> list:
    body = {
        "timeMin": start_iso,
        "timeMax": end_iso,
        "timeZone": settings.calendar_timezone,
        "items": [{"id": "primary"}],
    }
    result = await google_calendar_request(pool, "POST", "/freeBusy", json_data=body)
    if not result:
        return []
    return result.get("calendars", {}).get("primary", {}).get("busy", [])


async def get_available_slots(
    pool: asyncpg.Pool,
    days_ahead: int = 14,
    target_date: Optional[str] = None,
) -> list[dict]:
    """
    Slots libres en el calendario de Gustavo. L-V, 9am-5:30pm hora México,
    slots de 30 min.

    - Sin target_date: 3 slots distribuidos en 3 días distintos (offsets
      +1, +3, +5), alterna mañana/tarde. Para la propuesta inicial.
    - Con target_date (YYYY-MM-DD): TODOS los slots libres de 30 min de
      ese día. Úsalo cuando el usuario pida un día específico.
    """
    tz_offset = timedelta(hours=-6)
    now_utc = datetime.now(timezone.utc)

    # Si pidieron una fecha específica, acotamos la búsqueda a ese día.
    if target_date:
        try:
            requested = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=None)
        except ValueError:
            logger.warning("get_available_slots: target_date inválida %r", target_date)
            return []
        day_start_utc = requested.replace(hour=0, minute=0, second=0) - tz_offset
        day_end_utc = day_start_utc + timedelta(days=1)
        search_start = max(day_start_utc, now_utc)
        search_end = day_end_utc
    else:
        search_start = now_utc
        search_end = now_utc + timedelta(days=days_ahead)

    busy = await get_busy_periods(pool, search_start.isoformat(), search_end.isoformat())
    busy_intervals = []
    for b in busy:
        try:
            busy_intervals.append((
                datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                datetime.fromisoformat(b["end"].replace("Z", "+00:00")),
            ))
        except Exception:
            logger.warning("could not parse busy slot: %s", b)

    def _slot_is_free(slot_start: datetime, slot_end: datetime) -> bool:
        for b_start, b_end in busy_intervals:
            if not (slot_end <= b_start or slot_start >= b_end):
                return False
        return True

    def _all_business_slots(day_local: datetime) -> list[datetime]:
        if day_local.weekday() >= 5:
            return []
        slots = []
        for hour in range(9, 18):
            for minute in (0, 30):
                if hour == 17 and minute == 30:
                    continue
                local_dt = day_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
                utc_dt = local_dt - tz_offset
                if utc_dt > now_utc:
                    slots.append(utc_dt)
        return slots

    def _format_slot(slot_utc: datetime) -> dict:
        local_dt = slot_utc + tz_offset
        return {
            "start_iso": slot_utc.isoformat(),
            "end_iso": (slot_utc + timedelta(minutes=30)).isoformat(),
            "display": local_dt.strftime("%A %d/%m a las %I:%M %p"),
            "date": local_dt.strftime("%Y-%m-%d"),
            "time": local_dt.strftime("%H:%M"),
        }

    # Modo "fecha específica": devolver TODOS los slots libres ese día.
    if target_date:
        try:
            day_local = datetime.strptime(target_date, "%Y-%m-%d")
        except ValueError:
            return []
        all_slots = _all_business_slots(day_local)
        free = [s for s in all_slots if _slot_is_free(s, s + timedelta(minutes=30))]
        return [_format_slot(s) for s in free]

    # Modo default: 3 slots distribuidos.
    def _pick_slot(day_local: datetime, prefer: str) -> Optional[datetime]:
        all_slots = _all_business_slots(day_local)
        free = [s for s in all_slots if _slot_is_free(s, s + timedelta(minutes=30))]
        if not free:
            return None
        morning = [s for s in free if (s + tz_offset).hour < 13]
        afternoon = [s for s in free if (s + tz_offset).hour >= 13]
        primary = morning if prefer == "morning" else afternoon
        fallback = afternoon if prefer == "morning" else morning
        candidates = primary if primary else fallback
        if not candidates:
            return None
        idx = len(candidates) // 2 if len(candidates) > 1 else 0
        return candidates[idx]

    today_local = (now_utc + tz_offset).replace(hour=0, minute=0, second=0, microsecond=0)
    target_offsets = [1, 3, 5]
    prefer_pattern = ["morning", "afternoon", "morning"]

    chosen: list[dict] = []
    used_dates: set = set()

    for target_offset, prefer in zip(target_offsets, prefer_pattern):
        for day_offset in range(target_offset, target_offset + 7):
            candidate_day = today_local + timedelta(days=day_offset)
            if candidate_day.date() in used_dates:
                continue
            slot_utc = _pick_slot(candidate_day, prefer)
            if slot_utc:
                chosen.append(_format_slot(slot_utc))
                used_dates.add(candidate_day.date())
                break
    return chosen


def _validate_future_iso(iso_str: str) -> tuple[bool, Optional[str]]:
    """Bloquea fechas alucinadas en el pasado o > 1 año adelante."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError) as e:
        return False, f"Formato de fecha inválido ({iso_str}): {e}"

    if dt < datetime.now(timezone.utc):
        return False, (
            f"La fecha {iso_str} está en el pasado. NO inventes fechas. "
            "Llama get_available_slots y usa exactamente uno de los slots que devuelva."
        )
    if dt > datetime.now(timezone.utc) + timedelta(days=365):
        return False, f"La fecha {iso_str} está muy lejos en el futuro (>1 año)."
    return True, None


async def book_meeting(
    pool: asyncpg.Pool,
    *,
    start_iso: str,
    end_iso: str,
    attendee_email: str,
    attendee_name: str,
    clinic_name: str = "",
    wa_id: str = "",
) -> dict:
    valid, error = _validate_future_iso(start_iso)
    if not valid:
        logger.error("rejected book_meeting bad start_iso=%s: %s", start_iso, error)
        return {"success": False, "error": error}

    summary = f"Korelabs - Llamada con {attendee_name}"
    if clinic_name:
        summary += f" ({clinic_name})"

    description = (
        f"Llamada de descubrimiento Korelabs con {attendee_name}.\n\n"
        f"Contacto: {attendee_email}\n"
        f"Tel: +{wa_id}\n"
        + (f"Negocio: {clinic_name}\n" if clinic_name else "")
        + "\nAgendado automáticamente por Kora (asistente AI de Korelabs)."
    )

    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_iso, "timeZone": settings.calendar_timezone},
        "end": {"dateTime": end_iso, "timeZone": settings.calendar_timezone},
        "attendees": [{"email": attendee_email}],
        "conferenceData": {
            "createRequest": {
                "requestId": f"korelabs-call-{wa_id}-{int(datetime.now().timestamp())}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "email", "minutes": 60},
                {"method": "popup", "minutes": 15},
            ],
        },
    }

    result = await google_calendar_request(
        pool, "POST", "/calendars/primary/events",
        json_data=event,
        params={"conferenceDataVersion": 1, "sendUpdates": "all"},
    )

    if not result:
        return {"success": False, "error": "No se pudo crear el evento"}

    meet_link = ""
    for ep in result.get("conferenceData", {}).get("entryPoints", []):
        if ep.get("entryPointType") == "video":
            meet_link = ep.get("uri", "")
            break

    await save_meeting(
        pool, wa_id=wa_id, event_id=result["id"], attendee_email=attendee_email,
        start_iso=start_iso, end_iso=end_iso, meet_link=meet_link,
    )
    await save_meeting_action(
        pool, wa_id=wa_id, event_id=result["id"], action="create",
        attendee_email=attendee_email,
        details=f"{attendee_name} - {clinic_name}".strip(" -"),
    )

    return {
        "success": True,
        "event_id": result["id"],
        "meet_link": meet_link,
        "html_link": result.get("htmlLink", ""),
    }


# ============================================
# List / Cancel / Reschedule
# ============================================

async def _verify_attendee(pool: asyncpg.Pool, event_id: str, attendee_email: str) -> tuple[bool, Optional[dict]]:
    event = await google_calendar_request(pool, "GET", f"/calendars/primary/events/{event_id}")
    if not event:
        return False, None
    needle = attendee_email.lower().strip()
    attendees = event.get("attendees", [])
    authorized = any((a.get("email", "") or "").lower() == needle for a in attendees)
    return authorized, event


async def list_user_meetings(
    pool: asyncpg.Pool,
    *,
    attendee_email: str,
    days_ahead: int = 90,
    days_back: int = 30,
) -> list[dict]:
    if not attendee_email:
        return []

    now_utc = datetime.now(timezone.utc)
    start_search = now_utc - timedelta(days=days_back)
    end_search = now_utc + timedelta(days=days_ahead)
    tz_offset = timedelta(hours=-6)

    params = {
        "timeMin": start_search.isoformat(),
        "timeMax": end_search.isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": 50,
    }
    result = await google_calendar_request(pool, "GET", "/calendars/primary/events", params=params)
    if not result:
        return []

    needle = attendee_email.lower().strip()
    matching = []
    for event in result.get("items", []):
        if event.get("status") == "cancelled":
            continue
        attendees = event.get("attendees", [])
        if not any((a.get("email", "") or "").lower() == needle for a in attendees):
            continue
        start = event.get("start", {})
        end = event.get("end", {})
        if "dateTime" not in start:
            continue

        try:
            start_dt = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
            display = (start_dt + tz_offset).strftime("%A %d/%m a las %I:%M %p")
        except Exception:
            display = start.get("dateTime", "")

        meet_link = ""
        for ep in event.get("conferenceData", {}).get("entryPoints", []):
            if ep.get("entryPointType") == "video":
                meet_link = ep.get("uri", "")
                break

        matching.append({
            "event_id": event.get("id"),
            "summary": event.get("summary", "(sin título)"),
            "start_iso": start.get("dateTime"),
            "end_iso": end.get("dateTime"),
            "display": display,
            "meet_link": meet_link,
        })
    return matching


async def cancel_meeting(
    pool: asyncpg.Pool, *, event_id: str, attendee_email: str, wa_id: str = "",
) -> dict:
    authorized, event = await _verify_attendee(pool, event_id, attendee_email)
    if event is None:
        return {"success": False, "error": "No se encontró la cita"}
    if not authorized:
        logger.warning("unauthorized cancel: %s on %s", attendee_email, event_id)
        return {"success": False, "error": "No tienes permiso para cancelar esta cita"}

    result = await google_calendar_request(
        pool, "DELETE", f"/calendars/primary/events/{event_id}",
        params={"sendUpdates": "all"},
    )
    if not result:
        return {"success": False, "error": "No se pudo cancelar la cita"}

    await save_meeting_action(
        pool, wa_id=wa_id, event_id=event_id, action="cancel",
        attendee_email=attendee_email,
        details=f"Original: {event.get('summary', '')} - {event.get('start', {}).get('dateTime', '')}",
    )
    return {
        "success": True,
        "event_id": event_id,
        "summary": event.get("summary", ""),
        "message": "Cita cancelada y notificaciones enviadas por correo.",
    }


async def reschedule_meeting(
    pool: asyncpg.Pool, *,
    event_id: str, new_start_iso: str, new_end_iso: str,
    attendee_email: str, wa_id: str = "",
) -> dict:
    valid, error = _validate_future_iso(new_start_iso)
    if not valid:
        logger.error("rejected reschedule bad new_start_iso=%s: %s", new_start_iso, error)
        return {"success": False, "error": error}

    authorized, event = await _verify_attendee(pool, event_id, attendee_email)
    if event is None:
        return {"success": False, "error": "No se encontró la cita"}
    if not authorized:
        logger.warning("unauthorized reschedule: %s on %s", attendee_email, event_id)
        return {"success": False, "error": "No tienes permiso para reagendar esta cita"}

    original_start = event.get("start", {}).get("dateTime", "")
    patch_data = {
        "start": {"dateTime": new_start_iso, "timeZone": settings.calendar_timezone},
        "end": {"dateTime": new_end_iso, "timeZone": settings.calendar_timezone},
    }
    result = await google_calendar_request(
        pool, "PATCH", f"/calendars/primary/events/{event_id}",
        json_data=patch_data, params={"sendUpdates": "all"},
    )
    if not result:
        return {"success": False, "error": "No se pudo reagendar la cita"}

    await save_meeting_action(
        pool, wa_id=wa_id, event_id=event_id, action="reschedule",
        attendee_email=attendee_email,
        details=f"From {original_start} to {new_start_iso}",
    )

    meet_link = ""
    for ep in result.get("conferenceData", {}).get("entryPoints", []):
        if ep.get("entryPointType") == "video":
            meet_link = ep.get("uri", "")
            break

    return {
        "success": True,
        "event_id": event_id,
        "new_start_iso": new_start_iso,
        "new_end_iso": new_end_iso,
        "meet_link": meet_link,
        "message": "Cita reagendada y notificaciones enviadas por correo.",
    }
