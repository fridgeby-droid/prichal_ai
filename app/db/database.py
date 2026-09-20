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
    """
    Neon often provides channel_binding=require.
    asyncpg does not need that option; sslmode=require is enough.
    """
    value = dsn.strip()
    if value.startswith("postgresql+asyncpg://"):
        value = "postgresql://" + value[len("postgresql+asyncpg://"):]

    parts = urlsplit(value)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() != "channel_binding"
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


async def init_db() -> None:
    global _pool
    if _pool is not None:
        return

    dsn = _normalize_neon_dsn(settings.database_url)
    _pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=1,
        max_size=5,
        command_timeout=90,
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
                (SELECT COUNT(*) FROM seller_shifts) AS shifts
            """
        )

    return {
        "ok": True,
        "db_time": row["now"].isoformat(),
        "stores": row["stores"],
        "sales": row["sales"],
        "sale_items": row["sale_items"],
        "shifts": row["shifts"],
    }
