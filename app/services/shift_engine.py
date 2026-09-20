from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

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
    def _signed_amount(self, event: SaleEvent) -> Decimal:
        value = abs(event.amount)
        return -value if event.is_return else value

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
        groups: dict[
            tuple[int, date, str, str],
            list[SaleEvent],
        ] = defaultdict(list)

        for event in events:
            native_key = event.native_shift_key

            if not native_key:
                continue

            # business_date is part of the key intentionally:
            # one Saby cash shift must never bridge two Причал business days.
            group_key = (
                event.point_id,
                event.business_date,
                native_key,
                event.seller_key,
            )

            groups[group_key].append(event)

        result: list[CashShift] = []

        for (
            point_id,
            business_date,
            native_key,
            seller_key,
        ), group in groups.items():

            key = (
                f"native:{point_id}:"
                f"{business_date.isoformat()}:"
                f"{native_key}:{seller_key}"
            )

            result.append(
                self._build_cash_shift(
                    group,
                    "saby_native",
                    key,
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
        async with pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    point_id,
                    sale_id,
                    business_date,
                    business_shift_type,
                    sale_datetime,

                    seller_id,
                    seller_name,

                    total_price,
                    is_return,

                    saby_shift_id,
                    saby_shift_number

                FROM sales

                WHERE deleted=FALSE

                  AND business_date
                      BETWEEN $1 AND $2

                  AND sale_datetime IS NOT NULL

                  AND business_shift_type
                      IN ('DAY', 'NIGHT')

                  AND (
                      seller_id IS NOT NULL
                      OR seller_name <> ''
                  )

                ORDER BY
                    business_date,
                    point_id,
                    sale_datetime
                """,
                date_from,
                date_to,
            )

        events: list[SaleEvent] = []

        for row in rows:
            events.append(
                SaleEvent(
                    point_id=row["point_id"],
                    sale_id=row["sale_id"],
                    business_date=row["business_date"],
                    check_shift_type=row["business_shift_type"],
                    when=row["sale_datetime"],

                    seller_id=row["seller_id"],
                    seller_name=row["seller_name"] or "",

                    amount=Decimal(
                        row["total_price"] or 0
                    ),
                    is_return=bool(
                        row["is_return"]
                    ),

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

        cash_shifts.sort(
            key=lambda shift: (
                shift.business_date,
                shift.point_id,
                shift.shift_type,
                shift.started_at,
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
                                shift.key,

                                shift.point_id,
                                shift.business_date,
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
                            for shift in cash_shifts
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
                                shift.point_id,
                                shift.business_date,
                                shift.shift_type,

                                shift.seller_key,
                                shift.seller_id,
                                shift.seller_name,

                                shift.started_at,
                                shift.ended_at,

                                shift.check_count,
                                shift.net_revenue,

                                shift.cash_shift_count,
                                json.dumps(
                                    shift.cash_shift_keys,
                                    ensure_ascii=False,
                                ),

                                shift.source,
                                shift.confidence,
                                shift.status,

                                shift.duration_hours,
                            )
                            for shift in work_shifts
                        ],
                    )

                # Legacy table should no longer be used.
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
