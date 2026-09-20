from __future__ import annotations

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

    # Saby documents DateWTZ as YYYY-MM-DD hh:mm:ss,
    # but we tolerate ISO strings with offsets too.
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


def _item_key(position: dict[str, Any], index: int) -> str:
    for key in ("SaleNomenclature", "Key", "NomenclatureUUID"):
        value = position.get(key)
        if value not in (None, ""):
            return f"{key}:{value}"
    return f"line:{index}"


class SabySyncService:
    async def sync_recent(self, days: int | None = None) -> dict:
        days = days or settings.sync_recent_days
        days = max(1, min(days, settings.max_manual_sync_days))

        today = datetime.now(ZoneInfo(settings.business_tz)).date()
        date_from = today - timedelta(days=days - 1)
        return await self.sync_range(date_from, today)

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

            async with pool().acquire() as conn:
                async with conn.transaction():
                    for point in points:
                        await conn.execute(
                            """
                            INSERT INTO stores(point_id, name, address, locality, updated_at)
                            VALUES($1, $2, $3, $4, NOW())
                            ON CONFLICT(point_id) DO UPDATE SET
                                name = EXCLUDED.name,
                                address = EXCLUDED.address,
                                locality = EXCLUDED.locality,
                                updated_at = NOW()
                            """,
                            point["id"],
                            point["name"],
                            point.get("address", ""),
                            point.get("locality", ""),
                        )

            current = date_from
            while current <= date_to:
                current_iso = current.isoformat()

                for point in points:
                    orders = await saby_client.orders_for_point_date(
                        point["id"],
                        current_iso,
                        need_discount_info=True,
                    )

                    async with pool().acquire() as conn:
                        async with conn.transaction():
                            for order in orders:
                                sale_id = _sale_id(order)
                                if sale_id is None:
                                    # A sale without Saby Sale id cannot be safely upserted.
                                    logger.warning(
                                        "Skipped Saby order without Sale id: point=%s key=%s",
                                        point["id"],
                                        order.get("Key"),
                                    )
                                    continue

                                seller_id = _seller_id(order)

                                await conn.execute(
                                    """
                                    INSERT INTO sales(
                                        point_id, sale_id, sale_key, sale_number,
                                        sale_datetime, opened_at, closed_at,
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
                                        $10,$11,$12,
                                        $13,$14,
                                        $15,$16,
                                        $17,$18,
                                        $19,$20,
                                        $21::jsonb,NOW()
                                    )
                                    ON CONFLICT(point_id, sale_id) DO UPDATE SET
                                        sale_key = EXCLUDED.sale_key,
                                        sale_number = EXCLUDED.sale_number,
                                        sale_datetime = EXCLUDED.sale_datetime,
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
                                    """,
                                    point["id"],
                                    sale_id,
                                    str(order.get("Key") or ""),
                                    str(order.get("Number") or ""),
                                    _parse_dt(order.get("DateWTZ")),
                                    _parse_dt(order.get("OpenedWTZ")),
                                    _parse_dt(order.get("ClosedWTZ")),
                                    seller_id,
                                    str(order.get("SellerName") or "").strip(),
                                    _int_or_none(order.get("Shift")),
                                    str(order.get("ShiftNumber") or "").strip(),
                                    _int_or_none(order.get("Teller")),
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
                                sales_count += 1

                                # Replace items for a sale so changes/refunds are reflected.
                                await conn.execute(
                                    """
                                    DELETE FROM sale_items
                                    WHERE point_id=$1 AND sale_id=$2
                                    """,
                                    point["id"],
                                    sale_id,
                                )

                                positions = order.get("SaleNomenclatures") or []
                                for idx, pos in enumerate(positions):
                                    key = _item_key(pos, idx)
                                    await conn.execute(
                                        """
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
                                        """,
                                        point["id"],
                                        sale_id,
                                        key,
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
                                    items_count += 1

                current += timedelta(days=1)

            # Rebuild with one-day buffers so night shifts crossing midnight
            # are reconstructed consistently.
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
            }

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
