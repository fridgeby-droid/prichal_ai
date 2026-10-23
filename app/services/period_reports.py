"""Read-only deterministic management reports over the existing retail ledger."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db.database import pool
from app.services.analytics import analytics_service
from app.services.daily_report import totals

PAYMENTS_SQL = """
SELECT p.point_id, p.business_date, p.business_shift_type AS shift_type,
 COUNT(*) AS records,
 COUNT(*) FILTER (WHERE NOT p.is_return) AS checks,
 COUNT(*) FILTER (WHERE p.is_return) AS return_checks,
 COALESCE(SUM(CASE WHEN p.is_return THEN 0 ELSE ABS(p.amount) END),0) AS revenue,
 COALESCE(SUM(CASE WHEN p.is_return THEN ABS(p.amount) ELSE 0 END),0) AS returns,
 COUNT(*) FILTER (WHERE p.source='sale_total_fallback') AS fallback
FROM retail_sale_payments p JOIN retail_sales s
 ON s.point_id=p.point_id AND s.sale_id=p.sale_id
WHERE NOT s.deleted AND p.business_date BETWEEN $1 AND $2
 AND ($3::BIGINT IS NULL OR p.point_id=$3)
GROUP BY p.point_id,p.business_date,p.business_shift_type
"""

PRODUCTS_SQL = """
SELECT CASE WHEN NULLIF(i.product_uuid,'') IS NOT NULL THEN 'uuid:'||i.product_uuid
 WHEN i.product_id IS NOT NULL THEN 'id:'||i.product_id::TEXT
 ELSE 'unidentified:'||i.point_id::TEXT||':'||i.sale_id::TEXT||':'||i.item_key END AS product_key,
 MAX(i.name) AS name, i.unit_name AS unit,
 SUM(CASE WHEN i.is_return THEN 0 ELSE i.quantity END) AS quantity,
 SUM(CASE WHEN i.is_return THEN 0 ELSE i.total_price END) AS revenue,
 SUM(CASE WHEN i.is_return THEN ABS(i.total_price) ELSE 0 END) AS returns
FROM retail_sale_items i JOIN retail_sales s
 ON s.point_id=i.point_id AND s.sale_id=i.sale_id
WHERE NOT s.deleted AND NOT i.refused AND s.business_date BETWEEN $1 AND $2
 AND ($3::BIGINT IS NULL OR i.point_id=$3)
