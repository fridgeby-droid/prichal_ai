"""Deterministic local reports: item comparisons, customer card, payment anomalies."""
import json
from datetime import date,datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
from app.config import get_settings
from app.db.database import pool
from app.services.detail_reports import report,number
from app.services.period_reports import point_filter
from app.services.report_helpers import compare_products,quantity
from app.services.management_reports import money

NOTE='Локальная история; полнота Saby не подтверждена. Учётка Saby не доказывает фактического сотрудника.'


def dates(start,end):
    first,last=date.fromisoformat(start),date.fromisoformat(end)
    if first>last or last>datetime.now(ZoneInfo(get_settings().business_tz)).date():
        raise ValueError('Некорректный период')
    return first,last


async def product_comparison(start,end,previous_start,previous_end,point_id='',seller_id='',shift='',query='',match='contains',modifiers=False,offset=0):
    a,z=dates(start,end);b,y=dates(previous_start,previous_end)
    if offset<0: raise ValueError('offset должен быть неотрицательным')
    kind='modifiers' if modifiers else 'quantities'
    current=await report(kind,start,end,point_id,seller_id,shift,query,match=match)
    previous=await report(kind,previous_start,previous_end,point_id,seller_id,shift,query,match=match)
    rows=compare_products(current['store_product_totals'],previous['store_product_totals'],(z-a).days+1,(y-b).days+1)
    totals=compare_products(current['product_totals'],previous['product_totals'],(z-a).days+1,(y-b).days+1)
    lines=[f'Сравнение товаров: {previous_start}…{previous_end} → {start}…{end}',
           'База → текущий период. Нет записи — н/д, не доказанный ноль. Возвраты отдельно.']
    if (z-a)!=(y-b): lines.append('Периоды разной длины: учитывайте количество в среднем за календарный день.')
    for r in rows[offset:offset+50]:
        lines.append(f"Точка {r['point_id']}: {r['name']} [{r['product_key']}; {r['unit']}]: "
            f"количество {quantity(r['quantity_previous'])} → {quantity(r['quantity_current'])}; Δ {quantity(r['quantity_delta'])}; "
            f"сумма {money(r['revenue_previous'])} → {money(r['revenue_current'])} ₽; "
            f"средняя цена {money(r['average_unit_price_previous'])} → {money(r['average_unit_price_current'])}; "
            f"возврат количества {quantity(r['return_quantity_previous'])} → {quantity(r['return_quantity_current'])}; "
            f"сумма возвратных строк {money(r['return_revenue_previous'])} → {money(r['return_revenue_current'])}; "
            f"в день {quantity(r['quantity_per_day_previous'])} → {quantity(r['quantity_per_day_current'])}; "
            f"доля покупок {money(r['purchase_share_percent_previous'])}% → {money(r['purchase_share_percent_current'])}%")
    next_offset=offset+50 if offset+50<len(rows) else None
    lines.extend([f'Групп: {len(rows)}; следующая страница offset={next_offset}.',NOTE])
    return dict(rows=rows,product_totals=totals,next_offset=next_offset,report_text='\n'.join(lines),complete_source=False,
                date_from=start,date_to=end,previous_start=previous_start,previous_end=previous_end)


async def customer_card(customer_id,end,offset=0):
    cid=number(customer_id)
    if cid is None or offset<0: raise ValueError('Укажите положительный ID клиента и offset')
    _,last=dates(end,end)
    async with pool().acquire() as conn:
        async with conn.transaction(isolation='repeatable_read',readonly=True):
            summary=dict(await conn.fetchrow('''SELECT COUNT(*) purchases,
                COUNT(DISTINCT business_date) visit_days,MIN(business_date) first_purchase,
                MAX(business_date) last_purchase,SUM(total_price) amount,
                AVG(total_price) average_sale,COUNT(DISTINCT point_id) stores
                FROM retail_sales WHERE NOT deleted AND NOT is_return
                  AND customer_id=$1 AND business_date<=$2''',cid,last))
            stores=[dict(r) for r in await conn.fetch('''SELECT s.point_id,MAX(t.name) store_name,
                COUNT(*) purchases,COUNT(DISTINCT s.business_date) visit_days,SUM(s.total_price) amount,
                MIN(s.business_date) first_purchase,MAX(s.business_date) last_purchase
                FROM retail_sales s JOIN retail_stores t USING(point_id)
                WHERE NOT s.deleted AND NOT s.is_return AND s.customer_id=$1 AND s.business_date<=$2
                GROUP BY s.point_id ORDER BY s.point_id''',cid,last)]
            bonuses=dict(await conn.fetchrow('''SELECT COUNT(*) eligible_purchases,
                COUNT(*) FILTER(WHERE b.status='OK' AND b.fingerprint=md5(s.raw_json::text)) current_responses,
                SUM((b.response->>'bonusDec')::numeric) FILTER(WHERE b.status='OK' AND b.fingerprint=md5(s.raw_json::text)) known_debit,
                SUM((b.response->>'bonusInc')::numeric) FILTER(WHERE b.status='OK' AND b.fingerprint=md5(s.raw_json::text)) known_to_accrue,
                MAX(b.attempted_at) last_attempt
                FROM retail_sales s LEFT JOIN bonus_observations b USING(point_id,sale_id)
                WHERE NOT s.deleted AND NOT s.is_return AND s.customer_id=$1 AND s.business_date<=$2''',cid,last))
    for r in stores: r['point_id']=str(r['point_id'])
    summary['days_since_purchase']=(last-summary['last_purchase']).days if summary['last_purchase'] else None
    summary['mean_days_between_visits']=(Decimal((summary['last_purchase']-summary['first_purchase']).days)/(summary['visit_days']-1)
                                         if summary['visit_days']>1 else None)
    next_offset=offset+50 if offset+50<len(stores) else None
    lines=[f'Клиент {cid}, история по {last}',
        f"Покупок: {summary['purchases']}; дней посещений: {summary['visit_days']}; магазинов: {summary['stores']}",
        f"Первая покупка: {summary['first_purchase']}; последняя: {summary['last_purchase']}; давность: {summary['days_since_purchase']}",
        f"Сумма продаж: {money(summary['amount'])} ₽; средняя сумма продажи: {money(summary['average_sale'])} ₽",
        f"Средний интервал между днями посещений: {money(summary['mean_days_between_visits'])} дней",
        f"Бонусные ответы: {bonuses['current_responses']} из {bonuses['eligible_purchases']}; "
        f"известно списано: {money(bonuses['known_debit'])}; к начислению: {money(bonuses['known_to_accrue'])}",
        'Бонусы — баллы, начисления не подтверждены как проведённые; полный баланс карты не восстанавливается.']
    for r in stores[offset:offset+50]: lines.append(f"{r['store_name']} [{r['point_id']}]: {r['purchases']} покупок; {money(r['amount'])} ₽; последняя {r['last_purchase']}")
    lines.extend([f'Следующая страница магазинов offset={next_offset}',NOTE])
    return dict(customer_id=str(cid),as_of=end,summary=summary,bonuses=bonuses,rows=stores,
                next_offset=next_offset,report_text='\n'.join(lines),complete_source=False)


