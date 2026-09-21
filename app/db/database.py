from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import asyncpg

from app.config import get_settings


logger = logging.getLogger(__name__)
settings = get_settings()

_pool: asyncpg.Pool | None = None


def _normalize_neon_dsn(dsn: str) -> str:
    value = dsn.strip()

    if value.startswith("postgresql+asyncpg://"):
        value = "postgresql://" + value[len("postgresql+asyncpg://"):]

    parts = urlsplit(value)

    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() != "channel_binding"
    ]

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        )
    )


async def init_db() -> None:
    global _pool

    if _pool is not None:
        return

    _pool = await asyncpg.create_pool(
        dsn=_normalize_neon_dsn(settings.database_url),
        min_size=1,
        max_size=5,
        command_timeout=90,
        server_settings={
            "timezone": settings.business_tz,
        },
    )

    schema_path = Path(__file__).with_name("schema.sql")
    schema = schema_path.read_text(encoding="utf-8")

    async with _pool.acquire() as conn:
        await conn.execute(schema)

    logger.info("PostgreSQL schema is ready")


async def close_db() -> None:
    global _pool

    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialized")
    return _pool


async def health() -> dict:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                NOW() AS now,

                (SELECT COUNT(*) FROM stores) AS stores,
                (SELECT COUNT(*) FROM sales) AS sales,
                (SELECT COUNT(*) FROM sale_items) AS sale_items,
                (SELECT COUNT(*) FROM sale_payments) AS sale_payments,

                (SELECT COUNT(*) FROM cash_shifts) AS cash_shifts,
                (SELECT COUNT(*) FROM employee_work_shifts) AS shifts
            """
        )

    return {
        "ok": True,
        "db_time": row["now"].isoformat(),
        "stores": row["stores"],
        "sales": row["sales"],
        "sale_items": row["sale_items"],
        "sale_payments": row["sale_payments"],
        "cash_shifts": row["cash_shifts"],
        "shifts": row["shifts"],
    }


async def reset_saby_data() -> None:
    """
    Clears only Saby-derived/analytical data.
    DB structure and app_meta remain.
    """
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                TRUNCATE TABLE
                    employee_work_shifts,
                    cash_shifts,
                    seller_shifts,
                    sale_items,
                    sale_payments,
                    sales,
                    sync_runs,
                    stores
                RESTART IDENTITY CASCADE
                """
            )
