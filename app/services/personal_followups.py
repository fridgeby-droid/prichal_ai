"""Personal Telegram subscriptions and durable, deterministic follow-up checks."""
import asyncio
import hashlib
import json
import logging
import re
from datetime import date,datetime,timedelta,timezone
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo
from app.config import get_settings
from app.db.database import pool
from app.services import period_reports as periods
from app.services.daily_reconcile import inspect as coverage
from app.services.lock_guard import keep_lock_alive,release_lock

PREFIX='assistant:personal:v1:'
LOCK=731039
logger=logging.getLogger(__name__)
METRICS={'revenue_per_day':'Выручка в день','checks_per_day':'Платежей в день','average_check':'Средний чек'}


def now(): return datetime.now(timezone.utc)
def local(stamp): return stamp.astimezone(ZoneInfo(get_settings().business_tz))
def closed(stamp):
    return (local(stamp)-timedelta(hours=get_settings().business_day_start_hour)).date()-timedelta(days=1)
def decode(value): return json.loads(value) if isinstance(value,str) else value
def defaults():
    return {'enabled':False,'morning':True,'tasks_enabled':True,'time':'10:00','tasks':{},'morning_job':None}


def require_actor(actor):
    if not actor or not actor.get('private') or int(actor.get('user_id',0)) not in get_settings().allowed_user_ids:
        raise ValueError('Настройки и поручения доступны только в личном чате разрешённого пользователя с ботом.')
    return int(actor['user_id'])


async def edit(user_id,fn):
    if user_id not in get_settings().allowed_user_ids: raise ValueError('Доступ запрещён')
    key=PREFIX+str(user_id)
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute('INSERT INTO app_meta(key,value,updated_at) VALUES($1,$2::jsonb,now()) ON CONFLICT(key) DO NOTHING',key,json.dumps(defaults()))
            state=decode(await conn.fetchval('SELECT value FROM app_meta WHERE key=$1 FOR UPDATE',key))
            result=fn(state)
            await conn.execute('UPDATE app_meta SET value=$2::jsonb,updated_at=now() WHERE key=$1',key,json.dumps(state,ensure_ascii=False,default=str))
            return result


def preferences(state,setting,value):
    if setting=='time':
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value): raise ValueError('Время: ЧЧ:ММ по Екатеринбургу.')
        state['time']=value
    elif setting in ('enabled','morning','tasks_enabled'):
        if value not in ('on','off'): raise ValueError('Значение: on или off')
        state[setting]=value=='on'
    elif setting!='status': raise ValueError('Настройка: enabled, morning, tasks_enabled, time')
    return {'enabled':state['enabled'],'morning':state['morning'],'tasks_enabled':state['tasks_enabled'],
            'time':state['time'],'timezone':get_settings().business_tz,
            'morning_status':(state.get('morning_job') or {}).get('status')}


async def configure(actor,setting='status',value=''):
    return await edit(require_actor(actor),lambda s:preferences(s,setting,value))


def overlaps(ranges,first,last):
    return any(date.fromisoformat(r['start'])<=last and date.fromisoformat(r['end'])>=first for r in ranges)


def ready(info,first,last):
    return (info.get('has_local_sales',False) and
            not any(overlaps(info.get(k,[]),first,last) for k in ('missing_load','pending_reconcile','review')))


def aggregate(rows,first,last,shift):
    selected=[r for r in rows if not shift or r['shift_type']==shift]
    observed={r['business_date'] for r in selected}
    count=sum(r['checks'] for r in selected)
    revenue=sum((Decimal(str(r['revenue'])) for r in selected),Decimal(0))
    days=(last-first).days+1
    return {'revenue_per_day':str(revenue/days) if selected else None,
            'checks_per_day':str(Decimal(count)/days) if selected else None,
            'average_check':str(revenue/count) if count else None,
            'observed_days':len(observed),'calendar_days':days,'checks':count,
            'all_days_observed':len(observed)==days}


