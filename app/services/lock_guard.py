"""Keep a long-lived advisory-lock session alive; stop work if it is lost."""
import asyncio
import logging
from contextlib import asynccontextmanager, suppress
import asyncpg

logger = logging.getLogger(__name__)


@asynccontextmanager
async def keep_lock_alive(conn, interval=30):
    owner = asyncio.current_task()
    lost = False

    async def heartbeat():
        nonlocal lost
        try:
            while True:
                await asyncio.sleep(interval)
                await conn.execute('SELECT 1')
        except asyncio.CancelledError:
            raise
        except Exception:
            lost = True
            owner.cancel()

    task = asyncio.create_task(heartbeat())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        if lost:
            raise ValueError('DATABASE_LOCK_SESSION_LOST')


async def release_lock(conn, sql, key):
    try:
        await conn.execute(sql, key)
    except (asyncpg.InterfaceError, asyncpg.PostgresConnectionError, ConnectionError):
        # PostgreSQL releases session locks when the session disappears. Do not
        # turn committed COMPLETED blocks into a failed command on cleanup only.
        logger.warning('LOCK_CLEANUP_CONNECTION_UNAVAILABLE: committed progress is preserved')
