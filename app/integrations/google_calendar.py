"""
Google Calendar: refresh de tokens, freeBusy, slots, creación, cancelación
y reagendamiento.

Adaptado del bot de WhatsApp pero parametrizado por pool (cada tenant tiene
su BD donde viven los `google_tokens`). El OAuth inicial NO se hace acá:
asumimos que el bot WhatsApp del tenant ya lo hizo y los tokens están en
la tabla `google_tokens`. Acá solo refrescamos.
"""
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg
import httpx

from app.config import logger, settings
from app.integrations.whatsapp import (
    extract_wa_message_id,
    is_configured as whatsapp_is_configured,
    send_whatsapp_message,
)
from app.models.conversations import save_outgoing_wa_message
from app.models.google_tokens import get_latest_token, save_google_tokens
from app.models.meetings import save_meeting, save_meeting_action, update_meeting_schedule


class CalendarUnavailableError(RuntimeError):
    """Google Calendar no pudo confirmar disponibilidad."""


SPANISH_WEEKDAYS = [
    "lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo",
]


def _calendar_tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.calendar_timezone)
    except ZoneInfoNotFoundError:
        logger.error("invalid CALENDAR_TIMEZONE=%s, falling back to UTC", settings.calendar_timezone)
        return ZoneInfo("UTC")


def _parse_iso_datetime(iso_str: str) -> datetime:
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _display_slot(slot_utc: datetime, tz: ZoneInfo) -> str:
    local_dt = slot_utc.astimezone(tz)
    weekday = SPANISH_WEEKDAYS[local_dt.weekday()]
    return f"{weekday} {local_dt:%d/%m} a las {local_dt:%H:%M}"


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
        raise CalendarUnavailableError("No se pudo consultar disponibilidad de Google Calendar")
    primary = result.get("calendars", {}).get("primary")
    if not isinstance(primary, dict):
        raise CalendarUnavailableError("Google Calendar no devolvió el calendario primary")
    if primary.get("errors"):
        raise CalendarUnavailableError(f"Google Calendar devolvió errores: {primary['errors']}")
    return primary.get("busy", []) or []


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
    tz = _calendar_tz()
    now_utc = datetime.now(timezone.utc)

    # Si pidieron una fecha específica, acotamos la búsqueda a ese día.
    if target_date:
        try:
            date_only = datetime.strptime(target_date, "%Y-%m-%d").date()
        except ValueError:
            logger.warning("get_available_slots: target_date inválida %r", target_date)
            return []
        day_start_local = datetime.combine(date_only, time.min, tzinfo=tz)
        day_start_utc = day_start_local.astimezone(timezone.utc)
        day_end_utc = (day_start_local + timedelta(days=1)).astimezone(timezone.utc)
        search_start = max(day_start_utc, now_utc)
        search_end = day_end_utc
    else:
        search_start = now_utc
        search_end = now_utc + timedelta(days=days_ahead)

    if search_start >= search_end:
        return []

    busy = await get_busy_periods(pool, search_start.isoformat(), search_end.isoformat())
    busy_intervals = []
    for b in busy:
        try:
            busy_intervals.append((
                _parse_iso_datetime(b["start"]),
                _parse_iso_datetime(b["end"]),
            ))
        except Exception:
            logger.warning("could not parse busy slot: %s", b)

    def _slot_is_free(slot_start: datetime, slot_end: datetime) -> bool:
        for b_start, b_end in busy_intervals:
            if not (slot_end <= b_start or slot_start >= b_end):
                return False
        return True

    def _all_business_slots(day_local: date) -> list[datetime]:
        if day_local.weekday() >= 5:
            return []
        slots = []
        for hour in range(9, 18):
            for minute in (0, 30):
                if hour == 17 and minute == 30:
                    continue
                local_dt = datetime.combine(day_local, time(hour, minute), tzinfo=tz)
                utc_dt = local_dt.astimezone(timezone.utc)
                if utc_dt > now_utc:
                    slots.append(utc_dt)
        return slots

    def _format_slot(slot_utc: datetime) -> dict:
        local_dt = slot_utc.astimezone(tz)
        return {
            "start_iso": slot_utc.isoformat(),
            "end_iso": (slot_utc + timedelta(minutes=30)).isoformat(),
            "display": _display_slot(slot_utc, tz),
            "date": local_dt.strftime("%Y-%m-%d"),
            "time": local_dt.strftime("%H:%M"),
        }

    # Modo "fecha específica": devolver TODOS los slots libres ese día.
    if target_date:
        all_slots = _all_business_slots(date_only)
        free = [s for s in all_slots if _slot_is_free(s, s + timedelta(minutes=30))]
        return [_format_slot(s) for s in free]

    # Modo default: 3 slots distribuidos.
    def _pick_slot(day_local: date, prefer: str) -> Optional[datetime]:
        all_slots = _all_business_slots(day_local)
        free = [s for s in all_slots if _slot_is_free(s, s + timedelta(minutes=30))]
        if not free:
            return None
        morning = [s for s in free if s.astimezone(tz).hour < 13]
        afternoon = [s for s in free if s.astimezone(tz).hour >= 13]
        primary = morning if prefer == "morning" else afternoon
        fallback = afternoon if prefer == "morning" else morning
        candidates = primary if primary else fallback
        if not candidates:
            return None
        idx = len(candidates) // 2 if len(candidates) > 1 else 0
        return candidates[idx]

    today_local = now_utc.astimezone(tz).date()
    target_offsets = [1, 3, 5]
    prefer_pattern = ["morning", "afternoon", "morning"]

    chosen: list[dict] = []
    used_dates: set = set()

    for target_offset, prefer in zip(target_offsets, prefer_pattern):
        for day_offset in range(target_offset, target_offset + 7):
            candidate_day = today_local + timedelta(days=day_offset)
            if candidate_day in used_dates:
                continue
            slot_utc = _pick_slot(candidate_day, prefer)
            if slot_utc:
                chosen.append(_format_slot(slot_utc))
                used_dates.add(candidate_day)
                break
    return chosen