async def metrics(first,last,point,shift):
    sql='SELECT id,status FROM sync_runs WHERE date_from<=$2 AND date_to>=$1 ORDER BY id DESC LIMIT 1'
    async with pool().acquire() as conn:
        before=await conn.fetchrow(sql,first,last)
        if before and before['status']!='OK': raise ValueError('Дождитесь успешной загрузки выбранного периода.')
        rows=await conn.fetch(periods.PAYMENTS_SQL,first,last,int(point))
        after=await conn.fetchrow(sql,first,last)
        if before!=after: raise ValueError('Данные изменились во время проверки. Повторите после загрузки.')
    return aggregate(rows,first,last,shift)


async def create_task(actor,point_id,shift='',metric='revenue_per_day',after_days=7,threshold_percent=0):
    user_id=require_actor(actor)
    if shift not in ('','DAY','NIGHT') or metric not in METRICS: raise ValueError('Неподдерживаемая смена или показатель')
    if not 1<=after_days<=90 or not 0<=threshold_percent<=1000: raise ValueError('Срок 1–90 дней; порог 0–1000%.')
    await periods.point_filter(point_id)
    if not point_id: raise ValueError('Нужен конкретный магазин')
    stores=await periods.stores(point_id)
    name=next(s['name'] for s in stores['stores'] if s['point_id']==point_id)
    stamp=now();base_end=closed(stamp);base_start=base_end-timedelta(days=after_days-1)
    first=base_end+timedelta(days=1);last=base_end+timedelta(days=after_days)
    if not ready(await coverage(),base_start,base_end):
        raise ValueError('Базовый период ещё не загружен или не сверен. Сначала дождитесь загрузки и сверки.')
    base=await metrics(base_start,base_end,point_id,shift)
    if not base['all_days_observed'] or base[metric] is None:
        raise ValueError('В базовом периоде есть дни без записей выбранной смены. Нельзя зафиксировать надёжную базу.')
    due=max(stamp+timedelta(days=after_days),datetime.combine(last+timedelta(days=1),datetime.min.time(),ZoneInfo(get_settings().business_tz))+timedelta(hours=get_settings().business_day_start_hour,minutes=30))
    fingerprint=hashlib.sha256(json.dumps([actor.get('message_id'),point_id,shift,metric,after_days,threshold_percent],ensure_ascii=False).encode()).hexdigest()
    def add(state):
        for task in state['tasks'].values():
            if task.get('request_fingerprint')==fingerprint:
                return {**task,'delivery_enabled':state['enabled'] and state['tasks_enabled']}
        if sum(t['status'] not in ('DONE','CANCELLED','DELIVERY_UNKNOWN') for t in state['tasks'].values())>=30:
            raise ValueError('Не более 30 активных задач.')
        terminal=[t for t in state['tasks'].values() if t['status'] in ('DONE','CANCELLED','DELIVERY_UNKNOWN')]
        for old in terminal[:-100]: state['tasks'].pop(old['id'],None)
        task={'id':uuid4().hex[:12],'point_id':point_id,'store':name,'shift':shift,'metric':metric,
              'threshold_percent':threshold_percent,'baseline':base,'baseline_from':str(base_start),'baseline_to':str(base_end),
              'date_from':str(first),'date_to':str(last),'due_at':due.isoformat(),'next_at':due.isoformat(),
              'created_at':stamp.isoformat(),'status':'PENDING','request_fingerprint':fingerprint}
        state['tasks'][task['id']]=task
        return {**task,'delivery_enabled':state['enabled'] and state['tasks_enabled']}
    return await edit(user_id,add)


def task_action(state,action,task_id=''):
    if action=='list':
        active=[t for t in state['tasks'].values() if t['status'] not in ('DONE','CANCELLED','DELIVERY_UNKNOWN')]
        history=[t for t in state['tasks'].values() if t['status'] in ('DONE','CANCELLED','DELIVERY_UNKNOWN')]
        return active+history[-20:]
    task=state['tasks'].get(task_id)
    if not task: raise ValueError('Задача не найдена в вашем списке')
    status=task['status']
    if action=='cancel':
        if status in ('DONE','SENDING','DELIVERY_UNKNOWN'): raise ValueError('Доставленный или уже отправляемый результат нельзя отменить.')
        task['status']='CANCELLED'
    elif action=='pause':
        if status not in ('PENDING','PROCESSING','WAITING'): raise ValueError('Эту задачу нельзя приостановить.')
        task['status']='PAUSED'
    elif action=='resume':
        if status!='PAUSED': raise ValueError('Возобновить можно только приостановленную задачу.')
        task['status']='PENDING';task['next_at']=now().isoformat()
    else: raise ValueError('Действие: list, pause, resume, cancel')
    return task


