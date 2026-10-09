import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from app.services import analytics, shift_engine


class Pool:
    def __init__(self, rows):
        self.fetch = AsyncMock(return_value=rows)

    @asynccontextmanager
    async def acquire(self):
        yield self


def setup(monkeypatch, **changes):
    # Native NIGHT ends after 08:00. Its tail belongs to the next clock day,
    # but remains in the same paid shift under the established native rule.
    events = []
    for index, (stamp, amount) in enumerate([
        ("2026-07-01T20:00:00", "100"),
        ("2026-07-01T22:00:00", "200"),
        ("2026-07-02T04:00:00", "300"),
        ("2026-07-02T08:05:00", "1008"),
    ]):
        dt = datetime.fromisoformat(stamp).replace(tzinfo=ZoneInfo("Asia/Yekaterinburg"))
        events.append(dict(point_id=5590, sale_id=index, business_date=date(2026, 7, 1 if index < 3 else 2),
                           business_shift_type="NIGHT" if index < 3 else "DAY", carried_at=dt,
                           seller_id=1, seller_name="Seller", signed_amount=Decimal(amount),
                           saby_shift_id=77, saby_shift_number="77", payment_key=str(index)))
    row = dict(point_id=5590, store="Test store", sale_count=3, sale_total=Decimal("600"),
               payment_checks=3, payment_revenue=Decimal("600"), returns_amount=0,
               work_checks=4, work_revenue=Decimal("1608"), work_shifts=1,
               review_shifts=0, fallback_payments=0, no_seller_checks=0)
    row.update(changes)
    monkeypatch.setattr(shift_engine, "pool", lambda: Pool(events))
    monkeypatch.setattr(analytics, "pool", lambda: Pool([row]))
    return events


def test_whole_native_shift_is_not_false_reconcile_error(monkeypatch):
    setup(monkeypatch)
    report = asyncio.run(analytics.analytics_service.reconcile("2026-07-01"))
    assert report["ok"] is True
    row = report["stores"][0]
    assert row["payment_vs_work"] == -1008
    assert row["attribution_revenue_delta"] == 1008
    assert row["attribution_checks_delta"] == 1
    assert row["expected_vs_work"] == 0


@pytest.mark.parametrize("changes", [
    {"work_revenue": Decimal("1607")}, {"work_checks": 3},
    {"work_revenue": Decimal("1609")}, {"work_checks": 5},
    {"fallback_payments": 1}, {"no_seller_checks": 1}, {"review_shifts": 1},
])
def test_real_mismatches_and_quality_flags_still_block(monkeypatch, changes):
    setup(monkeypatch, **changes)
    report = asyncio.run(analytics.analytics_service.reconcile("2026-07-01"))
    assert report["ok"] is False


def test_next_day_tail_is_reported_as_outbound_attribution(monkeypatch):
    setup(monkeypatch, sale_count=1, sale_total=1008, payment_checks=1,
          payment_revenue=1008, work_checks=0, work_revenue=0, work_shifts=0)
    report = asyncio.run(analytics.analytics_service.reconcile("2026-07-02"))
    assert report["ok"] is True
    assert report["stores"][0]["attribution_revenue_delta"] == -1008


def test_expected_only_store_is_not_silently_omitted(monkeypatch):
    setup(monkeypatch)
    monkeypatch.setattr(analytics, "pool", lambda: Pool([]))
    report = asyncio.run(analytics.analytics_service.reconcile("2026-07-01"))
    assert report["ok"] is False
    assert report["stores"][0]["expected_work_checks"] == 4


def test_week_and_day_native_attribution_match(monkeypatch):
    setup(monkeypatch)
    engine = shift_engine.shift_engine
    day = asyncio.run(engine.expected_cash_shifts(date(2026, 7, 1), date(2026, 7, 1)))
    week = asyncio.run(engine.expected_cash_shifts(date(2026, 7, 1), date(2026, 7, 7)))
    assert day == week
