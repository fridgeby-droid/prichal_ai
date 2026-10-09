"""Read stored work shifts and their exact cash-shift components."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db.database import pool


def keep_saby_account_warning(item: dict, max_hours: float) -> bool:
    """Owner decision: retain Saby attribution; duration-only native REVIEW is advisory."""
    cash = item.get("cash_components", [])
    if not (item.get("status") == "REVIEW" and item.get("source") == "saby_native"
            and float(item.get("duration_hours", 0)) > max_hours and cash
            and not item.get("missing_cash_shift_keys")
            and len(cash) == item.get("cash_shift_count")):
        return False
    for component in cash:
        if component.get("source") != "saby_native":
            return False
        if component.get("status") != "AUTO" and not (
            component.get("status") == "REVIEW"
            and float(component.get("duration_hours", 0)) > max_hours
        ):
            return False
    return (sum(c["check_count"] for c in cash) == item["check_count"]
            and sum((Decimal(str(c["net_revenue"])) for c in cash), Decimal("0"))
            == Decimal(str(item["net_revenue"])))


async def inspect_shifts(point_id: int, day: date) -> dict:
    settings = get_settings()
    async with pool().acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            work = await conn.fetch(
                """SELECT id, seller_name, business_date, shift_type, started_at, ended_at,
                          duration_hours, status, source, confidence, check_count,
                          net_revenue, cash_shift_count, cash_shift_keys
                   FROM retail_employee_work_shifts
                   WHERE point_id=$1 AND business_date=$2 ORDER BY started_at, id""",
                point_id, day,
            )
            result = []
            for row in work:
                item = dict(row)
                keys = item["cash_shift_keys"]
                if isinstance(keys, str):
                    keys = json.loads(keys)
                cash = await conn.fetch(
                    """SELECT cash_shift_key, saby_shift_id, saby_shift_number, seller_name,
                              business_date, shift_type, started_at, ended_at, duration_hours,
                              status, source, confidence, dominant_share, check_count, net_revenue
                       FROM retail_cash_shifts WHERE point_id=$1 AND cash_shift_key=ANY($2::text[])
                       ORDER BY started_at, cash_shift_key""", point_id, keys,
                )
                reasons = []
                if float(item["duration_hours"]) > settings.shift_max_duration_hours:
                    reasons.append("WORK_DURATION_EXCEEDS_LIMIT")
                if any(c["status"] != "AUTO" for c in cash):
                    reasons.append("CASH_COMPONENT_REQUIRES_REVIEW")
                missing = sorted(set(keys) - {c["cash_shift_key"] for c in cash})
                if missing:
                    reasons.append("CASH_COMPONENT_MISSING")
                if item["status"] != "AUTO" and not reasons:
                    reasons.append("STORED_REVIEW_NOT_EXPLAINED_BY_CURRENT_LIMITS")
                item["cash_shift_keys"] = keys
                item["cash_components"] = [dict(c) for c in cash]
                item["review_reasons_current_settings"] = reasons
                item["missing_cash_shift_keys"] = missing
                result.append(item)

    # Present times explicitly in the business timezone, not the host timezone.
    for item in result:
        for record in [item, *item["cash_components"]]:
            for key in ("started_at", "ended_at"):
                if record[key] is not None:
                    record[key] = record[key].astimezone(ZoneInfo(settings.business_tz)).isoformat()
    return {"point_id": point_id, "business_date": day.isoformat(),
            "timezone": settings.business_tz,
            "current_limits": {"max_duration_hours": settings.shift_max_duration_hours,
                               "merge_gap_hours": settings.work_shift_merge_gap_hours},
            "work_shifts": result}
