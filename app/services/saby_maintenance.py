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
from app.services.lock_guard import keep_lock_alive, release_lock

KEY='maintenance:saby:active:v1'
LOCK=731033
COMPONENTS=('stores','sales','catalog','prices','bonuses')


def terminal(state):
    return state.get('status') in ('COMPLETED','PARTIAL') or (
        state.get('status')=='ERROR' and state.get('error_code')=='BONUS_LOAD_INCOMPLETE')


def state_key(component, automatic=False):
    if automatic: return f'maintenance:saby:auto:{component}:v1'
    return KEY if component is None else f'maintenance:saby:component:{component}:v1'


class StageBudgetReached(Exception):
    pass


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
        components=await conn.fetch("SELECT key,value FROM app_meta WHERE key LIKE 'maintenance:saby:component:%' ORDER BY key")
        automatic=await conn.fetch("SELECT key,value FROM app_meta WHERE key LIKE 'maintenance:saby:auto:%' OR key='maintenance:saby:scheduler:v1' ORDER BY key")
        locks=await conn.fetch('''SELECT pid,granted FROM pg_locks
            WHERE locktype='advisory' AND classid=0 AND objid=$1 AND objsubid=1''',LOCK)
    windows=missing_ranges(sales['date_from'],closed_day(),coverage) if sales['date_from'] else []
    bonuses=await bonus_load.plan()
    return {'mode':'LOCAL_CHECK','saby_requests_made':0,'stores':stores,'sales':sales,
            'summary':{
                'sales_backfill':'HAS_UNCOVERED_DAYS' if windows else 'NO_UNCOVERED_CLOSED_DAYS' if sales['date_from'] else 'NO_LOCAL_SALES',
                'catalog_snapshot_at':catalog.get('fetched_at') if catalog else None,
                'bonus_new_sales':bonuses.get('new_sales'),
                'bonus_retry_errors':bonuses.get('retry_errors'),
                'bonus_unsupported':bonuses.get('unsupported_saved'),
                'maintenance_session_present':any(r['granted'] for r in locks),
                'bonus_session_present':any(r['granted'] for r in bonuses.get('loader_lock_sessions',[])),
                'note':'Сеанс с блокировкой не доказывает продвижение. Сравните время и счётчики двух проверок; полнота источника не подтверждена.'},
            'sales_windows_without_completed_backfill':windows,
            'latest_sync':dict(sync) if sync else None,
            'catalog':{k:catalog.get(k) for k in ('fetched_at','complete_snapshot','revision')} if catalog else None,
            'prices':[dict(r) for r in prices],'maintenance':state,
            'component_updates':{r['key']:decode(r['value']) for r in components},
            'automatic_updates':{r['key']:decode(r['value']) for r in automatic},
            'maintenance_lock_sessions':[dict(r) for r in locks],
            'bonuses':bonuses,
            'source_complete':False,
            'note':'Локальное наличие не подтверждает полноту Saby. RUNNING не доказывает работу процесса. '
                   'Проверка не обращается к Saby; картинки хранятся в Core по отдельному контракту.'}


