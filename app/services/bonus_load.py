"""Resumable order-level observations, never an inferred loyalty ledger."""
import asyncio
import json
from datetime import datetime, timezone
import httpx
from app.db.database import pool
from app.services.saby import saby_client
from app.services.bonus_probe import candidate_key, describe
from app.services.order_transport import cooldown
from app.services.bonus_pacing import BonusPacer,current_pacer

LOCK = 731031
QUEUE_ORDER = '''CASE WHEN b.point_id IS NULL THEN 0
                      WHEN b.status='ERROR' THEN 2 ELSE 1 END,
                 CASE WHEN b.status='ERROR' THEN b.attempted_at END ASC NULLS FIRST,
                 s.business_date,s.point_id,s.sale_id'''
FIELDS = ('bonusAction','bonusDec','bonusInc','bonusIncDateTime',
          'availableBonusBalance','totalBonusBalance','availableBonusDec',
          'userBonusDec','availableBonusInc','cardId','personalCardId')


def normalize(body):
    check = describe(body, 1, '0')
    if check['status'] not in ('REFERENCE_VALUES_MATCH','REFERENCE_NOT_MATCHED'):
        raise ValueError(check['status'])
    for field in ('cardId','personalCardId'):
        value = body.get(field)
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError('INVALID_CARD_ID')
    result = {key: body[key] for key in FIELDS if key in body}
    for field in ('cardId','personalCardId'):
        if result.get(field) is not None: result[field] = str(result[field])
    return result


async def read_bonus(key):
    pacer=current_pacer.get()
    for attempt in range(4):
        try:
            return normalize(await saby_client._get(f'/retail/order/{key}/bonus-read', {}))
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            limited, seconds = cooldown(exc.response)
            if pacer: pacer.failure(limited)
            if limited and attempt == 3:
                raise ValueError('RETRY_LATER') from None
            if status not in (429,500,502,503,504) or attempt == 3:
                raise ValueError(f'HTTP_{status}') from None
            _, seconds = cooldown(exc.response)
            if seconds is None or seconds > 600: raise ValueError('RETRY_LATER') from None
            await asyncio.sleep(max(pacer.delay if pacer else 3, 2**(attempt+1), seconds+1))
        except (httpx.TimeoutException,httpx.TransportError):
            if pacer: pacer.failure()
            if attempt == 3: raise ValueError('NETWORK_ERROR') from None
            await asyncio.sleep(max(pacer.delay if pacer else 3,2**(attempt+1)))