GROUP BY product_key,i.unit_name
"""


def period(start, end):
    start, end = date.fromisoformat(start), date.fromisoformat(end)
    today = datetime.now(ZoneInfo(get_settings().business_tz)).date()
    if start > end or (end-start).days >= 92 or end > today:
        raise ValueError('Укажите период YYYY-MM-DD: от 1 до 92 дней, без будущих дат.')
    return start, end


def delta(current, previous):
    current, previous = Decimal(str(current)), Decimal(str(previous))
    return {'absolute': current-previous,
            'percent': (current-previous)/previous*100 if previous else None}


async def stores(query=''):
    async with pool().acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(
            'SELECT point_id,name,address,locality FROM retail_stores ORDER BY name,point_id')]
    q = query.strip().casefold()
    exact = [r for r in rows if str(r['point_id']) == q]
    rows = exact or [r for r in rows if not q or q in r['name'].casefold() or q in (r['address'] or '').casefold()]
    return {'source': 'postgresql', 'stores': [{**r, 'point_id': str(r['point_id'])} for r in rows],
            'ambiguous': bool(q and len(rows)>1), 'excluded_points': ['23109']}


async def point_filter(point_id):
    if not point_id:
        return None
    if not point_id.isdecimal() or not 0 < int(point_id) < 2**63:
        raise ValueError('Нужен точный point_id из list_stores.')
    found = await stores(point_id)
    if not any(s['point_id']==point_id for s in found['stores']):
        raise ValueError('Магазин не найден или исключён из розничной аналитики.')
    return int(point_id)


def quality(rows, store_rows, start, end, latest, success):
    observed = {(r['point_id'], r['business_date']) for r in rows}
    days = [start+timedelta(days=i) for i in range((end-start).days+1)]
    missing = [{'point_id': str(s['point_id']), 'dates': [d.isoformat() for d in days
                 if (s['point_id'],d) not in observed]} for s in store_rows]
    settings = get_settings()
    current = (datetime.now(ZoneInfo(settings.business_tz)) - timedelta(hours=settings.business_day_start_hour)).date()
    stamp = success.get('finished_at') if success else None
    age = (datetime.now(ZoneInfo(settings.business_tz))-stamp).total_seconds()/3600 if stamp else None
    return {'period_closed': end < current, 'completeness': 'UNCONFIRMED',
            'last_success_age_hours': round(age,2) if age is not None else None,
            'reconciliation': 'NOT_CHECKED', 'last_run': latest,
            'last_success_overlapping_period': success,
            'store_dates_without_records': [m for m in missing if m['dates']],
            'fallback_records': sum(r['fallback'] for r in rows),
            'note': 'Отсутствие записей не означает нулевые продажи. Успешный sync не доказывает полноту всех точек. Для сверки вызовите get_load_reconcile_status.'}


async def snapshot(start, end, point_id=''):
    start, end = period(start, end)
    point = await point_filter(point_id)
    async with pool().acquire() as conn:
        async with conn.transaction(isolation='repeatable_read', readonly=True):
            shops = [dict(r) for r in await conn.fetch(
                'SELECT point_id,name FROM retail_stores WHERE ($1::BIGINT IS NULL OR point_id=$1) ORDER BY name', point)]
            rows = [dict(r) for r in await conn.fetch(PAYMENTS_SQL, start, end, point)]
            latest = await conn.fetchrow('SELECT status,started_at,finished_at,date_from,date_to FROM sync_runs ORDER BY id DESC LIMIT 1')
            success = await conn.fetchrow("SELECT status,finished_at,date_from,date_to FROM sync_runs WHERE status='OK' AND date_from<=$2 AND date_to>=$1 ORDER BY finished_at DESC LIMIT 1", start, end)
    return start, end, rows, shops, quality(rows, shops, start, end,
        dict(latest) if latest else None, dict(success) if success else None)


def envelope(start, end, q):
    settings = get_settings()
    return {'date_from': start.isoformat(), 'date_to': end.isoformat(),
            'timezone': settings.business_tz, 'business_day_start_hour': settings.business_day_start_hour,
            'generated_at': datetime.now(ZoneInfo(settings.business_tz)).isoformat(),
            'source': 'postgresql_payment_ledger', 'quality': q,
            'definitions': 'Выручка без вычитания возвратов. Чеки — записи платёжного слоя Saby, не уникальные sale_id. DAY/NIGHT по времени платежа, не графику сотрудника. РЦ исключён.'}


async def summary(start, end, point_id=''):
    start,end,rows,shops,q = await snapshot(start,end,point_id)
    return {**envelope(start,end,q), 'totals': totals(rows), 'stores': [
        {'point_id': str(s['point_id']), 'name': s['name'], **totals([r for r in rows if r['point_id']==s['point_id']])}
        for s in shops]}


async def shifts(start, end, point_id=''):
    start,end,rows,shops,q = await snapshot(start,end,point_id)
    def split(selected):
        return {label: totals([r for r in selected if (r['shift_type'] or 'UNKNOWN')==label])
                for label in ('DAY','NIGHT','UNKNOWN')}
    return {**envelope(start,end,q), 'shifts': split(rows), 'stores': [
        {'point_id':str(s['point_id']), 'name':s['name'], 'shifts':split([r for r in rows if r['point_id']==s['point_id']])}
        for s in shops]}


async def compare(start, end, previous_start, previous_end, point_id=''):
    current = await summary(start,end,point_id)
    previous = await summary(previous_start,previous_end,point_id)
    fields = ('revenue','checks','average','returns','return_checks')
    def changes(a,b):
        return {key: delta(a[key],b[key]) for key in fields}
    old = {s['point_id']:s for s in previous['stores']}
    rows=[]
    for s in current['stores']:
        prior = old.get(s['point_id'])
        rows.append({'point_id':s['point_id'], 'name':s['name'],
                     'has_records_both_periods':bool(prior and prior['records'] and s['records']),
                     'changes':changes(s,prior) if prior and prior['records'] and s['records'] else None})
    return {'current':current, 'previous':previous,
            'changes':changes(current['totals'],previous['totals']) if current['totals']['records'] and previous['totals']['records'] else None,
            'store_changes':rows, 'equal_duration':(date.fromisoformat(end)-date.fromisoformat(start)) == (date.fromisoformat(previous_end)-date.fromisoformat(previous_start)),
            'note':'Сравнение доступных записей, не доказательство причин. Проверьте качество, одинаковую длину периодов и состав работающих магазинов. Процент при нулевой базе — null.'}


async def products(start, end, point_id='', previous_start='', previous_end='', limit=20):
    start,end = period(start,end)
    if bool(previous_start) != bool(previous_end):
        raise ValueError('Для сравнения нужны обе даты предыдущего периода.')
    before = period(previous_start,previous_end) if previous_start else None
    point = await point_filter(point_id)
    limit = max(1,min(int(limit),50))
    async with pool().acquire() as conn:
        async with conn.transaction(isolation='repeatable_read', readonly=True):
            current = [dict(r) for r in await conn.fetch(PRODUCTS_SQL,start,end,point)]
            previous = [dict(r) for r in await conn.fetch(PRODUCTS_SQL,*before,point)] if before else []
    a = {(r['product_key'],r['unit']):r for r in current}
    b = {(r['product_key'],r['unit']):r for r in previous}
    result=[]
    for key in a.keys() | b.keys():
        x,y = a.get(key),b.get(key)
        row = dict(x or y)
        row.update(current=x, previous=y, changes=delta(x['revenue'] if x else 0,y['revenue'] if y else 0) if before else None)
        result.append(row)
    result.sort(key=lambda r: (-abs(r['changes']['absolute']) if before else -r['revenue'],r['product_key'],r['unit']))
    return {'date_from':start,'date_to':end,'point_id':point_id or None,
            'previous_from':previous_start or None,'previous_to':previous_end or None,
            'source':'sale_items.total_price; бизнес-дата продажи',
            'categories_available':False, 'quality':{'completeness':'UNCONFIRMED','reconciliation':'NOT_CHECKED'},
            'products':result[:limit], 'total_groups':len(result),'truncated':len(result)>limit,
            'note':'Это суммы товарных строк, не платёжная выручка: возможны расхождения и различия дат. Категории в этом ответе отсутствуют: используйте get_category_sales. Единицы разделены; неопознанные товары не склеиваются по названию. Без записей в периоде нельзя утверждать отсутствие продаж. Для проверки используйте get_load_reconcile_status.'}


async def status(start, end, point_id=''):
    # Reconciliation reconstructs expected shifts: keep the explicit check bounded.
    first,last = period(start,end)
    if (last-first).days >= 7:
        raise ValueError('Сверку запрашивайте блоками не более 7 дней.')
    first,last,rows,shops,q = await snapshot(start,end,point_id)
    checks=[]
    for offset in range((last-first).days+1):
        day = first+timedelta(days=offset)
        result = await analytics_service.reconcile(day.isoformat())
        selected = [r for r in result['stores'] if not point_id or str(r['point_id'])==point_id]
        checks.append({'date':day.isoformat(), 'stores':[{
            k:r[k] for k in ('point_id','store','status','expected_vs_work','fallback_payments','blocking_review_shifts')}
            for r in selected], 'status':'NO_DATA' if not selected else 'REVIEW' if any(r['status']!='OK' for r in selected) else 'OK'})
    q['reconciliation'] = 'REVIEW' if any(c['status']=='REVIEW' for c in checks) else 'NO_DATA' if any(c['status']=='NO_DATA' for c in checks) else 'OK'
    return {**envelope(first,last,q), 'checks':checks,
            'note':'Сверка проверяет согласованность локальных данных, а не полноту источника Saby. Отсутствующие магазин-даты требуют отдельной проверки.'}