async def tasks(actor,action='list',task_id=''):
    return await edit(require_actor(actor),lambda s:task_action(s,action,task_id))


def expired(job,stamp):
    return stamp-datetime.fromisoformat(job['claimed_at'])>timedelta(minutes=10)


def claim(state,stamp):
    # A send interrupted by restart has unknown delivery; never send it automatically again.
    for job in list(state['tasks'].values())+([state['morning_job']] if state.get('morning_job') else []):
        if job['status']=='SENDING' and expired(job,stamp): job['status']='DELIVERY_UNKNOWN'
        if job['status']=='PROCESSING' and expired(job,stamp): job['status']='WAITING';job['next_at']=stamp.isoformat()
    if not state['enabled']: return None
    candidates=[]
    if state['tasks_enabled']:
        candidates=[t for t in state['tasks'].values() if t['status'] in ('PENDING','WAITING') and datetime.fromisoformat(t['next_at'])<=stamp]
    if state['morning'] and local(stamp).strftime('%H:%M')>=state['time']:
        day=str(closed(stamp));old=state.get('morning_job')
        if not old or (old.get('day')!=day and old['status'] not in ('PROCESSING','SENDING')):
            state['morning_job']={'id':'morning:'+day,'day':day,'status':'PENDING','next_at':stamp.isoformat(),'due_at':stamp.isoformat()}
        job=state['morning_job']
        if job['status'] in ('PENDING','WAITING') and datetime.fromisoformat(job['next_at'])<=stamp: candidates.append(job)
    if not candidates: return None
    job=min(candidates,key=lambda t:t['next_at'])
    job.update(status='PROCESSING',claimed_at=stamp.isoformat(),token=uuid4().hex)
    return dict(job)


def find_job(state,job):
    return state.get('morning_job') if job['id'].startswith('morning:') else state['tasks'].get(job['id'])


def update_job(state,job,status,**fields):
    target=find_job(state,job)
    if not target or target.get('token')!=job['token'] or target['status'] not in ('PROCESSING','SENDING'): return False
    if status=='SENDING' and (not state['enabled'] or not state['morning' if job['id'].startswith('morning:') else 'tasks_enabled']):
        target.update(status='WAITING',next_at=now().isoformat())
        return False
    target.update(status=status,**fields)
    return True


def outcome(job,current):
    metric=job['metric'];base=Decimal(job['baseline'][metric]);value=Decimal(current[metric])
    delta=value-base
    percent=delta/base*100 if base else None
    improved=delta>0 and percent is not None and percent>=Decimal(str(job['threshold_percent']))
    label='Улучшение достигнуто' if improved else 'Показатель снизился' if delta<0 else 'Без изменения' if delta==0 else 'Рост есть, заданный порог не достигнут'
    if percent is None: label='База равна нулю: процентное улучшение не определено'
    return {'baseline':str(base),'current':str(value),'delta':str(delta),'percent':str(percent) if percent is not None else None,'conclusion':label}


def fmt(value): return f'{Decimal(str(value)):,.2f}'.replace(',',' ').replace('.',',') if value is not None else 'н/д'


