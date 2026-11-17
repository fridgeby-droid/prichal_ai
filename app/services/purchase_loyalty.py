"""Bounded-memory purchase/loyalty analysis over saved observations."""
import json
from decimal import Decimal
from app.db.database import pool
from app.services.period_reports import period, point_filter
from app.services.check_amounts import parameters
from app.services.detail_reports import number
from app.services.sale_tree import numeric


def classify(row):
    if row['customer_id'] is None:
        return 'without_card', None, None
    if row['status'] != 'OK' or not row['fresh']:
        return 'unknown', None, None
    body=row['response']
    try:
        if isinstance(body,str): body=json.loads(body)
    except (ValueError,TypeError): return 'unknown',None,None
    if not isinstance(body,dict): return 'unknown',None,None
    card=any(str(body.get(k) or '').isdecimal() and int(body[k])>0 for k in ('cardId','personalCardId'))
    inc,dec=numeric(body.get('bonusInc')),numeric(body.get('bonusDec'))
    return ('with_card' if card else 'unknown'),inc,dec


def empty():
    return dict(purchases=0,amount=Decimal(0),known_amount_count=0,unknown_amount_count=0,
                bonus_to_accrue=Decimal(0),bonus_spent=Decimal(0),known_inc=0,known_dec=0)


def add(group,row,inc,dec):
    group['purchases']+=1
    amount=numeric(row['total_price'])
    if amount is None: group['unknown_amount_count']+=1
    else:
        group['amount']+=amount;group['known_amount_count']+=1
    for value,key,count in ((inc,'bonus_to_accrue','known_inc'),(dec,'bonus_spent','known_dec')):
        if value is not None:
            group[key]+=value;group[count]+=1


def finish(group,total_count,total_amount):
    return {**group,'average':group['amount']/group['known_amount_count'] if group['known_amount_count'] else None,
            'purchase_share_percent':Decimal(group['purchases'])/total_count*100 if total_count else None,
            'amount_share_percent':group['amount']/total_amount*100 if total_amount else None,
            'bonus_to_accrue':group['bonus_to_accrue'] if group['known_inc'] else None,
            'bonus_spent':group['bonus_spent'] if group['known_dec'] else None}


async def report(start,end,point_id='',amount='',comparison='gt',loyalty='all',
                 operation='all',seller_id='',shift='',offset=0):
    first,last=period(start,end);point=await point_filter(point_id);seller=number(seller_id)
    value,op=parameters(amount or '0',comparison)
    if loyalty not in ('all','with_card','without_card','unknown'): raise ValueError('Неизвестная группа карт.')
    if operation not in ('all','accrual','debit'): raise ValueError('Операция all/accrual/debit.')
    if shift not in ('','DAY','NIGHT') or not 0<=offset<=10000: raise ValueError('Некорректная смена/страница.')
    sql=f'''SELECT s.point_id,s.sale_id,s.sale_number,s.business_date,s.total_price,
        s.customer_id,s.seller_id,s.seller_name,b.status,b.response,
        (b.fingerprint=md5(s.raw_json::text)) fresh
        FROM retail_sales s LEFT JOIN bonus_observations b USING(point_id,sale_id)
        WHERE NOT s.deleted AND NOT s.is_return AND s.point_id<>23109
        AND s.business_date BETWEEN $1 AND $2 AND ($3::BIGINT IS NULL OR s.point_id=$3)
        AND ($4::BOOLEAN OR s.total_price {op} $5)
        AND ($6::BIGINT IS NULL OR s.seller_id=$6)
        AND ($7::TEXT='' OR s.business_shift_type=$7)
        ORDER BY s.total_price DESC NULLS LAST,s.point_id,s.sale_id'''
    groups={key:empty() for key in ('with_card','without_card','unknown')}
    selected=empty(); examples=[]
    async with pool().acquire() as conn:
        async with conn.transaction(isolation='repeatable_read',readonly=True):
            async for row in conn.cursor(sql,first,last,point,not bool(amount),value,seller,shift,prefetch=200):
                kind,inc,dec=classify(row)
                add(groups[kind],row,inc,dec)
                if loyalty!='all' and kind!=loyalty: continue
                if operation=='accrual' and (inc is None or inc<=0): continue
                if operation=='debit' and (dec is None or dec<=0): continue
                add(selected,row,inc,dec)
                if offset < selected['purchases'] <= offset+20:
                    examples.append(dict(point_id=str(row['point_id']),sale_id=str(row['sale_id']),
                        number=row['sale_number'],business_date=str(row['business_date']),
                        amount=row['total_price'],seller_id=str(row['seller_id']) if row['seller_id'] else None,
                        seller_name=row['seller_name'],loyalty=kind,bonus_to_accrue=inc,bonus_spent=dec))
    total_count=sum(g['purchases'] for g in groups.values())
    total_amount=sum(g['amount'] for g in groups.values())
    return dict(point_id=str(point) if point else None,date_from=str(first),date_to=str(last),
        amount_threshold=amount or None,comparison=comparison,loyalty=loyalty,operation=operation,
        total_in_amount_scope=total_count,groups={k:finish(g,total_count,total_amount) for k,g in groups.items()},
        selected=finish(selected,total_count,total_amount),rows=examples,
        next_offset=offset+20 if selected['purchases']>offset+20 else None,
        status='OK' if total_count else 'NO_LOCAL_RECORDS',complete_source=False,
        definitions='Суммы покупок TotalPrice, не платёжная выручка. Доли от всех покупок в выбранном периоде/суммовом фильтре; группы до фильтра операции. '
        'Без Customer — без карты по правилу владельца. Карта подтверждена только свежим OK ответом с ID карты. '
        'Ошибки, устаревшие/неподдерживаемые/отсутствующие ответы и ответ без ID карты — unknown. '
        'bonusInc — к начислению, не факт проведённого начисления. Баллы не рубли, неизвестное не ноль. '
        'Операции фильтруются по известному положительному bonusInc/bonusDec. Возвраты и РЦ исключены. '
        'Список — 20 покупок по сумме убыванию; ID карт и контакты не выдаются.')
