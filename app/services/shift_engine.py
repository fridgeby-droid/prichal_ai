from __future__ import annotations

import json
from collections import Counter, defaultdict
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
    business_date: date
    check_shift_type: str
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

        return "name:" + " ".join(
            self.seller_name.casefold().split()
        )

    @property
    def display_name(self) -> str:
        if self.seller_name.strip():
            return self.seller_name.strip()

        if self.seller_id is not None:
            return f"Seller #{self.seller_id}"

        return "Продавец не определён"

    @property
    def native_shift_key(self) -> str:
        if self.saby_shift_id is not None:
            return f"id:{self.saby_shift_id}"

        if self.saby_shift_number:
            return f"number:{self.saby_shift_number}"

        return ""


@dataclass(slots=True)
class CashShift:
    key: str

    point_id: int
    business_date: date
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


@dataclass(slots=True)
class WorkShift:
    point_id: int
    business_date: date
    shift_type: str

    seller_key: str
    seller_id: int | None
    seller_name: str

    started_at: datetime
    ended_at: datetime

    check_count: int
    net_revenue: Decimal

    cash_shift_count: int
    cash_shift_keys: list[str]

    source: str
    confidence: float
    status: str

    duration_hours: float


class ShiftEngine:
    def _tz(self) -> ZoneInfo:
        return ZoneInfo(settings.business_tz)

    def _local(self, dt: datetime) -> datetime:
        return dt.astimezone(self._tz())

    def _clock_type(self, dt: datetime) -> str:
        local = self._local(dt)
        if settings.shift_day_start_hour <= local.hour < settings.shift_night_start_hour:
            return "DAY"
        return "NIGHT"

    def _native_anchor(self, events: list[SaleEvent]) -> tuple[date, str, float]:
        """Anchor the WHOLE native Saby shift before splitting by seller."""
        types = [self._clock_type(event.when) for event in events]
        counts = Counter(types)
        shift_type = "DAY" if counts["DAY"] >= counts["NIGHT"] else "NIGHT"
        dominant_share = counts[shift_type] / len(events)
        local_times = [self._local(event.when) for event in events]

        if shift_type == "DAY":
            dates = [
                dt.date() for dt in local_times
                if settings.shift_day_start_hour <= dt.hour < settings.shift_night_start_hour
            ]
            if not dates:
                dates = [dt.date() for dt in local_times]
            business_date = Counter(dates).most_common(1)[0][0]
        else:
            evening_dates = [
                dt.date() for dt in local_times
                if dt.hour >= settings.shift_night_start_hour
            ]
            if evening_dates:
                business_date = Counter(evening_dates).most_common(1)[0][0]
            else:
                business_date = Counter(dt.date() for dt in local_times).most_common(1)[0][0] - timedelta(days=1)

        return business_date, shift_type, dominant_share

    def _signed_amount(self, event: SaleEvent) -> Decimal:
        # Business rule: returns remain factual events/checks, but they
        # neither enter revenue nor reduce revenue.
        if event.is_return:
            return Decimal("0")
        return abs(event.amount)

    def _build_cash_shift(
        self,
        events: list[SaleEvent],
        source: str,
        key: str,
    ) -> CashShift:
        events = sorted(
            events,
            key=lambda event: event.when,
        )

        day_count = sum(
            1
            for event in events
            if event.check_shift_type == "DAY"
        )
        night_count = len(events) - day_count

        shift_type = (
            "DAY"
            if day_count >= night_count
            else "NIGHT"
        )

        dominant = max(day_count, night_count)
        dominant_share = dominant / len(events)

        started_at = events[0].when
        ended_at = events[-1].when

        duration_hours = max(
            0.0,
            (ended_at - started_at).total_seconds() / 3600,
        )

        revenue = sum(
            (
                self._signed_amount(event)
                for event in events
            ),
            Decimal("0"),
        )

        native_ids = {
            event.saby_shift_id
            for event in events
            if event.saby_shift_id is not None
        }

        native_numbers = {
            event.saby_shift_number
            for event in events
            if event.saby_shift_number
        }

        saby_shift_id = (
            next(iter(native_ids))
            if len(native_ids) == 1
            else None
        )

        saby_shift_number = (
            next(iter(native_numbers))
            if len(native_numbers) == 1
            else ""
        )

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

        return CashShift(
            key=key,

            point_id=first.point_id,
            business_date=first.business_date,
            shift_type=shift_type,

            seller_key=first.seller_key,
            seller_id=first.seller_id,
            seller_name=first.display_name,

            started_at=started_at,
            ended_at=ended_at,

            check_count=len(events),
            net_revenue=revenue,

            source=source,
            confidence=confidence,
            status=status,

            saby_shift_id=saby_shift_id,
            saby_shift_number=saby_shift_number,

            duration_hours=duration_hours,
            dominant_share=dominant_share,
        )

    def _native_cash_shifts(
        self,
        events: list[SaleEvent],
    ) -> list[CashShift]:
        # IMPORTANT: group a native Saby shift across clock/business-day
        # boundaries first. Only then assign operational date/type.
        native_groups: dict[tuple[int, str], list[SaleEvent]] = defaultdict(list)

        for event in events:
            native_key = event.native_shift_key
            if not native_key:
                continue
            native_groups[(event.point_id, native_key)].append(event)

        result: list[CashShift] = []

        for (point_id, native_key), native_events in native_groups.items():
            native_events.sort(key=lambda event: event.when)
            business_date, shift_type, dominant_share = self._native_anchor(native_events)

            by_seller: dict[str, list[SaleEvent]] = defaultdict(list)
            for event in native_events:
                by_seller[event.seller_key].append(event)

            for seller_key, group in by_seller.items():
                group.sort(key=lambda event: event.when)
                first = group[0]
                started_at = group[0].when
                ended_at = group[-1].when
                duration_hours = max(0.0, (ended_at - started_at).total_seconds() / 3600)
                revenue = sum((self._signed_amount(event) for event in group), Decimal("0"))

                native_ids = {e.saby_shift_id for e in group if e.saby_shift_id is not None}
                native_numbers = {e.saby_shift_number for e in group if e.saby_shift_number}
                saby_shift_id = next(iter(native_ids)) if len(native_ids) == 1 else None
                saby_shift_number = next(iter(native_numbers)) if len(native_numbers) == 1 else ""

                status = "AUTO"
                confidence = 1.0
                if duration_hours > settings.shift_max_duration_hours:
                    status = "REVIEW"
                    confidence = 0.95

                key = (
                    f"native:{point_id}:{business_date.isoformat()}:"
                    f"{shift_type}:{native_key}:{seller_key}"
                )

                result.append(
                    CashShift(
                        key=key,
                        point_id=point_id,
                        business_date=business_date,
                        shift_type=shift_type,
                        seller_key=seller_key,
                        seller_id=first.seller_id,
                        seller_name=first.display_name,
                        started_at=started_at,
                        ended_at=ended_at,
                        check_count=len(group),
                        net_revenue=revenue,
                        source="saby_native",
                        confidence=confidence,
                        status=status,
                        saby_shift_id=saby_shift_id,
                        saby_shift_number=saby_shift_number,
                        duration_hours=duration_hours,
                        dominant_share=dominant_share,
                    )
                )

        return result

    def _fallback_cash_shifts(
        self,
        events: list[SaleEvent],
    ) -> list[CashShift]:
        groups: dict[
            tuple[int, date, str],
            list[SaleEvent],
        ] = defaultdict(list)

        for event in events:
            if event.native_shift_key:
                continue

            groups[
                (
                    event.point_id,
                    event.business_date,
                    event.seller_key,
                )
            ].append(event)

        gap = timedelta(
            hours=settings.shift_session_gap_hours
        )

        result: list[CashShift] = []

        for (
            point_id,
            business_date,
            seller_key,
        ), seller_events in groups.items():

            seller_events.sort(
                key=lambda event: event.when
            )

            session: list[SaleEvent] = []
            session_index = 0

            for event in seller_events:
                if (
                    session
                    and event.when - session[-1].when > gap
                ):
                    key = (
                        f"fallback:{point_id}:"
                        f"{business_date.isoformat()}:"
                        f"{seller_key}:{session_index}"
                    )

                    result.append(
                        self._build_cash_shift(
                            session,
                            "fallback_reconstructed",
                            key,
                        )
                    )

                    session_index += 1
                    session = []

                session.append(event)

            if session:
                key = (
                    f"fallback:{point_id}:"
                    f"{business_date.isoformat()}:"
                    f"{seller_key}:{session_index}"
                )

                result.append(
                    self._build_cash_shift(
                        session,
                        "fallback_reconstructed",
                        key,
                    )
                )

        return result

    def _consolidate_work_shifts(
        self,
        cash_shifts: list[CashShift],
    ) -> list[WorkShift]:
        groups: dict[
            tuple[int, date, str, str],
            list[CashShift],
        ] = defaultdict(list)

        for shift in cash_shifts:
            groups[
                (
                    shift.point_id,
                    shift.business_date,
                    shift.shift_type,
                    shift.seller_key,
                )
            ].append(shift)

        merge_gap = timedelta(
            hours=settings.work_shift_merge_gap_hours
        )

        result: list[WorkShift] = []

        for group in groups.values():
            group.sort(
                key=lambda shift: shift.started_at
            )

            current: list[CashShift] = []

            def flush() -> None:
                if not current:
                    return

                started_at = current[0].started_at
                ended_at = max(
                    item.ended_at
                    for item in current
                )

                duration_hours = max(
                    0.0,
                    (
                        ended_at - started_at
                    ).total_seconds() / 3600,
                )

                sources = {
                    item.source
                    for item in current
                }

                if sources == {"saby_native"}:
                    source = "saby_native"
                elif sources == {"fallback_reconstructed"}:
                    source = "fallback_reconstructed"
                else:
                    source = "mixed"

                statuses = {
                    item.status
                    for item in current
                }

                if (
                    "AMBIGUOUS" in statuses
                    or duration_hours > settings.shift_max_duration_hours
                ):
                    status = "REVIEW"
                elif "REVIEW" in statuses:
                    status = "REVIEW"
                else:
                    status = "AUTO"

                confidence = min(
                    item.confidence
                    for item in current
                )

                first = current[0]

                result.append(
                    WorkShift(
                        point_id=first.point_id,
                        business_date=first.business_date,
                        shift_type=first.shift_type,

                        seller_key=first.seller_key,
                        seller_id=first.seller_id,
                        seller_name=first.seller_name,

                        started_at=started_at,
                        ended_at=ended_at,

                        check_count=sum(
                            item.check_count
                            for item in current
                        ),

                        net_revenue=sum(
                            (
                                item.net_revenue
                                for item in current
                            ),
                            Decimal("0"),
                        ),

                        cash_shift_count=len(current),
                        cash_shift_keys=[
                            item.key
                            for item in current
                        ],

                        source=source,
                        confidence=confidence,
                        status=status,

                        duration_hours=duration_hours,
                    )
                )

            for cash_shift in group:
                if not current:
                    current = [cash_shift]
                    continue

                last = current[-1]

                # Consolidate a Saby cash close/reopen into one paid shift
                # when the same employee continues the same DAY/NIGHT block.
                if cash_shift.started_at - last.ended_at <= merge_gap:
                    current.append(cash_shift)
                else:
                    flush()
                    current = [cash_shift]

            flush()

        result.sort(
            key=lambda shift: (
                shift.business_date,
                shift.point_id,
                shift.shift_type,
                shift.started_at,
                shift.seller_name,
            )
        )

        return result


    async def rebuild_range(
        self,
        date_from: date,
        date_to: date,
    ) -> int:
        """
        Builds shifts from payment/check facts, not sale totals.

        One Sale can contain multiple Payments. Each payment has its own
        CarriedWTZ / Amount / Shift / Teller and must be attributed
        independently.
        """
        tz = self._tz()
        start_dt = datetime.combine(
            date_from - timedelta(days=1),
            datetime.min.time(),
            tzinfo=tz,
        )
        end_dt = datetime.combine(
            date_to + timedelta(days=2),
            datetime.min.time(),
            tzinfo=tz,
        )

        async with pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    p.point_id,
                    p.sale_id,
                    p.business_date,
                    p.business_shift_type,
                    p.carried_at,
                    p.seller_id,
                    p.seller_name,
                    p.signed_amount,
                    p.saby_shift_id,
                    p.saby_shift_number,
                    p.payment_key
                FROM sale_payments p
                JOIN sales s
                  ON s.point_id=p.point_id
                 AND s.sale_id=p.sale_id
                WHERE s.deleted=FALSE
                  AND p.carried_at >= $1
                  AND p.carried_at < $2
                  AND p.carried_at IS NOT NULL
                  AND p.business_shift_type IN ('DAY', 'NIGHT')
                  AND (p.seller_id IS NOT NULL OR p.seller_name <> '')
                ORDER BY p.point_id, p.saby_shift_id NULLS LAST, p.carried_at, p.payment_key
                """,
                start_dt,
                end_dt,
            )

        events: list[SaleEvent] = []

        for row in rows:
            signed_amount = Decimal(
                row["signed_amount"] or 0
            )

            events.append(
                SaleEvent(
                    point_id=row["point_id"],
                    sale_id=row["sale_id"],
                    business_date=row["business_date"],
                    check_shift_type=row["business_shift_type"],
                    when=row["carried_at"],

                    seller_id=row["seller_id"],
                    seller_name=row["seller_name"] or "",

                    # Preserve the sign from payment ledger.  The common
                    # helper applies abs(), so mark negative ledger rows as
                    # returns to keep them negative inside shift revenue.
                    amount=signed_amount,
                    is_return=(signed_amount < 0),

                    saby_shift_id=row["saby_shift_id"],
                    saby_shift_number=(
                        row["saby_shift_number"] or ""
                    ),
                )
            )

        cash_shifts = (
            self._native_cash_shifts(events)
            +
            self._fallback_cash_shifts(events)
        )

        # Only now, after anchoring whole native shifts, select requested dates.
        cash_shifts = [
            shift for shift in cash_shifts
            if date_from <= shift.business_date <= date_to
        ]

        cash_shifts.sort(
            key=lambda item: (
                item.business_date,
                item.point_id,
                item.shift_type,
                item.started_at,
            )
        )

        work_shifts = self._consolidate_work_shifts(
            cash_shifts
        )

        async with pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    DELETE FROM employee_work_shifts
                    WHERE business_date BETWEEN $1 AND $2
                    """,
                    date_from,
                    date_to,
                )

                await conn.execute(
                    """
                    DELETE FROM cash_shifts
                    WHERE business_date BETWEEN $1 AND $2
                    """,
                    date_from,
                    date_to,
                )

                if cash_shifts:
                    await conn.executemany(
                        """
                        INSERT INTO cash_shifts(
                            cash_shift_key,

                            point_id,
                            business_date,
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
                            $1,
                            $2,$3,$4,
                            $5,$6,$7,
                            $8,$9,
                            $10,$11,
                            $12,$13,$14,
                            $15,$16,
                            $17,$18,
                            NOW()
                        )
                        """,
                        [
                            (
                                item.key,

                                item.point_id,
                                item.business_date,
                                item.shift_type,

                                item.seller_key,
                                item.seller_id,
                                item.seller_name,

                                item.started_at,
                                item.ended_at,

                                item.check_count,
                                item.net_revenue,

                                item.source,
                                item.confidence,
                                item.status,

                                item.saby_shift_id,
                                item.saby_shift_number,

                                item.duration_hours,
                                item.dominant_share,
                            )
                            for item in cash_shifts
                        ],
                    )

                if work_shifts:
                    await conn.executemany(
                        """
                        INSERT INTO employee_work_shifts(
                            point_id,
                            business_date,
                            shift_type,

                            seller_key,
                            seller_id,
                            seller_name,

                            started_at,
                            ended_at,

                            check_count,
                            net_revenue,

                            cash_shift_count,
                            cash_shift_keys,

                            source,
                            confidence,
                            status,

                            duration_hours,

                            updated_at
                        )
                        VALUES(
                            $1,$2,$3,
                            $4,$5,$6,
                            $7,$8,
                            $9,$10,
                            $11,$12::jsonb,
                            $13,$14,$15,
                            $16,
                            NOW()
                        )
                        """,
                        [
                            (
                                item.point_id,
                                item.business_date,
                                item.shift_type,

                                item.seller_key,
                                item.seller_id,
                                item.seller_name,

                                item.started_at,
                                item.ended_at,

                                item.check_count,
                                item.net_revenue,

                                item.cash_shift_count,
                                json.dumps(
                                    item.cash_shift_keys,
                                    ensure_ascii=False,
                                ),

                                item.source,
                                item.confidence,
                                item.status,

                                item.duration_hours,
                            )
                            for item in work_shifts
                        ],
                    )

                await conn.execute(
                    """
                    DELETE FROM seller_shifts
                    WHERE work_date BETWEEN $1 AND $2
                    """,
                    date_from,
                    date_to,
                )

        return len(work_shifts)


shift_engine = ShiftEngine()
