from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.agent.executive import ask_executive_agent
from app.config import get_settings
from app.db.database import health as db_health, reset_saby_data
from app.services.analytics import analytics_service
from app.services.saby import saby_client
from app.services.sync import sync_service
from app.services.shift_engine import shift_engine


router = Router()
settings = get_settings()
logger = logging.getLogger(__name__)

_agent_semaphore = asyncio.Semaphore(2)
_sync_lock = asyncio.Lock()
_current_sync_task: asyncio.Task | None = None


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
        "Причал AI v0.2.6 ✅\n\n"
        "Добавлено:\n"
        "• Neon/PostgreSQL;\n"
        "• история Saby;\n"
        "• позиции чеков;\n"
        "• ShiftEngine DAY/NIGHT;\n"
        "• история смен продавцов.\n\n"
        "Диагностика: /db\n"
        "Ручная синхронизация: /sync 3\n"
        "Смены: /shifts вчера\n"
        "Диагностика смен: /shiftdebug вчера\n"
        "Пересборка смен: /rebuildshifts 4\n\n"
        "Можно писать обычным языком:\n"
        "«Как вчера отработала сеть?»\n"
        "«Кто работал ночью вчера?»\n"
        "«Сколько смен отработал Иванов в этом месяце?»"
    )


@router.message(Command("ping"))
async def ping(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    await message.answer("pong ✅ | v0.2")


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


@router.message(Command("db"))
async def database_status(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    try:
        db = await db_health()
        coverage = await analytics_service.coverage()
        last = coverage.get("last_sync") or {}

        await message.answer(
            "🗄 Neon/PostgreSQL ✅\n\n"
            f"Магазинов: {db['stores']}\n"
            f"Продаж: {db['sales']}\n"
            f"Позиций: {db['sale_items']}\n"
            f"Cash shifts: {db.get('cash_shifts', 0)}\n"
            f"Рабочих смен: {db['shifts']}\n"
            f"Business dates: {coverage.get('min_date')} → {coverage.get('max_date')}\n"
            f"Последний sync: {last.get('status', '—')}"
        )
    except Exception as exc:
        logger.exception("DB check failed")
        await message.answer(f"⚠️ Ошибка PostgreSQL:\n{exc}")


@router.message(Command("sync"))
async def manual_sync(message: Message) -> None:
    global _current_sync_task

    if not _allowed(message):
        await _reject(message)
        return

    parts = (message.text or "").split()
    days = settings.sync_recent_days

    if len(parts) > 1:
        try:
            days = int(parts[1])
        except ValueError:
            await message.answer("Формат: /sync 3")
            return

    days = max(1, min(days, settings.max_manual_sync_days))

    if _sync_lock.locked() or (
        _current_sync_task is not None
        and not _current_sync_task.done()
    ):
        await message.answer(
            "⏳ Синхронизация уже выполняется.\n"
            "Для отмены: /cancelsync"
        )
        return

    status = await message.answer(
        f"🔄 Синхронизирую последние {days} дн...\n"
        "Отмена: /cancelsync"
    )

    try:
        async with _sync_lock:
            _current_sync_task = asyncio.create_task(
                sync_service.sync_recent(days)
            )
            result = await _current_sync_task

        await status.edit_text(
            "✅ Синхронизация завершена\n\n"
            f"Период: {result['date_from']} → {result['date_to']}\n"
            f"Точек: {result['stores']}\n"
            f"Продаж upsert: {result['sales_upserted']}\n"
            f"Позиций: {result['items_upserted']}\n"
            f"Смен построено: {result['shifts_built']}\n"
            f"Saby jobs: {result.get('fetch_jobs', '—')} | "
            f"параллельность: {result.get('concurrency', '—')}"
        )

    except asyncio.CancelledError:
        await status.edit_text("🛑 Синхронизация отменена.")

    except Exception as exc:
        logger.exception("Manual sync failed")
        await status.edit_text(f"⚠️ Ошибка sync:\n{exc}")

    finally:
        _current_sync_task = None


@router.message(Command("cancelsync"))
async def cancel_sync(message: Message) -> None:
    global _current_sync_task

    if not _allowed(message):
        await _reject(message)
        return

    if (
        _current_sync_task is None
        or _current_sync_task.done()
    ):
        await message.answer("ℹ️ Активной ручной синхронизации нет.")
        return

    _current_sync_task.cancel()
    await message.answer("🛑 Команда отмены отправлена.")


@router.message(Command("shifts"))
async def shifts(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    parts = (message.text or "").split(maxsplit=1)
    date_value = parts[1] if len(parts) > 1 else "вчера"

    try:
        data = await analytics_service.shift_summary(date_value)
        rows = data["shifts"][:30]

        lines = [
            f"🕒 Рабочие смены за business date {data['business_date']}",
            f"Всего: {data['total']} | AUTO: {data['auto']} | "
            f"REVIEW: {data['review']} | AMBIGUOUS: {data['ambiguous']}",
            f"Saby native: {data.get('saby_native', 0)} | "
            f"fallback: {data.get('fallback', 0)}",
            "",
        ]

        for row in rows:
            icon = "☀️" if row["shift_type"] == "DAY" else "🌙"
            lines.append(
                f"{icon} {row['store']} — {row['seller_name']}\n"
                f"{row['check_count']} чек. | {row['net_revenue']:.2f} ₽ | "
                f"{row['cash_shift_count']} cash shift | "
                f"{row['status']} {row['confidence']:.0%}"
            )

        if data["total"] > len(rows):
            lines.append(f"\nПоказаны первые {len(rows)} из {data['total']}.")

        await message.answer("\n".join(lines)[:3900])

    except Exception as exc:
        logger.exception("Shift report failed")
        await message.answer(f"⚠️ Ошибка:\n{exc}")



@router.message(Command("shiftdebug"))
async def shift_debug(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    parts = (message.text or "").split(maxsplit=1)
    date_value = parts[1] if len(parts) > 1 else "вчера"

    try:
        data = await analytics_service.shift_diagnostics(date_value)

        checks = data["checks"]
        cash = data["cash_shifts"]
        work = data["work_shifts"]

        await message.answer(
            "🔎 Business Day / Shift debug\n\n"
            f"Business date: {data['business_date']}\n"
            f"TZ: {data['timezone']}\n"
            f"Day start: {data['business_day_start_hour']:02d}:00\n\n"

            f"Чеков: {checks['checks']}\n"
            f"DAY checks: {checks['day_checks']}\n"
            f"NIGHT checks: {checks['night_checks']}\n"
            f"Seller ID: {checks['seller_id_checks']}\n"
            f"Saby Shift ID: {checks['shift_id_checks']}\n"
            f"CarriedWTZ: {checks['carried_time']}\n\n"

            f"Cash shifts: {cash['total']}\n"
            f"— Saby native: {cash['native']}\n"
            f"— fallback: {cash['fallback']}\n\n"

            f"Employee work shifts: {work['total']}\n"
            f"— DAY: {work['day']}\n"
            f"— NIGHT: {work['night']}\n"
            f"— AUTO: {work['auto']}\n"
            f"— REVIEW: {work['review']}\n"
            f"Cash segments inside work shifts: {work['cash_segments']}"
        )

    except Exception as exc:
        logger.exception("Shift debug failed")

        await message.answer(
            f"⚠️ Shift debug error:\n{exc}"
        )


@router.message(Command("rebuildshifts"))
async def rebuild_shifts(message: Message) -> None:
    if not _allowed(message):
        await _reject(message); return
    parts=(message.text or '').split(); days=4
    if len(parts)>1:
        try: days=max(1,min(int(parts[1]),60))
        except ValueError:
            await message.answer('Формат: /rebuildshifts 4'); return
    try:
        from datetime import datetime,timedelta
        from zoneinfo import ZoneInfo
        today=datetime.now(ZoneInfo(settings.business_tz)).date()
        date_from=today-timedelta(days=days-1)
        count=await shift_engine.rebuild_range(date_from,today)
        await message.answer(f"✅ ShiftEngine пересобран\n\nПериод: {date_from.isoformat()} → {today.isoformat()}\nСмен: {count}")
    except Exception as exc:
        logger.exception('Shift rebuild failed'); await message.answer(f"⚠️ Shift rebuild error:\n{exc}")


@router.message(Command("reconcile"))
async def reconcile_business_day(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    parts = (message.text or "").split(maxsplit=1)
    date_value = parts[1] if len(parts) > 1 else "вчера"

    try:
        data = await analytics_service.reconcile(date_value)

        lines = [
            f"🧮 Reconcile business day {data['business_date']}",
            f"Окно: {data['window']}",
            "",
        ]

        for row in data["stores"]:
            icon = "✅" if row["status"] == "OK" else "⚠️"

            lines.append(
                f"{icon} {row['store']}\n"
                f"RAW: {row['raw_revenue']:.2f} ₽ / {row['raw_checks']} чек.\n"
                f"Смены: {row['work_revenue']:.2f} ₽ / {row['work_checks']} чек.\n"
                f"Разница: {row['difference']:.2f} ₽ | "
                f"work shifts: {row['work_shifts']} | "
                f"без продавца: {row['no_seller_checks']} | "
                f"review shifts: {row['review_shifts']}"
            )

        lines.append(
            "\nИТОГ: "
            + ("✅ OK" if data["ok"] else "⚠️ REVIEW")
        )

        text = "\n\n".join(lines)

        for index in range(0, len(text), 3900):
            await message.answer(
                text[index:index + 3900]
            )

    except Exception as exc:
        logger.exception("Reconcile failed")

        await message.answer(
            f"⚠️ Reconcile error:\n{exc}"
        )


@router.message(Command("resetdata"))
async def reset_data(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    parts = (message.text or "").split(maxsplit=1)

    if len(parts) < 2 or parts[1].strip().lower() != "confirm":
        await message.answer(
            "⚠️ Эта команда удалит ВСЕ загруженные Saby-данные "
            "и производную аналитику Причал AI.\n\n"
            "Причал Core не затрагивается.\n\n"
            "Для подтверждения отправьте:\n"
            "/resetdata confirm"
        )
        return

    if (
        _current_sync_task is not None
        and not _current_sync_task.done()
    ):
        await message.answer(
            "⛔ Сначала остановите sync: /cancelsync"
        )
        return

    try:
        await reset_saby_data()

        await message.answer(
            "🧹 Saby/analytics data очищены.\n\n"
            "Следующий шаг для чистой загрузки:\n"
            "/sync 7"
        )

    except Exception as exc:
        logger.exception("Reset data failed")

        await message.answer(
            f"⚠️ Reset error:\n{exc}"
        )




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