async def evaluate(job):
    if job['id'].startswith('morning:'):
        day=date.fromisoformat(job['day'])
        if not ready(await coverage(),day,day): return None
        from app.services.management_reports import report
        base=day-timedelta(days=7)
        data=await report(str(day),str(day),str(base),str(base),kind='comparison')
        if not data.get('has_current_records'): return None
        m=data['metrics'];change=m['revenue']['change']
        lines=[f'Утренний обзор · {day}',f"Выручка: {fmt(m['revenue']['current'])} ₽",f"Платежей: {fmt(m['checks']['current'])}",f"Средний чек: {fmt(m['average']['current'])} ₽"]
        base_ready=ready(await coverage(),base,base)
        if base_ready and change: lines.append(f"К {base}: {fmt(change['percent'])}%")
        else: lines.append('База сравнения не подтверждена загрузкой и сверкой.')
        rows=data['dimensions']['stores']['rows'][:3]
        for row in rows:
            amount=fmt(row['current'])+' ₽' if row['observed_current'] else 'нет записей'
            lines.append(f"{row['name']}: {amount}"+(f" · Δ {fmt(row['change']['absolute'])} ₽" if base_ready and row.get('change') and not row['observation_warning'] else ' · сравнение недоступно'))
        return '\n'.join(lines),{'date':str(day)}
    first,last=date.fromisoformat(job['date_from']),date.fromisoformat(job['date_to'])
    if not ready(await coverage(),first,last): return None
    current=await metrics(first,last,job['point_id'],job['shift'])
    if not current['all_days_observed'] or current[job['metric']] is None: return None
    result=outcome(job,current)
    shift={'DAY':'Дневная смена','NIGHT':'Ночная смена','':'Все смены'}[job['shift']]
    text=(f"Проверка задачи {job['id']}\n{job['store']} · {shift}\n{METRICS[job['metric']]}\n"
          f"База {job['baseline_from']}–{job['baseline_to']}: {fmt(result['baseline'])}\n"
          f"Проверка {first}–{last}: {fmt(result['current'])}\n"
          f"Изменение: {fmt(result['delta'])} · {fmt(result['percent'])}%\n{result['conclusion']}")
    if (last-first).days+1 not in (7,14,21,28,35,42,49,56,63,70,77,84): text+='\nСостав дней недели в периодах различается.'
    return text,result


async def process(bot,user_id,job):
    try:
        result=await asyncio.wait_for(evaluate(job),timeout=180)
    except Exception:
        logger.exception('Personal check failed')
        result=None
    if result is None:
        delayed=now()-datetime.fromisoformat(job['due_at'])>=timedelta(days=3)
        if not delayed:
            await edit(user_id,lambda s:update_job(s,job,'WAITING',next_at=(now()+timedelta(hours=1)).isoformat(),reason='WAITING_FOR_DATA'))
            return
        result=(f"Проверка {job['id']}: за три дня не удалось получить достаточные данные. Итог не рассчитан. Проверьте /loadstatus и список /tasks.",{'status':'INSUFFICIENT_DATA'})
    text,details=result
    prepared=await edit(user_id,lambda s:update_job(s,job,'SENDING',claimed_at=now().isoformat(),result=details))
    if not prepared: return
    try:
        message=await bot.send_message(user_id,text)
    except Exception:
        logger.exception('Personal notification delivery uncertain')
        await edit(user_id,lambda s:update_job(s,job,'DELIVERY_UNKNOWN',result=details))
        return
    await edit(user_id,lambda s:update_job(s,job,'DONE',finished_at=now().isoformat(),telegram_message_id=message.message_id,result=details))


async def tick(bot):
    async with pool().acquire() as guard:
        if not await guard.fetchval('SELECT pg_try_advisory_lock($1)',LOCK): return
        try:
            async with keep_lock_alive(guard):
                # Only users who configured personal delivery; removed allowlist users are ignored.
                async with pool().acquire() as conn:
                    keys=await conn.fetch("SELECT key FROM app_meta WHERE key LIKE $1",PREFIX+'%')
                for row in keys:
                    uid=int(row['key'][len(PREFIX):])
                    if uid not in get_settings().allowed_user_ids: continue
                    job=await edit(uid,lambda s:claim(s,now()))
                    if job: await process(bot,uid,job)
        finally:
            await release_lock(guard,'SELECT pg_advisory_unlock($1)',LOCK)


async def run_loop(bot):
    while True:
        try: await tick(bot)
        except asyncio.CancelledError: raise
        except Exception: logger.exception('Personal scheduler failed')
        await asyncio.sleep(30)
