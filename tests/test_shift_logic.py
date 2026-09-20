from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

# This file documents the intended business cases.
# Runtime integration tests are performed after deployment with real Saby data.

TZ = ZoneInfo("Asia/Yekaterinburg")


def orientation(dt):
    return "DAY" if 8 <= dt.astimezone(TZ).hour < 20 else "NIGHT"


def test_boundaries():
    assert orientation(datetime(2026, 9, 20, 8, 0, tzinfo=TZ)) == "DAY"
    assert orientation(datetime(2026, 9, 20, 19, 59, tzinfo=TZ)) == "DAY"
    assert orientation(datetime(2026, 9, 20, 20, 0, tzinfo=TZ)) == "NIGHT"
    assert orientation(datetime(2026, 9, 21, 3, 0, tzinfo=TZ)) == "NIGHT"
