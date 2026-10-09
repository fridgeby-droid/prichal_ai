import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

from app.services import shift_inspection


def test_report_reads_linked_components_and_explains_long_merge(monkeypatch):
    start = datetime(2026, 7, 10, 5, tzinfo=timezone.utc)
    end = datetime(2026, 7, 11, 0, tzinfo=timezone.utc)
    work = dict(id=1, cash_shift_keys='["a", "b"]', duration_hours=19,
                status="REVIEW", started_at=start, ended_at=end)
    components = [dict(cash_shift_key=key, status="AUTO", started_at=start, ended_at=end)
                  for key in ("a", "b")]
    class Connection:
        fetch = AsyncMock(side_effect=[[work], components])

        @asynccontextmanager
        async def transaction(self, **kwargs):
            assert kwargs == {"isolation": "repeatable_read", "readonly": True}
            yield

    connection = Connection()
    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield connection

    monkeypatch.setattr(shift_inspection, "pool", Pool)
    report = asyncio.run(shift_inspection.inspect_shifts(5598, date(2026, 7, 10)))
    item = report["work_shifts"][0]
    assert item["review_reasons_current_settings"] == ["WORK_DURATION_EXCEEDS_LIMIT"]
    assert len(item["cash_components"]) == 2
    assert item["started_at"].endswith("+05:00")
    assert connection.fetch.await_args_list[1].args[1:] == (5598, ["a", "b"])
