"""Persistent orchestration of existing supported Saby readers."""
import json
from datetime import date,datetime,timedelta
from zoneinfo import ZoneInfo
from uuid import uuid4
from app.config import get_settings
from app.db.database import pool
from app.services import bonus_load
from app.services.sync import sync_service
from app.services.backfill import backfill_service
from app.services.payroll import payroll_service
from app.services.catalog_categories import sync_catalog,META_KEY
from app.services.price_lists import list_prices,get_price,allowed_point
from app.services.safe_errors import error_details

KEY='maintenance:saby:active:v1'
LOCK=731033


def decode(value):
    return json.loads(value) if isinstance(value,str) else value


async def save(key,value):
    async with pool().acquire() as conn:
        await conn.execute('''INSERT INTO app_meta(key,value,updated_at) VALUES($1,$2::jsonb,now())
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()''',
            key,json.dumps(value,ensure_ascii=False,default=str))


def closed_day():
    settings=get_settings()
    return (datetime.now(ZoneInfo(settings.business_tz))-
            timedelta(hours=settings.business_day_start_hour)).date()-timedelta(days=1)


def missing_ranges(start,end,coverage):
    """Only committed backfill chunks are evidence; receipt presence is not."""
    intervals=[]
    for row in coverage:
        if row['next_date'] is None: continue
        left=max(start,row['date_from'])
        right=min(end,row['date_to'],row['next_date']-timedelta(days=1))
        if left<=right: intervals.append((left,right))
    cursor=start
    result=[]
    for left,right in sorted(intervals):
        if left>cursor: result.append((cursor,left-timedelta(days=1)))
        cursor=max(cursor,right+timedelta(days=1))
    if cursor<=end: result.append((cursor,end))
    return [{'start':str(a),'end':str(b)} for a,b in result]


async def check():
    async with pool().acquire() as conn:
        sales=dict(await conn.fetchrow('''SELECT COUNT(*) count,MIN(business_date) date_from,
            MAX(business_date) date_to,MAX(synced_at) last_local_write FROM retail_sales WHERE NOT deleted'''))
        stores=await conn.fetchval('SELECT COUNT(*) FROM retail_stores')
        sync=await conn.fetchrow('''SELECT id,status,date_from,date_to,started_at,finished_at
            FROM sync_runs ORDER BY id DESC LIMIT 1''')
        catalog=decode(await conn.fetchval('SELECT value FROM app_meta WHERE key=$1',META_KEY))
        state=decode(await conn.fetchval('SELECT value FROM app_meta WHERE key=$1',KEY))
        prices=await conn.fetch('''SELECT key,updated_at,value->>'complete_snapshot' complete_snapshot
            FROM app_meta WHERE key LIKE 'maintenance:price:%' ORDER BY key''')
        coverage=await conn.fetch('SELECT date_from,date_to,next_date FROM backfill_runs')
    windows=missing_ranges(sales['date_from'],closed_day(),coverage) if sales['date_from'] else []
    return {'mode':'LOCAL_CHECK','saby_requests_made':0,'stores':stores,'sales':sales,
            'sales_windows_without_completed_backfill':windows,
            'latest_sync':dict(sync) if sync else None,
            'catalog':{k:catalog.get(k) for k in ('fetched_at','complete_snapshot','revision')} if catalog else None,
            'prices':[dict(r) for r in prices],'maintenance':state,
            'bonuses':await bonus_load.plan(),
            'source_complete':False,
            'note':'Локальное наличие не подтверждает полноту Saby. RUNNING не доказывает работу процесса. '
                   'Проверка не обращается к Saby; картинки хранятся в Core по отдельному контракту.'}


