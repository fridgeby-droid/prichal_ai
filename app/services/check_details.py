"""Read-only purchase search and cards. Never expose raw JSON or customer contacts."""
from datetime import datetime
from zoneinfo import ZoneInfo
from app.config import get_settings
from app.db.database import pool
from app.services.period_reports import period, point_filter
from app.services.check_amounts import parameters
from app.services.detail_reports import number

FIELDS = '''s.point_id,s.sale_id,COALESCE(NULLIF(s.raw_json->>'Number',''),s.sale_number) number,
 st.name store,s.sale_datetime,s.business_date,s.business_shift_type shift,
 s.seller_id,s.seller_name,s.total_price,s.is_return'''
BASE = ' FROM retail_sales s LEFT JOIN retail_stores st ON st.point_id=s.point_id '
NOTE = 'Одна покупка — point_id + sale_id, сумма TotalPrice. Платёжные записи не прибавлять к сумме покупки. Учётка Saby не доказывает фактического сотрудника. РЦ исключён.'


def serial(row):
    result = dict(row)
    for key in ('point_id','sale_id','seller_id','product_id'):
        if key in result and result[key] is not None: result[key] = str(result[key])
    if isinstance(result.get('sale_datetime'), datetime):
        result['sale_datetime'] = result['sale_datetime'].astimezone(ZoneInfo(get_settings().business_tz)).isoformat()
    return result


async def search(start, end, point_id='', minimum='', maximum='', seller_id='', shift='',
                 product='', sale_number='', date_basis='business', returns=False,
                 order='amount_desc', offset=0, limit=20):
    first,last=period(start,end)
    point=await point_filter(point_id)
    seller=number(seller_id)
    lower=parameters(minimum,'gte')[0] if minimum else None
    upper=parameters(maximum,'lte')[0] if maximum else None
    if lower is not None and upper is not None and lower>upper: raise ValueError('Нижняя сумма больше верхней.')
    if shift not in ('','DAY','NIGHT'): raise ValueError('Смена DAY или NIGHT.')
    if date_basis not in ('business','calendar'): raise ValueError('Дата business или calendar.')
    orders={'amount_desc':'s.total_price DESC NULLS LAST','amount_asc':'s.total_price ASC NULLS LAST',
            'recent':'s.sale_datetime DESC NULLS LAST'}
    if order not in orders or not 1<=limit<=50 or not 0<=offset<=10000: raise ValueError('Некорректная сортировка/страница.')
    if len(product)>200 or len(sale_number)>100: raise ValueError('Слишком длинный поиск.')
    day='s.business_date' if date_basis=='business' else '(s.sale_datetime AT TIME ZONE $11)::DATE'
    where=f'''WHERE NOT s.deleted AND s.point_id<>23109 AND {day} BETWEEN $1 AND $2
        AND ($3::BIGINT IS NULL OR s.point_id=$3)
        AND ($4::NUMERIC IS NULL OR s.total_price >= $4)
        AND ($5::NUMERIC IS NULL OR s.total_price <= $5)
        AND ($6::BIGINT IS NULL OR s.seller_id=$6)
        AND ($7::TEXT='' OR s.business_shift_type=$7)
        AND ($8::TEXT='' OR EXISTS(SELECT 1 FROM retail_sale_items i
            WHERE i.point_id=s.point_id AND i.sale_id=s.sale_id AND NOT i.refused
            AND POSITION(LOWER($8) IN LOWER(i.name))>0))
        AND ($9::TEXT='' OR COALESCE(NULLIF(s.raw_json->>'Number',''),s.sale_number)=$9)
        AND s.is_return=$10 AND $11::TEXT IS NOT NULL'''
    args=(first,last,point,lower,upper,seller,shift,product,sale_number,returns,get_settings().business_tz)
    async with pool().acquire() as conn:
        async with conn.transaction(isolation='repeatable_read',readonly=True):
            total=await conn.fetchval('SELECT COUNT(*)'+BASE+where,*args)
            rows=await conn.fetch('SELECT '+FIELDS+BASE+where+
                f' ORDER BY {orders[order]},s.point_id,s.sale_id LIMIT $12 OFFSET $13',*args,limit,offset)
    return dict(point_id=str(point) if point else None,date_from=str(first),date_to=str(last),
                date_basis=date_basis,minimum=minimum or None,maximum=maximum or None,
                seller_id=seller_id or None,shift=shift or None,product=product,number=sale_number,
                returns_only=returns,order=order,total_matches=total,rows=[serial(r) for r in rows],
                next_offset=offset+limit if offset+limit<total else None,
                status='FOUND' if total else 'NOT_FOUND',complete_source=False,definition=NOTE)


async def card(point_id,sale_id,offset=0):
    point=await point_filter(point_id)
    sale=number(sale_id)
    if point is None or sale is None or not 0<=offset<=10000: raise ValueError('Нужны точные ID магазина и продажи, корректная страница.')
    async with pool().acquire() as conn:
        async with conn.transaction(isolation='repeatable_read',readonly=True):
            row=await conn.fetchrow('SELECT '+FIELDS+BASE+
                'WHERE s.point_id=$1 AND s.sale_id=$2 AND NOT s.deleted AND s.point_id<>23109',point,sale)
            if not row: return dict(point_id=str(point),status='NOT_FOUND',complete_source=False)
            items=await conn.fetch('''SELECT item_key,product_id,name,quantity,unit_name,total_price,
                is_return,refused FROM retail_sale_items WHERE point_id=$1 AND sale_id=$2
                ORDER BY item_key LIMIT 51 OFFSET $3''',point,sale,offset)
            payments=await conn.fetch('''SELECT payment_key,check_number,carried_at,amount,signed_amount,
                is_return,nonfiscal,source FROM retail_sale_payments WHERE point_id=$1 AND sale_id=$2
                ORDER BY payment_key LIMIT 51 OFFSET $3''',point,sale,offset)
    return dict(point_id=str(point),status='FOUND',purchase=serial(row),items=[serial(r) for r in items[:50]],
                payments=[serial(r) for r in payments[:50]],
                next_offset=offset+50 if len(items)>50 or len(payments)>50 else None,
                complete_source=False,definition=NOTE,
                note='Товары и оплаты показаны отдельно. Бонусы не выводятся из скидок. Номер продажи и check_number оплаты могут различаться.')
