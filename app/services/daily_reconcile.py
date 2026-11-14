"""Local closed-day checks, distinct from source loading and backfill runs."""
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from app.config import get_settings
from app.db.database import pool
from app.services.analytics import analytics_service

PREFIX='maintenance:daily-reconcile:'


def ranges(days):
    result=[]
    for day in sorted(set(days)):
        if result and date.fromisoformat(result[-1]['end'])+timedelta(days=1)==day:
            result[-1]['end']=str(day)
        else: result.append({'start':str(day),'end':str(day)})
    return result


def classify(first,last,backfills,syncs,checks,tz,start_hour):
    missing=[]; pending=[]; review=[]; candidates=[]
    if first is None: return dict(missing_load=[],pending_reconcile=[],review=[],candidates=[])
    day=first
    syncs=sorted(syncs,key=lambda r:r['id'],reverse=True)
    while day<=last:
        # Backfill checkpoints already require reconciliation before committing.
        covered=any(r['next_date'] and r['date_from']<=day<=r['date_to'] and day<r['next_date'] for r in backfills)
        if not covered:
            # Include in-progress/failed overlapping reads: they can have changed
            # local rows and invalidate an older successful check.
            latest=next((r for r in syncs if r['date_from']<=day and r['date_to']>=day),None)
            closed_at=datetime.combine(day+timedelta(days=1),datetime.min.time(),ZoneInfo(tz))+timedelta(hours=start_hour)
            loaded=bool(latest and latest['status']=='OK' and latest['date_to']>=day+timedelta(days=1)
                        and latest['finished_at'] and latest['started_at']>=closed_at)
            if not loaded:
                missing.append(day)
            else:
                record=checks.get(str(day))
                if record and record.get('sync_run_id')==latest['id']:
                    if record.get('status')=='REVIEW': review.append(day)
                    elif record.get('status')!='OK': pending.append(day);candidates.append((day,latest['id']))
                else:
                    pending.append(day);candidates.append((day,latest['id']))
        day+=timedelta(days=1)
    return dict(missing_load=ranges(missing),pending_reconcile=ranges(pending),
                review=ranges(review),candidates=candidates)


async def inspect():
    settings=get_settings()
    last=(datetime.now(ZoneInfo(settings.business_tz))-timedelta(hours=settings.business_day_start_hour)).date()-timedelta(days=1)
    async with pool().acquire() as conn:
        first=await conn.fetchval('SELECT MIN(business_date) FROM retail_sales WHERE NOT deleted')
        backfills=await conn.fetch('SELECT date_from,date_to,next_date FROM backfill_runs')
        syncs=await conn.fetch('SELECT id,status,date_from,date_to,started_at,finished_at FROM sync_runs ORDER BY id DESC')
        rows=await conn.fetch("SELECT key,value - 'report' AS value FROM app_meta WHERE key LIKE $1",PREFIX+'%')
    checks={r['key'][len(PREFIX):]:(json.loads(r['value']) if isinstance(r['value'],str) else r['value']) for r in rows}
    result=classify(first,last,backfills,syncs,checks,settings.business_tz,settings.business_day_start_hour)
    result['closed_through']=str(last)
    result['has_local_sales']=first is not None
    result['checks']=checks
    return result


async def run_next():
    coverage=await inspect()
    if not coverage['candidates']:
        return {'status':'COMPLETED','checked_days':0,'missing_load':coverage['missing_load'],
                'review':coverage['review'],'source_complete':False}
    day,run_id=coverage['candidates'][0]
    report=await analytics_service.reconcile(str(day))
    async with pool().acquire() as conn:
        # A manual sync may have started during reconciliation. Do not publish
        # the result as current when the generation of local data has changed.
        latest=await conn.fetchrow('''SELECT id,status FROM sync_runs
            WHERE date_from<=$1 AND date_to>=$1 ORDER BY id DESC LIMIT 1''',day)
        if not latest or latest['id']!=run_id or latest['status']!='OK':
            return {'status':'PENDING','reason':'SALES_CHANGED_DURING_RECONCILE','source_complete':False}
        record={'business_date':str(day),'sync_run_id':run_id,
                'checked_at':datetime.now(timezone.utc).isoformat(),
                'status':'OK' if report.get('ok') is True else 'REVIEW',
                'report':report,'source_complete':False}
        await conn.execute('''INSERT INTO app_meta(key,value,updated_at) VALUES($1,$2::jsonb,now())
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()''',
            PREFIX+str(day),json.dumps(record,ensure_ascii=False,default=str))
    return {'status':'PENDING' if len(coverage['candidates'])>1 else 'COMPLETED',
            'checked_days':1,'business_date':str(day),'reconcile_status':record['status'],
            'sync_run_id':run_id,'source_complete':False}
