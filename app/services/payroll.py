from __future__ import annotations

import json
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db.database import pool


settings = get_settings()


WEEKDAY_ALIASES = {
    "ALL": None,
    "ВСЕ": None,
    "*": None,

    "MON": 0,
    "ПН": 0,
    "MONDAY": 0,

    "TUE": 1,
    "ВТ": 1,
    "TUESDAY": 1,

    "WED": 2,
    "СР": 2,
    "WEDNESDAY": 2,

    "THU": 3,
    "ЧТ": 3,
    "THURSDAY": 3,

    "FRI": 4,
    "ПТ": 4,
    "FRIDAY": 4,

    "SAT": 5,
    "СБ": 5,
    "SATURDAY": 5,

    "SUN": 6,
    "ВС": 6,
    "SUNDAY": 6,
}

WEEKDAY_LABELS = {
    None: "ALL",
    0: "ПН",
    1: "ВТ",
    2: "СР",
    3: "ЧТ",
    4: "ПТ",
    5: "СБ",
    6: "ВС",
}


def _money(value: Any) -> float:
    return round(float(value or 0), 2)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or 0))


class PayrollService:
    def parse_weekday(self, value: str) -> int | None:
        key = value.strip().upper()

        if key in WEEKDAY_ALIASES:
            return WEEKDAY_ALIASES[key]

        try:
            numeric = int(key)
        except ValueError as exc:
            raise ValueError(
                "День недели: ALL/ВСЕ, ПН..ВС, MON..SUN или 0..6."
            ) from exc

        if numeric < 0 or numeric > 6:
            raise ValueError("День недели должен быть от 0 до 6.")

        return numeric

    def parse_tiers(self, raw: str) -> list[dict]:
        """
        Telegram format:
        1.00=0.03;1.25=0.05

        A tier applies from min_ratio inclusively until the next tier.
        """
        tiers: list[dict] = []

        for part in raw.split(";"):
            part = part.strip()
            if not part:
                continue

            if "=" not in part:
                raise ValueError(
                    "Тарифы: например 1.00=0.03;1.25=0.05"
                )

            min_raw, percent_raw = [
                item.strip()
                for item in part.split("=", 1)
            ]

            min_ratio = float(min_raw)
            percent = float(percent_raw)

            if min_ratio < 0 or percent < 0:
                raise ValueError("Порог и процент не могут быть отрицательными.")

            tiers.append(
                {
                    "min_ratio": min_ratio,
                    "max_ratio": None,
                    "percent": percent,
                }
            )

        if not tiers:
            return []

        tiers.sort(key=lambda item: item["min_ratio"])

        for index in range(len(tiers) - 1):
            tiers[index]["max_ratio"] = tiers[index + 1]["min_ratio"]

        return tiers

    def select_kpi_percent(
        self,
        ratio: Decimal | float,
        tiers: list[dict],
    ) -> Decimal:
        ratio_value = Decimal(str(ratio))

        selected = Decimal("0")

        for tier in sorted(
            tiers,
            key=lambda item: Decimal(str(item.get("min_ratio", 0))),
        ):
            min_ratio = Decimal(str(tier.get("min_ratio", 0)))
            max_raw = tier.get("max_ratio")
            max_ratio = (
                Decimal(str(max_raw))
                if max_raw is not None
                else None
            )

            if ratio_value < min_ratio:
                continue

            if max_ratio is not None and ratio_value >= max_ratio:
                continue

            selected = Decimal(str(tier.get("percent", 0)))
            break

        return selected

    async def _resolve_store(self, query: str):
        value = query.strip()

        async with pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    point_id,
                    name,
                    address
                FROM stores
                WHERE CAST(point_id AS TEXT)=$1
                   OR LOWER(name) LIKE LOWER($2)
                   OR LOWER(address) LIKE LOWER($2)
                ORDER BY name
                LIMIT 20
                """,
                value,
                f"%{value}%",
            )

        if not rows:
            return None, {
                "error": "Магазин не найден.",
                "query": query,
            }

        if len(rows) > 1:
            return None, {
                "error": "Название магазина неоднозначно.",
                "matches": [dict(row) for row in rows],
            }

        return rows[0], None

    async def set_plan(
        self,
        store_query: str,
        shift_type: str,
        weekday_raw: str,
        amount: Decimal | float | str,
        valid_from: date,
        valid_to: date | None = None,
        note: str = "",
        source: str = "MANUAL",
    ) -> dict:
        store, error = await self._resolve_store(store_query)

        if error:
            return error

        shift_type = shift_type.strip().upper()

        if shift_type not in {"DAY", "NIGHT"}:
            raise ValueError("Тип смены должен быть DAY или NIGHT.")

        weekday = self.parse_weekday(weekday_raw)
        amount_value = _decimal(amount)

        if amount_value < 0:
            raise ValueError("План не может быть отрицательным.")

        if valid_to is not None and valid_to < valid_from:
            raise ValueError("valid_to раньше valid_from.")

        async with pool().acquire() as conn:
            plan_id = await conn.fetchval(
                """
                INSERT INTO shift_plans(
                    point_id,
                    shift_type,
                    weekday,
                    plan_amount,
                    valid_from,
                    valid_to,
                    active,
                    source,
                    note
                )
                VALUES($1,$2,$3,$4,$5,$6,TRUE,$7,$8)
                RETURNING id
                """,
                store["point_id"],
                shift_type,
                weekday,
                amount_value,
                valid_from,
                valid_to,
                source,
                note,
            )

        return {
            "id": plan_id,
            "point_id": store["point_id"],
            "store": store["name"],
            "shift_type": shift_type,
            "weekday": WEEKDAY_LABELS[weekday],
            "plan_amount": _money(amount_value),
            "valid_from": valid_from.isoformat(),
            "valid_to": valid_to.isoformat() if valid_to else None,
            "source": source,
        }

    async def resolve_plan(
        self,
        point_id: int,
        business_date: date,
        shift_type: str,
    ) -> dict | None:
        weekday = business_date.weekday()

        async with pool().acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    p.*,
                    st.name AS store
                FROM shift_plans p
                JOIN stores st
                  ON st.point_id=p.point_id
                WHERE p.point_id=$1
                  AND p.shift_type=$2
                  AND p.active=TRUE
                  AND p.valid_from <= $3
                  AND (p.valid_to IS NULL OR p.valid_to >= $3)
                  AND (p.weekday=$4 OR p.weekday IS NULL)
                ORDER BY
                    CASE WHEN p.weekday=$4 THEN 1 ELSE 0 END DESC,
                    p.valid_from DESC,
                    p.id DESC
                LIMIT 1
                """,
                point_id,
                shift_type,
                business_date,
                weekday,
            )

        if not row:
            return None

        return {
            "id": row["id"],
            "point_id": row["point_id"],
            "store": row["store"],
            "shift_type": row["shift_type"],
            "weekday": WEEKDAY_LABELS[row["weekday"]],
            "plan_amount": _money(row["plan_amount"]),
            "valid_from": row["valid_from"].isoformat(),
            "valid_to": (
                row["valid_to"].isoformat()
                if row["valid_to"]
                else None
            ),
            "source": row["source"],
            "note": row["note"],
        }

    async def resolve_plan_for_store(
        self,
        store_query: str,
        business_date: date,
        shift_type: str,
    ) -> dict:
        store, error = await self._resolve_store(store_query)

        if error:
            return error

        plan = await self.resolve_plan(
            store["point_id"],
            business_date,
            shift_type.upper(),
        )

        if not plan:
            return {
                "error": "План для смены не найден.",
                "store": store["name"],
                "business_date": business_date.isoformat(),
                "shift_type": shift_type.upper(),
            }

        plan["business_date"] = business_date.isoformat()
        return plan

    async def list_plans(
        self,
        store_query: str = "",
        active_only: bool = True,
    ) -> dict:
        point_id = None
        store_name = "вся сеть"

        if store_query.strip():
            store, error = await self._resolve_store(store_query)
            if error:
                return error

            point_id = store["point_id"]
            store_name = store["name"]

        async with pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    p.*,
                    st.name AS store
                FROM shift_plans p
                JOIN stores st
                  ON st.point_id=p.point_id
                WHERE ($1::BIGINT IS NULL OR p.point_id=$1)
                  AND ($2::BOOLEAN=FALSE OR p.active=TRUE)
                ORDER BY
                    st.name,
                    p.shift_type,
                    p.weekday NULLS FIRST,
                    p.valid_from DESC,
                    p.id DESC
                """,
                point_id,
                active_only,
            )

        return {
            "scope": store_name,
            "plans": [
                {
                    "id": row["id"],
                    "point_id": row["point_id"],
                    "store": row["store"],
                    "shift_type": row["shift_type"],
                    "weekday": WEEKDAY_LABELS[row["weekday"]],
                    "plan_amount": _money(row["plan_amount"]),
                    "valid_from": row["valid_from"].isoformat(),
                    "valid_to": (
                        row["valid_to"].isoformat()
                        if row["valid_to"]
                        else None
                    ),
                    "active": row["active"],
                    "source": row["source"],
                    "note": row["note"],
                }
                for row in rows
            ],
        }

    async def resolve_policy(
        self,
        role: str,
        for_date: date,
    ) -> dict | None:
        role = role.strip().upper()

        async with pool().acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM payroll_policies
                WHERE role=$1
                  AND active=TRUE
                  AND valid_from <= $2
                  AND (valid_to IS NULL OR valid_to >= $2)
                ORDER BY valid_from DESC, version DESC
                LIMIT 1
                """,
                role,
                for_date,
            )

        if not row:
            return None

        tiers = row["kpi_tiers"]

        if isinstance(tiers, str):
            tiers = json.loads(tiers)

        return {
            "id": row["id"],
            "role": row["role"],
            "version": row["version"],
            "name": row["name"],
            "valid_from": row["valid_from"].isoformat(),
            "valid_to": (
                row["valid_to"].isoformat()
                if row["valid_to"]
                else None
            ),
            "active": row["active"],
            "base_per_shift": _money(row["base_per_shift"]),
            "kpi_basis": row["kpi_basis"],
            "kpi_tiers": tiers or [],
            "exam_percent": float(row["exam_percent"] or 0),
            "exam_basis": row["exam_basis"],
            "metadata": row["metadata"],
        }

    async def list_policies(self, role: str = "") -> dict:
        role_value = role.strip().upper() or None

        async with pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM payroll_policies
                WHERE ($1::TEXT IS NULL OR role=$1)
                ORDER BY role, version
                """,
                role_value,
            )

        result = []

        for row in rows:
            tiers = row["kpi_tiers"]

            if isinstance(tiers, str):
                tiers = json.loads(tiers)

            result.append(
                {
                    "id": row["id"],
                    "role": row["role"],
                    "version": row["version"],
                    "name": row["name"],
                    "valid_from": row["valid_from"].isoformat(),
                    "valid_to": (
                        row["valid_to"].isoformat()
                        if row["valid_to"]
                        else None
                    ),
                    "active": row["active"],
                    "base_per_shift": _money(row["base_per_shift"]),
                    "kpi_basis": row["kpi_basis"],
                    "kpi_tiers": tiers or [],
                    "exam_percent": float(row["exam_percent"] or 0),
                    "exam_basis": row["exam_basis"],
                }
            )

        return {
            "role": role_value or "ALL",
            "policies": result,
        }

    async def create_policy(
        self,
        role: str,
        valid_from: date,
        base_per_shift: Decimal | float | str,
        tiers_raw: str,
        exam_percent: Decimal | float | str,
        *,
        kpi_basis: str = "STORE_SHIFT_REVENUE",
        exam_basis: str = "PERSONAL_MONTH_REVENUE",
        name: str = "",
    ) -> dict:
        role = role.strip().upper()

        if role not in {"SELLER", "NIGHT_ASSISTANT"}:
            raise ValueError("Роль: SELLER или NIGHT_ASSISTANT.")

        tiers = self.parse_tiers(tiers_raw)
        base_value = _decimal(base_per_shift)
        exam_value = _decimal(exam_percent)

        if base_value < 0 or exam_value < 0:
            raise ValueError("Фикс/экзаменационный процент не могут быть отрицательными.")

        async with pool().acquire() as conn:
            async with conn.transaction():
                version = await conn.fetchval(
                    """
                    SELECT COALESCE(MAX(version),0)+1
                    FROM payroll_policies
                    WHERE role=$1
                    """,
                    role,
                )

                # Close only policies that are open and begin before the new one.
                await conn.execute(
                    """
                    UPDATE payroll_policies
                    SET
                        valid_to=$2 - 1,
                        updated_at=NOW()
                    WHERE role=$1
                      AND active=TRUE
                      AND valid_from < $2
                      AND (valid_to IS NULL OR valid_to >= $2)
                    """,
                    role,
                    valid_from,
                )

                policy_name = name.strip() or f"{role} Policy v{version}"

                policy_id = await conn.fetchval(
                    """
                    INSERT INTO payroll_policies(
                        role,
                        version,
                        name,
                        valid_from,
                        valid_to,
                        active,
                        base_per_shift,
                        kpi_basis,
                        kpi_tiers,
                        exam_percent,
                        exam_basis,
                        metadata
                    )
                    VALUES(
                        $1,$2,$3,$4,NULL,TRUE,$5,$6,$7::jsonb,$8,$9,'{}'::jsonb
                    )
                    RETURNING id
                    """,
                    role,
                    version,
                    policy_name,
                    valid_from,
                    base_value,
                    kpi_basis,
                    json.dumps(tiers, ensure_ascii=False),
                    exam_value,
                    exam_basis,
                )

        return (await self.resolve_policy(role, valid_from)) or {
            "id": policy_id,
            "role": role,
            "version": version,
        }

    async def sync_seller_identities(self) -> dict:
        async with pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT
                    seller_id,
                    seller_name
                FROM employee_work_shifts
                WHERE seller_id IS NOT NULL
                  AND seller_name <> ''
                ORDER BY seller_name
                """
            )

            inserted = 0
            updated = 0

            for row in rows:
                existing = await conn.fetchrow(
                    """
                    SELECT id, full_name
                    FROM employee_identity_map
                    WHERE role='SELLER'
                      AND saby_seller_id=$1
                      AND active=TRUE
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    row["seller_id"],
                )

                if existing:
                    if existing["full_name"] != row["seller_name"]:
                        await conn.execute(
                            """
                            UPDATE employee_identity_map
                            SET
                                full_name=$2,
                                updated_at=NOW()
                            WHERE id=$1
                            """,
                            existing["id"],
                            row["seller_name"],
                        )
                        updated += 1
                    continue

                await conn.execute(
                    """
                    INSERT INTO employee_identity_map(
                        role,
                        full_name,
                        saby_seller_id,
                        valid_from,
                        active,
                        source
                    )
                    VALUES(
                        'SELLER',
                        $1,
                        $2,
                        DATE '2026-07-01',
                        TRUE,
                        'SABY_AUTO'
                    )
                    """,
                    row["seller_name"],
                    row["seller_id"],
                )
                inserted += 1

        return {
            "found_sellers": len(rows),
            "inserted": inserted,
            "updated": updated,
        }

    async def _resolve_identity_by_name(
        self,
        name_query: str,
        role: str = "SELLER",
    ):
        async with pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM employee_identity_map
                WHERE role=$1
                  AND active=TRUE
                  AND LOWER(full_name) LIKE LOWER($2)
                ORDER BY full_name
                LIMIT 20
                """,
                role.upper(),
                f"%{name_query.strip()}%",
            )

        if not rows:
            return None, {
                "error": "Сотрудник не найден в identity map.",
                "query": name_query,
                "role": role.upper(),
            }

        if len(rows) > 1:
            return None, {
                "error": "Имя сотрудника неоднозначно.",
                "matches": [
                    {
                        "id": row["id"],
                        "full_name": row["full_name"],
                        "saby_seller_id": row["saby_seller_id"],
                        "core_employee_id": row["core_employee_id"],
                    }
                    for row in rows
                ],
            }

        return rows[0], None

    async def set_exam(
        self,
        employee_query: str,
        month: date,
        passed: bool,
        score: Decimal | float | str | None = None,
        role: str = "SELLER",
        source: str = "MANUAL",
    ) -> dict:
        identity, error = await self._resolve_identity_by_name(
            employee_query,
            role,
        )

        if error:
            return error

        month = month.replace(day=1)

        score_value = (
            _decimal(score)
            if score not in (None, "")
            else None
        )

        async with pool().acquire() as conn:
            await conn.execute(
                """
                INSERT INTO employee_exam_results(
                    identity_id,
                    month,
                    passed,
                    score,
                    source
                )
                VALUES($1,$2,$3,$4,$5)
                ON CONFLICT(identity_id, month) DO UPDATE SET
                    passed=EXCLUDED.passed,
                    score=EXCLUDED.score,
                    source=EXCLUDED.source,
                    updated_at=NOW()
                """,
                identity["id"],
                month,
                passed,
                score_value,
                source,
            )

        return {
            "identity_id": identity["id"],
            "employee": identity["full_name"],
            "role": identity["role"],
            "month": month.isoformat(),
            "passed": passed,
            "score": float(score_value) if score_value is not None else None,
            "source": source,
        }

    async def seller_daily_preview(
        self,
        business_date: date,
    ) -> dict:
        """
        Deterministic seller shift payroll preview.

        Plan achievement:
            total revenue of the whole STORE DAY/NIGHT shift / plan.

        KPI payment:
            selected KPI percent * seller's personal revenue.

        Exam bonus is monthly and therefore not included in daily shift_pay.
        """
        async with pool().acquire() as conn:
            rows = await conn.fetch(
                """
                WITH shift_totals AS (
                    SELECT
                        point_id,
                        business_date,
                        shift_type,
                        SUM(net_revenue) AS store_shift_revenue
                    FROM employee_work_shifts
                    WHERE business_date=$1
                    GROUP BY point_id, business_date, shift_type
                )
                SELECT
                    ws.id AS work_shift_id,
                    ws.point_id,
                    st.name AS store,
                    ws.business_date,
                    ws.shift_type,
                    ws.seller_id,
                    ws.seller_name,
                    ws.net_revenue AS seller_revenue,
                    ws.status AS shift_status,
                    shift_totals.store_shift_revenue
                FROM employee_work_shifts ws
                JOIN stores st
                  ON st.point_id=ws.point_id
                JOIN shift_totals
                  ON shift_totals.point_id=ws.point_id
                 AND shift_totals.business_date=ws.business_date
                 AND shift_totals.shift_type=ws.shift_type
                WHERE ws.business_date=$1
                ORDER BY st.name, ws.shift_type, ws.seller_name
                """,
                business_date,
            )

        result = []

        for row in rows:
            policy = await self.resolve_policy(
                "SELLER",
                business_date,
            )

            plan = await self.resolve_plan(
                row["point_id"],
                business_date,
                row["shift_type"],
            )

            seller_revenue = _decimal(row["seller_revenue"])
            store_shift_revenue = _decimal(row["store_shift_revenue"])

            item = {
                "work_shift_id": row["work_shift_id"],
                "point_id": row["point_id"],
                "store": row["store"],
                "business_date": row["business_date"].isoformat(),
                "shift_type": row["shift_type"],
                "seller_id": row["seller_id"],
                "seller_name": row["seller_name"],
                "seller_revenue": _money(seller_revenue),
                "store_shift_revenue": _money(store_shift_revenue),
                "shift_status": row["shift_status"],
            }

            if row["shift_status"] != "AUTO":
                item.update(
                    {
                        "payroll_status": "REVIEW_SHIFT",
                        "reason": "Смена не AUTO.",
                    }
                )
                result.append(item)
                continue

            if not plan:
                item.update(
                    {
                        "payroll_status": "MISSING_PLAN",
                        "reason": "Не найден план магазина/смены.",
                    }
                )
                result.append(item)
                continue

            if not policy:
                item.update(
                    {
                        "payroll_status": "MISSING_POLICY",
                        "reason": "Не найдена политика SELLER.",
                    }
                )
                result.append(item)
                continue

            plan_amount = _decimal(plan["plan_amount"])

            ratio = (
                store_shift_revenue / plan_amount
                if plan_amount > 0
                else Decimal("0")
            )

            kpi_percent = self.select_kpi_percent(
                ratio,
                policy["kpi_tiers"],
            )

            base_pay = _decimal(policy["base_per_shift"])
            kpi_pay = seller_revenue * kpi_percent
            shift_pay = base_pay + kpi_pay

            item.update(
                {
                    "payroll_status": "OK",
                    "plan_id": plan["id"],
                    "plan_amount": _money(plan_amount),
                    "plan_weekday": plan["weekday"],
                    "achievement_ratio": round(float(ratio), 4),
                    "achievement_percent": round(float(ratio * 100), 2),
                    "policy_version": policy["version"],
                    "base_pay": _money(base_pay),
                    "kpi_percent": float(kpi_percent),
                    "kpi_pay": _money(kpi_pay),
                    "shift_pay_without_exam": _money(shift_pay),
                }
            )

            result.append(item)

        return {
            "business_date": business_date.isoformat(),
            "logic": {
                "kpi_trigger_basis": "STORE_SHIFT_REVENUE",
                "kpi_payment_basis": "SELLER_PERSONAL_REVENUE",
                "exam": "MONTHLY_SEPARATE",
            },
            "rows": result,
            "ok": all(
                item.get("payroll_status") == "OK"
                for item in result
            ) if result else False,
        }

    async def seller_month_preview(
        self,
        month: date,
    ) -> dict:
        month_start = month.replace(day=1)
        month_end = date(
            month_start.year,
            month_start.month,
            monthrange(
                month_start.year,
                month_start.month,
            )[1],
        )

        current = month_start
        daily_rows: list[dict] = []

        while current <= month_end:
            preview = await self.seller_daily_preview(current)
            daily_rows.extend(preview["rows"])
            current += timedelta(days=1)

        by_seller: dict[str, dict] = {}

        for row in daily_rows:
            seller_key = (
                str(row.get("seller_id"))
                if row.get("seller_id") is not None
                else row["seller_name"]
            )

            bucket = by_seller.setdefault(
                seller_key,
                {
                    "seller_id": row.get("seller_id"),
                    "seller_name": row["seller_name"],
                    "shift_count": 0,
                    "personal_revenue": Decimal("0"),
                    "base_pay": Decimal("0"),
                    "kpi_pay": Decimal("0"),
                    "shift_pay_without_exam": Decimal("0"),
                    "problems": [],
                },
            )

            if row.get("payroll_status") != "OK":
                bucket["problems"].append(
                    {
                        "business_date": row["business_date"],
                        "store": row["store"],
                        "shift_type": row["shift_type"],
                        "status": row.get("payroll_status"),
                    }
                )
                continue

            bucket["shift_count"] += 1
            bucket["personal_revenue"] += _decimal(row["seller_revenue"])
            bucket["base_pay"] += _decimal(row["base_pay"])
            bucket["kpi_pay"] += _decimal(row["kpi_pay"])
            bucket["shift_pay_without_exam"] += _decimal(
                row["shift_pay_without_exam"]
            )

        result = []

        for bucket in by_seller.values():
            exam_bonus = Decimal("0")
            exam_passed = False
            exam_percent = Decimal("0")

            identity = None

            if bucket["seller_id"] is not None:
                async with pool().acquire() as conn:
                    identity = await conn.fetchrow(
                        """
                        SELECT *
                        FROM employee_identity_map
                        WHERE role='SELLER'
                          AND saby_seller_id=$1
                          AND active=TRUE
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        bucket["seller_id"],
                    )

            policy = await self.resolve_policy(
                "SELLER",
                month_start,
            )

            if identity and policy:
                async with pool().acquire() as conn:
                    exam = await conn.fetchrow(
                        """
                        SELECT passed, score, source
                        FROM employee_exam_results
                        WHERE identity_id=$1
                          AND month=$2
                        """,
                        identity["id"],
                        month_start,
                    )

                if exam and exam["passed"]:
                    exam_passed = True
                    exam_percent = _decimal(policy["exam_percent"])
                    exam_bonus = (
                        bucket["personal_revenue"]
                        * exam_percent
                    )

            total = (
                bucket["shift_pay_without_exam"]
                + exam_bonus
            )

            result.append(
                {
                    "seller_id": bucket["seller_id"],
                    "seller_name": bucket["seller_name"],
                    "shift_count": bucket["shift_count"],
                    "personal_revenue": _money(
                        bucket["personal_revenue"]
                    ),
                    "base_pay": _money(bucket["base_pay"]),
                    "kpi_pay": _money(bucket["kpi_pay"]),
                    "exam_passed": exam_passed,
                    "exam_percent": float(exam_percent),
                    "exam_bonus": _money(exam_bonus),
                    "total_pay": _money(total),
                    "problems": bucket["problems"],
                    "status": (
                        "OK"
                        if not bucket["problems"]
                        else "REVIEW"
                    ),
                }
            )

        result.sort(
            key=lambda item: item["seller_name"]
        )

        return {
            "month": month_start.strftime("%Y-%m"),
            "role": "SELLER",
            "rows": result,
            "ok": all(
                item["status"] == "OK"
                for item in result
            ) if result else False,
        }


payroll_service = PayrollService()
