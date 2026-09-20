from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db.database import pool
from app.services.saby import saby_client
from app.services.shift_engine import shift_engine


logger = logging.getLogger(__name__)
settings = get_settings()


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(settings.business_tz))
    return dt


def _sale_id(order: dict[str, Any]) -> int | None:
    return _int_or_none(order.get("Sale"))


def _seller_id(order: dict[str, Any]) -> int | None:
    seller = order.get("Seller")
    if isinstance(seller, dict):
        for key in ("id", "ID", "Id"):
            value = _int_or_none(seller.get(key))
            if value is not None:
                return value
        return None
    return _int_or_none(seller)


def _payment_context(order: dict[str, Any]) -> dict[str, Any]:
    payments = order.get("Payments") or []
    if not isinstance(payments, list):
        payments = []

    check_dt = None
    source = ""
    shift_id = _int_or_none(order.get("Shift"))
    shift_number = str(order.get("ShiftNumber") or "").strip()
    teller_id = _int_or_none(order.get("Teller"))

    for payment in payments:
        if not isinstance(payment, dict):
            continue

        if check_dt is None:
            for field in ("CarriedWTZ", "ClosedWTZ", "OpenedWTZ"):
                dt = _parse_dt(payment.get(field))
                if dt is not None:
                    check_dt = dt
                    source = f"Payments.{field}"
                    break

        if shift_id is None:
            shift_id = _int_or_none(payment.get("Shift"))
        if not shift_number:
            shift_number = str(payment.get("ShiftNumber") or "").strip()
        if teller_id is None:
            teller_id = _int_or_none(payment.get("Teller"))

    if check_dt is None:
        check_dt = _parse_dt(order.get("DateWTZ"))
        source = "DateWTZ"

    return {
        "check_datetime": check_dt,
        "order_datetime": _parse_dt(order.get("DateWTZ")),
        "check_time_source": source,
        "shift_id": shift_id,
        "shift_number": shift_number,
        "teller_id": teller_id,
    }


def _item_key(position: dict[str, Any], index: int) -> str:
    for key in ("SaleNomenclature", "Key", "NomenclatureUUID"):
        value = position.get(key)
        if value not in (None, ""):
            return f"{key}:{value}"
    return f"line:{index}"


SALE_UPSERT_SQL = """
INSERT INTO sales(
    point_id, sale_id, sale_key, sale_number,
    sale_datetime, order_datetime, check_time_source,
    opened_at, closed_at,
    seller_id, seller_name,
    saby_shift_id, saby_shift_number, teller_id,
    customer_id, customer_name,
    total_price, total_discount,
    is_return, deleted,
    warehouse_id, warehouse_name,
    raw_json, synced_at
)
VALUES(
    $1,$2,$3,$4,
    $5,$6,$7,
    $8,$9,
    $10,$11,
    $12,$13,$14,
    $15,$16,
    $17,$18,
    $19,$20,
    $21,$22,
    $23::jsonb,NOW()
)
ON CONFLICT(point_id, sale_id) DO UPDATE SET
    sale_key = EXCLUDED.sale_key,
    sale_number = EXCLUDED.sale_number,
    sale_datetime = EXCLUDED.sale_datetime,
    order_datetime = EXCLUDED.order_datetime,
    check_time_source = EXCLUDED.check_time_source,
    opened_at = EXCLUDED.opened_at,
    closed_at = EXCLUDED.closed_at,
    seller_id = EXCLUDED.seller_id,
    seller_name = EXCLUDED.seller_name,
    saby_shift_id = EXCLUDED.saby_shift_id,
    saby_shift_number = EXCLUDED.saby_shift_number,
    teller_id = EXCLUDED.teller_id,
    customer_id = EXCLUDED.customer_id,
    customer_name = EXCLUDED.customer_name,
    total_price = EXCLUDED.total_price,
    total_discount = EXCLUDED.total_discount,
    is_return = EXCLUDED.is_return,
    deleted = EXCLUDED.deleted,
    warehouse_id = EXCLUDED.warehouse_id,
    warehouse_name = EXCLUDED.warehouse_name,
    raw_json = EXCLUDED.raw_json,
    synced_at = NOW()
"""

