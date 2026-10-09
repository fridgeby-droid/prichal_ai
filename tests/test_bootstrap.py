import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date, datetime
from unittest.mock import AsyncMock

import pytest

from app.services import backfill as module
from app.services.sync import SabySyncService
from app.services import sync as sync_module
from app.services.saby import SabyClient


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class Connection:
    def __init__(self):
        self.state = dict(id=1, date_from="2026-07-01", date_to="2026-07-14",
                          next_date="2026-07-01", chunk_days=7, status="PENDING")
        self.checkpoints = []
        self.reports = []

    async def execute(self, sql, *args):
        if "pg_advisory_unlock" in sql:
            return
        if "INSERT INTO app_meta" in sql:
            self.reports.append(json.loads(args[1]))
        elif "chunks_completed=chunks_completed+1" in sql:
            self.checkpoints.append(args[1])
            self.state["next_date"] = args[1].isoformat()
            self.state["status"] = "COMPLETED" if args[1] > date(2026, 7, 14) else "RUNNING"
        elif "status='ERROR'" in sql:
            self.state["status"] = "ERROR"
        elif "status='PAUSED'" in sql:
            self.state["status"] = "PAUSED"

    async def fetchval(self, *args):
        return True


def setup_run(monkeypatch):
    conn = Connection()
    service = module.BackfillService()
    monkeypatch.setattr(module, "pool", lambda: FakePool(conn))
    monkeypatch.setattr(service, "get_run", AsyncMock(side_effect=lambda _: dict(conn.state)))
    monkeypatch.setattr(service, "_preflight", AsyncMock(return_value={7}))
    sync = AsyncMock(return_value={"stores": 1})
    monkeypatch.setattr(module.sync_service, "sync_range", sync)
    return conn, service, sync


def test_reconcile_failure_keeps_week_and_resume_repeats_it(monkeypatch):
    conn, service, sync = setup_run(monkeypatch)
    review = True

    async def reconcile(day):
        return {"business_date": day, "ok": not review, "stores": []}

    monkeypatch.setattr(module.analytics_service, "reconcile", reconcile)
    with pytest.raises(ValueError, match="Reconcile REVIEW"):
        asyncio.run(service.run(1))
    assert conn.state["next_date"] == "2026-07-01"
    assert conn.state["status"] == "ERROR"
    assert conn.checkpoints == []
    assert len(conn.reports[0]["days"]) == 7
    assert conn.reports[0]["ok"] is False

    review = False
    result = asyncio.run(service.run(1))
    assert result["status"] == "COMPLETED"
    assert conn.checkpoints == [date(2026, 7, 8), date(2026, 7, 15)]
    calls = sync.await_args_list
    assert calls[0].args == calls[1].args == (date(2026, 7, 1), date(2026, 7, 8))
    assert calls[2].args == (date(2026, 7, 8), date(2026, 7, 15))
    assert calls[1].kwargs["rebuild_to"] == date(2026, 7, 7)
    assert calls[1].kwargs["expected_point_ids"] == {7}


def test_source_error_does_not_advance(monkeypatch):
    conn, service, sync = setup_run(monkeypatch)
    sync.side_effect = RuntimeError("source unavailable")
    with pytest.raises(RuntimeError):
        asyncio.run(service.run(1))
    assert conn.checkpoints == []
    assert conn.state["status"] == "ERROR"


def test_cancellation_keeps_checkpoint(monkeypatch):
    conn, service, sync = setup_run(monkeypatch)
    sync.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.run(1))
    assert conn.state["status"] == "PAUSED"
    assert conn.state["next_date"] == "2026-07-01"


def test_second_worker_is_rejected(monkeypatch):
    conn, service, sync = setup_run(monkeypatch)
    conn.fetchval = AsyncMock(return_value=False)
    with pytest.raises(ValueError, match="Другой backfill"):
        asyncio.run(service.run(1))
    sync.assert_not_awaited()


def test_store_stage_does_not_load_sales(monkeypatch):
    service = SabySyncService()
    points = [{"id": 7, "name": "Store"}]
    monkeypatch.setattr(sync_module.saby_client, "list_points", AsyncMock(return_value=points))
    orders = AsyncMock()
    monkeypatch.setattr(sync_module.saby_client, "orders_for_point_date", orders)
    write = AsyncMock()
    monkeypatch.setattr(service, "_upsert_stores", write)
    assert asyncio.run(service.sync_stores()) == points
    write.assert_awaited_once_with(points)
    orders.assert_not_awaited()


def test_empty_store_directory_is_rejected(monkeypatch):
    monkeypatch.setattr(sync_module.saby_client, "list_points", AsyncMock(return_value=[]))
    with pytest.raises(ValueError, match="пустой"):
        asyncio.run(SabySyncService().sync_stores())


def test_invalid_source_response_is_not_empty_success(monkeypatch):
    client = SabyClient()
    monkeypatch.setattr(client, "_get", AsyncMock(return_value={"error": "unavailable"}))
    with pytest.raises(ValueError, match="orders"):
        asyncio.run(client.orders_for_point_date(7, "2026-07-01"))


def test_backfill_requires_stores_and_disabled_auto_sync(monkeypatch):
    service = module.BackfillService()
    monkeypatch.setattr(module.settings, "auto_sync_enabled", True)
    with pytest.raises(ValueError, match="AUTO_SYNC_ENABLED"):
        asyncio.run(service._preflight(date(2026, 7, 1)))
    monkeypatch.setattr(module.settings, "auto_sync_enabled", False)
    conn = Connection()
    conn.fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(module, "pool", lambda: FakePool(conn))
    with pytest.raises(ValueError, match="stores"):
        asyncio.run(service._preflight(date(2026, 7, 1)))


def test_before_eight_yesterday_is_still_open(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 10, 7, 59, tzinfo=tz)

    monkeypatch.setattr(module, "datetime", Clock)
    conn = Connection()
    conn.fetch = AsyncMock(return_value=[{"point_id": 7}])
    monkeypatch.setattr(module, "pool", lambda: FakePool(conn))
    service = module.BackfillService()
    with pytest.raises(ValueError, match="business days"):
        asyncio.run(service._preflight(date(2026, 7, 9)))
    assert asyncio.run(service._preflight(date(2026, 7, 8))) == {7}


def test_web_and_cli_import_without_starting_services():
    from app.web import app
    from scripts.bootstrap import main
    assert app.version == "0.3.8"
    assert callable(main)


def test_dense_source_with_repeated_pages_is_rejected(monkeypatch):
    client = SabyClient()
    # Force dense slices all the way to guarded pagination.
    orders = [{"Sale": i, "DateWTZ": "2026-07-01T10:00:00+05:00"} for i in range(100)]
    monkeypatch.setattr(client, "_get", AsyncMock(return_value={"orders": orders}))
    with pytest.raises(ValueError, match="повторяет страницу"):
        asyncio.run(client.orders_for_point_date(7, "2026-07-01"))
