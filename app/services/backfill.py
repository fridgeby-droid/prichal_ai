from __future__ import annotations

import asyncio
import logging
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Awaitable, Callable

from app.config import get_settings
from app.db.database import pool
from app.services.sync import sync_service
from app.services.analytics import analytics_service


logger = logging.getLogger(__name__)
settings = get_settings()

ProgressCallback = Callable[[dict], Awaitable[None]]


class BackfillService:
    async def _preflight(self, date_to: date) -> set[int]:
        if settings.auto_sync_enabled or settings.auto_sync_on_start:
            raise ValueError("Для backfill задайте AUTO_SYNC_ENABLED=false и AUTO_SYNC_ON_START=false.")
        current_business_date = (
            datetime.now(ZoneInfo(settings.business_tz))
            - timedelta(hours=settings.business_day_start_hour)
        ).date()
        if date_to >= current_business_date:
            raise ValueError("Backfill допускает только завершённые business days (после 08:00 следующего дня).")
        async with pool().acquire() as conn:
            rows = await conn.fetch("SELECT point_id FROM retail_stores ORDER BY point_id")
        if not rows:
            raise ValueError("Сначала загрузите stores: /loadstores или python -m scripts.bootstrap stores")
        return {row["point_id"] for row in rows}

    async def reconcile_chunk(self, run_id: int, date_from: date, date_to: date) -> dict:
        reports = []
        day = date_from
        while day <= date_to:
            reports.append(await analytics_service.reconcile(day.isoformat()))
            day += timedelta(days=1)
        report = {
            "run_id": run_id,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "ok": all(item["ok"] for item in reports),
            "days": reports,
        }
        async with pool().acquire() as conn:
            await conn.execute(
                """INSERT INTO app_meta(key, value) VALUES($1, $2::jsonb)
                ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()""",
                f"backfill:{run_id}:reconcile:{date_from.isoformat()}:{date_to.isoformat()}",
                json.dumps(report, ensure_ascii=False),
            )
        return report

    async def create_run(
        self,
        date_from: date,
        date_to: date,
        chunk_days: int | None = None,
    ) -> int:
        if date_to < date_from:
            raise ValueError("Дата окончания раньше даты начала.")
        await self._preflight(date_to)

        chunk_days = chunk_days or settings.backfill_chunk_days
        chunk_days = max(1, min(int(chunk_days), 31))

        async with pool().acquire() as conn:
            run_id = await conn.fetchval(
                """
                INSERT INTO backfill_runs(
                    date_from,
                    date_to,
                    next_date,
                    chunk_days,
                    status
                )
                VALUES($1,$2,$1,$3,'PENDING')
                RETURNING id
                """,
                date_from,
                date_to,
                chunk_days,
            )

        return int(run_id)

    async def get_run(self, run_id: int) -> dict | None:
        async with pool().acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM backfill_runs
                WHERE id=$1
                """,
                run_id,
            )

        return self._serialize(row) if row else None

    async def latest_run(self) -> dict | None:
        async with pool().acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM backfill_runs
                ORDER BY id DESC
                LIMIT 1
                """
            )

        return self._serialize(row) if row else None

    async def latest_resumable_run(self) -> dict | None:
        async with pool().acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM backfill_runs
                WHERE status IN ('PENDING','RUNNING','PAUSED','ERROR')
                  AND next_date <= date_to
                ORDER BY id DESC
                LIMIT 1
                """
            )

        return self._serialize(row) if row else None

    async def pause(self, run_id: int) -> None:
        async with pool().acquire() as conn:
            await conn.execute(
                """
                UPDATE backfill_runs
                SET
                    status='PAUSED',
                    updated_at=NOW()
                WHERE id=$1
                  AND status <> 'COMPLETED'
                """,
                run_id,
            )

    async def run(
        self,
        run_id: int,
        progress_callback: ProgressCallback | None = None,
    ) -> dict:
        # A session lock also protects against a second CLI/container worker.
        async with pool().acquire() as lock_conn:
            lock_key = f"prichal-ai:{settings.db_schema}:backfill"
            acquired = await lock_conn.fetchval(
                "SELECT pg_try_advisory_lock(hashtext($1))", lock_key
            )
            if not acquired:
                raise ValueError("Другой backfill уже выполняется. Дождитесь его завершения.")
            try:
                return await self._run(run_id, progress_callback)
            finally:
                await lock_conn.execute("SELECT pg_advisory_unlock(hashtext($1))", lock_key)

    async def _run(
        self,
        run_id: int,
        progress_callback: ProgressCallback | None = None,
    ) -> dict:
        state = await self.get_run(run_id)
        if not state:
            raise ValueError(f"Backfill #{run_id} не найден.")

        date_from = date.fromisoformat(state["date_from"])
        date_to = date.fromisoformat(state["date_to"])
        next_date = date.fromisoformat(state["next_date"])
        chunk_days = int(state["chunk_days"])

        if next_date > date_to:
            await self._complete(run_id)
            return (await self.get_run(run_id)) or state

        expected_point_ids = await self._preflight(date_to)

        async with pool().acquire() as conn:
            await conn.execute(
                """
                UPDATE backfill_runs
                SET
                    status='RUNNING',
                    started_at=COALESCE(started_at,NOW()),
                    finished_at=NULL,
                    last_error='',
                    updated_at=NOW()
                WHERE id=$1
                """,
                run_id,
            )

        try:
            while next_date <= date_to:
                chunk_from = next_date
                chunk_to = min(
                    chunk_from + timedelta(days=chunk_days - 1),
                    date_to,
                )

                # Причал business date chunk_to includes the NIGHT that ends
                # on the following calendar morning. Therefore Saby must be
                # fetched through chunk_to + 1 calendar day.
                calendar_fetch_to = chunk_to + timedelta(days=1)

                logger.info(
                    "Backfill #%s chunk %s -> %s; Saby calendar fetch -> %s",
                    run_id,
                    chunk_from,
                    chunk_to,
                    calendar_fetch_to,
                )

                result = await sync_service.sync_range(
                    chunk_from,
                    calendar_fetch_to,
                    rebuild_from=chunk_from,
                    rebuild_to=chunk_to,
                    expected_point_ids=expected_point_ids,
                )

                report = await self.reconcile_chunk(run_id, chunk_from, chunk_to)
                if not report["ok"]:
                    failed_days = ", ".join(item["business_date"] for item in report["days"] if not item["ok"])
                    raise ValueError(
                        f"Reconcile REVIEW: {failed_days}. Прогресс блока не сохранён. "
                        "Проверьте /reconcile ДАТА; после исправления /backfillresume."
                    )

                next_date = chunk_to + timedelta(days=1)
                completed_days = min(
                    (next_date - date_from).days,
                    (date_to - date_from).days + 1,
                )

                async with pool().acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE backfill_runs
                        SET
                            next_date=$2,
                            completed_days=$3,
                            chunks_completed=chunks_completed+1,

                            stores_count=GREATEST(stores_count,$4),
                            sales_upserted=sales_upserted+$5,
                            items_upserted=items_upserted+$6,
                            shifts_built=shifts_built+$7,

                            last_chunk_from=$8,
                            last_chunk_to=$9,

                            status=CASE
                                WHEN $2 > date_to
                                THEN 'COMPLETED'
                                ELSE 'RUNNING'
                            END,

                            finished_at=CASE
                                WHEN $2 > date_to
                                THEN NOW()
                                ELSE NULL
                            END,

                            updated_at=NOW(),
                            last_error=''

                        WHERE id=$1
                        """,
                        run_id,
                        next_date,
                        completed_days,
                        int(result.get("stores", 0)),
                        int(result.get("sales_upserted", 0)),
                        int(result.get("items_upserted", 0)),
                        int(result.get("shifts_built", 0)),
                        chunk_from,
                        chunk_to,
                    )

                current = await self.get_run(run_id)

                if progress_callback and current:
                    await progress_callback(current)

            final = await self.get_run(run_id)
            if final is None:
                raise RuntimeError("Backfill state disappeared.")

            return final

        except asyncio.CancelledError:
            logger.warning("Backfill #%s paused by user", run_id)
            await self.pause(run_id)
            raise

        except Exception as exc:
            logger.exception("Backfill #%s failed", run_id)

            async with pool().acquire() as conn:
                await conn.execute(
                    """
                    UPDATE backfill_runs
                    SET
                        status='ERROR',
                        last_error=$2,
                        updated_at=NOW()
                    WHERE id=$1
                    """,
                    run_id,
                    str(exc)[:4000],
                )

            raise

    async def _complete(self, run_id: int) -> None:
        async with pool().acquire() as conn:
            await conn.execute(
                """
                UPDATE backfill_runs
                SET
                    status='COMPLETED',
                    finished_at=COALESCE(finished_at,NOW()),
                    updated_at=NOW()
                WHERE id=$1
                """,
                run_id,
            )

    def _serialize(self, row) -> dict:
        data = dict(row)

        for key in (
            "date_from",
            "date_to",
            "next_date",
            "last_chunk_from",
            "last_chunk_to",
        ):
            value = data.get(key)
            data[key] = value.isoformat() if value else None

        for key in (
            "created_at",
            "started_at",
            "updated_at",
            "finished_at",
        ):
            value = data.get(key)
            data[key] = value.isoformat() if value else None

        total_days = (
            date.fromisoformat(data["date_to"])
            - date.fromisoformat(data["date_from"])
        ).days + 1

        data["total_days"] = total_days
        data["progress_percent"] = round(
            100 * int(data["completed_days"]) / total_days,
            1,
        ) if total_days else 100.0

        chunk_days = max(1, int(data["chunk_days"]))
        data["total_chunks_estimate"] = (
            total_days + chunk_days - 1
        ) // chunk_days

        return data


backfill_service = BackfillService()
