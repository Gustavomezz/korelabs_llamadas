from fastapi import APIRouter, Header, HTTPException

from app.config import settings

router = APIRouter(prefix="/admin", tags=["admin"])


def _check_admin(token: str | None) -> None:
    if not settings.admin_token or token != settings.admin_token:
        raise HTTPException(status_code=401, detail="unauthorized")


@router.get("/ping")
async def ping(x_admin_token: str | None = Header(default=None)):
    _check_admin(x_admin_token)
    return {"status": "ok"}
