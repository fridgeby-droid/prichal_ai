from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
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

    @property
    def has_native_shift(self) -> bool:
        return self.saby_shift_id is not None or bool(self.saby_shift_number)

    @property
    def native_shift_key(self) -> str:
        if self.saby_shift_id is not None:
            return f"id:{self.saby_shift_id}"
        if self.saby_shift_number:
            return f"number:{self.saby_shift_number}"
        return ""


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
    """
    Priority:
    1. Saby native Shift ID / ShiftNumber + Seller.
    2. Seller-first reconstruction by activity gap only for sales
       without a native shift identifier.

    DAY/NIGHT is derived from actual fiscal check timestamps
    (Payments.CarriedWTZ stored in sales.sale_datetime).
    """

    def _tz(self) -> ZoneInfo:
        return ZoneInfo(settings.business_tz)

    def _orientation(self, dt: datetime) -> str:
        hour = dt.astimezone(self._tz()).hour
        if settings.shift_day_start_hour <= hour < settings.shift_night_start_hour:
            return "DAY"
        return "NIGHT"

    def _work_date(self, events: list[SaleEvent], shift_type: str) -> date:
        local_events = [event.when.astimezone(self._tz()) for event in events]

        if shift_type == "DAY":
            candidates = [
                dt.date()
                for dt in local_events
                if settings.shift_day_start_hour
                <= dt.hour
                < settings.shift_night_start_hour
            ]
            return candidates[0] if candidates else local_events[0].date()

        # A night shift belongs to the evening on which it started.
        evening_dates = [
            dt.date()
            for dt in local_events
            if dt.hour >= settings.shift_night_start_hour
        ]
        if evening_dates:
            return evening_dates[0]

        # If we only have checks after midnight, anchor them to the previous day.
        return local_events[0].date() - timedelta(days=1)

    @staticmethod
    def _uniform_native_shift(
        events: list[SaleEvent],
    ) -> tuple[int | None, str]:
        ids = {
            event.saby_shift_id
            for event in events
            if event.saby_shift_id is not None
        }
        numbers = {
            event.saby_shift_number
            for event in events
            if event.saby_shift_number
        }

        shift_id = next(iter(ids)) if len(ids) == 1 else None
        shift_number = next(iter(numbers)) if len(numbers) == 1 else ""
        return shift_id, shift_number

    def _build_result(
        self,
        events: list[SaleEvent],
        source: str,
    ) -> ShiftResult:
        events = sorted(events, key=lambda event: event.when)

        orientation_counts = {"DAY": 0, "NIGHT": 0}
        for event in events:
            orientation_counts[self._orientation(event.when)] += 1

        shift_type = (
            "DAY"
            if orientation_counts["DAY"] >= orientation_counts["NIGHT"]
            else "NIGHT"
        )

        dominant = orientation_counts[shift_type]
        dominant_share = dominant / len(events)

        started_at = events[0].when
        ended_at = events[-1].when
        duration_hours = max(
            0.0,
            (ended_at - started_at).total_seconds() / 3600,
        )

        saby_shift_id, saby_shift_number = self._uniform_native_shift(events)

        net_revenue = Decimal("0")
        for event in events:
            amount = abs(event.amount)
            net_revenue += -amount if event.is_return else amount

        # Native Saby shift identity is stronger evidence than reconstructed gap.
        if source == "saby_native":
            confidence = 1.0
            if duration_hours > settings.shift_max_duration_hours:
                status = "REVIEW"
                confidence = 0.95
            elif dominant_share < settings.shift_ambiguous_share:
                status = "REVIEW"
                confidence = max(0.80, dominant_share)
            else:
                status = "AUTO"
        else:
            confidence = dominant_share
            if duration_hours > settings.shift_max_duration_hours:
                status = "REVIEW"
            elif dominant_share >= settings.shift_auto_share:
                status = "AUTO"
            elif dominant_share >= settings.shift_ambiguous_share:
                status = "REVIEW"
            else:
                status = "AMBIGUOUS"

        first = events[0]

        return ShiftResult(
            point_id=first.point_id,
            work_date=self._work_date(events, shift_type),
            shift_type=shift_type,
            seller_key=first.seller_key,
            seller_id=first.seller_id,
            seller_name=first.display_name,
            started_at=started_at,
            ended_at=ended_at,
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

    def _native_results(
        self,
        events: list[SaleEvent],
    ) -> list[ShiftResult]:
        groups: dict[tuple[int, str, str], list[SaleEvent]] = defaultdict(list)

        for event in events:
            if not event.has_native_shift:
                continue

            # Seller remains part of the key:
            # one cash shift can contain a handover/change of employee.
            key = (
                event.point_id,
                event.native_shift_key,
                event.seller_key,
            )
            groups[key].append(event)

        return [
            self._build_result(group, "saby_native")
            for group in groups.values()
        ]

    def _fallback_results(
        self,
        events: list[SaleEvent],
    ) -> list[ShiftResult]:
        groups: dict[tuple[int, str], list[SaleEvent]] = defaultdict(list)

        for event in events:
            if event.has_native_shift:
                continue
            groups[(event.point_id, event.seller_key)].append(event)

        gap = timedelta(hours=settings.shift_session_gap_hours)
        results: list[ShiftResult] = []

        for seller_events in groups.values():
            seller_events.sort(key=lambda event: event.when)
            session: list[SaleEvent] = []

            for event in seller_events:
                if session and event.when - session[-1].when > gap:
                    results.append(
                        self._build_result(
                            session,
                            "fallback_reconstructed",
                        )
                    )
                    session = []
                session.append(event)

            if session:
                results.append(
                    self._build_result(
                        session,
                        "fallback_reconstructed",
                    )
                )

        return results

    async def rebuild_range(self, date_from: date, date_to: date) -> int:
        # Buffers are required for night shifts crossing midnight.
        query_from = date_from - timedelta(days=1)
        query_to = date_to + timedelta(days=2)

        start_dt = datetime.combine(
            query_from,
            datetime.min.time(),
            tzinfo=self._tz(),
        )
        end_dt = datetime.combine(
            query_to,
            datetime.min.time(),
            tzinfo=self._tz(),
        )

        async with pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    point_id,
                    sale_id,
                    sale_datetime,
                    seller_id,
                    seller_name,
                    total_price,
                    is_return,
                    saby_shift_id,
                    saby_shift_number
                FROM sales
                WHERE deleted=FALSE
                  AND sale_datetime >= $1
                  AND sale_datetime < $2
                  AND sale_datetime IS NOT NULL
                  AND (seller_id IS NOT NULL OR seller_name <> '')
                ORDER BY point_id, sale_datetime
                """,
                start_dt,
                end_dt,
            )

        events: list[SaleEvent] = []
        for row in rows:
            events.append(
                SaleEvent(
                    point_id=row["point_id"],
                    sale_id=row["sale_id"],
                    when=row["sale_datetime"],
                    seller_id=row["seller_id"],
                    seller_name=row["seller_name"] or "",
                    amount=Decimal(row["total_price"] or 0),
                    is_return=bool(row["is_return"]),
                    saby_shift_id=row["saby_shift_id"],
                    saby_shift_number=row["saby_shift_number"] or "",
                )
            )

        candidates = self._native_results(events) + self._fallback_results(events)
        results = [
            result
            for result in candidates
            if date_from <= result.work_date <= date_to
        ]

        # Stable sort makes reports predictable.
        results.sort(
            key=lambda result: (
                result.work_date,
                result.point_id,
                result.shift_type,
                result.started_at,
                result.seller_name,
            )
        )

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

                rows_to_insert = [
                    (
                        result.point_id,
                        result.work_date,
                        result.shift_type,
                        result.seller_key,
                        result.seller_id,
                        result.seller_name,
                        result.started_at,
                        result.ended_at,
                        result.check_count,
                        result.net_revenue,
                        result.source,
                        result.confidence,
                        result.status,
                        result.saby_shift_id,
                        result.saby_shift_number,
                        result.duration_hours,
                        result.dominant_share,
                    )
                    for result in results
                ]

                if rows_to_insert:
                    await conn.executemany(
                        """
                        INSERT INTO seller_shifts(
                            point_id,
                            work_date,
                            shift_type,
                            seller_key,
                            seller_id,
                            seller_name,
                            started_at,
                            ended_at,
                            check_count,
                            net_revenue,
                            source,
                            confidence,
                            status,
                            saby_shift_id,
                            saby_shift_number,
                            duration_hours,
                            dominant_share,
                            updated_at
                        )
                        VALUES(
                            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,
                            $11,$12,$13,$14,$15,$16,$17,NOW()
                        )
                        """,
                        rows_to_insert,
                    )

        return len(results)


shift_engine = ShiftEngine()
