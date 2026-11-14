"""One durable, sequential worker. Sales are selected before background jobs."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.db.database import pool
from app.services import saby_maintenance as maintenance, bonus_load
from app.services.sync import sync_service
from app.services.lock_guard import keep_lock_alive, release_lock
from app.services.safe_errors import error_details
from app.services import daily_reconcile

KEY='maintenance:saby:scheduler:v1'
LOCK=731034
JOBS=('sales','reconcile','bonuses_new','stores','catalog','prices','bonuses_errors')
logger=logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc)


def timestamp(value):
    return datetime.fromisoformat(value) if value else None


def interval(job, settings):
    if job in ('sales','reconcile'): return max(5,settings.sync_interval_minutes)*60
    if job=='bonuses_new': return max(5,settings.auto_bonus_interval_minutes)*60
    if job=='bonuses_errors': return 86400
    return max(1,settings.auto_source_interval_hours)*3600


def select_job(state,settings,at):
    sales=state.get('sales',{})
    ready=[]
    for job in JOBS:
        if job!='sales':
            if not settings.auto_sources_enabled: continue
            # Background reads start only after a successful sales cycle.
            if sales.get('status')!='OK': continue
            if job=='prices' and not settings.saby_price_list_point_ids.strip(): continue
        due=timestamp(state.get(job,{}).get('next_run_at'))
        if due is None or due<=at:
            if job=='sales': return job
            ready.append((due or datetime.min.replace(tzinfo=timezone.utc),JOBS.index(job),job))
    # Old errors cannot jump ahead of any ready normal job. Oldest due time
    # prevents a large bonus backlog or many price lists starving other sources.
    normal=[item for item in ready if item[2]!='bonuses_errors']
    return min(normal or ready)[2] if ready else None


async def read_state(key):
    async with pool().acquire() as conn:
        return maintenance.decode(await conn.fetchval('SELECT value FROM app_meta WHERE key=$1',key))


async def bonus_busy(conn):
    return bool(await conn.fetchval('''SELECT EXISTS(SELECT 1 FROM pg_locks
        WHERE locktype='advisory' AND classid=0 AND objid=$1 AND objsubid=1
        AND granted AND database=(SELECT oid FROM pg_database WHERE datname=current_database()))''',bonus_load.LOCK))


async def execute_job(job,settings):
    if job in ('stores','catalog','prices'):
        previous=await read_state(maintenance.state_key(job,True))
        resume=bool(previous and previous.get('status')!='COMPLETED')
        # A price list is a checkpoint; yield to sales between complete lists.
        return await maintenance.update(component=job,automatic=True,resume=resume,
                                        max_stages=1 if job=='prices' else None)
    async with pool().acquire() as conn:
        if not await conn.fetchval('SELECT pg_try_advisory_lock($1)',maintenance.LOCK):
            return {'status':'BUSY'}
        try:
            async with keep_lock_alive(conn):
                if await bonus_busy(conn): return {'status':'BUSY'}
                if job=='sales':
                    result=await sync_service.sync_recent(settings.sync_recent_days)
                    return {'status':'COMPLETED','result':result}
                if job=='reconcile':
                    return await daily_reconcile.run_next()
                return await bonus_load.load(
                    limit=min(200,max(1,settings.auto_bonus_batch_size)) if job=='bonuses_new' else 10,
                    queue_mode='new' if job=='bonuses_new' else 'errors',max_seconds=120,coordinated=True)
        finally:
            await release_lock(conn,'SELECT pg_advisory_unlock($1)',maintenance.LOCK)


async def tick():
    settings=get_settings()
    if not settings.auto_sync_enabled: return
    async with pool().acquire() as conn:
        if not await conn.fetchval('SELECT pg_try_advisory_lock($1)',LOCK): return
        try:
            # Do not compete with a direct bonuses CLI process.
            if await bonus_busy(conn): return
            state=await read_state(KEY) or {}
            if 'sales' not in state and not settings.auto_sync_on_start:
                state['sales']={'status':'WAITING','next_run_at':(now()+timedelta(seconds=interval('sales',settings))).isoformat()}
                await maintenance.save(KEY,state)
            job=select_job(state,settings,now())
            if job is None: return
            old=state.get(job,{})
            state[job]={**old,'status':'RUNNING','started_at':now().isoformat(),
                        'finished_at':None,'error_code':None}
            await maintenance.save(KEY,state)
            logger.info('Automatic source update started: %s',job)
            try:
                async with keep_lock_alive(conn):
                    # Catalog restarts from the last complete snapshot if interrupted.
                    timeout=1200 if job=='catalog' else 300 if job!='sales' else 7200
                    result=await asyncio.wait_for(execute_job(job,settings),timeout=timeout)
                status=result.get('status')
                if status=='BUSY' or result.get('error_code') in ('BONUS_LOADER_ALREADY_RUNNING','MAINTENANCE_ALREADY_RUNNING'):
                    state[job]={**old,'next_run_at':(now()+timedelta(seconds=60)).isoformat()}
                    await maintenance.save(KEY,state)
                    return
                if status=='PENDING':
                    state[job].update(status='PENDING',result=result)
                    delay=60
                elif job=='reconcile' and (result.get('reconcile_status')=='REVIEW' or result.get('review')):
                    state[job].update(status='REVIEW',result=result,error_code='RECONCILE_REVIEW')
                    delay=interval(job,settings)
                elif job=='reconcile' and result.get('missing_load'):
                    state[job].update(status='WAITING_FOR_LOAD',result=result)
                    delay=interval(job,settings)
                elif (job=='bonuses_new' and status=='PARTIAL_ERRORS'
                      and 0<int(result.get('errors',0))<5
                      and int(result.get('saved',0))+int(result.get('unsupported',0))>0):
                    state[job].update(status='PARTIAL',result=result,error_code='BONUS_ITEMS_DEFERRED',
                                      last_progress_at=now().isoformat())
                    delay=60
                elif status in ('COMPLETED','BATCH_FINISHED','LOCAL_SALES_PROCESSED'):
                    state[job].update(status='OK',last_success_at=now().isoformat(),result=result)
                    delay=60 if job=='bonuses_new' and status=='BATCH_FINISHED' else interval(job,settings)
                    if job=='sales':
                        state.setdefault('reconcile',{})['next_run_at']=now().isoformat()
                else:
                    state[job].update(status='ERROR',result=result,error_code=result.get('error',result.get('error_code','SOURCE_UPDATE_INCOMPLETE')))
                    delay=300 if job=='sales' else 86400 if job=='bonuses_errors' else 3600
            except asyncio.CancelledError:
                state[job].update(status='INTERRUPTED',finished_at=now().isoformat(),next_run_at=now().isoformat())
                await maintenance.save(KEY,state)
                raise
            except Exception as exc:
                if type(exc) is ValueError and str(exc)=='MAINTENANCE_ALREADY_RUNNING':
                    state[job]={**old,'next_run_at':(now()+timedelta(seconds=60)).isoformat()}
                    await maintenance.save(KEY,state)
                    return
                state[job].update(status='ERROR',**error_details(exc))
                delay=300 if job=='sales' else 86400 if job=='bonuses_errors' else 3600
            state[job].update(finished_at=now().isoformat(),next_run_at=(now()+timedelta(seconds=delay)).isoformat())
            await maintenance.save(KEY,state)
            logger.info('Automatic source update finished: %s status=%s',job,state[job]['status'])
        finally:
            await release_lock(conn,'SELECT pg_advisory_unlock($1)',LOCK)


async def run_loop():
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Automatic source worker failed; retry in 60 seconds')
        await asyncio.sleep(60)