async def update(start=None,end=None,all_history=False,resume=False):
    settings=get_settings()
    if settings.auto_sync_enabled or settings.auto_sync_on_start:
        raise ValueError('DISABLE_AUTO_SYNC_FOR_MAINTENANCE')
    async with pool().acquire() as lock_conn:
        if not await lock_conn.fetchval('SELECT pg_try_advisory_lock($1)',LOCK):
            raise ValueError('MAINTENANCE_ALREADY_RUNNING')
        try:
            state=decode(await lock_conn.fetchval('SELECT value FROM app_meta WHERE key=$1',KEY))
            if resume:
                if not state: raise ValueError('NO_MAINTENANCE_TO_RESUME')
                if state['status']=='COMPLETED': return state
            else:
                if state and state['status']!='COMPLETED':
                    raise ValueError('UNFINISHED_MAINTENANCE_USE_RESUME')
                end=end or closed_day()
                if all_history:
                    start=await lock_conn.fetchval('SELECT MIN(business_date) FROM retail_sales WHERE NOT deleted')
                    if start is None: raise ValueError('EMPTY_DB_SPECIFY_START_AND_END')
                start=start or end-timedelta(days=6)
                if start>end or end>closed_day(): raise ValueError('ONLY_CLOSED_BUSINESS_DAYS')
                points=[allowed_point(p.strip()) for p in settings.saby_price_list_point_ids.split(',') if p.strip()]
                state={'id':str(uuid4()),'status':'RUNNING','start':str(start),'end':str(end),
                       'done':[],'price_points':sorted(set(points)),'warnings':[], 'source_complete':False}
                coverage=await lock_conn.fetch('SELECT date_from,date_to,next_date FROM backfill_runs')
                state['sales_windows']=missing_ranges(start,end,coverage)
                if not points: state['warnings'].append('PRICE_POINTS_NOT_CONFIGURED')
                await save(KEY,state)
            state['status']='RUNNING'
            state.pop('error_type',None)
            state.pop('error_code',None)
            state.pop('next',None)
            async def stage(name,fn):
                if name in state['done']: return
                state['current_stage']=name
                await save(KEY,state)
                print(f'Saby update: {name}',flush=True)
                await fn()
                state['done'].append(name)
                await save(KEY,state)
            async def sales():
                for window in state['sales_windows']:
                    if window.get('completed'): continue
                    if not window.get('run_id'):
                        window['run_id']=await backfill_service.create_run(
                            date.fromisoformat(window['start']),date.fromisoformat(window['end']),chunk_days=7)
                        await save(KEY,state)
                    result=await backfill_service.run(window['run_id'])
                    if result['status']!='COMPLETED': raise ValueError('BACKFILL_NOT_COMPLETED')
                    window['completed']=True
                    await save(KEY,state)
            async def catalog():
                await sync_catalog(True)
            async def bonuses():
                state.pop('bonus_result',None)
                await save(KEY,state)
                result=await bonus_load.load(limit=0)
                state['bonus_result']=result
                if result['status'] in ('STOPPED','PARTIAL_ERRORS'): raise ValueError('BONUS_LOAD_INCOMPLETE')
            try:
                await stage('stores',sync_service.sync_stores)
                await stage('sales_payments_items_shifts_reconcile',sales)
                await stage('seller_identities',payroll_service.sync_seller_identities)
                await stage('catalog',catalog)
                for point in state['price_points']:
                    async def listing(point=point):
                        data=await list_prices(point)
                        if data.get('complete_snapshot') is not True: raise ValueError('INCOMPLETE_PRICES')
                        await save(f'maintenance:price:list:{point}',data)
                    await stage(f'price_list:{point}',listing)
                    async with pool().acquire() as conn:
                        listing_data=decode(await conn.fetchval('SELECT value FROM app_meta WHERE key=$1',f'maintenance:price:list:{point}'))
                    for price in listing_data['price_lists']:
                        price_id=price['id']
                        async def contents(point=point,price_id=price_id):
                            data=await get_price(point,price_id)
                            if data.get('complete_snapshot') is not True: raise ValueError('INCOMPLETE_PRICE')
                            await save(f'maintenance:price:content:{point}:{price_id}',data)
                        await stage(f'price:{point}:{price_id}',contents)
                # Longest stage last, durable per-sale cache shared with bonuses CLI.
                await stage('bonuses_all_local_customer_sales',bonuses)
            except Exception as exc:
                state['status']='ERROR'
                state.update(error_details(exc))
                await save(KEY,state)
                return state
            state['status']='COMPLETED'
            state['current_stage']=None
            await save(KEY,state)
            return state
        finally:
            await lock_conn.execute('SELECT pg_advisory_unlock($1)',LOCK)
