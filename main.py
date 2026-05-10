from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import logger
from app.database import close_pools, init_pools
from app.routers import admin, twilio_status, twilio_stream, twilio_voice


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("starting korelabs_llamadas")
    await init_pools()
    yield
    await close_pools()
    logger.info("shutdown complete")


app = FastAPI(title="korelabs_llamadas", lifespan=lifespan)

app.include_router(admin.router)
app.include_router(twilio_voice.router)
app.include_router(twilio_status.router)
app.include_router(twilio_stream.router)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "service": "korelabs_llamadas"}


@app.get("/")
async def root():
    return {"service": "korelabs_llamadas", "docs": "/docs"}