async def payment_audit(start,end,point_id='',offset=0):
    first,last=dates(start,end)
    if offset<0: raise ValueError('offset должен быть неотрицательным')
    point=await point_filter(point_id)
    async with pool().acquire() as conn:
        rows=[dict(r) for r in await conn.fetch('''SELECT s.point_id,s.sale_id,s.sale_number,
            s.business_date,s.seller_id,s.seller_name,s.is_return sale_is_return,s.total_price,
            COUNT(p.payment_key) payment_rows,
            COUNT(p.payment_key) FILTER(WHERE NOT p.is_return) nonreturn_rows,
            COUNT(p.payment_key) FILTER(WHERE p.is_return) return_rows,
            SUM(CASE WHEN p.is_return THEN -ABS(p.amount) ELSE ABS(p.amount) END) signed_payments,
            jsonb_agg(jsonb_build_object('payment_key',p.payment_key,'amount',p.amount,
                'is_return',p.is_return,'source',p.source,'business_date',p.business_date,
                'seller_id',p.seller_id)) FILTER(WHERE p.payment_key IS NOT NULL) payments
            FROM retail_sales s LEFT JOIN retail_sale_payments p USING(point_id,sale_id)
            WHERE NOT s.deleted AND (s.business_date BETWEEN $1 AND $2 OR EXISTS(
                SELECT 1 FROM retail_sale_payments matched WHERE matched.point_id=s.point_id
                AND matched.sale_id=s.sale_id AND matched.business_date BETWEEN $1 AND $2))
              AND ($3::bigint IS NULL OR s.point_id=$3)
            GROUP BY s.point_id,s.sale_id,s.sale_number,s.business_date,s.seller_id,
                s.seller_name,s.is_return,s.total_price
            HAVING COUNT(p.payment_key)<>1 OR BOOL_OR(p.is_return IS DISTINCT FROM s.is_return)
              OR BOOL_OR(p.business_date IS DISTINCT FROM s.business_date)
              OR BOOL_OR(p.seller_id IS DISTINCT FROM s.seller_id)
            ORDER BY s.point_id,s.sale_id''',first,last,point)]
    for row in rows:
        for k in ('point_id','sale_id','seller_id'):
            if row[k] is not None: row[k]=str(row[k])
        if isinstance(row['payments'],str): row['payments']=json.loads(row['payments'])
    lines=[f'Диагностика оплат продаж: {start}…{end}; точка {point or "сеть"}',
           'Период по дате продажи ИЛИ оплаты. Выбраны продажи с несколькими/отсутствующими оплатами или различиями даты, продавца, возврата. Это кандидаты для проверки, не доказанные дубли.']
    for r in rows[offset:offset+50]:
        lines.append(f"Точка {r['point_id']}, продажа ID {r['sale_id']}, номер {r['sale_number']}: "
            f"{r['payment_rows']} оплат ({r['nonreturn_rows']} обычных, {r['return_rows']} возвратных); "
            f"TotalPrice {money(r['total_price'])}; платежи с учётом возвратов {money(r['signed_payments'])}; "
            f"продавец {r['seller_name']} [{r['seller_id']}]")
        for pay in (r['payments'] or []):
            lines.append(f"  Оплата {pay['payment_key']}: {money(pay['amount'])}; возврат={pay['is_return']}; дата={pay['business_date']}; учётка={pay['seller_id']}; источник={pay['source']}")
    next_offset=offset+50 if offset+50<len(rows) else None
    lines.extend([f'Продаж для проверки: {len(rows)}; следующая страница offset={next_offset}',NOTE])
    return dict(rows=rows,total=len(rows),next_offset=next_offset,report_text='\n'.join(lines),complete_source=False)