ITEM_INSERT_SQL = """
INSERT INTO sale_items(
    point_id, sale_id, item_key,
    item_id, line_number,
    product_id, product_uuid, product_number,
    name, short_name, barcode,
    quantity, total_price, total_discount,
    planned_cost, total_cost,
    is_return, refused, unit_name,
    raw_json
)
VALUES(
    $1,$2,$3,
    $4,$5,
    $6,$7,$8,
    $9,$10,$11,
    $12,$13,$14,
    $15,$16,
    $17,$18,$19,
    $20::jsonb
)
ON CONFLICT(point_id, sale_id, item_key) DO UPDATE SET
    item_id = EXCLUDED.item_id,
    line_number = EXCLUDED.line_number,
    product_id = EXCLUDED.product_id,
    product_uuid = EXCLUDED.product_uuid,
    product_number = EXCLUDED.product_number,
    name = EXCLUDED.name,
    short_name = EXCLUDED.short_name,
    barcode = EXCLUDED.barcode,
    quantity = EXCLUDED.quantity,
    total_price = EXCLUDED.total_price,
    total_discount = EXCLUDED.total_discount,
    planned_cost = EXCLUDED.planned_cost,
    total_cost = EXCLUDED.total_cost,
    is_return = EXCLUDED.is_return,
    refused = EXCLUDED.refused,
    unit_name = EXCLUDED.unit_name,
    raw_json = EXCLUDED.raw_json
"""


