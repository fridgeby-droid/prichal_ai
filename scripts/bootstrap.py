"""Clean initial load using the existing schema and business services."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str), flush=True)


async def execute(args):
    from app.db.database import init_db, close_db, health, pool
    from app.services.backfill import backfill_service
    from app.services.sync import sync_service
    from app.services.payroll import payroll_service
    from app.services.storage import storage

    await init_db()
    try:
        if args.command == "init-db":
            output(await health())
        elif args.command == "status":
            output({"database": await health(), "s3": await storage.health(),
                    "backfill": await backfill_service.latest_run()})
        elif args.command == "stores":
            output(await sync_service.sync_stores())
        elif args.command == "shift-report":
            from app.services.shift_inspection import inspect_shifts
            output(await inspect_shifts(args.point_id, args.day))
        elif args.command == "backfill":
            run_id = await backfill_service.create_run(args.date_from, args.date_to, chunk_days=7)
            output({"created_run_id": run_id})
            async def progress(state):
                output(state)
            output(await backfill_service.run(run_id, progress_callback=progress))
        elif args.command == "resume":
            output(await backfill_service.run(args.run_id))
        elif args.command == "reconcile-report":
            async with pool().acquire() as conn:
                rows = await conn.fetch(
                    "SELECT key, value, updated_at FROM app_meta WHERE key LIKE $1 ORDER BY key",
                    f"backfill:{args.run_id}:reconcile:%",
                )
            output([{**dict(row), "value": json.loads(row["value"])} for row in rows])
        elif args.command == "foundation":
            state = await backfill_service.get_run(args.run_id)
            if not state or state["status"] != "COMPLETED":
                raise ValueError("Сначала завершите указанный backfill и reconcile.")
            # Recheck the whole selected period before deriving identities.
            report = await backfill_service.reconcile_chunk(
                args.run_id, date.fromisoformat(state["date_from"]), date.fromisoformat(state["date_to"])
            )
            if not report["ok"]:
                raise ValueError("Reconcile REVIEW: foundation остановлен; смотрите reconcile-report.")
            output({"identity": await payroll_service.sync_seller_identities(),
                    "policies": await payroll_service.list_policies("SELLER"),
                    "next": "Задайте реальные планы и результаты экзаменов; проверьте payrollpreview/payrollmonth."})
    finally:
        await close_db()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init-db", "status", "stores"):
        commands.add_parser(name)
    inspection = commands.add_parser("shift-report")
    inspection.add_argument("point_id", type=int)
    inspection.add_argument("day", type=date.fromisoformat)
    load = commands.add_parser("backfill")
    load.add_argument("date_from", type=date.fromisoformat)
    load.add_argument("date_to", type=date.fromisoformat)
    for name in ("resume", "reconcile-report", "foundation"):
        cmd = commands.add_parser(name)
        cmd.add_argument("run_id", type=int)
    args = parser.parse_args()
    try:
        asyncio.run(execute(args))
    except (Exception, KeyboardInterrupt) as exc:
        parser.exit(1, f"Bootstrap остановлен: {exc}\n")


if __name__ == "__main__":
    main()
