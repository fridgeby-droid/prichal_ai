import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from app.services.saby import SabyClient
from app.services import backfill
from test_bootstrap import FakePool


def retail_db():
    conn = sqlite3.connect(":memory:")
    tables = ["stores", "sales", "sale_payments", "sale_items", "cash_shifts", "employee_work_shifts", "seller_shifts", "shift_plans"]
    for table in tables:
        conn.execute(f"CREATE TABLE {table}(point_id INTEGER, amount NUMERIC)")
        conn.executemany(f"INSERT INTO {table} VALUES (?, ?)", [(23109, 1596), (5583, 100), (5598, 200)])
    ddl = (Path(__file__).parents[1] / "app/db/schema.sql").read_text(encoding="utf-8")
    scope = ddl[ddl.index("CREATE OR REPLACE VIEW excluded_retail_points"):]
    conn.executescript(scope.replace("CREATE OR REPLACE VIEW", "CREATE VIEW"))
    return conn, tables


def test_historical_rc_excluded_without_deleting_facts():
    conn, tables = retail_db()
    try:
        for table in tables:
            assert conn.execute(f"SELECT COUNT(*), SUM(amount) FROM retail_{table}").fetchone() == (2, 300)
            assert conn.execute(f"SELECT amount FROM {table} WHERE point_id=23109").fetchone() == (1596,)
        assert conn.execute("SELECT SUM(p.amount) FROM retail_sale_payments p JOIN retail_sales s ON s.point_id=p.point_id").fetchone() == (300,)
    finally:
        conn.close()


@pytest.mark.parametrize("allowed", [set(), {23109, 5583, 5598}, {23109}])
def test_rc_never_requested_even_with_explicit_saby_filter(monkeypatch, allowed):
    client = SabyClient()
    client.settings = SimpleNamespace(saby_points_filter=allowed)
    monkeypatch.setattr(client, "_get", AsyncMock(return_value={"salesPoints": [
        {"id": 23109, "name": "Renamed RC"}, {"id": 5583, "name": "Store A"}, {"id": 5598, "name": "Store B"}
    ]}))
    points = asyncio.run(client.list_points())
    assert {p["id"] for p in points} == ({5583, 5598} if allowed != {23109} else set())


def test_resume_preflight_uses_same_store_scope(monkeypatch):
    from datetime import date
    conn, _ = retail_db()
    class Connection:
        async def fetch(self, sql):
            return [{"point_id": row[0]} for row in conn.execute(sql)]
    monkeypatch.setattr(backfill, "pool", lambda: FakePool(Connection()))
    monkeypatch.setattr(backfill, "settings", SimpleNamespace(auto_sync_enabled=False, auto_sync_on_start=False, business_tz="Asia/Yekaterinburg", business_day_start_hour=8))
    try:
        assert asyncio.run(backfill.BackfillService()._preflight(date(2020, 1, 1))) == {5583, 5598}
    finally:
        conn.close()
