from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.agent.executive import ask_executive_agent
from app.config import get_settings
from app.services.saby import saby_client


router = Router()
settings = get_settings()
logger = logging.getLogger(__name__)
_agent_semaphore = asyncio.Semaphore(2)


def _allowed(message: Message) -> bool:
    user = message.from_user
    return bool(user and user.id in settings.allowed_user_ids)


async def _reject(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else "unknown"
    await message.answer(
        "⛔ Доступ к Причал AI не разрешён.\n"
        f"Ваш Telegram user_id: {user_id}"
    )


@router.message(CommandStart())
async def start(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    await message.answer(
        "Причал AI запущен ✅\n\n"
        "Первая сборка уже умеет читать Saby:\n"
        "• сводка продаж по сети;\n"
        "• показатели конкретного магазина;\n"
        "• топ товаров;\n"
        "• точки продаж.\n\n"
        "Пишите обычным языком, например:\n"
        "«Как вчера отработала сеть?»\n"
        "«Покажи топ-10 товаров вчера»\n"
        "«Как вчера отработал Космонавтов?»"
    )


@router.message(Command("ping"))
async def ping(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    await message.answer("pong ✅")


@router.message(Command("whoami"))
async def whoami(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    user = message.from_user
    username = f"@{user.username}" if user.username else "—"
    await message.answer(
        "🔐 Доступ разрешён\n"
        f"user_id: {user.id}\n"
        f"username: {username}"
    )


@router.message(Command("saby"))
async def saby(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    try:
        points = await saby_client.list_points()
        names = "\n".join(
            f"• {p['name']} — {p['id']}"
            for p in points[:30]
        )
        await message.answer(
            f"Saby подключён ✅\nТочек: {len(points)}\n\n{names}"
        )
    except Exception as exc:
        logger.exception("Saby check failed")
        await message.answer(f"⚠️ Ошибка Saby:\n{exc}")


@router.message(F.text)
async def ai_text(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    text = (message.text or "").strip()
    if not text:
        return

    status = await message.answer("🤖 Анализирую…")

    try:
        async with _agent_semaphore:
            answer = await ask_executive_agent(text)

        if not answer:
            answer = "Не удалось сформировать ответ."

        chunks = [
            answer[i:i + 3900]
            for i in range(0, len(answer), 3900)
        ] or [answer]

        await status.edit_text(chunks[0])
        for chunk in chunks[1:]:
            await message.answer(chunk)

    except Exception as exc:
        logger.exception("Agent request failed")
        await status.edit_text(
            "⚠️ Запрос не выполнен.\n\n"
            f"{type(exc).__name__}: {exc}"
        )