async def load(start=None, end=None, point=None, limit=100, refresh=False):
    """Each sale commits separately. Global DB lock prevents competing loaders."""
    async with pool().acquire() as conn:
        if not await conn.fetchval('SELECT pg_try_advisory_lock($1)', LOCK):
            raise ValueError('BONUS_LOADER_ALREADY_RUNNING')
        started = datetime.now(timezone.utc)
        counts = {'saved':0,'unsupported':0,'errors':0}
        failed_keys = []
        consecutive_errors = 0
        pacer=BonusPacer()
        token=current_pacer.set(pacer)
        try:
            initial_pending=await conn.fetchval('''SELECT COUNT(*) FROM retail_sales s
                LEFT JOIN bonus_observations b USING(point_id,sale_id)
                WHERE NOT s.deleted AND s.customer_id IS NOT NULL
                AND ($1::date IS NULL OR s.business_date >= $1)
                AND ($2::date IS NULL OR s.business_date <= $2)
                AND ($3::bigint IS NULL OR s.point_id=$3)
                AND (b.point_id IS NULL OR b.status='ERROR' OR $4)''',start,end,point,refresh)
            print('Bonus pacing: starts at 3s; floor 2s; 50 clean successes per 0.25s step; sequential requests',flush=True)
            while True:
                rows = await conn.fetch('''SELECT s.point_id,s.sale_id,s.raw_json,
                    md5(s.raw_json::text) fingerprint FROM retail_sales s
                    LEFT JOIN bonus_observations b USING(point_id,sale_id)
                    WHERE NOT s.deleted AND s.customer_id IS NOT NULL
                    AND ($1::date IS NULL OR s.business_date >= $1)
                    AND ($2::date IS NULL OR s.business_date <= $2)
                    AND ($3::bigint IS NULL OR s.point_id=$3)
                    AND NOT ((s.point_id::text || ':' || s.sale_id::text)=ANY($6::text[]))
                    AND (b.point_id IS NULL
                      OR b.status='ERROR' OR ($4 AND b.attempted_at<$5))
                    ORDER BY ''' + QUEUE_ORDER + ''' LIMIT 1''',
                    start,end,point,refresh,started,failed_keys)
                if not rows: break
                row = rows[0]
                raw = row['raw_json']
                if isinstance(raw,str): raw=json.loads(raw)
                key = candidate_key(raw)
                data = None
                error = None
                status = 'UNSUPPORTED'
                if key:
                    await asyncio.sleep(pacer.delay)
                    failures_before=pacer.failures
                    try:
                        data = await read_bonus(key)
                        status = 'OK'
                        if pacer.failures==failures_before: pacer.success()
                    except Exception as exc:
                        # Only controlled codes; never persist URLs, tokens or upstream text.
                        error = str(exc) if isinstance(exc,ValueError) and str(exc) in {
                            'INVALID_RESPONSE','SOURCE_ERROR','BONUS_BALANCE_ERROR',
                            'ACTION_MISSING_OR_UNSUPPORTED','INVALID_AMOUNTS','INVALID_CARD_ID',
                            'RETRY_LATER','NETWORK_ERROR'} else type(exc).__name__
                        if isinstance(exc,ValueError) and str(exc).startswith('HTTP_'):
                            error = str(exc)
                        status = ('UNSUPPORTED' if error == 'ACTION_MISSING_OR_UNSUPPORTED' else 'ERROR')
                        if pacer.failures==failures_before: pacer.failure()
                await conn.execute('''INSERT INTO bonus_observations
                    (point_id,sale_id,fingerprint,status,response,attempted_at,error_code)
                    VALUES($1,$2,$3,$4,$5::jsonb,now(),$6)
                    ON CONFLICT(point_id,sale_id) DO UPDATE SET
                    fingerprint=EXCLUDED.fingerprint,status=EXCLUDED.status,
                    response=CASE WHEN EXCLUDED.status='OK' THEN EXCLUDED.response
                                  ELSE bonus_observations.response END,
                    attempted_at=now(),error_code=EXCLUDED.error_code''',
                    row['point_id'],row['sale_id'],row['fingerprint'],status,
                    json.dumps(data) if data is not None else None,error)
                counts[{'OK':'saved','ERROR':'errors','UNSUPPORTED':'unsupported'}[status]] += 1
                if status == 'ERROR':
                    failed_keys.append(f"{row['point_id']}:{row['sale_id']}")
                    consecutive_errors += 1
                    print(f"Bonus deferred: point={row['point_id']} sale={row['sale_id']} code={error}",flush=True)
                    if error in {'HTTP_401','HTTP_403','HTTP_429','RETRY_LATER'} or consecutive_errors >= 5:
                        return {**counts,**pacer.metrics(counts,initial_pending,refresh),'status':'STOPPED','error':error,
                                'reason':'GLOBAL_OR_REPEATED_FAILURE','complete_history':False,
                                'next':'Повторите команду позже: сохранённые ответы пропускаются.'}
                else:
                    consecutive_errors = 0
                if sum(counts.values()) % 25 == 0:
                    print(json.dumps({'event':'bonus_progress',**counts,**pacer.metrics(counts,initial_pending,refresh)},ensure_ascii=False),flush=True)
                if limit and sum(counts.values()) >= limit:
                    return {**counts,**pacer.metrics(counts,initial_pending,refresh),'status':'PARTIAL_ERRORS' if counts['errors'] else 'BATCH_FINISHED','complete_history':False}
            return {**counts,**pacer.metrics(counts,initial_pending,refresh),'status':'PARTIAL_ERRORS' if counts['errors'] else 'LOCAL_SALES_PROCESSED',
                    'complete_history':False}
        finally:
            current_pacer.reset(token)
            await conn.execute('SELECT pg_advisory_unlock($1)', LOCK)


