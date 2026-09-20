from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from fastapi import FastAPI

from app.bot.router import router
from app.config import get_settings
from app.db.database import close_db, health as db_health, init_db
from app.services.sync import sync_service


settings = get_settings()
logger = logging.getLogger(__name__)

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

bot = Bot(settings.telegram_bot_token)
dp = Dispatcher()
dp.include_router(router)


async def auto_sync_loop() -> None:
    first = True

    while True:
        try:
            if (
                settings.auto_sync_enabled
                and (settings.auto_sync_on_start or not first)
            ):
                logger.info(
                    "Starting auto sync for recent %s days",
                    settings.sync_recent_days,
                )
                result = await sync_service.sync_recent(settings.sync_recent_days)
                logger.info("Auto sync completed: %s", result)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Auto sync failed")

        first = False
        await asyncio.sleep(max(5, settings.sync_interval_minutes) * 60)


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

    try:
        yield
    finally:
        for task in (polling_task, sync_task):
            task.cancel()

        for task in (polling_task, sync_task):
            try:
                await task
            except asyncio.CancelledError:
                pass

        await bot.session.close()
        await close_db()


app = FastAPI(
    title="Причал AI",
    version="0.2.1",
    lifespan=lifespan,
)


@app.get("/")
async def root():
    return {
        "service": "prichal-ai",
        "version": "0.2.1",
        "status": "ok",
    }


@app.get("/health")
async def health():
    db = await db_health()
    return {
        "ok": True,
        "service": "prichal-ai",
        "version": "0.2.1",
        "database": db,
    }