def _validate_future_iso(iso_str: str) -> tuple[bool, Optional[str]]:
    """Bloquea fechas alucinadas en el pasado o > 1 año adelante."""
    try:
        dt = _parse_iso_datetime(iso_str)
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


def _validate_meeting_window(start_iso: str, end_iso: str) -> tuple[bool, Optional[str], Optional[datetime], Optional[datetime]]:
    try:
        start_dt = _parse_iso_datetime(start_iso)
        end_dt = _parse_iso_datetime(end_iso)
    except (ValueError, TypeError) as e:
        return False, f"Formato de fecha inválido: {e}", None, None

    now_utc = datetime.now(timezone.utc)
    if start_dt < now_utc:
        return False, (
            f"La fecha {start_iso} está en el pasado. Llama get_available_slots "
            "y usa exactamente uno de los slots que devuelva."
        ), None, None
    if start_dt > now_utc + timedelta(days=365):
        return False, f"La fecha {start_iso} está muy lejos en el futuro (>1 año).", None, None
    if end_dt <= start_dt:
        return False, "La hora de fin debe ser posterior a la hora de inicio.", None, None
    if end_dt - start_dt != timedelta(minutes=30):
        return False, "Las llamadas deben durar exactamente 30 minutos.", None, None

    tz = _calendar_tz()
    local_start = start_dt.astimezone(tz)
    local_end = end_dt.astimezone(tz)
    if local_start.weekday() >= 5:
        return False, "Solo se pueden agendar llamadas de lunes a viernes.", None, None
    if (
        local_start.time() < time(9, 0)
        or local_start.time() > time(17, 0)
        or local_end.time() > time(17, 30)
    ):
        return False, "El horario debe estar dentro de 9:00 a 17:30 hora México.", None, None
    return True, None, start_dt, end_dt


async def _window_is_free(pool: asyncpg.Pool, start_dt: datetime, end_dt: datetime) -> bool:
    busy = await get_busy_periods(pool, start_dt.isoformat(), end_dt.isoformat())
    for b in busy:
        try:
            b_start = _parse_iso_datetime(b["start"])
            b_end = _parse_iso_datetime(b["end"])
        except Exception:
            logger.warning("could not parse busy slot while validating booking: %s", b)
            return False
        if not (end_dt <= b_start or start_dt >= b_end):
            return False
    return True


