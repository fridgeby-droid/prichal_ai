"""Counts unique Saby purchases by TotalPrice, never item/payment row counts."""
from decimal import Decimal, InvalidOperation
from app.db.database import pool
from app.services.period_reports import period, point_filter

OPERATORS = {'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<=', 'eq': '='}


def parameters(amount, comparison):
    if comparison not in OPERATORS:
        raise ValueError('Сравнение: gt, gte, lt, lte, eq.')
    try:
        value = Decimal(str(amount).replace(',', '.'))
    except InvalidOperation:
        raise ValueError('Укажите сумму числом.') from None
    if not value.is_finite() or value < 0 or value > Decimal('1000000000000'):
        raise ValueError('Некорректная сумма.')
    return value, OPERATORS[comparison]


async def report(start, end, amount='500', comparison='gt', point_id=''):
    value, operator = parameters(amount, comparison)
    first, last = period(start, end)
    point = await point_filter(point_id)
    sql = f'''SELECT s.point_id, MAX(st.name) name,
        COUNT(*) total_purchases,
        COUNT(*) FILTER (WHERE s.total_price IS NULL) unknown_amount,
        COUNT(*) FILTER (WHERE s.total_price {operator} $4) matched_purchases
        FROM retail_sales s LEFT JOIN retail_stores st ON st.point_id=s.point_id
        WHERE NOT s.deleted AND NOT s.is_return AND s.point_id<>23109
        AND s.business_date BETWEEN $1 AND $2
        AND ($3::BIGINT IS NULL OR s.point_id=$3)
        GROUP BY s.point_id ORDER BY s.point_id'''
    async with pool().acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(sql, first, last, point, value)]
    total = sum(r['total_purchases'] for r in rows)
    matched = sum(r['matched_purchases'] for r in rows)
    unknown = sum(r['unknown_amount'] for r in rows)
    for row in rows:
        row['point_id'] = str(row['point_id'])
    return dict(date_from=str(first), date_to=str(last), point_id=str(point) if point else None,
                amount=str(value), comparison=comparison, total_purchases=total,
                matched_purchases=matched if total else None, unknown_amount=unknown,
                stores=rows, complete_source=False,
                definition='Одна покупка = point_id + sale_id; сумма retail_sales.total_price. '
                'Возвраты, удалённые продажи и РЦ исключены. Не число платёжных записей или фискальных документов.',
                status='OK' if total else 'NO_LOCAL_RECORDS')
