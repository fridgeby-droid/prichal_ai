from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db.database import pool


settings = get_settings()


@dataclass(slots=True)
class SaleEvent:
    point_id: int
    sale_id: int
    when: datetime
    seller_id: int | None
    seller_name: str
    amount: Decimal
    is_return: bool
    saby_shift_id: int | None
    saby_shift_number: str

    @property
    def seller_key(self) -> str:
        if self.seller_id is not None:
            return f"id:{self.seller_id}"
        return "name:" + " ".join(self.seller_name.casefold().split())

    @property
    def display_name(self) -> str:
        if self.seller_name.strip():
            return self.seller_name.strip()
        if self.seller_id is not None:
            return f"Seller #{self.seller_id}"
        return "Продавец не определён"


@dataclass(slots=True)
class ShiftResult:
    point_id: int
    work_date: date
    shift_type: str
    seller_key: str
    seller_id: int | None
    seller_name: str
    started_at: datetime
    ended_at: datetime
    check_count: int
    net_revenue: Decimal
    source: str
    confidence: float
    status: str
    saby_shift_id: int | None
    saby_shift_number: str
    duration_hours: float
    dominant_share: float


class ShiftEngine:
    def _tz(self) -> ZoneInfo:
        return ZoneInfo(settings.business_tz)

    def _orientation(self, dt: datetime) -> str:
        hour = dt.astimezone(self._tz()).hour
        if settings.shift_day_start_hour <= hour < settings.shift_night_start_hour:
            return "DAY"
        return "NIGHT"

    def _work_date(self, events: list[SaleEvent], shift_type: str) -> date:
        local_events = [e.when.astimezone(self._tz()) for e in events]

        if shift_type == "DAY":
            day_candidates = [
                dt.date()
                for dt in local_events
                if settings.shift_day_start_hour
                <= dt.hour
                < settings.shift_night_start_hour
            ]
            return day_candidates[0] if day_candidates else local_events[0].date()

        # Night work date is the evening date. If a session only contains
        # post-midnight checks, it belongs to the previous calendar date.
        evening = [
            dt.date()
            for dt in local_events
            if dt.hour >= settings.shift_night_start_hour
        ]
        if evening:
            return evening[0]

        return local_events[0].date() - timedelta(days=1)

    @staticmethod
    def _uniform_saby_shift(events: list[SaleEvent]) -> tuple[int | None, str]:
        ids = {e.saby_shift_id for e in events if e.saby_shift_id is not None}
        nums = {e.saby_shift_number for e in events if e.saby_shift_number}

        shift_id = next(iter(ids)) if len(ids) == 1 else None
        shift_number = next(iter(nums)) if len(nums) == 1 else ""
        return shift_id, shift_number

    def _classify_session(self, events: list[SaleEvent]) -> ShiftResult:
        events = sorted(events, key=lambda e: e.when)
        counts = {"DAY": 0, "NIGHT": 0}

        for event in events:
            counts[self._orientation(event.when)] += 1

        shift_type = "DAY" if counts["DAY"] >= counts["NIGHT"] else "NIGHT"
        dominant = counts[shift_type]
        dominant_share = dominant / len(events)

        started = events[0].when
        ended = events[-1].when
        duration_hours = max(0.0, (ended - started).total_seconds() / 3600)

        saby_shift_id, saby_shift_number = self._uniform_saby_shift(events)
        has_saby_shift = saby_shift_id is not None or bool(saby_shift_number)

        if has_saby_shift:
            source = "hybrid"
        else:
            source = "reconstructed"

        # Confidence follows the logic we already validated in Apps Script:
        # dominant share + duration sanity. Saby shift id is additional evidence,
        # not a replacement for time classification.
        confidence = dominant_share
        if has_saby_shift:
            confidence = min(1.0, confidence + 0.10)

        if duration_hours > settings.shift_max_duration_hours:
            status = "REVIEW"
        elif dominant_share >= settings.shift_auto_share:
            status = "AUTO"
        elif dominant_share >= settings.shift_ambiguous_share:
            status = "REVIEW"
        else:
            status = "AMBIGUOUS"

        net_revenue = Decimal("0")
        for event in events:
            value = abs(event.amount)
            net_revenue += -value if event.is_return else value

        first = events[0]

        return ShiftResult(
            point_id=first.point_id,
            work_date=self._work_date(events, shift_type),
            shift_type=shift_type,
            seller_key=first.seller_key,
            seller_id=first.seller_id,
            seller_name=first.display_name,
            started_at=started,
            ended_at=ended,
            check_count=len(events),
            net_revenue=net_revenue,
            source=source,
            confidence=confidence,
            status=status,
            saby_shift_id=saby_shift_id,
            saby_shift_number=saby_shift_number,
            duration_hours=duration_hours,
            dominant_share=dominant_share,
        )

    async def rebuild_range(self, date_from: date, date_to: date) -> int:
        # One-day buffers cover night sessions crossing midnight.
        query_from = date_from - timedelta(days=1)
        query_to = date_to + timedelta(days=2)

        async with pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    point_id, sale_id, sale_datetime,
                    seller_id, seller_name,
                    total_price, is_return,
                    saby_shift_id, saby_shift_number
                FROM sales
                WHERE deleted=FALSE
                  AND sale_datetime >= $1
                  AND sale_datetime < $2
                  AND (seller_id IS NOT NULL OR seller_name <> '')
                  AND sale_datetime IS NOT NULL
                ORDER BY point_id, COALESCE(CAST(seller_id AS TEXT), seller_name), sale_datetime
                """,
                datetime.combine(query_from, datetime.min.time(), tzinfo=self._tz()),
                datetime.combine(query_to, datetime.min.time(), tzinfo=self._tz()),
            )

        grouped: dict[tuple[int, str], list[SaleEvent]] = defaultdict(list)

        for row in rows:
            event = SaleEvent(
                point_id=row["point_id"],
                sale_id=row["sale_id"],
                when=row["sale_datetime"],
                seller_id=row["seller_id"],
                seller_name=row["seller_name"],
                amount=Decimal(row["total_price"] or 0),
                is_return=bool(row["is_return"]),
                saby_shift_id=row["saby_shift_id"],
                saby_shift_number=row["saby_shift_number"] or "",
            )
            grouped[(event.point_id, event.seller_key)].append(event)

        results: list[ShiftResult] = []
        gap = timedelta(hours=settings.shift_session_gap_hours)

        for events in grouped.values():
            events.sort(key=lambda e: e.when)
            session: list[SaleEvent] = []

            for event in events:
                if session and event.when - session[-1].when > gap:
                    result = self._classify_session(session)
                    if date_from <= result.work_date <= date_to:
                        results.append(result)
                    session = []

                session.append(event)

            if session:
                result = self._classify_session(session)
                if date_from <= result.work_date <= date_to:
                    results.append(result)

        async with pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    DELETE FROM seller_shifts
                    WHERE work_date BETWEEN $1 AND $2
                    """,
                    date_from,
                    date_to,
                )

                for shift in results:
                    await conn.execute(
                        """
                        INSERT INTO seller_shifts(
                            point_id, work_date, shift_type,
                            seller_key, seller_id, seller_name,
                            started_at, ended_at,
                            check_count, net_revenue,
                            source, confidence, status,
                            saby_shift_id, saby_shift_number,
                            duration_hours, dominant_share,
                            updated_at
                        )
                        VALUES(
                            $1,$2,$3,
                            $4,$5,$6,
                            $7,$8,
                            $9,$10,
                            $11,$12,$13,
                            $14,$15,
                            $16,$17,
                            NOW()
                        )
                        """,
                        shift.point_id,
                        shift.work_date,
                        shift.shift_type,
                        shift.seller_key,
                        shift.seller_id,
                        shift.seller_name,
                        shift.started_at,
                        shift.ended_at,
                        shift.check_count,
                        shift.net_revenue,
                        shift.source,
                        shift.confidence,
                        shift.status,
                        shift.saby_shift_id,
                        shift.saby_shift_number,
                        shift.duration_hours,
                        shift.dominant_share,
                    )

        return len(results)


shift_engine = ShiftEngine()