async def book_meeting(
    pool: asyncpg.Pool,
    *,
    start_iso: str,
    end_iso: str,
    attendee_name: str,
    attendee_email: str = "",
    clinic_name: str = "",
    wa_id: str = "",
    delivery_phone: str = "",
) -> dict:
    """Crea evento en Google Calendar.

    attendee_email es OPCIONAL — si está vacío, el evento se crea sin
    attendees (solo lo ve el owner del calendar). El Meet link se manda
    por WhatsApp a `delivery_phone` (o wa_id si no se especificó).
    """
    valid, error, start_dt, end_dt = _validate_meeting_window(start_iso, end_iso)
    if not valid:
        logger.error("rejected book_meeting bad window start=%s end=%s: %s", start_iso, end_iso, error)
        return {"success": False, "error": error}
    assert start_dt is not None and end_dt is not None
    try:
        is_free = await _window_is_free(pool, start_dt, end_dt)
    except CalendarUnavailableError as e:
        logger.error("rejected book_meeting because availability check failed: %s", e)
        return {
            "success": False,
            "error": "No pude confirmar disponibilidad en Google Calendar. No agendes todavía; ofrece intentar de nuevo.",
        }
    if not is_free:
        logger.warning("rejected book_meeting busy slot start=%s end=%s", start_iso, end_iso)
        return {
            "success": False,
            "error": "Ese horario ya no está libre. Llama get_available_slots otra vez y ofrece otro horario.",
        }

    summary = f"Korelabs - Llamada con {attendee_name}"
    if clinic_name:
        summary += f" ({clinic_name})"

    contact_line = f"Contacto: {attendee_email}\n" if attendee_email else ""
    delivery_line = ""
    if delivery_phone and delivery_phone != wa_id:
        delivery_line = f"WA delivery: +{delivery_phone}\n"

    description = (
        f"Llamada de descubrimiento Korelabs con {attendee_name}.\n\n"
        f"{contact_line}"
        f"Tel: +{wa_id}\n"
        f"{delivery_line}"
        + (f"Negocio: {clinic_name}\n" if clinic_name else "")
        + "\nAgendado automáticamente por Kora (asistente AI de Korelabs)."
    )

    event: dict = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_iso, "timeZone": settings.calendar_timezone},
        "end": {"dateTime": end_iso, "timeZone": settings.calendar_timezone},
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
    # Solo añadimos attendees si el usuario realmente dio un correo —
    # sin attendees, Google no manda invite por email (el evento solo
    # existe en el calendar del owner). Eso está bien porque el Meet
    # link se entrega por WhatsApp.
    if attendee_email:
        event["attendees"] = [{"email": attendee_email}]

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

    # Enviar Meet link por WhatsApp. Es el canal PRINCIPAL de entrega cuando
    # no se dio correo. Si dieron correo, el WhatsApp es adicional al email.
    # Si el usuario pidió otro número (delivery_phone), va ahí.
    whatsapp_sent = False
    target_wa = delivery_phone or wa_id
    if meet_link and target_wa and whatsapp_is_configured():
        first_name = (attendee_name or "").split(" ")[0] or "hola"
        email_line = (
            "También te llega la invitación a tu correo.\n"
            if attendee_email else ""
        )
        wa_text = (
            f"¡Listo, {first_name}! 🎉\n\n"
            f"Tu llamada con el equipo de Korelabs está agendada.\n\n"
            f"📅 Únete por Google Meet:\n{meet_link}\n\n"
            f"{email_line}"
            f"¡Nos vemos pronto!"
        )
        wa_response = await send_whatsapp_message(target_wa, wa_text)
        if wa_response:
            whatsapp_sent = True
            wa_message_id = extract_wa_message_id(wa_response)
            try:
                # Si delivery_phone != wa_id, guardamos en el contacto del
                # número destino — así aparece en su inbox WA.
                await save_outgoing_wa_message(
                    pool, wa_id=target_wa, content=wa_text, wa_message_id=wa_message_id,
                )
            except Exception:
                logger.exception(
                    "could not persist WA message to conversations table wa_id=%s",
                    target_wa,
                )

    return {
        "success": True,
        "event_id": result["id"],
        "meet_link": meet_link,
        "html_link": result.get("htmlLink", ""),
        "whatsapp_sent": whatsapp_sent,
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
    valid, error, _, _ = _validate_meeting_window(new_start_iso, new_end_iso)
    if not valid:
        logger.error("rejected reschedule bad window start=%s end=%s: %s", new_start_iso, new_end_iso, error)
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

    await update_meeting_schedule(
        pool,
        event_id=event_id,
        wa_id=wa_id,
        attendee_email=attendee_email,
        start_iso=new_start_iso,
        end_iso=new_end_iso,
        meet_link=meet_link,
    )
    return {
        "success": True,
        "event_id": event_id,
        "new_start_iso": new_start_iso,
        "new_end_iso": new_end_iso,
        "meet_link": meet_link,
        "message": "Cita reagendada y notificaciones enviadas por correo.",
    }
