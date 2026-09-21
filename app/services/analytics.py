from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db.database import pool
from app.services.saby import saby_client


settings = get_settings()


def _money(value) -> float:
    return round(float(value or 0), 2)


class AnalyticsService:
    def resolve_date(
        self,
        value: str = "вчера",
    ) -> date:
        return date.fromisoformat(
            saby_client.resolve_date(value)
        )

    async def coverage(self) -> dict:
        async with pool().acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    MIN(business_date) AS min_date,
                    MAX(business_date) AS max_date,
                    COUNT(*) AS sales,
                    COUNT(DISTINCT point_id) AS stores
                FROM sales
                WHERE deleted=FALSE
                  AND business_date IS NOT NULL
                """
            )

            last_sync = await conn.fetchrow(
                """
                SELECT
                    id,
                    started_at,
                    finished_at,
                    date_from,
                    date_to,
                    status,
                    stores_count,
                    sales_upserted,
                    items_upserted,
                    shifts_built,
                    error_text
                FROM sync_runs
                ORDER BY id DESC
                LIMIT 1
                """
            )

        return {
            "min_date": (
                row["min_date"].isoformat()
                if row["min_date"]
                else None
            ),
            "max_date": (
                row["max_date"].isoformat()
                if row["max_date"]
                else None
            ),
            "sales": row["sales"],
            "stores": row["stores"],
            "business_day": (
                f"{settings.business_day_start_hour:02d}:00"
                " → next day "
                f"{settings.business_day_start_hour:02d}:00"
            ),
            "last_sync": (
                dict(last_sync)
                if last_sync
                else None
            ),
        }

    async def network_day(
        self,
        date_value: str = "вчера",
    ) -> dict:
        day = self.resolve_date(date_value)

        async with pool().acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE p.is_return=FALSE
                    ) AS sales_checks,

                    COUNT(*) FILTER (
                        WHERE p.is_return=TRUE
                    ) AS return_checks,

                    COUNT(DISTINCT p.point_id) AS stores,

                    COALESCE(
                        SUM(p.signed_amount),
                        0
                    ) AS net_revenue,

                    COALESCE(
                        SUM(s.total_discount),
                        0
                    ) AS discounts,

                    COUNT(
                        DISTINCT NULLIF(
                            p.seller_name,
                            ''
                        )
                    ) AS sellers

                FROM sale_payments p

                JOIN sales s
                  ON s.point_id=p.point_id
                 AND s.sale_id=p.sale_id

                WHERE s.deleted=FALSE
                  AND p.business_date=$1
                """,
                day,
            )

            items = await conn.fetchrow(
                """
                SELECT
                    COALESCE(
                        SUM(
                            CASE
                                WHEN i.is_return
                                THEN -ABS(i.total_cost)
                                ELSE i.total_cost
                            END
                        ),
                        0
                    ) AS cost

                FROM sale_items i

                JOIN sales s
                  ON s.point_id=i.point_id
                 AND s.sale_id=i.sale_id

                WHERE s.deleted=FALSE
                  AND s.business_date=$1
                  AND i.refused=FALSE
                """,
                day,
            )

        checks = int(row["sales_checks"] or 0)

        revenue = Decimal(
            row["net_revenue"] or 0
        )

        cost = Decimal(
            items["cost"] or 0
        )

        avg_check = (
            revenue / checks
            if checks
            else Decimal("0")
        )

        profit = revenue - cost

        margin = (
            profit / revenue * 100
            if revenue > 0
            else Decimal("0")
        )

        return {
            "business_date": day.isoformat(),
            "window": (
                f"{day.isoformat()} "
                f"{settings.business_day_start_hour:02d}:00"
                " → next day "
                f"{settings.business_day_start_hour:02d}:00"
            ),
            "stores": row["stores"],
            "sales_checks": checks,
            "return_checks": row["return_checks"],
            "net_revenue": _money(revenue),
            "average_check": _money(avg_check),
            "discounts": _money(
                row["discounts"]
            ),
            "actual_cost": _money(cost),
            "gross_profit": _money(profit),
            "gross_margin_percent": round(
                float(margin),
                2,
            ),
            "unique_sellers": row["sellers"],
            "source": "postgresql_business_day",
        }

    async def _resolve_store(
        self,
        query: str,
    ):
        q = query.strip()

        async with pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    point_id,
                    name,
                    address,
                    locality

                FROM stores

                WHERE CAST(point_id AS TEXT)=$1
                   OR LOWER(name) LIKE LOWER($2)
                   OR LOWER(address) LIKE LOWER($2)

                ORDER BY name
                LIMIT 20
                """,
                q,
                f"%{q}%",
            )

        if not rows:
            return None, {
                "error": "Магазин не найден.",
                "query": query,
            }

        if len(rows) > 1:
            return None, {
                "error": (
                    "Название магазина неоднозначно."
                ),
                "matches": [
                    dict(row)
                    for row in rows
                ],
            }

        return rows[0], None

    async def store_day(
        self,
        store_query: str,
        date_value: str = "вчера",
    ) -> dict:
        day = self.resolve_date(date_value)

        store, error = await self._resolve_store(
            store_query
        )

        if error:
            return error

        async with pool().acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE p.is_return=FALSE
                    ) AS sales_checks,

                    COUNT(*) FILTER (
                        WHERE p.is_return=TRUE
                    ) AS return_checks,

                    COALESCE(
                        SUM(p.signed_amount),
                        0
                    ) AS net_revenue,

                    COUNT(
                        DISTINCT NULLIF(
                            p.seller_name,
                            ''
                        )
                    ) AS sellers

                FROM sale_payments p

                JOIN sales s
                  ON s.point_id=p.point_id
                 AND s.sale_id=p.sale_id

                WHERE p.point_id=$1
                  AND s.deleted=FALSE
                  AND p.business_date=$2
                """,
                store["point_id"],
                day,
            )

            shift_rows = await conn.fetch(
                """
                SELECT
                    shift_type,
                    seller_name,

                    started_at,
                    ended_at,

                    check_count,
                    net_revenue,

                    cash_shift_count,

                    source,
                    confidence,
                    status,
                    duration_hours

                FROM employee_work_shifts

                WHERE point_id=$1
                  AND business_date=$2

                ORDER BY
                    shift_type,
                    started_at
                """,
                store["point_id"],
                day,
            )

        checks = int(
            row["sales_checks"] or 0
        )

        revenue = Decimal(
            row["net_revenue"] or 0
        )

        return {
            "business_date": day.isoformat(),
            "point_id": store["point_id"],
            "store": store["name"],
            "address": store["address"],

            "sales_checks": checks,
            "return_checks": row["return_checks"],

            "net_revenue": _money(
                revenue
            ),

            "average_check": _money(
                revenue / checks
                if checks
                else 0
            ),

            "unique_sellers": row["sellers"],

            "employee_work_shifts": [
                {
                    **dict(shift),
                    "started_at": (
                        shift["started_at"].isoformat()
                    ),
                    "ended_at": (
                        shift["ended_at"].isoformat()
                    ),
                    "net_revenue": _money(
                        shift["net_revenue"]
                    ),
                    "confidence": float(
                        shift["confidence"]
                    ),
                    "duration_hours": float(
                        shift["duration_hours"]
                    ),
                }
                for shift in shift_rows
            ],

            "source": "postgresql_business_day",
        }

    async def top_products(
        self,
        date_value: str = "вчера",
        limit: int = 10,
        store_query: str = "",
    ) -> dict:
        day = self.resolve_date(date_value)

        limit = max(
            1,
            min(
                int(limit),
                30,
            ),
        )

        point_id = None
        store_name = "вся сеть"

        if store_query.strip():
            store, error = await self._resolve_store(
                store_query
            )

            if error:
                return error

            point_id = store["point_id"]
            store_name = store["name"]

        async with pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    COALESCE(
                        NULLIF(i.product_uuid,''),
                        CAST(i.product_id AS TEXT),
                        i.name
                    ) AS product_key,

                    MAX(
                        COALESCE(
                            NULLIF(i.name,''),
                            NULLIF(i.short_name,''),
                            i.product_uuid
                        )
                    ) AS name,

                    COALESCE(
                        SUM(
                            CASE
                                WHEN i.is_return
                                THEN -ABS(i.quantity)
                                ELSE i.quantity
                            END
                        ),
                        0
                    ) AS quantity,

                    COALESCE(
                        SUM(
                            CASE
                                WHEN i.is_return
                                THEN -ABS(i.total_price)
                                ELSE i.total_price
                            END
                        ),
                        0
                    ) AS revenue,

                    COALESCE(
                        SUM(
                            CASE
                                WHEN i.is_return
                                THEN -ABS(i.total_cost)
                                ELSE i.total_cost
                            END
                        ),
                        0
                    ) AS cost

                FROM sale_items i

                JOIN sales s
                  ON s.point_id=i.point_id
                 AND s.sale_id=i.sale_id

                WHERE s.deleted=FALSE
                  AND i.refused=FALSE
                  AND s.business_date=$1

                  AND (
                      $2::BIGINT IS NULL
                      OR s.point_id=$2
                  )

                GROUP BY product_key

                ORDER BY revenue DESC

                LIMIT $3
                """,
                day,
                point_id,
                limit,
            )

        result = []

        for row in rows:
            revenue = Decimal(
                row["revenue"] or 0
            )

            cost = Decimal(
                row["cost"] or 0
            )

            result.append(
                {
                    "product_key": row["product_key"],
                    "name": row["name"],

                    "quantity": round(
                        float(
                            row["quantity"] or 0
                        ),
                        3,
                    ),

                    "net_revenue": _money(
                        revenue
                    ),

                    "actual_cost": _money(
                        cost
                    ),

                    "gross_profit": _money(
                        revenue - cost
                    ),
                }
            )

        return {
            "business_date": day.isoformat(),
            "scope": store_name,
            "top_products": result,
            "source": "postgresql_business_day",
        }

    async def shift_summary(
        self,
        date_value: str = "вчера",
    ) -> dict:
        day = self.resolve_date(date_value)

        async with pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    ws.point_id,
                    st.name AS store,

                    ws.shift_type,
                    ws.seller_name,

                    ws.started_at,
                    ws.ended_at,

                    ws.check_count,
                    ws.net_revenue,

                    ws.cash_shift_count,

                    ws.source,
                    ws.confidence,
                    ws.status,
                    ws.duration_hours

                FROM employee_work_shifts ws

                JOIN stores st
                  ON st.point_id=ws.point_id

                WHERE ws.business_date=$1

                ORDER BY
                    st.name,
                    ws.shift_type,
                    ws.started_at
                """,
                day,
            )

        return {
            "business_date": day.isoformat(),

            "total": len(rows),

            "auto": sum(
                1
                for row in rows
                if row["status"] == "AUTO"
            ),

            "review": sum(
                1
                for row in rows
                if row["status"] == "REVIEW"
            ),

            "ambiguous": sum(
                1
                for row in rows
                if row["status"] == "AMBIGUOUS"
            ),

            "saby_native": sum(
                1
                for row in rows
                if row["source"] == "saby_native"
            ),

            "fallback": sum(
                1
                for row in rows
                if row["source"]
                == "fallback_reconstructed"
            ),

            "mixed": sum(
                1
                for row in rows
                if row["source"] == "mixed"
            ),

            "shifts": [
                {
                    **dict(row),

                    "started_at": (
                        row["started_at"].isoformat()
                    ),

                    "ended_at": (
                        row["ended_at"].isoformat()
                    ),

                    "net_revenue": _money(
                        row["net_revenue"]
                    ),

                    "confidence": float(
                        row["confidence"]
                    ),

                    "duration_hours": float(
                        row["duration_hours"]
                    ),
                }
                for row in rows
            ],
        }

    async def seller_shifts(
        self,
        seller_query: str,
        date_from: str = "",
        date_to: str = "",
    ) -> dict:
        today = datetime.now(
            ZoneInfo(settings.business_tz)
        ).date()

        start = (
            self.resolve_date(date_from)
            if date_from.strip()
            else today.replace(day=1)
        )

        end = (
            self.resolve_date(date_to)
            if date_to.strip()
            else today
        )

        async with pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    ws.business_date,
                    ws.point_id,
                    st.name AS store,

                    ws.shift_type,
                    ws.seller_name,

                    ws.started_at,
                    ws.ended_at,

                    ws.check_count,
                    ws.net_revenue,

                    ws.cash_shift_count,

                    ws.source,
                    ws.confidence,
                    ws.status

                FROM employee_work_shifts ws

                JOIN stores st
                  ON st.point_id=ws.point_id

                WHERE LOWER(ws.seller_name)
                      LIKE LOWER($1)

                  AND ws.business_date
                      BETWEEN $2 AND $3

                ORDER BY
                    ws.business_date,
                    ws.started_at
                """,
                f"%{seller_query.strip()}%",
                start,
                end,
            )

        sellers = sorted(
            {
                row["seller_name"]
                for row in rows
            }
        )

        if len(sellers) > 1:
            return {
                "error": "Имя продавца неоднозначно.",
                "matches": sellers,
            }

        return {
            "seller": (
                sellers[0]
                if sellers
                else seller_query
            ),

            "date_from": start.isoformat(),
            "date_to": end.isoformat(),

            "shift_count": len(rows),

            "total_revenue": _money(
                sum(
                    (
                        Decimal(
                            row["net_revenue"] or 0
                        )
                        for row in rows
                    ),
                    Decimal("0"),
                )
            ),

            "shifts": [
                {
                    **dict(row),

                    "business_date": (
                        row["business_date"].isoformat()
                    ),

                    "started_at": (
                        row["started_at"].isoformat()
                    ),

                    "ended_at": (
                        row["ended_at"].isoformat()
                    ),

                    "net_revenue": _money(
                        row["net_revenue"]
                    ),

                    "confidence": float(
                        row["confidence"]
                    ),
                }
                for row in rows
            ],
        }


    async def reconcile(
        self,
        date_value: str = "вчера",
    ) -> dict:
        day = self.resolve_date(date_value)

        async with pool().acquire() as conn:
            rows = await conn.fetch(
                """
                WITH sale_totals AS (
                    SELECT
                        point_id,

                        COUNT(*) AS sale_count,

                        COALESCE(
                            SUM(
                                CASE
                                    WHEN is_return
                                    THEN -ABS(total_price)
                                    ELSE total_price
                                END
                            ),
                            0
                        ) AS sale_total

                    FROM sales

                    WHERE deleted=FALSE
                      AND business_date=$1

                    GROUP BY point_id
                ),

                payments AS (
                    SELECT
                        p.point_id,

                        COUNT(*) AS payment_checks,

                        COUNT(*) FILTER (
                            WHERE p.source='sale_total_fallback'
                        ) AS fallback_payments,

                        COUNT(*) FILTER (
                            WHERE p.seller_id IS NULL
                              AND p.seller_name=''
                        ) AS no_seller_checks,

                        COALESCE(
                            SUM(p.signed_amount),
                            0
                        ) AS payment_revenue

                    FROM sale_payments p

                    JOIN sales s
                      ON s.point_id=p.point_id
                     AND s.sale_id=p.sale_id

                    WHERE s.deleted=FALSE
                      AND p.business_date=$1

                    GROUP BY p.point_id
                ),

                work AS (
                    SELECT
                        point_id,

                        COALESCE(
                            SUM(check_count),
                            0
                        ) AS work_checks,

                        COALESCE(
                            SUM(net_revenue),
                            0
                        ) AS work_revenue,

                        COUNT(*) AS work_shifts,

                        COUNT(*) FILTER (
                            WHERE status <> 'AUTO'
                        ) AS review_shifts

                    FROM employee_work_shifts

                    WHERE business_date=$1

                    GROUP BY point_id
                )

                SELECT
                    st.point_id,
                    st.name AS store,

                    COALESCE(sale_totals.sale_count,0)
                        AS sale_count,

                    COALESCE(sale_totals.sale_total,0)
                        AS sale_total,

                    COALESCE(payments.payment_checks,0)
                        AS payment_checks,

                    COALESCE(payments.payment_revenue,0)
                        AS payment_revenue,

                    COALESCE(payments.fallback_payments,0)
                        AS fallback_payments,

                    COALESCE(payments.no_seller_checks,0)
                        AS no_seller_checks,

                    COALESCE(work.work_checks,0)
                        AS work_checks,

                    COALESCE(work.work_revenue,0)
                        AS work_revenue,

                    COALESCE(work.work_shifts,0)
                        AS work_shifts,

                    COALESCE(work.review_shifts,0)
                        AS review_shifts

                FROM stores st

                LEFT JOIN sale_totals
                  ON sale_totals.point_id=st.point_id

                LEFT JOIN payments
                  ON payments.point_id=st.point_id

                LEFT JOIN work
                  ON work.point_id=st.point_id

                WHERE sale_totals.point_id IS NOT NULL
                   OR payments.point_id IS NOT NULL
                   OR work.point_id IS NOT NULL

                ORDER BY st.name
                """,
                day,
            )

        result = []

        for row in rows:
            sale_total = Decimal(
                row["sale_total"] or 0
            )
            payment_revenue = Decimal(
                row["payment_revenue"] or 0
            )
            work_revenue = Decimal(
                row["work_revenue"] or 0
            )

            sale_vs_payment = sale_total - payment_revenue
            payment_vs_work = payment_revenue - work_revenue

            payment_work_money_ok = (
                abs(payment_vs_work) < Decimal("0.01")
            )

            payment_work_checks_ok = (
                row["payment_checks"] == row["work_checks"]
            )

            work_ok = (
                payment_work_money_ok
                and payment_work_checks_ok
                and row["no_seller_checks"] == 0
                and row["review_shifts"] == 0
                and row["fallback_payments"] == 0
            )

            result.append(
                {
                    "point_id": row["point_id"],
                    "store": row["store"],

                    "sale_count": row["sale_count"],
                    "sale_total": _money(sale_total),

                    "payment_checks": row["payment_checks"],
                    "payment_revenue": _money(payment_revenue),

                    "sale_vs_payment": _money(sale_vs_payment),

                    "work_checks": row["work_checks"],
                    "work_revenue": _money(work_revenue),

                    "payment_vs_work": _money(payment_vs_work),

                    "fallback_payments": row["fallback_payments"],
                    "no_seller_checks": row["no_seller_checks"],

                    "work_shifts": row["work_shifts"],
                    "review_shifts": row["review_shifts"],

                    "status": (
                        "OK"
                        if work_ok
                        else "REVIEW"
                    ),
                }
            )

        return {
            "business_date": day.isoformat(),

            "window": (
                f"{day.isoformat()} "
                f"{settings.business_day_start_hour:02d}:00"
                " → next day "
                f"{settings.business_day_start_hour:02d}:00"
            ),

            "ok": all(
                row["status"] == "OK"
                for row in result
            ),

            "stores": result,
        }



    async def shift_diagnostics(
        self,
        date_value: str = "вчера",
    ) -> dict:
        day = self.resolve_date(date_value)

        async with pool().acquire() as conn:
            async with conn.transaction(
                isolation="repeatable_read",
                readonly=True,
            ):
                payments = await conn.fetchrow(
                    """
                    SELECT
                        COUNT(*) AS checks,

                        COUNT(*) FILTER (
                            WHERE p.business_shift_type='DAY'
                        ) AS day_checks,

                        COUNT(*) FILTER (
                            WHERE p.business_shift_type='NIGHT'
                        ) AS night_checks,

                        COUNT(*) FILTER (
                            WHERE p.seller_id IS NOT NULL
                        ) AS seller_id_checks,

                        COUNT(*) FILTER (
                            WHERE p.saby_shift_id IS NOT NULL
                        ) AS shift_id_checks,

                        COUNT(*) FILTER (
                            WHERE p.source='saby_payment'
                        ) AS saby_payments,

                        COUNT(*) FILTER (
                            WHERE p.source='sale_total_fallback'
                        ) AS fallbacks,

                        COALESCE(
                            SUM(p.signed_amount),
                            0
                        ) AS payment_revenue

                    FROM sale_payments p

                    JOIN sales s
                      ON s.point_id=p.point_id
                     AND s.sale_id=p.sale_id

                    WHERE s.deleted=FALSE
                      AND p.business_date=$1
                    """,
                    day,
                )

                cash = await conn.fetchrow(
                    """
                    SELECT
                        COUNT(*) AS total,

                        COUNT(*) FILTER (
                            WHERE source='saby_native'
                        ) AS native,

                        COUNT(*) FILTER (
                            WHERE source='fallback_reconstructed'
                        ) AS fallback,

                        COALESCE(
                            SUM(net_revenue),
                            0
                        ) AS revenue

                    FROM cash_shifts

                    WHERE business_date=$1
                    """,
                    day,
                )

                work = await conn.fetchrow(
                    """
                    SELECT
                        COUNT(*) AS total,

                        COUNT(*) FILTER (
                            WHERE shift_type='DAY'
                        ) AS day,

                        COUNT(*) FILTER (
                            WHERE shift_type='NIGHT'
                        ) AS night,

                        COUNT(*) FILTER (
                            WHERE status='AUTO'
                        ) AS auto,

                        COUNT(*) FILTER (
                            WHERE status='REVIEW'
                        ) AS review,

                        COALESCE(
                            SUM(cash_shift_count),
                            0
                        ) AS cash_segments,

                        COALESCE(
                            SUM(net_revenue),
                            0
                        ) AS revenue

                    FROM employee_work_shifts

                    WHERE business_date=$1
                    """,
                    day,
                )

        def money(value):
            return _money(value)

        return {
            "business_date": day.isoformat(),
            "timezone": settings.business_tz,
            "business_day_start_hour": (
                settings.business_day_start_hour
            ),

            "payments": {
                **dict(payments),
                "payment_revenue": money(
                    payments["payment_revenue"]
                ),
            },

            "cash_shifts": {
                **dict(cash),
                "revenue": money(cash["revenue"]),
            },

            "work_shifts": {
                **dict(work),
                "revenue": money(work["revenue"]),
            },
        }

    async def money_debug(
        self,
        store_query: str,
        date_value: str = "вчера",
    ) -> dict:
        day = self.resolve_date(date_value)

        store, error = await self._resolve_store(store_query)
        if error:
            return error

        start_local = datetime(
            day.year,
            day.month,
            day.day,
            settings.business_day_start_hour,
            0,
            0,
            tzinfo=ZoneInfo(settings.business_tz),
        )
        end_local = start_local + timedelta(days=1)

        async with pool().acquire() as conn:
            async with conn.transaction(
                isolation="repeatable_read",
                readonly=True,
            ):
                stored = await conn.fetchrow(
                    """
                    SELECT
                        COUNT(*) AS payment_rows,
                        COALESCE(SUM(p.signed_amount),0) AS signed_amount,
                        COALESCE(SUM(
                            CASE WHEN p.is_return
                                 THEN -ABS(p.amount)
                                 ELSE p.amount END
                        ),0) AS amount_signed_on_fly,
                        COALESCE(SUM(
                            CASE WHEN p.is_return
                                 THEN -ABS(
                                     p.cash_sum+p.bank_sum+
                                     p.certificate_sum+p.salary_sum
                                 )
                                 ELSE (
                                     p.cash_sum+p.bank_sum+
                                     p.certificate_sum+p.salary_sum
                                 )
                            END
                        ),0) AS tender_sum
                    FROM sale_payments p
                    JOIN sales s
                      ON s.point_id=p.point_id
                     AND s.sale_id=p.sale_id
                    WHERE s.deleted=FALSE
                      AND p.point_id=$1
                      AND p.business_date=$2
                    """,
                    store["point_id"],
                    day,
                )

                direct = await conn.fetchrow(
                    """
                    SELECT
                        COUNT(*) AS payment_rows,
                        COALESCE(SUM(p.signed_amount),0) AS signed_amount,
                        COALESCE(SUM(
                            CASE WHEN p.is_return
                                 THEN -ABS(p.amount)
                                 ELSE p.amount END
                        ),0) AS amount_signed_on_fly,
                        COALESCE(SUM(
                            CASE WHEN p.is_return
                                 THEN -ABS(
                                     p.cash_sum+p.bank_sum+
                                     p.certificate_sum+p.salary_sum
                                 )
                                 ELSE (
                                     p.cash_sum+p.bank_sum+
                                     p.certificate_sum+p.salary_sum
                                 )
                            END
                        ),0) AS tender_sum
                    FROM sale_payments p
                    JOIN sales s
                      ON s.point_id=p.point_id
                     AND s.sale_id=p.sale_id
                    WHERE s.deleted=FALSE
                      AND p.point_id=$1
                      AND p.carried_at >= $2
                      AND p.carried_at < $3
                    """,
                    store["point_id"],
                    start_local,
                    end_local,
                )

                hourly = await conn.fetch(
                    """
                    SELECT
                        EXTRACT(
                            HOUR FROM p.carried_at AT TIME ZONE $2
                        )::int AS local_hour,
                        COUNT(*) AS checks,
                        COALESCE(SUM(p.signed_amount),0) AS revenue,
                        COALESCE(SUM(
                            CASE WHEN p.is_return
                                 THEN -ABS(
                                     p.cash_sum+p.bank_sum+
                                     p.certificate_sum+p.salary_sum
                                 )
                                 ELSE (
                                     p.cash_sum+p.bank_sum+
                                     p.certificate_sum+p.salary_sum
                                 )
                            END
                        ),0) AS tender_sum
                    FROM sale_payments p
                    JOIN sales s
                      ON s.point_id=p.point_id
                     AND s.sale_id=p.sale_id
                    WHERE s.deleted=FALSE
                      AND p.point_id=$1
                      AND p.carried_at >= $3
                      AND p.carried_at < $4
                    GROUP BY local_hour
                    ORDER BY local_hour
                    """,
                    store["point_id"],
                    settings.business_tz,
                    start_local,
                    end_local,
                )

                shift_buckets = await conn.fetch(
                    """
                    SELECT
                        p.business_shift_type AS shift_type,
                        COUNT(*) AS checks,
                        COALESCE(SUM(p.signed_amount),0) AS revenue,
                        COALESCE(SUM(
                            CASE WHEN p.is_return
                                 THEN -ABS(
                                     p.cash_sum+p.bank_sum+
                                     p.certificate_sum+p.salary_sum
                                 )
                                 ELSE (
                                     p.cash_sum+p.bank_sum+
                                     p.certificate_sum+p.salary_sum
                                 )
                            END
                        ),0) AS tender_sum
                    FROM sale_payments p
                    JOIN sales s
                      ON s.point_id=p.point_id
                     AND s.sale_id=p.sale_id
                    WHERE s.deleted=FALSE
                      AND p.point_id=$1
                      AND p.carried_at >= $2
                      AND p.carried_at < $3
                    GROUP BY p.business_shift_type
                    ORDER BY p.business_shift_type
                    """,
                    store["point_id"],
                    start_local,
                    end_local,
                )

                samples = await conn.fetch(
                    """
                    SELECT
                        p.sale_id,
                        p.payment_key,
                        p.amount,
                        p.signed_amount,
                        p.cash_sum,
                        p.bank_sum,
                        p.certificate_sum,
                        p.salary_sum,
                        p.is_return,
                        p.business_date,
                        p.business_shift_type,
                        p.carried_at,
                        p.carried_at AT TIME ZONE $2 AS local_time,
                        p.raw_json->>'CarriedWTZ' AS raw_carried_wtz,
                        p.raw_json->>'Amount' AS raw_amount,
                        p.raw_json->>'CashSum' AS raw_cash_sum,
                        p.raw_json->>'BankSum' AS raw_bank_sum,
                        p.raw_json->>'PayCash' AS raw_pay_cash,
                        p.raw_json->>'PayBank' AS raw_pay_bank,
                        p.saby_shift_id,
                        p.saby_shift_number,
                        p.seller_name
                    FROM sale_payments p
                    JOIN sales s
                      ON s.point_id=p.point_id
                     AND s.sale_id=p.sale_id
                    WHERE s.deleted=FALSE
                      AND p.point_id=$1
                      AND p.carried_at >= ($3 - INTERVAL '2 hours')
                      AND p.carried_at < ($4 + INTERVAL '2 hours')
                    ORDER BY p.carried_at
                    LIMIT 40
                    """,
                    store["point_id"],
                    settings.business_tz,
                    start_local,
                    end_local,
                )

                sale_totals = await conn.fetchrow(
                    """
                    SELECT
                        COUNT(*) AS sales,
                        COALESCE(SUM(
                            CASE WHEN is_return
                                 THEN -ABS(total_price)
                                 ELSE total_price END
                        ),0) AS total_price
                    FROM sales
                    WHERE deleted=FALSE
                      AND point_id=$1
                      AND sale_datetime >= $2
                      AND sale_datetime < $3
                    """,
                    store["point_id"],
                    start_local,
                    end_local,
                )

        def money(value):
            return round(float(value or 0), 2)

        return {
            "business_date": day.isoformat(),
            "store": store["name"],
            "point_id": store["point_id"],
            "timezone": settings.business_tz,
            "window_start": start_local.isoformat(),
            "window_end": end_local.isoformat(),
            "stored_business_date": {
                "payment_rows": stored["payment_rows"],
                "signed_amount": money(stored["signed_amount"]),
                "amount_signed_on_fly": money(stored["amount_signed_on_fly"]),
                "tender_sum": money(stored["tender_sum"]),
            },
            "direct_timestamp_window": {
                "payment_rows": direct["payment_rows"],
                "signed_amount": money(direct["signed_amount"]),
                "amount_signed_on_fly": money(direct["amount_signed_on_fly"]),
                "tender_sum": money(direct["tender_sum"]),
            },
            "sale_totalprice_direct_window": {
                "sales": sale_totals["sales"],
                "total_price": money(sale_totals["total_price"]),
            },
            "shift_buckets": [
                {
                    "shift_type": row["shift_type"],
                    "checks": row["checks"],
                    "revenue": money(row["revenue"]),
                    "tender_sum": money(row["tender_sum"]),
                }
                for row in shift_buckets
            ],
            "hourly": [
                {
                    "hour": row["local_hour"],
                    "checks": row["checks"],
                    "revenue": money(row["revenue"]),
                    "tender_sum": money(row["tender_sum"]),
                }
                for row in hourly
            ],
            "samples": [
                {
                    **dict(row),
                    "amount": money(row["amount"]),
                    "signed_amount": money(row["signed_amount"]),
                    "cash_sum": money(row["cash_sum"]),
                    "bank_sum": money(row["bank_sum"]),
                    "certificate_sum": money(row["certificate_sum"]),
                    "salary_sum": money(row["salary_sum"]),
                    "business_date": row["business_date"].isoformat() if row["business_date"] else None,
                    "carried_at": row["carried_at"].isoformat() if row["carried_at"] else None,
                    "local_time": row["local_time"].isoformat() if row["local_time"] else None,
                }
                for row in samples
            ],
        }

    async def payment_debug(
        self,
        store_query: str,
        date_value: str = "вчера",
    ) -> dict:
        day = self.resolve_date(date_value)

        store, error = await self._resolve_store(store_query)
        if error:
            return error

        async with pool().acquire() as conn:
            async with conn.transaction(
                isolation="repeatable_read",
                readonly=True,
            ):
                grouped = await conn.fetch(
                    """
                    SELECT
                        p.seller_name,
                        p.business_shift_type,

                        COUNT(*) AS checks,

                        COALESCE(
                            SUM(p.signed_amount),
                            0
                        ) AS revenue_all,

                        COUNT(*) FILTER (
                            WHERE p.nonfiscal=TRUE
                        ) AS nonfiscal_checks,

                        COALESCE(
                            SUM(p.signed_amount) FILTER (
                                WHERE p.nonfiscal=TRUE
                            ),
                            0
                        ) AS nonfiscal_revenue,

                        COUNT(*) FILTER (
                            WHERE p.is_return=TRUE
                        ) AS return_checks,

                        COALESCE(
                            SUM(p.signed_amount) FILTER (
                                WHERE p.is_return=TRUE
                            ),
                            0
                        ) AS return_revenue,

                        COALESCE(
                            SUM(p.signed_amount) FILTER (
                                WHERE p.nonfiscal=FALSE
                            ),
                            0
                        ) AS fiscal_revenue

                    FROM sale_payments p

                    JOIN sales s
                      ON s.point_id=p.point_id
                     AND s.sale_id=p.sale_id

                    WHERE s.deleted=FALSE
                      AND p.point_id=$1
                      AND p.business_date=$2

                    GROUP BY
                        p.seller_name,
                        p.business_shift_type

                    ORDER BY
                        p.business_shift_type,
                        p.seller_name
                    """,
                    store["point_id"],
                    day,
                )

                totals = await conn.fetchrow(
                    """
                    SELECT
                        COUNT(*) AS checks,

                        COALESCE(
                            SUM(p.signed_amount),
                            0
                        ) AS revenue_all,

                        COUNT(*) FILTER (
                            WHERE p.nonfiscal=TRUE
                        ) AS nonfiscal_checks,

                        COALESCE(
                            SUM(p.signed_amount) FILTER (
                                WHERE p.nonfiscal=TRUE
                            ),
                            0
                        ) AS nonfiscal_revenue,

                        COUNT(*) FILTER (
                            WHERE p.is_return=TRUE
                        ) AS return_checks,

                        COALESCE(
                            SUM(p.signed_amount) FILTER (
                                WHERE p.is_return=TRUE
                            ),
                            0
                        ) AS return_revenue,

                        COALESCE(
                            SUM(p.signed_amount) FILTER (
                                WHERE p.nonfiscal=FALSE
                            ),
                            0
                        ) AS fiscal_revenue

                    FROM sale_payments p

                    JOIN sales s
                      ON s.point_id=p.point_id
                     AND s.sale_id=p.sale_id

                    WHERE s.deleted=FALSE
                      AND p.point_id=$1
                      AND p.business_date=$2
                    """,
                    store["point_id"],
                    day,
                )

                suspicious = await conn.fetch(
                    """
                    SELECT
                        p.seller_name,
                        p.business_shift_type,

                        p.carried_at,
                        p.carried_at AT TIME ZONE $3 AS local_time,

                        p.check_number,
                        p.payment_key,

                        p.amount,
                        p.signed_amount,

                        p.nonfiscal,
                        p.is_return,

                        p.saby_shift_id,
                        p.saby_shift_number,

                        p.raw_json->>'Comment' AS comment,
                        p.raw_json->>'FiscalNumber' AS fiscal_number,
                        p.raw_json->>'FiscalSign' AS fiscal_sign,
                        p.raw_json->>'KKMName' AS kkm_name

                    FROM sale_payments p

                    JOIN sales s
                      ON s.point_id=p.point_id
                     AND s.sale_id=p.sale_id

                    WHERE s.deleted=FALSE
                      AND p.point_id=$1
                      AND p.business_date=$2

                      AND (
                          p.nonfiscal=TRUE
                          OR p.is_return=TRUE
                      )

                    ORDER BY
                        p.carried_at,
                        p.seller_name
                    """,
                    store["point_id"],
                    day,
                    settings.business_tz,
                )

                all_rows = await conn.fetch(
                    """
                    SELECT
                        p.seller_name,
                        p.business_shift_type,

                        p.carried_at,
                        p.carried_at AT TIME ZONE $3 AS local_time,

                        p.check_number,
                        p.payment_key,

                        p.amount,
                        p.signed_amount,

                        p.nonfiscal,
                        p.is_return,

                        p.saby_shift_id,
                        p.saby_shift_number

                    FROM sale_payments p

                    JOIN sales s
                      ON s.point_id=p.point_id
                     AND s.sale_id=p.sale_id

                    WHERE s.deleted=FALSE
                      AND p.point_id=$1
                      AND p.business_date=$2

                    ORDER BY
                        p.business_shift_type,
                        p.seller_name,
                        p.carried_at
                    """,
                    store["point_id"],
                    day,
                    settings.business_tz,
                )

        def money(value):
            return round(float(value or 0), 2)

        return {
            "business_date": day.isoformat(),
            "store": store["name"],
            "point_id": store["point_id"],
            "timezone": settings.business_tz,

            "totals": {
                **dict(totals),
                "revenue_all": money(totals["revenue_all"]),
                "nonfiscal_revenue": money(totals["nonfiscal_revenue"]),
                "return_revenue": money(totals["return_revenue"]),
                "fiscal_revenue": money(totals["fiscal_revenue"]),
            },

            "grouped": [
                {
                    **dict(row),
                    "revenue_all": money(row["revenue_all"]),
                    "nonfiscal_revenue": money(
                        row["nonfiscal_revenue"]
                    ),
                    "return_revenue": money(
                        row["return_revenue"]
                    ),
                    "fiscal_revenue": money(
                        row["fiscal_revenue"]
                    ),
                }
                for row in grouped
            ],

            "suspicious": [
                {
                    **dict(row),
                    "amount": money(row["amount"]),
                    "signed_amount": money(row["signed_amount"]),
                    "carried_at": (
                        row["carried_at"].isoformat()
                        if row["carried_at"]
                        else None
                    ),
                    "local_time": (
                        row["local_time"].isoformat()
                        if row["local_time"]
                        else None
                    ),
                }
                for row in suspicious
            ],

            "all_rows": [
                {
                    **dict(row),
                    "amount": money(row["amount"]),
                    "signed_amount": money(row["signed_amount"]),
                    "carried_at": (
                        row["carried_at"].isoformat()
                        if row["carried_at"]
                        else None
                    ),
                    "local_time": (
                        row["local_time"].isoformat()
                        if row["local_time"]
                        else None
                    ),
                }
                for row in all_rows
            ],
        }


analytics_service = AnalyticsService()
