from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings


AUTH_URL = "https://online.sbis.ru/oauth/service/"
API_BASE = "https://api.sbis.ru"


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _money(value: Decimal) -> float:
    return round(float(value), 2)


@dataclass(slots=True)
class TokenCache:
    token: str = ""
    created_at: datetime | None = None


class SabyClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._token = TokenCache()
        self._lock = asyncio.Lock()

    def _tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.business_tz)

    def resolve_date(self, value: str | None) -> str:
        now = datetime.now(self._tz())
        raw = (value or "").strip().lower()

        if raw in {"", "yesterday", "вчера"}:
            return (now.date() - timedelta(days=1)).isoformat()
        if raw in {"today", "сегодня"}:
            return now.date().isoformat()
        if raw in {"позавчера", "day before yesterday"}:
            return (now.date() - timedelta(days=2)).isoformat()

        try:
            return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
        except ValueError as exc:
            raise ValueError(
                "Дата должна быть YYYY-MM-DD, 'сегодня' или 'вчера'."
            ) from exc

    async def _authenticate(self, force: bool = False) -> str:
        async with self._lock:
            now = datetime.now(self._tz())
            if (
                not force
                and self._token.token
                and self._token.created_at
                and now - self._token.created_at < timedelta(minutes=25)
            ):
                return self._token.token

            payload = {
                "app_client_id": self.settings.saby_app_client_id,
                "app_secret": self.settings.saby_app_secret,
                "secret_key": self.settings.saby_secret_key,
            }

            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(AUTH_URL, json=payload)
                response.raise_for_status()
                data = response.json()

            token = str(data.get("token") or "").strip()
            if not token:
                raise RuntimeError("Saby не вернул token при сервисной авторизации.")

            self._token = TokenCache(token=token, created_at=now)
            return token

    async def _get(
        self,
        path: str,
        params: dict[str, Any],
        retry_auth: bool = True,
    ) -> dict[str, Any]:
        token = await self._authenticate()
        headers = {"X-SBISAccessToken": token}

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(
                f"{API_BASE}{path}",
                params=params,
                headers=headers,
            )

        if response.status_code in {401, 403} and retry_auth:
            token = await self._authenticate(force=True)
            headers["X-SBISAccessToken"] = token
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.get(
                    f"{API_BASE}{path}",
                    params=params,
                    headers=headers,
                )

        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"Неожиданный ответ Saby для {path}.")
        return data

    async def list_points(self) -> list[dict[str, Any]]:
        data = await self._get(
            "/retail/point/list",
            {
                "product": "retail",
                "page": 0,
                "pageSize": 500,
            },
        )
        points = data.get("salesPoints") or data.get("points") or []

        result: list[dict[str, Any]] = []
        allowed = self.settings.saby_points_filter

        for p in points:
            try:
                point_id = int(p.get("id"))
            except (TypeError, ValueError):
                continue

            if allowed and point_id not in allowed:
                continue

            result.append(
                {
                    "id": point_id,
                    "name": str(p.get("name") or point_id),
                    "address": str(p.get("address") or ""),
                    "locality": str(p.get("locality") or ""),
                }
            )

        return result

    async def _orders_for_point(
        self,
        point_id: int,
        date_value: str,
        need_discount_info: bool = True,
    ) -> list[dict[str, Any]]:
        day = self.resolve_date(date_value)
        page_size = 100
        result: list[dict[str, Any]] = []

        for page in range(self.settings.saby_max_pages_per_point):
            data = await self._get(
                "/retail/order/list",
                {
                    "pointId": point_id,
                    "fromDateTime": f"{day} 00:00:00",
                    "toDateTime": f"{day} 23:59:59",
                    "page": page,
                    "pageSize": page_size,
                    "needDiscountInfo": str(need_discount_info).lower(),
                },
            )
            orders = data.get("orders") or []
            if not isinstance(orders, list):
                break

            result.extend(orders)
            if len(orders) < page_size:
                break

        return result

    async def network_sales_summary(self, date_value: str = "вчера") -> dict[str, Any]:
        day = self.resolve_date(date_value)
        points = await self.list_points()

        gross = Decimal("0")
        returns = Decimal("0")
        net = Decimal("0")
        total_cost = Decimal("0")
        discounts = Decimal("0")
        sales_checks = 0
        return_checks = 0
        sellers: set[str] = set()
        point_rows: list[dict[str, Any]] = []

        for point in points:
            orders = await self._orders_for_point(point["id"], day)
            p_net = Decimal("0")
            p_checks = 0
            p_returns = Decimal("0")

            for order in orders:
                if bool(order.get("Deleted")):
                    continue

                amount_raw = _dec(order.get("TotalPrice"))
                is_return = bool(order.get("Return"))
                amount = -abs(amount_raw) if is_return else amount_raw
                net += amount
                p_net += amount

                if is_return:
                    returns += abs(amount_raw)
                    p_returns += abs(amount_raw)
                    return_checks += 1
                else:
                    gross += amount_raw
                    sales_checks += 1
                    p_checks += 1

                discounts += abs(_dec(order.get("TotalDiscount")))

                seller = str(order.get("SellerName") or "").strip()
                if seller:
                    sellers.add(seller)

                for position in order.get("SaleNomenclatures") or []:
                    if bool(position.get("Refused")):
                        continue
                    cost = _dec(position.get("TotalCost"))
                    if is_return and cost > 0:
                        cost = -cost
                    total_cost += cost

            point_rows.append(
                {
                    "point_id": point["id"],
                    "store": point["name"],
                    "net_revenue": _money(p_net),
                    "sales_checks": p_checks,
                    "returns_amount": _money(p_returns),
                }
            )

        avg_check = (net / sales_checks) if sales_checks else Decimal("0")
        gross_profit = net - total_cost
        margin = (gross_profit / net * 100) if net > 0 else Decimal("0")
        point_rows.sort(key=lambda x: x["net_revenue"], reverse=True)

        return {
            "date": day,
            "stores": len(points),
            "sales_checks": sales_checks,
            "return_checks": return_checks,
            "gross_sales": _money(gross),
            "returns_amount": _money(returns),
            "net_revenue": _money(net),
            "average_check": _money(avg_check),
            "discounts": _money(discounts),
            "actual_cost": _money(total_cost),
            "gross_profit": _money(gross_profit),
            "gross_margin_percent": round(float(margin), 2),
            "unique_sellers": len(sellers),
            "stores_by_revenue": point_rows,
        }

    async def store_sales_summary(
        self,
        store_query: str,
        date_value: str = "вчера",
    ) -> dict[str, Any]:
        day = self.resolve_date(date_value)
        query = store_query.strip().casefold()
        points = await self.list_points()
        matches = [
            p for p in points
            if query in p["name"].casefold()
            or query in p["address"].casefold()
            or query == str(p["id"])
        ]

        if not matches:
            return {
                "error": "Магазин не найден.",
                "query": store_query,
                "available_stores": [p["name"] for p in points],
            }
        if len(matches) > 1:
            return {
                "error": "Название неоднозначно.",
                "query": store_query,
                "matches": [
                    {"id": p["id"], "name": p["name"], "address": p["address"]}
                    for p in matches
                ],
            }

        point = matches[0]
        orders = await self._orders_for_point(point["id"], day)

        net = Decimal("0")
        gross = Decimal("0")
        returns = Decimal("0")
        total_cost = Decimal("0")
        discounts = Decimal("0")
        checks = 0
        return_checks = 0
        sellers: dict[str, Decimal] = {}

        for order in orders:
            if bool(order.get("Deleted")):
                continue

            amount_raw = _dec(order.get("TotalPrice"))
            is_return = bool(order.get("Return"))
            amount = -abs(amount_raw) if is_return else amount_raw
            net += amount

            if is_return:
                returns += abs(amount_raw)
                return_checks += 1
            else:
                gross += amount_raw
                checks += 1

            discounts += abs(_dec(order.get("TotalDiscount")))
            seller = str(order.get("SellerName") or "").strip() or "Не указан"
            sellers[seller] = sellers.get(seller, Decimal("0")) + amount

            for position in order.get("SaleNomenclatures") or []:
                if bool(position.get("Refused")):
                    continue
                cost = _dec(position.get("TotalCost"))
                if is_return and cost > 0:
                    cost = -cost
                total_cost += cost

        avg_check = (net / checks) if checks else Decimal("0")
        gross_profit = net - total_cost
        margin = (gross_profit / net * 100) if net > 0 else Decimal("0")
        seller_rows = [
            {"seller": name, "net_revenue": _money(value)}
            for name, value in sellers.items()
        ]
        seller_rows.sort(key=lambda x: x["net_revenue"], reverse=True)

        return {
            "date": day,
            "point_id": point["id"],
            "store": point["name"],
            "address": point["address"],
            "sales_checks": checks,
            "return_checks": return_checks,
            "gross_sales": _money(gross),
            "returns_amount": _money(returns),
            "net_revenue": _money(net),
            "average_check": _money(avg_check),
            "discounts": _money(discounts),
            "actual_cost": _money(total_cost),
            "gross_profit": _money(gross_profit),
            "gross_margin_percent": round(float(margin), 2),
            "sellers": seller_rows,
        }

    async def top_products(
        self,
        date_value: str = "вчера",
        limit: int = 10,
        store_query: str = "",
    ) -> dict[str, Any]:
        day = self.resolve_date(date_value)
        limit = max(1, min(int(limit), 30))
        points = await self.list_points()

        if store_query.strip():
            query = store_query.strip().casefold()
            matches = [
                p for p in points
                if query in p["name"].casefold()
                or query in p["address"].casefold()
                or query == str(p["id"])
            ]
            if len(matches) != 1:
                return {
                    "error": "Для товарного отчёта магазин должен определяться однозначно.",
                    "matches": [p["name"] for p in matches],
                }
            points = matches

        products: dict[str, dict[str, Any]] = {}

        for point in points:
            orders = await self._orders_for_point(point["id"], day)
            for order in orders:
                if bool(order.get("Deleted")):
                    continue

                order_is_return = bool(order.get("Return"))
                for pos in order.get("SaleNomenclatures") or []:
                    if bool(pos.get("Refused")):
                        continue

                    product_id = str(
                        pos.get("NomenclatureUUID")
                        or pos.get("Nomenclature")
                        or pos.get("SaleNomenclature")
                        or pos.get("Name")
                        or pos.get("ShortName")
                        or "unknown"
                    )
                    name = str(pos.get("Name") or pos.get("ShortName") or product_id)
                    qty = _dec(pos.get("Quantity"))
                    revenue = _dec(pos.get("TotalPrice"))
                    cost = _dec(pos.get("TotalCost"))
                    is_return = order_is_return or bool(pos.get("IsReturn"))

                    if is_return:
                        qty = -abs(qty)
                        revenue = -abs(revenue)
                        cost = -abs(cost)

                    row = products.setdefault(
                        product_id,
                        {
                            "product_id": product_id,
                            "name": name,
                            "quantity": Decimal("0"),
                            "revenue": Decimal("0"),
                            "cost": Decimal("0"),
                        },
                    )
                    row["quantity"] += qty
                    row["revenue"] += revenue
                    row["cost"] += cost

        rows: list[dict[str, Any]] = []
        for row in products.values():
            profit = row["revenue"] - row["cost"]
            rows.append(
                {
                    "product_id": row["product_id"],
                    "name": row["name"],
                    "quantity": round(float(row["quantity"]), 3),
                    "net_revenue": _money(row["revenue"]),
                    "actual_cost": _money(row["cost"]),
                    "gross_profit": _money(profit),
                }
            )

        rows.sort(key=lambda x: x["net_revenue"], reverse=True)
        return {
            "date": day,
            "scope": store_query.strip() or "вся сеть",
            "top_products": rows[:limit],
        }


saby_client = SabyClient()
