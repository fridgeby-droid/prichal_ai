"""Immutable observations of one explicitly mapped company/warehouse/product scope."""
import json
from datetime import datetime,timezone,time,timedelta
from zoneinfo import ZoneInfo
from uuid import uuid4
from app.config import get_settings
from app.db.database import pool
from app.services.price_lists import identifier
from app.services.sale_tree import numeric
from app.services.extra_reports import dates
from app.services.period_reports import point_filter


DDL='''CREATE TABLE IF NOT EXISTS inventory_observations (
 id text PRIMARY KEY, point_id bigint NOT NULL, company_id text NOT NULL,
 warehouse_id text NOT NULL, observed_at timestamptz NOT NULL, payload jsonb NOT NULL);
 CREATE INDEX IF NOT EXISTS inventory_observations_scope
 ON inventory_observations(point_id,company_id,warehouse_id,observed_at DESC);'''


async def save(point_id,snapshot,units):
    point=await point_filter(str(point_id))
    if point is None: raise ValueError('Нужен магазин для привязки склада')
    if snapshot.get('complete_requested_scope') is not True: raise ValueError('INCOMPLETE_STOCK_SNAPSHOT')
    amounts=snapshot.get('balances')
    if not isinstance(amounts,dict) or not amounts or set(amounts)!=set(units):
        raise ValueError('Нужны единицы для всех запрошенных ID')
    for pid,amount in amounts.items():
        identifier(pid)
        if numeric(amount) is None or not isinstance(units[pid],str) or not units[pid].strip():
            raise ValueError('STOCK_UNIT_OR_AMOUNT_INVALID')
    stamp=datetime.fromisoformat(snapshot['observed_at'])
    if stamp.tzinfo is None or stamp>datetime.now(timezone.utc)+timedelta(minutes=1):
        raise ValueError('STOCK_TIME_INVALID')
    company=identifier(snapshot['company_id']);warehouse=identifier(snapshot['warehouse_id'])
    uid=str(uuid4());payload={**snapshot,'units':units}
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(DDL)
            await conn.execute('''INSERT INTO inventory_observations
                (id,point_id,company_id,warehouse_id,observed_at,payload) VALUES($1,$2,$3,$4,$5,$6::jsonb)''',
                uid,point,company,warehouse,stamp,json.dumps(payload,ensure_ascii=False))
    return {'status':'SAVED','snapshot_id':uid,'products':len(amounts),'point_id':str(point),
        'observed_at':stamp.isoformat(),'all_warehouse_products':False}


def observations(records,only_zero=False):
    rows=[]
    for record in records:
        payload=record['payload'];payload=json.loads(payload) if isinstance(payload,str) else payload
        if payload.get('complete_requested_scope') is not True: continue
        for pid,amount in payload['balances'].items():
            if len(rows)>=100000: raise ValueError('Сузьте период: больше 100000 наблюдений товаров')
            value=numeric(amount)
            if value is None: continue
            if only_zero and value!=0: continue
            rows.append({'point_id':str(record['point_id']),'company_id':record['company_id'],
                'warehouse_id':record['warehouse_id'],'product_key':'id:'+pid,
                'unit':payload['units'][pid],'balance':value,'observed_at':record['observed_at'],
                'availability':'ZERO_OBSERVED' if value==0 else 'NEGATIVE_ANOMALY' if value<0 else 'POSITIVE_OBSERVED',
                'continuous_outage_confirmed':False})
    return rows


async def report(start,end,point_id='',kind='stock',offset=0,export_all=False):
    first,last=dates(start,end);point=await point_filter(point_id)
    if kind not in ('stock','stockouts') or offset<0 or (last-first).days>=93:
        raise ValueError('Некорректный вид/период отчёта остатков')
    tz=ZoneInfo(get_settings().business_tz)
    begin=datetime.combine(first,time.min,tz);finish=datetime.combine(last+timedelta(days=1),time.min,tz)
    async with pool().acquire() as conn:
        exists=await conn.fetchval("SELECT to_regclass('inventory_observations') IS NOT NULL")
        records=await conn.fetch('''SELECT point_id,company_id,warehouse_id,observed_at,payload
            FROM inventory_observations WHERE observed_at >= $1 AND observed_at < $2
            AND point_id<>23109 AND ($3::bigint IS NULL OR point_id=$3)
            ORDER BY observed_at DESC,id LIMIT 10001''',begin,finish,point) if exists else []
    if len(records)>10000: raise ValueError('Сузьте период остатков')
    rows=observations(records,kind=='stockouts')
    if kind=='stock':
        latest={}
        for row in rows:
            key=tuple(row[k] for k in ('point_id','company_id','warehouse_id','product_key','unit'))
            latest.setdefault(key,row)
        rows=list(latest.values())
    return {'kind':kind,'point_id':str(point) if point else None,'date_from':start,'date_to':end,
        'status':'OBSERVED' if records else 'NO_OBSERVATIONS','complete_source':False,
        'rows':rows if export_all else rows[offset:offset+50],'total_rows':len(rows),
        'next_offset':offset+50 if not export_all and offset+50<len(rows) else None,
        'metadata':{'basis':'warehouse_observations','all_warehouse_products':False,
            'note':'Остаток на время наблюдения, не восстановленная история. Ноль подтверждён только в указанные моменты; длительность отсутствия неизвестна. Склады не складываются автоматически.'}}
