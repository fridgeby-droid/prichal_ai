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
from app.services.backfill import backfill_service
from app.services.saby import saby_client
from app.services.sync import sync_service
from app.services.storage import storage
from app.services.shift_engine import shift_engine
from app.services.payroll import payroll_service
from app.version import APP_VERSION, BUILD_TAG


router = Router()
settings = get_settings()
logger = logging.getLogger(__name__)

_agent_semaphore = asyncio.Semaphore(2)
_sync_lock = asyncio.Lock()
_current_sync_task: asyncio.Task | None = None
_current_backfill_task: asyncio.Task | None = None
_current_backfill_run_id: int | None = None


def _allowed(message: Message) -> bool:
    user = message.from_user
    return bool(user and user.id in settings.allowed_user_ids)


async def _reject(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else "unknown"
    await message.answer(
        "⛔ Доступ к Причал AI не разрешён.\n"
        f"Ваш Telegram user_id: {user_id}"
    )


def _backfill_status_text(state: dict) -> str:
    status_icons = {
        "PENDING": "⏳",
        "RUNNING": "🔄",
        "PAUSED": "⏸",
        "ERROR": "⚠️",
        "COMPLETED": "✅",
    }

    icon = status_icons.get(state["status"], "ℹ️")

    lines = [
        f"{icon} Backfill #{state['id']} — {state['status']}",
        f"Период: {state['date_from']} → {state['date_to']}",
        f"Прогресс: {state['completed_days']}/{state['total_days']} дней "
        f"({state['progress_percent']:.1f}%)",
        f"Блоков: {state['chunks_completed']}/"
        f"~{state['total_chunks_estimate']}",
        f"Следующая дата: {state['next_date']}",
        f"Продаж upsert: {state['sales_upserted']}",
        f"Позиций upsert: {state['items_upserted']}",
        f"Смен построено: {state['shifts_built']}",
    ]

    if state.get("last_chunk_from"):
        lines.append(
            f"Последний блок: {state['last_chunk_from']} → "
            f"{state['last_chunk_to']}"
        )

    if state.get("last_error"):
        lines.append(f"Ошибка: {state['last_error'][:700]}")

    return "\n".join(lines)


async def _run_backfill_for_chat(
    bot,
    chat_id: int,
    run_id: int,
) -> None:
    global _current_backfill_task, _current_backfill_run_id

    async def progress(state: dict) -> None:
        await bot.send_message(
            chat_id,
            _backfill_status_text(state),
        )

    try:
        final = await backfill_service.run(
            run_id,
            progress_callback=progress,
        )

        if final["status"] == "COMPLETED":
            await bot.send_message(
                chat_id,
                "✅ Историческая загрузка завершена.\n\n"
                + _backfill_status_text(final),
            )

    except asyncio.CancelledError:
        state = await backfill_service.get_run(run_id)
        await bot.send_message(
            chat_id,
            "⏸ Историческая загрузка приостановлена.\n"
            "Продолжить: /backfillresume\n\n"
            + (_backfill_status_text(state) if state else ""),
        )

    except Exception as exc:
        logger.exception("Backfill task failed")

        state = await backfill_service.get_run(run_id)

        await bot.send_message(
            chat_id,
            "⚠️ Историческая загрузка остановилась с ошибкой.\n"
            "После устранения причины: /backfillresume\n\n"
            + (_backfill_status_text(state) if state else str(exc)),
        )

    finally:
        _current_backfill_task = None
        _current_backfill_run_id = None


@router.message(CommandStart())
async def start(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    await message.answer(
        "Причал AI " + APP_VERSION + " ✅\n\n"
        "Добавлено:\n"
        "• Timeweb PostgreSQL + S3;\n"
        "• история Saby;\n"
        "• позиции чеков;\n"
        "• ShiftEngine DAY/NIGHT;\n"
        "• история смен продавцов.\n\n"
        "Диагностика: /db\n"
        "Ручная синхронизация: /sync 3\n"
        "Магазины (первый этап): /loadstores\n"
        "История: /backfill 2026-07-01 2026-09-22\n"
        "Статус истории: /backfillstatus\n"
        "Планы: /plans [магазин]\n"
        "Payroll policy: /paypolicies SELLER\n"
        "Payroll preview: /payrollpreview 2026-09-19\n"
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
    await message.answer(
        f"pong ✅ | v{APP_VERSION} | {BUILD_TAG}"
    )


@router.message(Command("version"))
async def version(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    await message.answer(
        "ℹ️ Причал AI\n"
        f"version: {APP_VERSION}\n"
        f"build: {BUILD_TAG}"
    )


@router.message(Command("infrahealth"))
async def infra_health(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    try:
        db = await db_health()
        s3 = await storage.health()
        db_icon = "✅" if db.get("ok") else "❌"
        s3_icon = "✅" if s3.get("ok") else "❌"
        lines = [
            "🧱 Infrastructure health",
            "",
            f"{db_icon} PostgreSQL",
            f"DB: {db.get('database_name', '—')}",
            f"Schema: {db.get('schema', '—')}",
            f"TLS: {db.get('ssl_mode', '—')}",
            f"Stores: {db.get('stores', 0)} | Sales: {db.get('sales', 0)} | Shifts: {db.get('shifts', 0)}",
            "",
            f"{s3_icon} S3",
            f"Enabled: {s3.get('enabled', False)}",
            f"Bucket: {s3.get('bucket', '—')}",
            f"Prefix: {s3.get('prefix', '—')}",
        ]
        if s3.get("error"):
            lines.append(f"S3 error: {s3['error']}")
        await message.answer("\n".join(lines))
    except Exception as exc:
        logger.exception("Infrastructure health failed")
        await message.answer(f"⚠️ Infrastructure health error:\n{exc}")


@router.message(Command("buildinfo"))
async def buildinfo(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    await message.answer(
        "🧩 Build info\n"
        f"app_version: {APP_VERSION}\n"
        f"build_tag: {BUILD_TAG}\n"
        "paymentdebug_handler: DIRECT\n"
        "business_day: 08:00→08:00\n"
        "money_source: sale_payments"
    )


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
            "🗄 Timeweb PostgreSQL + S3 ✅\n\n"
            f"Магазинов: {db['stores']}\n"
            f"Продаж: {db['sales']}\n"
            f"Позиций: {db['sale_items']}\n"
            f"Payment/check facts: {db.get('sale_payments', 0)}\n"
            f"Cash shifts: {db.get('cash_shifts', 0)}\n"
            f"Рабочих смен: {db['shifts']}\n"
            f"Business dates: {coverage.get('min_date')} → {coverage.get('max_date')}\n"
            f"Последний sync: {last.get('status', '—')}"
        )
    except Exception as exc:
        logger.exception("DB check failed")
        await message.answer(f"⚠️ Ошибка PostgreSQL:\n{exc}")



@router.message(Command("loadstores"))
async def load_stores(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return
    if any(task and not task.done() for task in (_current_sync_task, _current_backfill_task)):
        await message.answer("Сначала дождитесь завершения sync/backfill или приостановите его.")
        return
    try:
        points = await sync_service.sync_stores()
        text = "Магазины загружены: " + str(len(points)) + "\n" + "\n".join(
            f"{p['id']}: {p['name']}" for p in points
        )
        for offset in range(0, len(text), 3900):
            await message.answer(text[offset:offset + 3900])
    except Exception as exc:
        await message.answer(f"Загрузка stores остановлена: {exc}")


@router.message(Command("backfill"))
async def backfill(message: Message) -> None:
    global _current_backfill_task, _current_backfill_run_id

    if not _allowed(message):
        await _reject(message)
        return

    if (
        _current_backfill_task is not None
        and not _current_backfill_task.done()
    ):
        await message.answer(
            "⏳ Историческая загрузка уже выполняется.\n"
            "Статус: /backfillstatus\n"
            "Пауза: /backfillcancel"
        )
        return

    if (
        _current_sync_task is not None
        and not _current_sync_task.done()
    ):
        await message.answer(
            "⛔ Сейчас выполняется ручной /sync. "
            "Сначала завершите или отмените его."
        )
        return

    parts = (message.text or "").split()

    if len(parts) not in (2, 3):
        await message.answer(
            "Формат:\n"
            "/backfill 2026-07-01 2026-09-22\n\n"
            "Если конечную дату не указать:\n"
            "/backfill 2026-07-01"
        )
        return

    from datetime import date, datetime, timedelta
    from zoneinfo import ZoneInfo

    try:
        date_from = date.fromisoformat(parts[1])
        date_to = (
            date.fromisoformat(parts[2])
            if len(parts) == 3
            else (datetime.now(ZoneInfo(settings.business_tz))
                  - timedelta(hours=settings.business_day_start_hour)).date() - timedelta(days=1)
        )
    except ValueError:
        await message.answer(
            "Дата должна быть в формате YYYY-MM-DD."
        )
        return

    if date_to < date_from:
        await message.answer(
            "Дата окончания не может быть раньше даты начала."
        )
        return

    try:
        run_id = await backfill_service.create_run(date_from, date_to)
    except ValueError as exc:
        await message.answer(str(exc))
        return

    _current_backfill_run_id = run_id
    _current_backfill_task = asyncio.create_task(
        _run_backfill_for_chat(
            message.bot,
            message.chat.id,
            run_id,
        )
    )

    total_days = (date_to - date_from).days + 1
    estimated_chunks = (
        total_days + settings.backfill_chunk_days - 1
    ) // settings.backfill_chunk_days

    warning = ""

    await message.answer(
        "🚚 Историческая загрузка запущена.\n\n"
        f"Run: #{run_id}\n"
        f"Период: {date_from.isoformat()} → {date_to.isoformat()}\n"
        f"Business dates: {total_days}\n"
        f"Размер блока: {settings.backfill_chunk_days} дней\n"
        f"Ориентировочно блоков: {estimated_chunks}\n\n"
        "На конец каждого блока автоматически забирается ещё "
        "следующий календарный день для ночной смены.\n\n"
        "Каждый блок проходит reconcile до сохранения прогресса.\n"
        "Статус: /backfillstatus\n"
        "Пауза: /backfillcancel"
        + warning
    )


@router.message(Command("backfillstatus"))
async def backfill_status(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    state = await backfill_service.latest_run()

    if not state:
        await message.answer(
            "ℹ️ Исторических загрузок ещё нет."
        )
        return

    await message.answer(
        _backfill_status_text(state)
    )


@router.message(Command("backfillcancel"))
async def backfill_cancel(message: Message) -> None:
    global _current_backfill_task, _current_backfill_run_id

    if not _allowed(message):
        await _reject(message)
        return

    if (
        _current_backfill_task is None
        or _current_backfill_task.done()
    ):
        state = await backfill_service.latest_resumable_run()

        if state and state["status"] == "RUNNING":
            await backfill_service.pause(state["id"])
            await message.answer(
                "⏸ В БД оставался RUNNING backfill без активной задачи. "
                "Он переведён в PAUSED.\n"
                "Продолжить: /backfillresume"
            )
            return

        await message.answer(
            "ℹ️ Активной исторической загрузки нет."
        )
        return

    _current_backfill_task.cancel()

    await message.answer(
        "⏸ Команда паузы отправлена. "
        "Текущий незавершённый блок может откатиться; "
        "следующий запуск повторит его безопасно."
    )


@router.message(Command("backfillresume"))
async def backfill_resume(message: Message) -> None:
    global _current_backfill_task, _current_backfill_run_id

    if not _allowed(message):
        await _reject(message)
        return

    if (
        _current_backfill_task is not None
        and not _current_backfill_task.done()
    ):
        await message.answer(
            "⏳ Backfill уже выполняется."
        )
        return

    if (
        _current_sync_task is not None
        and not _current_sync_task.done()
    ):
        await message.answer(
            "⛔ Сейчас выполняется ручной /sync."
        )
        return

    state = await backfill_service.latest_resumable_run()

    if not state:
        await message.answer(
            "ℹ️ Нет незавершённого backfill для продолжения."
        )
        return

    run_id = int(state["id"])

    _current_backfill_run_id = run_id
    _current_backfill_task = asyncio.create_task(
        _run_backfill_for_chat(
            message.bot,
            message.chat.id,
            run_id,
        )
    )

    await message.answer(
        "▶️ Историческая загрузка продолжена.\n\n"
        + _backfill_status_text(state)
    )


@router.message(Command("sync"))
async def manual_sync(message: Message) -> None:
    global _current_sync_task

    if not _allowed(message):
        await _reject(message)
        return

    if _current_backfill_task is not None and not _current_backfill_task.done():
        await message.answer("Сначала завершите backfill или приостановите его: /backfillcancel")
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

        p = data["payments"]
        cash = data["cash_shifts"]
        work = data["work_shifts"]

        await message.answer(
            "🔎 Payment / Shift debug\n\n"
            f"Business date: {data['business_date']}\n"
            f"TZ: {data['timezone']}\n"
            f"Day start: {data['business_day_start_hour']:02d}:00\n\n"

            f"PAYMENT/CHECK LEDGER\n"
            f"Чеков: {p['checks']}\n"
            f"DAY: {p['day_checks']} | NIGHT: {p['night_checks']}\n"
            f"Seller ID: {p['seller_id_checks']}\n"
            f"Saby Shift ID: {p['shift_id_checks']}\n"
            f"Saby payments: {p['saby_payments']}\n"
            f"Fallback sale totals: {p['fallbacks']}\n"
            f"Выручка payments: {p['payment_revenue']:.2f} ₽\n"
            f"Возвраты отдельно: {p.get('returns_amount', 0):.2f} ₽\n\n"

            f"CASH SHIFTS\n"
            f"Сегментов: {cash['total']}\n"
            f"Saby native: {cash['native']} | fallback: {cash['fallback']}\n"
            f"Выручка: {cash['revenue']:.2f} ₽\n\n"

            f"EMPLOYEE WORK SHIFTS\n"
            f"Смен: {work['total']}\n"
            f"DAY: {work['day']} | NIGHT: {work['night']}\n"
            f"AUTO: {work['auto']} | REVIEW: {work['review']}\n"
            f"Cash segments: {work['cash_segments']}\n"
            f"Выручка: {work['revenue']:.2f} ₽"
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



@router.message(Command("moneydebug"))
async def money_debug(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    raw = (message.text or "").strip()
    payload = raw[len("/moneydebug"):].strip()

    if "|" in payload:
        store_query, date_value = [
            part.strip()
            for part in payload.rsplit("|", 1)
        ]
    else:
        store_query = payload.strip()
        date_value = "вчера"

    if not store_query:
        await message.answer(
            "Формат:\n"
            "/moneydebug Батумская 5 | 2026-09-19"
        )
        return

    try:
        data = await analytics_service.money_debug(
            store_query,
            date_value,
        )

        if data.get("error"):
            await message.answer(str(data))
            return

        stored = data["stored_business_date"]
        direct = data["direct_timestamp_window"]
        sale = data["sale_totalprice_direct_window"]

        lines = [
            f"💰 Money debug — {data['store']}",
            f"Business date: {data['business_date']}",
            f"TZ: {data['timezone']}",
            f"Окно: {data['window_start']} → {data['window_end']}",
            "",
            "STORED business_date:",
            f"Payments: {stored['payment_rows']}",
            f"Amount: {stored['signed_amount']:.2f} ₽",
            f"Tender sum: {stored['tender_sum']:.2f} ₽",
            "",
            "DIRECT timestamp window:",
            f"Payments: {direct['payment_rows']}",
            f"Amount: {direct['signed_amount']:.2f} ₽",
            f"Tender sum: {direct['tender_sum']:.2f} ₽",
            "",
            "SALE TotalPrice direct window:",
            f"{sale['total_price']:.2f} ₽ / {sale['sales']} sales",
            "",
            "DAY/NIGHT:",
        ]

        for row in data["shift_buckets"]:
            lines.append(
                f"{row['shift_type']}: {row['revenue']:.2f} ₽ | "
                f"{row['checks']} чек. | tender {row['tender_sum']:.2f}"
            )

        lines.append("")
        lines.append("ПО ЧАСАМ (локальное время):")
        for row in data["hourly"]:
            lines.append(
                f"{row['hour']:02d}:00 — "
                f"{row['revenue']:.2f} ₽ | "
                f"{row['checks']} чек. | "
                f"tender {row['tender_sum']:.2f}"
            )

        lines.append("")
        lines.append("RAW boundary samples:")
        for row in data["samples"][:20]:
            tender = (
                row["cash_sum"] + row["bank_sum"] +
                row["certificate_sum"] + row["salary_sum"]
            )
            lines.append(
                f"• raw={row['raw_carried_wtz']} | "
                f"db={row['carried_at']} | "
                f"local={row['local_time']} | "
                f"Amount={row['amount']:.2f} | "
                f"tender={tender:.2f} | "
                f"{row['seller_name']}"
            )

        text = "\n".join(lines)
        for i in range(0, len(text), 3900):
            await message.answer(text[i:i+3900])

    except Exception as exc:
        logger.exception("Money debug failed")
        await message.answer(
            f"⚠️ Money debug error:\n{exc}"
        )


@router.message(Command("paymentdebug"))
async def payment_debug(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    raw = (message.text or "").strip()
    payload = raw[len("/paymentdebug"):].strip()

    if "|" in payload:
        store_query, date_value = [
            part.strip()
            for part in payload.rsplit("|", 1)
        ]
    else:
        store_query = payload.strip()
        date_value = "вчера"

    if not store_query:
        await message.answer(
            "Формат:\n"
            "/paymentdebug Батумская 5 | 2026-09-19"
        )
        return

    try:
        data = await analytics_service.payment_debug(
            store_query,
            date_value,
        )

        if data.get("error"):
            await message.answer(str(data))
            return

        total = data["totals"]

        lines = [
            f"🧾 DIRECT Payment debug — {data['store']}",
            f"Business date: {data['business_date']}",
            "",
            "ИТОГО",
            f"Все payments signed: {total['revenue_all']:.2f} ₽ "
            f"/ {total['checks']} чек.",
            f"Выручка БЕЗ возвратов: {total.get('sales_revenue', 0):.2f} ₽",
            f"Фискальные: {total['fiscal_revenue']:.2f} ₽",
            f"Nonfiscal: {total['nonfiscal_revenue']:.2f} ₽ "
            f"/ {total['nonfiscal_checks']} чек.",
            f"Возвраты: {total['return_revenue']:.2f} ₽ "
            f"/ {total['return_checks']} чек.",
            "",
            "ПО ПРОДАВЦАМ / СМЕНАМ:",
        ]

        for row in data["grouped"]:
            icon = "☀️" if row["business_shift_type"] == "DAY" else "🌙"

            lines.append(
                f"{icon} {row['seller_name'] or '—'}\n"
                f"sales={row.get('sales_revenue', 0):.2f} ₽ | "
                f"all_signed={row['revenue_all']:.2f} ₽ | "
                f"fiscal={row['fiscal_revenue']:.2f} ₽ | "
                f"nonfiscal={row['nonfiscal_revenue']:.2f} ₽ "
                f"({row['nonfiscal_checks']}) | "
                f"returns={row['return_revenue']:.2f} ₽ "
                f"({row['return_checks']})"
            )

        lines.append("")
        lines.append("ПОДОЗРИТЕЛЬНЫЕ ЧЕКИ:")

        if not data["suspicious"]:
            lines.append("— нет Nonfiscal/Return чеков")
        else:
            for row in data["suspicious"]:
                flags = []

                if row["nonfiscal"]:
                    flags.append("NONFISCAL")

                if row["is_return"]:
                    flags.append("RETURN")

                lines.append(
                    f"• {row['local_time']} | "
                    f"{row['seller_name']} | "
                    f"{row['signed_amount']:.2f} ₽ | "
                    f"{'/'.join(flags)} | "
                    f"check={row['check_number'] or '—'} | "
                    f"fiscal={row['fiscal_number'] or '—'}"
                )

        text = "\n".join(lines)

        for i in range(0, len(text), 3900):
            await message.answer(text[i:i+3900])

    except Exception as exc:
        logger.exception("Payment debug failed")
        await message.answer(
            f"⚠️ Payment debug error:\n{exc}"
        )


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
                f"Sale TotalPrice: {row['sale_total']:.2f} ₽ "
                f"({row['sale_count']} sales)\n"
                f"Payments revenue: {row['payment_revenue']:.2f} ₽ "
                f"({row['payment_checks']} rows)\n"
                f"Возвраты отдельно: {row.get('returns_amount', 0):.2f} ₽\n"
                f"Sale − Payments: {row['sale_vs_payment']:.2f} ₽\n"
                f"Work shifts: {row['work_revenue']:.2f} ₽ "
                f"({row['work_checks']} checks)\n"
                f"Payments − Shifts: {row['payment_vs_work']:.2f} ₽\n"
                f"Fallback: {row['fallback_payments']} | "
                f"без Seller: {row['no_seller_checks']} | "
                f"review shifts: {row['review_shifts']}"
            )

        lines.append(
            "\nИТОГ: "
            + ("✅ OK" if data["ok"] else "⚠️ REVIEW")
        )

        text = "\n\n".join(lines)

        for i in range(0, len(text), 3900):
            await message.answer(text[i:i + 3900])

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

    if (
        _current_backfill_task is not None
        and not _current_backfill_task.done()
    ):
        await message.answer(
            "⛔ Сначала приостановите backfill: /backfillcancel"
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




@router.message(Command("plans"))
async def plans(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    payload = (message.text or "")[len("/plans"):].strip()

    try:
        data = await payroll_service.list_plans(payload)

        if data.get("error"):
            await message.answer(str(data))
            return

        rows = data["plans"]

        if not rows:
            await message.answer(
                "ℹ️ Планы пока не заведены.\n\n"
                "Пример:\n"
                "/setplan Батумская 5 | NIGHT | ALL | 70000 | 2026-09-01"
            )
            return

        lines = [
            f"🎯 Планы — {data['scope']}",
            "",
        ]

        for row in rows:
            valid = row["valid_from"]
            if row["valid_to"]:
                valid += f" → {row['valid_to']}"
            else:
                valid += " → ∞"

            lines.append(
                f"#{row['id']} {row['store']} | "
                f"{row['shift_type']} | {row['weekday']} | "
                f"{row['plan_amount']:.2f} ₽ | {valid}"
            )

        text = "\n".join(lines)

        for i in range(0, len(text), 3900):
            await message.answer(text[i:i+3900])

    except Exception as exc:
        logger.exception("Plans failed")
        await message.answer(f"⚠️ Plans error:\n{exc}")


@router.message(Command("setplan"))
async def set_plan(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    from datetime import date

    payload = (message.text or "")[len("/setplan"):].strip()
    parts = [item.strip() for item in payload.split("|")]

    if len(parts) not in (5, 6):
        await message.answer(
            "Формат:\n"
            "/setplan Магазин | DAY/NIGHT | ALL/ПН..ВС | сумма | valid_from [| valid_to]\n\n"
            "Пример:\n"
            "/setplan Батумская 5 | NIGHT | ALL | 70000 | 2026-09-01\n\n"
            "Пятница-override:\n"
            "/setplan Батумская 5 | NIGHT | ПТ | 90000 | 2026-09-01"
        )
        return

    try:
        valid_from = date.fromisoformat(parts[4])
        valid_to = (
            date.fromisoformat(parts[5])
            if len(parts) == 6 and parts[5]
            else None
        )

        data = await payroll_service.set_plan(
            parts[0],
            parts[1],
            parts[2],
            parts[3],
            valid_from,
            valid_to,
        )

        if data.get("error"):
            await message.answer(str(data))
            return

        await message.answer(
            "✅ План сохранён\n\n"
            f"#{data['id']} {data['store']}\n"
            f"{data['shift_type']} | {data['weekday']}\n"
            f"{data['plan_amount']:.2f} ₽\n"
            f"с {data['valid_from']}"
            + (
                f" по {data['valid_to']}"
                if data["valid_to"]
                else ""
            )
        )

    except Exception as exc:
        logger.exception("Set plan failed")
        await message.answer(f"⚠️ Set plan error:\n{exc}")


@router.message(Command("plan"))
async def resolve_plan(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    from datetime import date

    payload = (message.text or "")[len("/plan"):].strip()
    parts = [item.strip() for item in payload.split("|")]

    if len(parts) != 3:
        await message.answer(
            "Формат:\n"
            "/plan Магазин | 2026-09-19 | NIGHT"
        )
        return

    try:
        data = await payroll_service.resolve_plan_for_store(
            parts[0],
            date.fromisoformat(parts[1]),
            parts[2],
        )

        if data.get("error"):
            await message.answer(str(data))
            return

        await message.answer(
            "🎯 Действующий план\n\n"
            f"{data['store']}\n"
            f"{data['business_date']} | {data['shift_type']}\n"
            f"План: {data['plan_amount']:.2f} ₽\n"
            f"Правило: {data['weekday']}\n"
            f"Источник: {data['source']}"
        )

    except Exception as exc:
        logger.exception("Resolve plan failed")
        await message.answer(f"⚠️ Plan error:\n{exc}")


@router.message(Command("paypolicies"))
async def pay_policies(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    role = (message.text or "")[len("/paypolicies"):].strip().upper()

    try:
        data = await payroll_service.list_policies(role)

        lines = [
            f"💼 Payroll policies — {data['role']}",
            "",
        ]

        if not data["policies"]:
            lines.append("— политик нет")

        for policy in data["policies"]:
            tiers = "; ".join(
                f"{tier['min_ratio']:.2f}→{tier['percent']*100:.1f}%"
                for tier in policy["kpi_tiers"]
            )

            valid = policy["valid_from"]
            if policy["valid_to"]:
                valid += f" → {policy['valid_to']}"
            else:
                valid += " → ∞"

            lines.append(
                f"{policy['role']} v{policy['version']} | {valid}\n"
                f"фикс {policy['base_per_shift']:.2f} ₽ | "
                f"KPI [{tiers or '—'}] | "
                f"экзамен {policy['exam_percent']*100:.1f}%"
            )

        await message.answer("\n\n".join(lines))

    except Exception as exc:
        logger.exception("Policies failed")
        await message.answer(f"⚠️ Policies error:\n{exc}")


@router.message(Command("paypolicy"))
async def pay_policy(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    parts = (message.text or "").split()

    if len(parts) not in (2, 3):
        await message.answer(
            "Формат:\n"
            "/paypolicy SELLER [2026-09-19]"
        )
        return

    role = parts[1].upper()

    try:
        for_date = (
            date.fromisoformat(parts[2])
            if len(parts) == 3
            else datetime.now(
                ZoneInfo(settings.business_tz)
            ).date()
        )

        policy = await payroll_service.resolve_policy(
            role,
            for_date,
        )

        if not policy:
            await message.answer(
                f"ℹ️ Для {role} на {for_date} политика не найдена."
            )
            return

        tiers = "\n".join(
            f"• от {tier['min_ratio']*100:.0f}%: "
            f"{tier['percent']*100:.1f}%"
            for tier in policy["kpi_tiers"]
        )

        await message.answer(
            f"💼 {policy['role']} policy v{policy['version']}\n\n"
            f"Дата: {for_date}\n"
            f"Фикс/смена: {policy['base_per_shift']:.2f} ₽\n"
            f"KPI basis: {policy['kpi_basis']}\n"
            f"{tiers or 'KPI tiers: —'}\n"
            f"Экзамен: {policy['exam_percent']*100:.1f}% "
            f"({policy['exam_basis']})"
        )

    except Exception as exc:
        logger.exception("Policy failed")
        await message.answer(f"⚠️ Policy error:\n{exc}")


@router.message(Command("setpolicy"))
async def set_policy(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    from datetime import date

    payload = (message.text or "")[len("/setpolicy"):].strip()
    parts = [item.strip() for item in payload.split("|")]

    if len(parts) != 5:
        await message.answer(
            "Формат:\n"
            "/setpolicy ROLE | valid_from | base | tiers | exam_percent\n\n"
            "Пример новой схемы продавцов:\n"
            "/setpolicy SELLER | 2026-10-01 | 2300 | "
            "1.00=0.04;1.25=0.06 | 0.03\n\n"
            "Старая политика автоматически закроется днём ранее."
        )
        return

    try:
        data = await payroll_service.create_policy(
            parts[0],
            date.fromisoformat(parts[1]),
            parts[2],
            parts[3],
            parts[4],
        )

        await message.answer(
            "✅ Создана новая payroll policy\n\n"
            f"{data['role']} v{data['version']}\n"
            f"Действует с {data['valid_from']}\n"
            f"Фикс: {data['base_per_shift']:.2f} ₽\n"
            f"Экзамен: {data['exam_percent']*100:.1f}%"
        )

    except Exception as exc:
        logger.exception("Set policy failed")
        await message.answer(f"⚠️ Set policy error:\n{exc}")


@router.message(Command("identitysync"))
async def identity_sync(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    try:
        data = await payroll_service.sync_seller_identities()

        await message.answer(
            "👥 Seller identity map\n\n"
            f"Найдено продавцов: {data['found_sellers']}\n"
            f"Добавлено: {data['inserted']}\n"
            f"Обновлено ФИО: {data['updated']}"
        )

    except Exception as exc:
        logger.exception("Identity sync failed")
        await message.answer(f"⚠️ Identity sync error:\n{exc}")


@router.message(Command("setexam"))
async def set_exam(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    from datetime import date

    payload = (message.text or "")[len("/setexam"):].strip()
    parts = [item.strip() for item in payload.split("|")]

    if len(parts) not in (3, 4):
        await message.answer(
            "Формат:\n"
            "/setexam Фамилия | YYYY-MM | PASS/FAIL [| score]\n\n"
            "Перед первым использованием: /identitysync"
        )
        return

    try:
        month = date.fromisoformat(parts[1] + "-01")
        passed = parts[2].upper() in {
            "PASS",
            "PASSED",
            "ДА",
            "YES",
            "1",
            "TRUE",
        }

        data = await payroll_service.set_exam(
            parts[0],
            month,
            passed,
            parts[3] if len(parts) == 4 else None,
        )

        if data.get("error"):
            await message.answer(str(data))
            return

        await message.answer(
            "✅ Экзамен сохранён\n\n"
            f"{data['employee']}\n"
            f"{data['month'][:7]}: "
            f"{'PASS' if data['passed'] else 'FAIL'}"
            + (
                f"\nscore: {data['score']}"
                if data["score"] is not None
                else ""
            )
        )

    except Exception as exc:
        logger.exception("Set exam failed")
        await message.answer(f"⚠️ Set exam error:\n{exc}")


@router.message(Command("payrollpreview"))
async def payroll_preview(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    from datetime import date

    parts = (message.text or "").split()

    if len(parts) != 2:
        await message.answer(
            "Формат:\n"
            "/payrollpreview 2026-09-19"
        )
        return

    try:
        data = await payroll_service.seller_daily_preview(
            date.fromisoformat(parts[1])
        )

        lines = [
            f"💰 Seller payroll preview — {data['business_date']}",
            "KPI trigger: план всей магазинной смены",
            "KPI начисление: личная выручка продавца",
            "",
        ]

        for row in data["rows"]:
            if row["payroll_status"] != "OK":
                lines.append(
                    f"⚠️ {row['store']} {row['shift_type']} — "
                    f"{row['seller_name']}: {row['payroll_status']}"
                )
                continue

            lines.append(
                f"✅ {row['store']} {row['shift_type']} — {row['seller_name']}\n"
                f"смена {row['store_shift_revenue']:.2f} / "
                f"план {row['plan_amount']:.2f} ₽ "
                f"({row['achievement_percent']:.1f}%)\n"
                f"личная выручка {row['seller_revenue']:.2f} ₽ | "
                f"фикс {row['base_pay']:.2f} | "
                f"KPI {row['kpi_percent']*100:.1f}% = "
                f"{row['kpi_pay']:.2f} | "
                f"итого без экзамена {row['shift_pay_without_exam']:.2f} ₽"
            )

        text = "\n\n".join(lines)

        for i in range(0, len(text), 3900):
            await message.answer(text[i:i+3900])

    except Exception as exc:
        logger.exception("Payroll preview failed")
        await message.answer(f"⚠️ Payroll preview error:\n{exc}")


@router.message(Command("payrollmonth"))
async def payroll_month(message: Message) -> None:
    if not _allowed(message):
        await _reject(message)
        return

    from datetime import date

    parts = (message.text or "").split()

    if len(parts) != 2:
        await message.answer(
            "Формат:\n"
            "/payrollmonth 2026-09"
        )
        return

    try:
        month = date.fromisoformat(parts[1] + "-01")

        data = await payroll_service.seller_month_preview(month)

        lines = [
            f"💰 Seller payroll month — {data['month']}",
            "",
        ]

        for row in data["rows"]:
            icon = "✅" if row["status"] == "OK" else "⚠️"

            lines.append(
                f"{icon} {row['seller_name']}\n"
                f"смен: {row['shift_count']} | "
                f"личная выручка: {row['personal_revenue']:.2f} ₽\n"
                f"фикс: {row['base_pay']:.2f} | "
                f"KPI: {row['kpi_pay']:.2f} | "
                f"экзамен: {row['exam_bonus']:.2f} | "
                f"ИТОГО: {row['total_pay']:.2f} ₽"
            )

            if row["problems"]:
                lines.append(
                    f"проблемных смен: {len(row['problems'])}"
                )

        text = "\n\n".join(lines)

        for i in range(0, len(text), 3900):
            await message.answer(text[i:i+3900])

    except Exception as exc:
        logger.exception("Payroll month failed")
        await message.answer(f"⚠️ Payroll month error:\n{exc}")


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
