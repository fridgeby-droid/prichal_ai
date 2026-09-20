from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db.database import pool
from app.services.saby import saby_client

settings = get_settings()


def _money(value) -> float:
    return round(float(value or 0), 2)


class AnalyticsService:
    def resolve_date(self, value: str = "вчера") -> date:
        return date.fromisoformat(saby_client.resolve_date(value))

    async def coverage(self) -> dict:
        async with pool().acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    MIN((sale_datetime AT TIME ZONE $1)::date) AS min_date,
                    MAX((sale_datetime AT TIME ZONE $1)::date) AS max_date,
                    COUNT(*) AS sales,
                    COUNT(DISTINCT point_id) AS stores
                FROM sales
                WHERE deleted=FALSE
                """,
                settings.business_tz,
            )
            last_sync = await conn.fetchrow(
                """
                SELECT id, started_at, finished_at, date_from, date_to,
                       status, stores_count, sales_upserted,
                       items_upserted, shifts_built, error_text
                FROM sync_runs
                ORDER BY id DESC
                LIMIT 1
                """
            )

        return {
            "min_date": row["min_date"].isoformat() if row["min_date"] else None,
            "max_date": row["max_date"].isoformat() if row["max_date"] else None,
            "sales": row["sales"],
            "stores": row["stores"],
            "last_sync": dict(last_sync) if last_sync else None,
        }

    async def network_day(self, date_value: str = "вчера") -> dict:
        day = self.resolve_date(date_value)
        async with pool().acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE is_return=FALSE) AS sales_checks,
                    COUNT(*) FILTER (WHERE is_return=TRUE) AS return_checks,
                    COUNT(DISTINCT point_id) AS stores,
                    COALESCE(SUM(CASE WHEN is_return THEN -ABS(total_price) ELSE total_price END),0) AS net_revenue,
                    COALESCE(SUM(total_discount),0) AS discounts,
                    COUNT(DISTINCT NULLIF(seller_name,'')) AS sellers
                FROM sales
                WHERE deleted=FALSE
                  AND (sale_datetime AT TIME ZONE $2)::date=$1
                """,
                day,
                settings.business_tz,
            )
            items = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(CASE WHEN i.is_return THEN -ABS(i.total_cost) ELSE i.total_cost END),0) AS cost
                FROM sale_items i
                JOIN sales s ON s.point_id=i.point_id AND s.sale_id=i.sale_id
                WHERE s.deleted=FALSE
                  AND (s.sale_datetime AT TIME ZONE $2)::date=$1
                  AND i.refused=FALSE
                """,
                day,
                settings.business_tz,
            )
        checks = int(row['sales_checks'] or 0)
        revenue = Decimal(row['net_revenue'] or 0)
        cost = Decimal(items['cost'] or 0)
        avg_check = revenue / checks if checks else Decimal('0')
        profit = revenue - cost
        margin = (profit / revenue * 100) if revenue > 0 else Decimal('0')
        return {
            'date': day.isoformat(), 'stores': row['stores'], 'sales_checks': checks,
            'return_checks': row['return_checks'], 'net_revenue': _money(revenue),
            'average_check': _money(avg_check), 'discounts': _money(row['discounts']),
            'actual_cost': _money(cost), 'gross_profit': _money(profit),
            'gross_margin_percent': round(float(margin),2), 'unique_sellers': row['sellers'],
            'source': 'postgresql',
        }

    async def _resolve_store(self, query: str):
        q = query.strip()
        async with pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT point_id, name, address, locality
                FROM stores
                WHERE CAST(point_id AS TEXT)=$1
                   OR LOWER(name) LIKE LOWER($2)
                   OR LOWER(address) LIKE LOWER($2)
                ORDER BY name LIMIT 20
                """, q, f"%{q}%"
            )
        if not rows:
            return None, {'error':'Магазин не найден.','query':query}
        if len(rows)>1:
            return None, {'error':'Название магазина неоднозначно.','matches':[dict(r) for r in rows]}
        return rows[0], None

    async def store_day(self, store_query: str, date_value: str = "вчера") -> dict:
        day = self.resolve_date(date_value)
        store, error = await self._resolve_store(store_query)
        if error: return error
        async with pool().acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE is_return=FALSE) AS sales_checks,
                    COUNT(*) FILTER (WHERE is_return=TRUE) AS return_checks,
                    COALESCE(SUM(CASE WHEN is_return THEN -ABS(total_price) ELSE total_price END),0) AS net_revenue,
                    COUNT(DISTINCT NULLIF(seller_name,'')) AS sellers
                FROM sales
                WHERE point_id=$1 AND deleted=FALSE
                  AND (sale_datetime AT TIME ZONE $3)::date=$2
                """,
                store['point_id'], day, settings.business_tz,
            )
            shift_rows = await conn.fetch(
                """
                SELECT shift_type, seller_name, started_at, ended_at,
                       check_count, net_revenue, source, confidence, status
                FROM seller_shifts
                WHERE point_id=$1 AND work_date=$2
                ORDER BY shift_type, started_at
                """, store['point_id'], day
            )
        checks = int(row['sales_checks'] or 0)
        revenue = Decimal(row['net_revenue'] or 0)
        return {
            'date': day.isoformat(), 'point_id': store['point_id'], 'store': store['name'],
            'address': store['address'], 'sales_checks': checks, 'return_checks': row['return_checks'],
            'net_revenue': _money(revenue), 'average_check': _money(revenue/checks if checks else 0),
            'unique_sellers': row['sellers'],
            'seller_shifts': [
                {**dict(r), 'started_at': r['started_at'].isoformat(), 'ended_at': r['ended_at'].isoformat(),
                 'net_revenue': _money(r['net_revenue']), 'confidence': float(r['confidence'])}
                for r in shift_rows
            ],
            'source':'postgresql'
        }

    async def top_products(self, date_value: str='вчера', limit: int=10, store_query: str='') -> dict:
        day=self.resolve_date(date_value); limit=max(1,min(int(limit),30)); point_id=None; store_name='вся сеть'
        if store_query.strip():
            store,error=await self._resolve_store(store_query)
            if error:return error
            point_id=store['point_id']; store_name=store['name']
        sql="""
            SELECT COALESCE(NULLIF(i.product_uuid,''), CAST(i.product_id AS TEXT), i.name) AS product_key,
                   MAX(COALESCE(NULLIF(i.name,''), NULLIF(i.short_name,''), i.product_uuid)) AS name,
                   COALESCE(SUM(CASE WHEN i.is_return THEN -ABS(i.quantity) ELSE i.quantity END),0) AS quantity,
                   COALESCE(SUM(CASE WHEN i.is_return THEN -ABS(i.total_price) ELSE i.total_price END),0) AS revenue,
                   COALESCE(SUM(CASE WHEN i.is_return THEN -ABS(i.total_cost) ELSE i.total_cost END),0) AS cost
            FROM sale_items i JOIN sales s ON s.point_id=i.point_id AND s.sale_id=i.sale_id
            WHERE s.deleted=FALSE AND i.refused=FALSE
              AND (s.sale_datetime AT TIME ZONE $4)::date=$1
              AND ($2::BIGINT IS NULL OR s.point_id=$2)
            GROUP BY product_key ORDER BY revenue DESC LIMIT $3
        """
        async with pool().acquire() as conn:
            rows=await conn.fetch(sql,day,point_id,limit,settings.business_tz)
        result=[]
        for r in rows:
            revenue=Decimal(r['revenue'] or 0); cost=Decimal(r['cost'] or 0)
            result.append({'product_key':r['product_key'],'name':r['name'],'quantity':round(float(r['quantity'] or 0),3),
                           'net_revenue':_money(revenue),'actual_cost':_money(cost),'gross_profit':_money(revenue-cost)})
        return {'date':day.isoformat(),'scope':store_name,'top_products':result,'source':'postgresql'}

    async def shift_summary(self, date_value: str='вчера') -> dict:
        day=self.resolve_date(date_value)
        async with pool().acquire() as conn:
            rows=await conn.fetch(
                """
                SELECT ss.point_id, st.name AS store, ss.shift_type, ss.seller_name,
                       ss.started_at, ss.ended_at, ss.check_count, ss.net_revenue,
                       ss.source, ss.confidence, ss.status, ss.duration_hours,
                       ss.dominant_share, ss.saby_shift_id, ss.saby_shift_number
                FROM seller_shifts ss JOIN stores st ON st.point_id=ss.point_id
                WHERE ss.work_date=$1 ORDER BY st.name, ss.shift_type, ss.started_at
                """, day)
        return {'date':day.isoformat(),'shifts':[
            {**dict(r),'started_at':r['started_at'].isoformat(),'ended_at':r['ended_at'].isoformat(),
             'net_revenue':_money(r['net_revenue']),'confidence':float(r['confidence']),
             'duration_hours':float(r['duration_hours']),'dominant_share':float(r['dominant_share'])}
            for r in rows],
            'total':len(rows),'auto':sum(1 for r in rows if r['status']=='AUTO'),
            'review':sum(1 for r in rows if r['status']=='REVIEW'),
            'ambiguous':sum(1 for r in rows if r['status']=='AMBIGUOUS')}

    async def seller_shifts(self, seller_query: str, date_from: str='', date_to: str='') -> dict:
        today=datetime.now(ZoneInfo(settings.business_tz)).date()
        start=self.resolve_date(date_from) if date_from.strip() else today.replace(day=1)
        end=self.resolve_date(date_to) if date_to.strip() else today
        async with pool().acquire() as conn:
            rows=await conn.fetch(
                """
                SELECT ss.work_date, ss.point_id, st.name AS store, ss.shift_type, ss.seller_name,
                       ss.started_at, ss.ended_at, ss.check_count, ss.net_revenue,
                       ss.source, ss.confidence, ss.status
                FROM seller_shifts ss JOIN stores st ON st.point_id=ss.point_id
                WHERE LOWER(ss.seller_name) LIKE LOWER($1) AND ss.work_date BETWEEN $2 AND $3
                ORDER BY ss.work_date, ss.started_at
                """,f"%{seller_query.strip()}%",start,end)
        sellers=sorted({r['seller_name'] for r in rows})
        if len(sellers)>1:return {'error':'Имя продавца неоднозначно.','matches':sellers}
        return {'seller':sellers[0] if sellers else seller_query,'date_from':start.isoformat(),'date_to':end.isoformat(),
                'shift_count':len(rows),'total_revenue':_money(sum(Decimal(r['net_revenue'] or 0) for r in rows)),
                'shifts':[{**dict(r),'work_date':r['work_date'].isoformat(),'started_at':r['started_at'].isoformat(),
                           'ended_at':r['ended_at'].isoformat(),'net_revenue':_money(r['net_revenue']),
                           'confidence':float(r['confidence'])} for r in rows]}

    async def shift_diagnostics(self, date_value: str='вчера') -> dict:
        day=self.resolve_date(date_value)
        async with pool().acquire() as conn:
            totals=await conn.fetchrow(
                """
                SELECT COUNT(*) AS sales_total,
                       COUNT(*) FILTER (WHERE seller_id IS NOT NULL) AS with_seller_id,
                       COUNT(*) FILTER (WHERE seller_name <> '') AS with_seller_name,
                       COUNT(*) FILTER (WHERE seller_id IS NULL AND seller_name='') AS without_seller,
                       COUNT(*) FILTER (WHERE teller_id IS NOT NULL) AS with_teller,
                       COUNT(*) FILTER (WHERE saby_shift_id IS NOT NULL) AS with_shift_id,
                       COUNT(*) FILTER (WHERE saby_shift_number <> '') AS with_shift_number,
                       COUNT(*) FILTER (WHERE check_time_source='Payments.CarriedWTZ') AS carried_time,
                       COUNT(*) FILTER (WHERE check_time_source='Payments.ClosedWTZ') AS closed_time,
                       COUNT(*) FILTER (WHERE check_time_source='Payments.OpenedWTZ') AS opened_time,
                       COUNT(*) FILTER (WHERE check_time_source='DateWTZ') AS datewtz_fallback,
                       COUNT(DISTINCT seller_id) FILTER (WHERE seller_id IS NOT NULL) AS unique_seller_ids,
                       COUNT(DISTINCT NULLIF(seller_name,'')) AS unique_seller_names,
                       MIN(sale_datetime) AS first_check,
                       MAX(sale_datetime) AS last_check
                FROM sales WHERE deleted=FALSE AND (sale_datetime AT TIME ZONE $2)::date=$1
                """,day,settings.business_tz)
            by_store=await conn.fetch(
                """
                SELECT st.name AS store,s.point_id,COUNT(*) AS sales,
                       COUNT(*) FILTER (WHERE s.seller_id IS NOT NULL) AS with_seller_id,
                       COUNT(*) FILTER (WHERE s.seller_name <> '') AS with_seller_name,
                       COUNT(DISTINCT s.seller_id) FILTER (WHERE s.seller_id IS NOT NULL) AS seller_ids,
                       COUNT(DISTINCT NULLIF(s.seller_name,'')) AS seller_names,
                       COUNT(*) FILTER (WHERE s.saby_shift_id IS NOT NULL) AS with_shift,
                       MIN(s.sale_datetime) AS first_check,
                       MAX(s.sale_datetime) AS last_check
                FROM sales s JOIN stores st ON st.point_id=s.point_id
                WHERE s.deleted=FALSE AND (s.sale_datetime AT TIME ZONE $2)::date=$1
                GROUP BY st.name,s.point_id ORDER BY st.name
                """,day,settings.business_tz)
            check_dates=await conn.fetch(
                """
                SELECT (sale_datetime AT TIME ZONE $1)::date AS local_date,
                       COUNT(*) AS sales,
                       COUNT(*) FILTER (WHERE seller_id IS NOT NULL OR seller_name <> '') AS seller_identified
                FROM sales
                WHERE deleted=FALSE AND sale_datetime IS NOT NULL
                GROUP BY local_date ORDER BY local_date
                """,settings.business_tz)
            document_dates=await conn.fetch(
                """
                SELECT (order_datetime AT TIME ZONE $1)::date AS local_date, COUNT(*) AS sales
                FROM sales
                WHERE deleted=FALSE AND order_datetime IS NOT NULL
                GROUP BY local_date ORDER BY local_date
                """,settings.business_tz)
            shift_dates=await conn.fetch(
                """
                SELECT work_date,COUNT(*) AS shifts,
                       COUNT(*) FILTER (WHERE status='AUTO') AS auto,
                       COUNT(*) FILTER (WHERE status='REVIEW') AS review,
                       COUNT(*) FILTER (WHERE status='AMBIGUOUS') AS ambiguous
                FROM seller_shifts GROUP BY work_date ORDER BY work_date
                """)
        def iso(v):
            return v.isoformat() if v else None
        return {
            'date':day.isoformat(),
            'timezone':settings.business_tz,
            'totals':{**dict(totals),'first_check':iso(totals['first_check']),'last_check':iso(totals['last_check'])},
            'by_store':[{**dict(r),'first_check':iso(r['first_check']),'last_check':iso(r['last_check'])} for r in by_store],
            'check_dates':[{'date':r['local_date'].isoformat(),'sales':r['sales'],'seller_identified':r['seller_identified']} for r in check_dates],
            'document_dates':[{'date':r['local_date'].isoformat(),'sales':r['sales']} for r in document_dates],
            'shift_dates':[{**dict(r),'work_date':r['work_date'].isoformat()} for r in shift_dates]
        }


analytics_service=AnalyticsService()
