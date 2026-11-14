from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from fastapi import FastAPI

from app.bot.router import router
from app.bot.presentation import TelegramPresentation
from app.config import get_settings
from app.core_api import router as core_api_router
from app.price_lists_api import router as price_lists_api_router
from app.services.core_schedule import schedule_loop
from app.db.database import close_db, health as db_health, init_db
from app.services.sync import sync_service
from app.services.auto_sources import run_loop
from app.services.storage import storage
from app.version import APP_VERSION, BUILD_TAG


settings = get_settings()
logger = logging.getLogger(__name__)

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

bot = Bot(settings.telegram_bot_token)
bot.session.middleware(TelegramPresentation())
dp = Dispatcher()
dp.include_router(router)


async def auto_sync_loop() -> None:
    await run_loop()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await bot.delete_webhook(drop_pending_updates=False)

    polling_task = asyncio.create_task(
        dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    )
    sync_task = asyncio.create_task(auto_sync_loop())
    core_schedule_task = asyncio.create_task(schedule_loop())

    try:
        yield
    finally:
        for task in (polling_task, sync_task, core_schedule_task):
            task.cancel()

        for task in (polling_task, sync_task, core_schedule_task):
            try:
                await task
            except asyncio.CancelledError:
                pass

        await bot.session.close()
        await close_db()


app = FastAPI(
    title="Причал AI",
    version=APP_VERSION,
    lifespan=lifespan,
)


app.include_router(core_api_router)
app.include_router(price_lists_api_router)

@app.get("/")
async def root():
    return {
        "service": "prichal-ai",
        "version": APP_VERSION,
        "status": "ok",
        "build": BUILD_TAG,
    }


@app.get("/health")
async def health():
    db = await db_health()
    s3 = await storage.health()
    return {
        "ok": bool(db.get("ok")) and bool(s3.get("ok")),
        "service": "prichal-ai",
        "version": APP_VERSION,
        "build": BUILD_TAG,
        "database": db,
        "s3": s3,
    }
