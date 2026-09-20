from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from fastapi import FastAPI

from app.bot.router import router
from app.config import get_settings


settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

bot = Bot(settings.telegram_bot_token)
dp = Dispatcher()
dp.include_router(router)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot.delete_webhook(drop_pending_updates=False)

    polling_task = asyncio.create_task(
        dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    )

    try:
        yield
    finally:
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()


app = FastAPI(
    title="Причал AI",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/")
async def root():
    return {
        "service": "prichal-ai",
        "version": "0.1.0",
        "status": "ok",
    }


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "prichal-ai",
        "version": "0.1.0",
    }