async def plan(start=None, end=None, point=None):
    """Local inventory only. No calls to Saby, no guessed absence of loyalty."""
    async with pool().acquire() as conn:
        lock_sessions = await conn.fetch('''SELECT l.pid, l.granted, a.state, a.state_change
            FROM pg_locks l LEFT JOIN pg_stat_activity a ON a.pid=l.pid
            WHERE l.locktype='advisory' AND l.classid=0 AND l.objid=$1
              AND l.objsubid=1
              AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database())''', LOCK)
        counts = dict(await conn.fetchrow('''SELECT COUNT(*) local_sales,
            COUNT(*) FILTER(WHERE s.customer_id IS NULL) excluded_without_customer,
            COUNT(*) FILTER(WHERE s.customer_id IS NOT NULL AND b.status='OK') saved_ok,
            COUNT(*) FILTER(WHERE s.customer_id IS NOT NULL AND b.status='UNSUPPORTED') unsupported_saved,
            COUNT(*) FILTER(WHERE s.customer_id IS NOT NULL AND b.point_id IS NULL) new_sales,
            COUNT(*) FILTER(WHERE s.customer_id IS NOT NULL AND b.status='ERROR') retry_errors,
            COUNT(*) FILTER(WHERE s.customer_id IS NOT NULL AND (b.point_id IS NULL OR b.status='ERROR')
                AND NULLIF(s.sale_key,'') IS NOT NULL) pending_requests,
            COUNT(*) FILTER(WHERE s.customer_id IS NOT NULL AND (b.point_id IS NULL OR b.status='ERROR')
                AND NULLIF(s.sale_key,'') IS NULL) pending_without_key,
            MAX(b.attempted_at) last_attempt_at,
            MAX(b.attempted_at) FILTER(WHERE b.status='OK') last_success_at
            FROM retail_sales s LEFT JOIN bonus_observations b USING(point_id,sale_id)
            WHERE NOT s.deleted
            AND ($1::date IS NULL OR s.business_date >= $1)
            AND ($2::date IS NULL OR s.business_date <= $2)
            AND ($3::bigint IS NULL OR s.point_id=$3)''',start,end,point))
        errors=[dict(r) for r in await conn.fetch('''SELECT b.point_id,b.sale_id,b.error_code,b.attempted_at
            FROM bonus_observations b JOIN retail_sales s USING(point_id,sale_id)
            WHERE b.status='ERROR' AND NOT s.deleted AND s.customer_id IS NOT NULL
              AND ($1::date IS NULL OR s.business_date >= $1)
              AND ($2::date IS NULL OR s.business_date <= $2)
              AND ($3::bigint IS NULL OR s.point_id=$3)
            ORDER BY b.attempted_at DESC LIMIT 10''',start,end,point)]
    return {**counts,'minimum_wait_hours':round(counts['pending_requests']*2/3600,2),
        'wait_hours_at_initial_3s':round(counts['pending_requests']*3/3600,2),
        'pacing':'ADAPTIVE_3_TO_2_SECONDS_BACKOFF_UP_TO_10',
        'timing_note':'Нижняя оценка — только паузы при 2с; текущую скорость и ETA смотрите в bonus_progress работающего загрузчика.',
        'changed_saved':None,'freshness_checked':False,'uuid_validation_performed':False,
        'queue_order':'NEW_FIRST_THEN_OLDEST_ERRORS','saby_requests_made':0,'recent_errors':errors,
        'note':'Лёгкий подсчёт по локальным полям. Контрольные суммы и UUID не проверялись; pending_requests — оценка по наличию ключа. Это не полнота Saby.',
        'loader_lock_sessions':[
        {k:(str(v) if k=='state_change' and v is not None else v) for k,v in dict(r).items()}
        for r in lock_sessions],
        'lock_note':'Блокировка означает занятый сеанс; idle в PostgreSQL не доказывает зависание: загрузчик может ждать Saby.'}