async def update(start=None,end=None,all_history=False,resume=False,component=None,
                 automatic=False,max_stages=None):
    if component is not None and component not in COMPONENTS:
        raise ValueError('INVALID_COMPONENT')
    if automatic and component not in ('stores','catalog','prices'): raise ValueError('INVALID_COMPONENT')
    key=state_key(component,automatic)
    settings=get_settings()
    if not automatic and (settings.auto_sync_enabled or settings.auto_sync_on_start):
        raise ValueError('DISABLE_AUTO_SYNC_FOR_MAINTENANCE')
    async with pool().acquire() as lock_conn:
        if not await lock_conn.fetchval('SELECT pg_try_advisory_lock($1)',LOCK):
            raise ValueError('MAINTENANCE_ALREADY_RUNNING')
        try:
            state=decode(await lock_conn.fetchval('SELECT value FROM app_meta WHERE key=$1',key))
            if resume:
                if not state: raise ValueError('NO_MAINTENANCE_TO_RESUME')
                if state['status']=='COMPLETED': return state
            else:
                if state and not terminal(state):
                    raise ValueError('UNFINISHED_MAINTENANCE_USE_RESUME')
                if state:
                    await save(f'maintenance:saby:history:{state["id"]}',state)
                end=end or closed_day()
                if all_history:
                    start=await lock_conn.fetchval('SELECT MIN(business_date) FROM retail_sales WHERE NOT deleted')
                    if start is None: raise ValueError('EMPTY_DB_SPECIFY_START_AND_END')
                start=start or end-timedelta(days=6)
                if start>end or end>closed_day(): raise ValueError('ONLY_CLOSED_BUSINESS_DAYS')
                points=[allowed_point(p.strip()) for p in settings.saby_price_list_point_ids.split(',') if p.strip()]
                if component=='prices' and not points: raise ValueError('PRICE_POINTS_NOT_CONFIGURED')
                state={'id':str(uuid4()),'status':'RUNNING','start':str(start),'end':str(end),
                       'done':[],'component':component,'price_points':sorted(set(points)),'warnings':[], 'source_complete':False}
                coverage=await lock_conn.fetch('SELECT date_from,date_to,next_date FROM backfill_runs')
                state['sales_windows']=missing_ranges(start,end,coverage)
                if not points: state['warnings'].append('PRICE_POINTS_NOT_CONFIGURED')
                await save(key,state)
            state['status']='RUNNING'
            state.pop('error_type',None)
            state.pop('error_code',None)
            state.pop('next',None)
            stages_run=0
            async def stage(name,fn):
                nonlocal stages_run
                group=('sales' if name in ('sales_payments_items_shifts_reconcile','seller_identities')
                       else 'prices' if name.startswith(('price:','price_list:'))
                       else 'bonuses' if name=='bonuses_all_local_customer_sales' else name)
                if component and group!=component: return
                if name in state['done']: return
                if max_stages is not None and stages_run>=max_stages: raise StageBudgetReached()
                state['current_stage']=name
                state['updated_at']=datetime.now().astimezone().isoformat()
                await save(key,state)
                print(f'Saby update: {name}',flush=True)
                async with keep_lock_alive(lock_conn):
                    await fn()
                state['done'].append(name)
                stages_run+=1
                state['updated_at']=datetime.now().astimezone().isoformat()
                await save(key,state)
            async def sales():
                for window in state['sales_windows']:
                    if window.get('completed'): continue
                    if not window.get('run_id'):
                        window['run_id']=await backfill_service.create_run(
                            date.fromisoformat(window['start']),date.fromisoformat(window['end']),chunk_days=7)
                        await save(key,state)
                    result=await backfill_service.run(window['run_id'])
                    if result['status']!='COMPLETED': raise ValueError('BACKFILL_NOT_COMPLETED')
                    window['completed']=True
                    await save(key,state)
            async def catalog():
                await sync_catalog(True)
            async def bonuses():
                state.pop('bonus_result',None)
                await save(key,state)
                result=await bonus_load.load(limit=0,coordinated=True)
                state['bonus_result']=result
                if result['status'] in ('STOPPED','PARTIAL_ERRORS'): raise ValueError('BONUS_LOAD_INCOMPLETE')
            try:
                await stage('stores',sync_service.sync_stores)
                await stage('sales_payments_items_shifts_reconcile',sales)
                await stage('seller_identities',payroll_service.sync_seller_identities)
                await stage('catalog',catalog)
                for point in (state['price_points'] if component in (None,'prices') else []):
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
            except StageBudgetReached:
                state['status']='PENDING'
                state['current_stage']=None
                state['updated_at']=datetime.now().astimezone().isoformat()
                await save(key,state)
                return state
            except Exception as exc:
                state['status']='PARTIAL' if str(exc)=='BONUS_LOAD_INCOMPLETE' else 'ERROR'
                state.update(error_details(exc))
                state['updated_at']=datetime.now().astimezone().isoformat()
                await save(key,state)
                return state
            state['status']='COMPLETED'
            state['current_stage']=None
            state['updated_at']=datetime.now().astimezone().isoformat()
            await save(key,state)
            return state
        finally:
            await release_lock(lock_conn,'SELECT pg_advisory_unlock($1)',LOCK)
