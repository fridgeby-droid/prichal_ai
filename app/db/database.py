from __future__ import annotations

import base64
import logging
import re
import ssl
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

from app.config import get_settings


logger = logging.getLogger(__name__)
settings = get_settings()

_pool: asyncpg.Pool | None = None


def _safe_schema(value: str) -> str:
    value = (value or "prichal_ai").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("DB_SCHEMA contains unsupported characters")
    return value


def normalize_postgres_dsn(dsn: str) -> str:
    """Normalize SQLAlchemy/Neon/libpq style DSNs for asyncpg.

    SSL is configured explicitly with asyncpg's `ssl=` argument, so SSL-only
    query parameters are removed from the DSN. This also avoids the historical
    `~/.postgresql/root.crt` lookup problem seen with Timeweb connections.
    """
    value = dsn.strip()

    if value.startswith("postgresql+asyncpg://"):
        value = "postgresql://" + value[len("postgresql+asyncpg://"):]

    parts = urlsplit(value)
    ignored = {
        "channel_binding",
        "sslmode",
        "sslrootcert",
        "sslcert",
        "sslkey",
    }

    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in ignored
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


def build_ssl_context(
    mode: str,
    ca_b64: str = "",
    ca_file: str = "",
):
    """Build asyncpg SSL configuration.

    Modes:
      disable      -> no TLS
      require      -> TLS; validates certificate when CA is supplied, otherwise
                      encrypts without CA verification (libpq-style require)
      verify-ca    -> CA required, hostname is not checked
      verify-full  -> CA required, hostname is checked

    Timeweb DBaaS supports TLS. For strict production verification use
    `verify-full` with DATABASE_SSL_CA_B64 or DATABASE_SSL_CA_FILE.
    """
    normalized = (mode or "require").strip().lower()

    if normalized in {"disable", "false", "off", "0"}:
        return False

    if normalized not in {"require", "verify-ca", "verify-full"}:
        raise ValueError(
            "DATABASE_SSL_MODE must be disable/require/verify-ca/verify-full"
        )

    ca_pem = ""
    if ca_b64.strip():
        try:
            ca_pem = base64.b64decode(ca_b64.strip()).decode("utf-8")
        except Exception as exc:
            raise ValueError("DATABASE_SSL_CA_B64 is not valid base64 PEM") from exc

    if ca_file.strip():
        path = Path(ca_file.strip())
        if not path.exists():
            raise FileNotFoundError(f"Database CA file not found: {path}")

    if normalized in {"verify-ca", "verify-full"} and not (ca_pem or ca_file.strip()):
        raise ValueError(
            f"DATABASE_SSL_MODE={normalized} requires DATABASE_SSL_CA_B64 "
            "or DATABASE_SSL_CA_FILE"
        )

    if ca_pem or ca_file.strip():
        context = ssl.create_default_context()
        if ca_pem:
            context.load_verify_locations(cadata=ca_pem)
        if ca_file.strip():
            context.load_verify_locations(cafile=ca_file.strip())
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = normalized == "verify-full"
        return context

    # `require`: encrypt transport without forcing a root.crt file.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


async def init_db() -> None:
    global _pool

    if _pool is not None:
        return

    schema = _safe_schema(settings.db_schema)
    ssl_config = build_ssl_context(
        settings.database_ssl_mode,
        settings.database_ssl_ca_b64,
        settings.database_ssl_ca_file,
    )

    _pool = await asyncpg.create_pool(
        dsn=normalize_postgres_dsn(settings.database_url),
        ssl=ssl_config,
        min_size=1,
        max_size=5,
        command_timeout=90,
        server_settings={
            "timezone": settings.business_tz,
            "search_path": f'"{schema}"',
            "application_name": "prichal-ai",
        },
    )

    schema_path = Path(__file__).with_name("schema.sql")
    schema_sql = schema_path.read_text(encoding="utf-8")

    try:
        async with _pool.acquire() as conn:
            async with conn.transaction():
                # Never resolve application tables from public/Core.
                await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                await conn.execute(f'SET search_path TO "{schema}"')
                await conn.execute(schema_sql)
    except BaseException:
        await close_db()
        raise

    logger.info(
        "PostgreSQL schema is ready | schema=%s | tls=%s",
        schema,
        settings.database_ssl_mode,
    )


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
    schema = _safe_schema(settings.db_schema)

    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                NOW() AS now,
                current_database() AS database_name,
                current_user AS database_user,

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
        "provider": "postgresql",
        "schema": schema,
        "ssl_mode": settings.database_ssl_mode,
        "db_time": row["now"].isoformat(),
        "database_name": row["database_name"],
        "database_user": row["database_user"],
        "stores": row["stores"],
        "sales": row["sales"],
        "sale_items": row["sale_items"],
        "sale_payments": row["sale_payments"],
        "cash_shifts": row["cash_shifts"],
        "shifts": row["shifts"],
    }


async def reset_saby_data() -> None:
    """Clears only Saby-derived/analytical data in the Причал AI schema."""
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                TRUNCATE TABLE
                    employee_work_shifts,
                    cash_shifts,
                    seller_shifts,
                    backfill_runs,
                    sale_items,
                    sale_payments,
                    sales,
                    sync_runs,
                    stores
                RESTART IDENTITY CASCADE
                """
            )
