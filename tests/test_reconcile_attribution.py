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
    monkeypatch.setattr(analytics, "inspect_shifts", AsyncMock(return_value={"work_shifts": []}))
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


def long_native_work():
    return dict(id=339, seller_name="Saby account", status="REVIEW", source="saby_native",
                duration_hours=23.35, cash_shift_count=2, missing_cash_shift_keys=[],
                check_count=4, net_revenue="1608", cash_components=[
                    dict(source="saby_native", status="AUTO", duration_hours=15.733,
                         check_count=3, net_revenue="600"),
                    dict(source="saby_native", status="AUTO", duration_hours=7.5,
                         check_count=1, net_revenue="1008")])


def test_owner_approved_long_native_shift_is_warning(monkeypatch):
    setup(monkeypatch, review_shifts=1)
    item = long_native_work()
    monkeypatch.setattr(analytics, "inspect_shifts", AsyncMock(return_value={"work_shifts": [item]}))
    report = asyncio.run(analytics.analytics_service.reconcile("2026-07-01"))
    row = report["stores"][0]
    assert report["ok"]
    assert row["review_shifts"] == 1
    assert row["blocking_review_shifts"] == 0
    assert row["warnings"][0]["work_shift_id"] == 339
    assert item["status"] == "REVIEW"  # stored payroll classification not rewritten


@pytest.mark.parametrize("changes", [
    {"work_revenue": 1607}, {"work_checks": 3},
    {"no_seller_checks": 1}, {"fallback_payments": 1}, {"review_shifts": 2},
])
def test_account_warning_does_not_hide_other_errors(monkeypatch, changes):
    setup(monkeypatch, **({"review_shifts": 1} | changes))
    monkeypatch.setattr(analytics, "inspect_shifts", AsyncMock(return_value={"work_shifts": [long_native_work()]}))
    assert not asyncio.run(analytics.analytics_service.reconcile("2026-07-01"))["ok"]


@pytest.mark.parametrize("change", ["missing", "fallback", "ambiguous", "short", "bad_sum"])
def test_only_duration_native_warning_is_eligible(change):
    from app.services.shift_inspection import keep_saby_account_warning
    item = long_native_work()
    if change == "missing":
        item["missing_cash_shift_keys"] = ["missing"]
    elif change == "fallback":
        item["cash_components"][0]["source"] = "fallback_reconstructed"
    elif change == "ambiguous":
        item["cash_components"][0]["status"] = "AMBIGUOUS"
    elif change == "short":
        item["duration_hours"] = 12
    else:
        item["cash_components"][0]["net_revenue"] = "601"
    assert not keep_saby_account_warning(item, 16)