class SabySyncService:
    async def sync_recent(self, days: int | None = None) -> dict:
        days = days or settings.sync_recent_days
        days = max(1, min(days, settings.max_manual_sync_days))

        today = datetime.now(ZoneInfo(settings.business_tz)).date()
        date_from = today - timedelta(days=days - 1)
        return await self.sync_range(date_from, today)

    async def _fetch_job(
        self,
        semaphore: asyncio.Semaphore,
        point: dict[str, Any],
        day: date,
    ) -> tuple[dict[str, Any], date, list[dict[str, Any]]]:
        async with semaphore:
            orders = await saby_client.orders_for_point_date(
                point["id"],
                day.isoformat(),
                need_discount_info=True,
            )
            return point, day, orders

    def _prepare_rows(
        self,
        point: dict[str, Any],
        orders: list[dict[str, Any]],
    ) -> tuple[list[tuple], list[tuple], list[tuple[int, int]]]:
        sale_rows: list[tuple] = []
        item_rows: list[tuple] = []
        sale_keys: list[tuple[int, int]] = []

        for order in orders:
            sale_id = _sale_id(order)
            if sale_id is None:
                logger.warning(
                    "Skipped Saby order without Sale id: point=%s key=%s",
                    point["id"],
                    order.get("Key"),
                )
                continue

            seller_id = _seller_id(order)
            payment_ctx = _payment_context(order)

            sale_rows.append(
                (
                    point["id"],
                    sale_id,
                    str(order.get("Key") or ""),
                    str(order.get("Number") or ""),
                    payment_ctx["check_datetime"],
                    payment_ctx["order_datetime"],
                    payment_ctx["check_time_source"],
                    _parse_dt(order.get("OpenedWTZ")),
                    _parse_dt(order.get("ClosedWTZ")),
                    seller_id,
                    str(order.get("SellerName") or "").strip(),
                    payment_ctx["shift_id"],
                    payment_ctx["shift_number"],
                    payment_ctx["teller_id"],
                    _int_or_none(order.get("Customer")),
                    str(order.get("CustomerName") or "").strip(),
                    _dec(order.get("TotalPrice")),
                    _dec(order.get("TotalDiscount")),
                    bool(order.get("Return")),
                    bool(order.get("Deleted")),
                    _int_or_none(order.get("Warehouse")),
                    str(order.get("WarehouseName") or "").strip(),
                    json.dumps(order, ensure_ascii=False, default=str),
                )
            )
            sale_keys.append((point["id"], sale_id))

            for idx, pos in enumerate(order.get("SaleNomenclatures") or []):
                item_rows.append(
                    (
                        point["id"],
                        sale_id,
                        _item_key(pos, idx),
                        _int_or_none(pos.get("SaleNomenclature")),
                        _int_or_none(pos.get("Number")),
                        _int_or_none(pos.get("Nomenclature")),
                        str(pos.get("NomenclatureUUID") or ""),
                        str(pos.get("NomenclatureNumber") or ""),
                        str(pos.get("Name") or ""),
                        str(pos.get("ShortName") or ""),
                        str(pos.get("Barcode") or ""),
                        _dec(pos.get("Quantity")),
                        _dec(pos.get("TotalPrice")),
                        _dec(pos.get("TotalDiscount")),
                        _dec(pos.get("PlannedCost")),
                        _dec(pos.get("TotalCost")),
                        bool(order.get("Return")) or bool(pos.get("IsReturn")),
                        bool(pos.get("Refused")),
                        str(pos.get("UnitName") or ""),
                        json.dumps(pos, ensure_ascii=False, default=str),
                    )
                )

        return sale_rows, item_rows, sale_keys

    async def _write_batch(
        self,
        point: dict[str, Any],
        orders: list[dict[str, Any]],
    ) -> tuple[int, int]:
        sale_rows, item_rows, sale_keys = self._prepare_rows(point, orders)
        if not sale_rows:
            return 0, 0

        async with pool().acquire() as conn:
            async with conn.transaction():
                # One executemany call instead of one DB request per sale.
                await conn.executemany(SALE_UPSERT_SQL, sale_rows)

                # Items are a full snapshot for each sale in Saby.
                await conn.executemany(
                    """
                    DELETE FROM sale_items
                    WHERE point_id=$1 AND sale_id=$2
                    """,
                    sale_keys,
                )

                if item_rows:
                    await conn.executemany(ITEM_INSERT_SQL, item_rows)

        return len(sale_rows), len(item_rows)

    async def sync_range(self, date_from: date, date_to: date) -> dict:
        if date_to < date_from:
            raise ValueError("date_to cannot be earlier than date_from")

        span = (date_to - date_from).days + 1
        if span > settings.max_manual_sync_days:
            raise ValueError(
                f"За один запуск можно синхронизировать максимум "
                f"{settings.max_manual_sync_days} дней."
            )

        async with pool().acquire() as conn:
            run_id = await conn.fetchval(
                """
                INSERT INTO sync_runs(date_from, date_to)
                VALUES($1, $2)
                RETURNING id
                """,
                date_from,
                date_to,
            )

        stores_count = 0
        sales_count = 0
        items_count = 0

        try:
            points = await saby_client.list_points()
            stores_count = len(points)

            # Upsert all stores in one batch.
            store_rows = [
                (
                    p["id"],
                    p["name"],
                    p.get("address", ""),
                    p.get("locality", ""),
                )
                for p in points
            ]
            async with pool().acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO stores(point_id, name, address, locality, updated_at)
                    VALUES($1, $2, $3, $4, NOW())
                    ON CONFLICT(point_id) DO UPDATE SET
                        name=EXCLUDED.name,
                        address=EXCLUDED.address,
                        locality=EXCLUDED.locality,
                        updated_at=NOW()
                    """,
                    store_rows,
                )

            days: list[date] = []
            current = date_from
            while current <= date_to:
                days.append(current)
                current += timedelta(days=1)

            jobs = [(point, day) for day in days for point in points]
            semaphore = asyncio.Semaphore(max(1, min(settings.sync_concurrency, 8)))

            # Fetch several store-days concurrently.
            fetched = await asyncio.gather(
                *[
                    self._fetch_job(semaphore, point, day)
                    for point, day in jobs
                ]
            )

            # DB writes are batched per store-day. This avoids thousands
            # of individual round trips while keeping transactions moderate.
            for point, day, orders in fetched:
                s_count, i_count = await self._write_batch(point, orders)
                sales_count += s_count
                items_count += i_count

            shifts_count = await shift_engine.rebuild_range(date_from, date_to)

            async with pool().acquire() as conn:
                await conn.execute(
                    """
                    UPDATE sync_runs
                    SET
                        finished_at=NOW(),
                        status='OK',
                        stores_count=$2,
                        sales_upserted=$3,
                        items_upserted=$4,
                        shifts_built=$5
                    WHERE id=$1
                    """,
                    run_id,
                    stores_count,
                    sales_count,
                    items_count,
                    shifts_count,
                )

            return {
                "ok": True,
                "run_id": run_id,
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "stores": stores_count,
                "sales_upserted": sales_count,
                "items_upserted": items_count,
                "shifts_built": shifts_count,
                "fetch_jobs": len(jobs),
                "concurrency": settings.sync_concurrency,
            }

        except asyncio.CancelledError:
            logger.warning("Saby sync cancelled: run_id=%s", run_id)
            async with pool().acquire() as conn:
                await conn.execute(
                    """
                    UPDATE sync_runs
                    SET finished_at=NOW(), status='CANCELLED',
                        error_text='Cancelled by user'
                    WHERE id=$1
                    """,
                    run_id,
                )
            raise

        except Exception as exc:
            logger.exception("Saby sync failed")
            async with pool().acquire() as conn:
                await conn.execute(
                    """
                    UPDATE sync_runs
                    SET finished_at=NOW(), status='ERROR', error_text=$2
                    WHERE id=$1
                    """,
                    run_id,
                    str(exc)[:4000],
                )
            raise


sync_service = SabySyncService()