def inventory(rows):
    counts = dict(local_sales=0,saved_ok=0,unsupported_saved=0,
                  changed_saved=0,new_sales=0,retry_errors=0,
                  pending_requests=0,pending_without_uuid=0,
                  pending_with_customer=0,excluded_without_customer=0)
    for row in rows:
        counts['local_sales'] += 1
        if row['customer_id'] is None:
            counts['excluded_without_customer'] += 1
            continue
        status = row['status']
        if status in ('OK','UNSUPPORTED'):
            counts['saved_ok' if status=='OK' else 'unsupported_saved'] += 1
            if not row['fresh']: counts['changed_saved'] += 1
            continue
        counts['retry_errors' if status=='ERROR' else 'new_sales'] += 1
        raw=row['raw_json']
        if isinstance(raw,str): raw=json.loads(raw)
        if not isinstance(raw,dict) or candidate_key(raw) is None:
            counts['pending_without_uuid'] += 1
        else:
            counts['pending_requests'] += 1
            if row['customer_id'] is not None: counts['pending_with_customer'] += 1
    return {**counts,'minimum_wait_hours':round(counts['pending_requests']*3/3600,2),
            'saby_requests_made':0,
            'queue_order':'NEW_FIRST_THEN_OLDEST_ERRORS',
            'note':'Оценка для обычного load, без --refresh. Время — только паузы, без ответов и повторов. '
                   'Сохранённые ответы повторно не читаются; изменённые продажи остаются '
                   'неподтверждёнными до явного --refresh. По правилу владельца продажи без Customer исключены из бонусных запросов.'}


async def report(start, end, point=None):
    async with pool().acquire() as conn:
        rows = await conn.fetch('''SELECT s.point_id,s.sale_id,s.is_return,s.customer_id,
            b.status,b.response,b.attempted_at,
            (b.fingerprint=md5(s.raw_json::text)) fresh,
            p.revenue,p.checks,p.fallback
            FROM retail_sales s LEFT JOIN bonus_observations b USING(point_id,sale_id)
            LEFT JOIN LATERAL (SELECT
              SUM(ABS(amount)) FILTER(WHERE NOT is_return) revenue,
              COUNT(*) FILTER(WHERE NOT is_return) checks,
              COUNT(*) FILTER(WHERE source='sale_total_fallback') fallback
              FROM retail_sale_payments p WHERE p.point_id=s.point_id
              AND p.sale_id=s.sale_id) p ON true
            WHERE NOT s.deleted AND s.business_date BETWEEN $1 AND $2
            AND ($3::bigint IS NULL OR s.point_id=$3)''',start,end,point)
    return summarize(rows)


def summarize(rows):
    from decimal import Decimal
    total = Decimal(0)
    debit = Decimal(0)
    accrual = Decimal(0)
    card_revenue = Decimal(0)
    card_purchases = known = debit_known = accrual_known = missing_payments = no_card = 0
    fallback = 0
    for row in rows:
        if row['is_return']: continue
        revenue = row['revenue']
        if revenue is None: missing_payments += 1
        else: total += Decimal(str(revenue))
        fallback += row['fallback'] or 0
        if row.get('customer_id',1) is None:
            no_card += 1
            continue
        if row['status'] != 'OK' or not row['fresh']: continue
        known += 1
        body = row['response']
        if isinstance(body,str): body=json.loads(body)
        if body.get('bonusDec') is not None:
            debit += Decimal(str(body['bonusDec'])); debit_known += 1
        if body.get('bonusInc') is not None:
            accrual += Decimal(str(body['bonusInc'])); accrual_known += 1
        if (body.get('cardId') or body.get('personalCardId')) and revenue is not None:
            card_revenue += Decimal(str(revenue)); card_purchases += 1
    purchases = sum(not r['is_return'] for r in rows)
    return {'local_purchases':purchases,'bonus_responses_current':known,
            'no_card_by_owner_rule':no_card,
            'unconfirmed_purchases':purchases-known-no_card,'payment_revenue':str(total),
            'missing_payment_purchases':missing_payments,'fallback_payment_rows':fallback,
            'bonus_debit_known':str(debit) if debit_known else None,
            'debit_value_count':debit_known,
            'bonus_to_accrue_known':str(accrual) if accrual_known else None,
            'accrual_value_count':accrual_known,
            'known_debit_to_payment_revenue_percent':str(debit/total*100) if debit_known and total else None,
            'card_purchases_with_payments':card_purchases,
            'card_purchase_average':str(card_revenue/card_purchases) if card_purchases else None,
            'complete_history':False,'source_complete':False,
            'note':'Предварительно. Покупка = уникальная продажа, не платёжная строка. '
                   'Период по business_date продажи; возвраты исключены. '
                   'Доля — известные списания / доступная платёжная выручка; бонусы в баллах, '
                   'денежный курс отдельно не подтверждён. Начисления — к начислению, '
                   'не журнал проведённых операций. Карты и балансы — наблюдения, не полный реестр.'}
