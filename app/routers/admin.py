from fastapi import APIRouter, Body, Header, HTTPException

from app.config import settings
from app.database import dashboard_pool
from app.integrations.dashboard_db import assign_voice_number, fetch_tenant_by_slug

router = APIRouter(prefix="/admin", tags=["admin"])


def _check_admin(token: str | None) -> None:
    if not settings.admin_token or token != settings.admin_token:
        raise HTTPException(status_code=401, detail="unauthorized")


@router.get("/ping")
async def ping(x_admin_token: str | None = Header(default=None)):
    _check_admin(x_admin_token)
    return {"status": "ok"}


@router.post("/tenants/{slug}/voice-number")
async def assign_number(
    slug: str,
    payload: dict = Body(...),
    x_admin_token: str | None = Header(default=None),
):
    """
    Asigna un número Twilio (E.164, ej. '+523321015972') a un tenant existente.
    El tenant debe haber sido dado de alta antes desde el dashboard.

    Body: {"e164": "+52332...", "enabled": true}
    """
    _check_admin(x_admin_token)
    e164 = payload.get("e164")
    enabled = bool(payload.get("enabled", True))
    if not e164 or not e164.startswith("+"):
        raise HTTPException(status_code=400, detail="e164 must start with '+'")

    tenant = await fetch_tenant_by_slug(dashboard_pool(), slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"tenant '{slug}' not found")

    updated = await assign_voice_number(dashboard_pool(), slug, e164, enabled)
    if not updated:
        raise HTTPException(status_code=500, detail="update failed")
    return {
        "status": "ok",
        "tenant_id": tenant.id,
        "slug": slug,
        "voice_phone_number_e164": e164,
        "voice_enabled": enabled,
    }
