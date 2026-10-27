"""Deterministic analytics of unique local sales; no remote calls or causal claims."""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import combinations
from statistics import median
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db.database import pool
from app.services.extra_reports import dates
from app.services.period_reports import point_filter

from app.services import business_insights as insights
CORE_KINDS = ('decomposition', 'hours', 'copurchases', 'customers', 'retention', 'returns', 'discounts', 'anomalies')
KINDS = CORE_KINDS + insights.KINDS
ZERO = Decimal(0)
NOTE = ('Локальные данные, полнота Saby не подтверждена; сверка не выполнялась. '
        'РЦ исключён. Покупка = уникальная продажа point_id + sale_id. '
        'Суммы продаж TotalPrice, не платёжная выручка. Возвраты отдельно. '
        'Учётка продавца не подтверждает фактического сотрудника. Нет записей ≠ нулевые продажи.')


def dec(value):
    return Decimal(str(value)) if value is not None else ZERO


def ratio(a, b):
    return dec(a) / dec(b) if b else None


def month(d):
    return d.replace(day=1)


def next_month(d):
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def stats(rows):
    purchases = [r for r in rows if not r['is_return']]
    total = sum((dec(r['total_price']) for r in purchases), ZERO)
    return dict(purchases=len(purchases), amount=total, average=ratio(total, len(purchases)),
                returns=sum((abs(dec(r['total_price'])) for r in rows if r['is_return']), ZERO))


def calculate(kind, sales, items, first, last, base_first=None, base_last=None, min_pairs=3):
    current = [s for s in sales if first <= s['business_date'] <= last]
    positive = [s for s in current if not s['is_return']]
    rows = []
    metadata = {'basis': 'unique_sale_total_price', 'totals': stats(current)}
    if kind == 'decomposition':
        previous = [s for s in sales if base_first <= s['business_date'] <= base_last]
        points = sorted({s['point_id'] for s in current + previous})
        for point in [None] + points:
            a = stats([s for s in previous if point is None or s['point_id'] == point])
            b = stats([s for s in current if point is None or s['point_id'] == point])
            # Symmetric exact decomposition: cross term shared equally.
            count_effect = average_effect = None
            if a['purchases'] and b['purchases']:
                count_effect = (b['purchases']-a['purchases'])*(a['average']+b['average'])/2
                average_effect = (b['average']-a['average'])*(a['purchases']+b['purchases'])/2
            rows.append(dict(point_id=str(point) if point else 'NETWORK', previous=a, current=b,
                amount_delta=b['amount']-a['amount'] if a['purchases'] and b['purchases'] else None,
                purchase_count_effect=count_effect, average_purchase_effect=average_effect,
                previous_per_day=ratio(a['amount'], (base_last-base_first).days+1) if a['purchases'] else None,
                current_per_day=ratio(b['amount'], (last-first).days+1) if b['purchases'] else None))
        metadata['interpretation'] = 'Арифметическое разложение, не причины. Состав магазинов и дни недели могут различаться.'
    elif kind == 'hours':
        groups = defaultdict(list)
        tz = ZoneInfo(get_settings().business_tz)
        for s in positive:
            stamp = s.get('sale_datetime')
            hour = stamp.astimezone(tz).hour if stamp and stamp.tzinfo else None
            groups[(str(s['point_id']), s['business_date'].isoweekday(), hour)].append(s)
        for (point, weekday, hour), values in sorted(groups.items(), key=lambda x: str(x[0])):
            calendar_days = sum((first+timedelta(days=n)).isoweekday() == weekday for n in range((last-first).days+1))
            v = stats(values)
            rows.append(dict(point_id=point, business_weekday=weekday, local_hour=hour, **v,
                calendar_weekdays=calendar_days, observed_days=len({s['business_date'] for s in values}),
                amount_per_calendar_weekday=ratio(v['amount'], calendar_days)))
        metadata['interpretation'] = 'Час sale_datetime в часовом поясе бизнеса; день недели по business_date. Отсутствующие часы не заполнены нулями.'
    elif kind == 'copurchases':
        baskets = defaultdict(set)
        names = {}
        valid = {(s['point_id'], s['sale_id']) for s in positive}
        for i in items:
            key = (i['point_id'], i['sale_id'])
            if key in valid and not i['is_return'] and i['product_key']:
                baskets[key].add(i['product_key'])
                names[i['product_key']] = i['name']
        counts, pairs = Counter(), Counter()
        for products in baskets.values():
            if len(products) > 100:
                raise ValueError('Слишком большая корзина для пар; сузьте период или проверьте данные.')
            counts.update(products)
            pairs.update(combinations(sorted(products), 2))
        n = len(positive)
        for (a, b), count in sorted(pairs.items(), key=lambda x: (-x[1], x[0])):
            if count < min_pairs:
                continue
            rows.append(dict(product_a=a, name_a=names[a], product_b=b, name_b=names[b],
                joint_purchases=count, eligible_purchases=n, support=ratio(count,n),
                confidence_a_to_b=ratio(count,counts[a]), confidence_b_to_a=ratio(count,counts[b]),
                lift=ratio(count*n, counts[a]*counts[b])))
        metadata['interpretation'] = 'Доли 0…1; товары считаются один раз в продаже. Модификаторы не включены. Связь не доказывает эффект рекомендации.'
    elif kind in ('customers', 'retention'):
        history = defaultdict(list)
        for s in sales:
            if not s['is_return'] and s.get('customer_id') is not None and s['business_date'] <= last:
                history[str(s['customer_id'])].append(s)
        first_seen = {c:min(s['business_date'] for s in ss) for c,ss in history.items()}
        metadata['interpretation'] = 'Новый = впервые наблюдается в доступной истории выбранной области, не первый визит в жизни. Без Customer не включены.'
        if kind == 'customers':
            for label in ('new_observed', 'returning_observed'):
                ids = {c for c,ss in history.items() if any(first<=s['business_date']<=last for s in ss)
                       and (first_seen[c]>=first) == (label=='new_observed')}
                chosen = [s for s in positive if str(s.get('customer_id')) in ids]
                rows.append(dict(segment=label, customers=len(ids), **stats(chosen)))
            metadata['purchases_without_customer'] = sum(s.get('customer_id') is None for s in positive)
        else:
            cohorts = defaultdict(set)
            for c,d in first_seen.items():
                if month(first) <= month(d) <= month(last):
                    cohorts[month(d)].add(c)
            for cohort, ids in sorted(cohorts.items()):
                period = cohort
                while period <= month(last):
                    mature = next_month(period) <= last+timedelta(days=1)
                    active = sum(any(month(s['business_date'])==period for s in history[c]) for c in ids)
                    rows.append(dict(cohort=cohort.isoformat(), month=period.isoformat(), cohort_size=len(ids),
                        active_customers=active, calendar_month_closed=mature,
                        retention=ratio(active,len(ids)) if mature else None))
                    period = next_month(period)
    elif kind == 'returns':
        groups = defaultdict(list)
        for s in current:
            groups[(str(s['point_id']), str(s['seller_id']) if s.get('seller_id') is not None else None)].append(s)
        for (point, seller), ss in sorted(groups.items(), key=lambda x:str(x[0])):
            v = stats(ss)
            rows.append(dict(point_id=point, seller_id=seller, **v,
                return_records=sum(s['is_return'] for s in ss), return_amount_to_sales=ratio(v['returns'],v['amount'])))
        product_returns = defaultdict(lambda: {'quantity':ZERO,'amount':ZERO})
        lookup = {(s['point_id'],s['sale_id']):s for s in current}
        for i in items:
            s = lookup.get((i['point_id'],i['sale_id']))
            if s and (s['is_return'] or i['is_return']):
                key = (i['product_key'], i['unit_name'])
                product_returns[key]['quantity'] += abs(dec(i['quantity']))
                product_returns[key]['amount'] += abs(dec(i['total_price']))
        rows.extend(dict(section='product_returns',product_key=k[0],unit=k[1],**v) for k,v in sorted(product_returns.items(), key=lambda x:str(x[0])))
        metadata['interpretation'] = 'Отношение возвратов к продажам периода, не доля возвращённых исходных покупок; связи с исходным чеком нет.'
    elif kind == 'discounts':
        groups = defaultdict(list)
        for s in positive:
            groups[str(s['point_id'])].append(s)
        for point, ss in sorted(groups.items()):
            known = [s for s in ss if s.get('explicit_discount') is not None]
            rows.append(dict(point_id=point, purchases=len(ss), discount_known=len(known),
                discount_unknown=len(ss)-len(known),
                explicit_discount=sum((dec(s['explicit_discount']) for s in known),ZERO) if known else None,
                purchases_with_discount=sum(dec(s['explicit_discount'])>0 for s in known)))
        prices = defaultdict(lambda: {'quantity':ZERO,'amount':ZERO})
        valid = {(s['point_id'],s['sale_id']) for s in positive}
        for i in items:
            if (i['point_id'],i['sale_id']) in valid and not i['is_return']:
                key = (i['product_key'],i['unit_name'])
                prices[key]['quantity'] += dec(i['quantity'])
                prices[key]['amount'] += dec(i['total_price'])
        rows.extend(dict(section='effective_prices',product_key=k[0],unit=k[1],**v,average_price=ratio(v['amount'],v['quantity'])) for k,v in sorted(prices.items(), key=lambda x:str(x[0])))
        metadata['interpretation'] = 'Только явный TotalDiscount: отсутствие поля неизвестно. Скидка не означает бонусы. Цена = сумма строк / количество, не прайс и не маржа.'
    elif kind == 'anomalies':
        daily = defaultdict(list)
        for s in sales:
            if not s['is_return']:
                daily[(s['point_id'],s['business_date'])].append(s)
        for (point, day), ss in sorted(daily.items()):
            if not first<=day<=last:
                continue
            history = [stats(daily[(point,day-timedelta(days=7*k))]) for k in range(1,9) if (point,day-timedelta(days=7*k)) in daily]
            v = stats(ss)
            for metric in ('amount','purchases','average'):
                values = [dec(h[metric]) for h in history if h[metric] is not None]
                baseline = median(values) if values else None
                deviation = ratio(dec(v[metric])-baseline,baseline) if baseline is not None else None
                rows.append(dict(point_id=str(point),date=day.isoformat(),metric=metric,value=v[metric],
                    baseline_median=baseline, observed_comparable_days=len(values), relative_change=deviation,
                    candidate=bool(len(values)>=4 and deviation is not None and abs(deviation)>=Decimal('0.30'))))
        metadata['interpretation'] = 'Кандидат: отклонение ≥30% от медианы ≥4 из 8 предыдущих одинаковых дней недели. Эвристика, не доказанная проблема. Пропуски не считаются нулями.'
    else:
        raise ValueError('Неизвестный отчёт')
    return rows, metadata


async def report(kind, start, end, point_id='', previous_start='', previous_end='', offset=0, min_pairs=3, export_all=False):
    if kind not in KINDS or offset<0 or min_pairs<1:
        raise ValueError('Некорректный вид отчёта, offset или min_pairs')
    first,last = dates(start,end)
    if (last-first).days>=366:
        raise ValueError('Выберите не более 366 дней')
    point = await point_filter(point_id)
    base_first=base_last=None
    lower = first-timedelta(days=56) if kind=='anomalies' else first
    if kind in ('decomposition','substitution'):
        base_first,base_last=dates(previous_start,previous_end)
        if base_last>=first or (base_last-base_first).days>=366:
            raise ValueError('Базовый период должен предшествовать текущему')
        lower=min(lower,base_first)
    elif previous_start or previous_end:
        raise ValueError('Базовый период поддерживает только decomposition')
    if kind in ('customers','retention') or kind in insights.HISTORY_KINDS:
        lower=None
    query_point=None if kind in insights.NETWORK_KINDS else point
    async with pool().acquire() as conn:
        async with conn.transaction(isolation='repeatable_read',readonly=True):
            sales=[dict(r) for r in await conn.fetch('''SELECT point_id,sale_id,business_date,sale_datetime,business_shift_type,
                seller_id,customer_id,total_price,is_return,
                raw_json->>'TotalDiscount' explicit_discount
                FROM retail_sales WHERE NOT deleted AND business_date<=$2
                AND ($1::date IS NULL OR business_date>=$1) AND ($3::bigint IS NULL OR point_id=$3)
                ORDER BY point_id,sale_id LIMIT 500001''',lower,last,query_point)]
            if len(sales)>500000:
                raise ValueError('Более 500000 продаж; выберите магазин или меньший период')
            items=[]
            if kind in ('copurchases','returns','discounts') or kind in insights.ITEM_KINDS:
                items=[dict(r) for r in await conn.fetch('''SELECT i.point_id,i.sale_id,i.name,i.unit_name,
                    i.quantity,i.total_price,i.is_return,
                    CASE WHEN NULLIF(i.product_uuid,'') IS NOT NULL THEN 'uuid:'||i.product_uuid
                         WHEN i.product_id IS NOT NULL THEN 'id:'||i.product_id::text ELSE NULL END product_key
                    FROM retail_sale_items i JOIN retail_sales s USING(point_id,sale_id)
                    WHERE NOT s.deleted AND NOT i.refused AND s.business_date BETWEEN $1 AND $2
                    AND COALESCE(LOWER(i.raw_json->>'IsModifier'),'false') NOT IN ('true','1')
                    AND ($3::bigint IS NULL OR s.point_id=$3) LIMIT 1000001''',lower,last,query_point)]
                if len(items)>1000000:
                    raise ValueError('Более 1000000 строк; сузьте период')
            if kind=='loyalty':
                bonuses=await conn.fetch('''SELECT s.point_id,s.sale_id,b.response,
                    b.status='OK' AND b.fingerprint=md5(s.raw_json::text) AS current
                    FROM retail_sales s JOIN bonus_observations b USING(point_id,sale_id)
                    WHERE NOT s.deleted AND NOT s.is_return AND s.customer_id IS NOT NULL
                    AND s.business_date BETWEEN $1 AND $2 AND ($3::bigint IS NULL OR s.point_id=$3)
                    LIMIT 500001''',first,last,point)
                bonus_map={(r['point_id'],r['sale_id']):r for r in bonuses}
                for s in sales:
                    b=bonus_map.get((s['point_id'],s['sale_id']))
                    if b: s.update(bonus_response=b['response'],bonus_current=b['current'])
            if kind in ('crosssell','seller_comparison') and insights.rules().get('crosssell'):
                from app.services.sale_tree import nodes
                lookup={(s['point_id'],s['sale_id']):s for s in sales}
                # Stream raw receipts, retaining only stable modifier keys, not payloads.
                async for r in conn.cursor('''SELECT point_id,sale_id,raw_json FROM retail_sales
                    WHERE NOT deleted AND NOT is_return AND business_date BETWEEN $1 AND $2
                    AND ($3::bigint IS NULL OR point_id=$3)''',first,last,point,prefetch=100):
                    keys=set()
                    for node in nodes(r['raw_json']):
                        i=node['item']
                        if i.get('IsModifier') is True and not node['is_return'] and not node['refused']:
                            if i.get('NomenclatureUUID'): keys.add('uuid:'+str(i['NomenclatureUUID']))
                            elif i.get('Nomenclature'): keys.add('id:'+str(i['Nomenclature']))
                    s=lookup.get((r['point_id'],r['sale_id']))
                    if s is not None: s['modifier_keys']=keys
            sync=await conn.fetchrow('SELECT status,started_at,finished_at FROM sync_runs ORDER BY id DESC LIMIT 1')
    if kind in insights.KINDS:
        rows,metadata=insights.calculate(kind,sales,items,first,last,base_first,base_last,point)
    else:
        rows,metadata=calculate(kind,sales,items,first,last,base_first,base_last,min_pairs)
    metadata['unidentified_item_rows']=sum(i['product_key'] is None for i in items)
    metadata['observed_date_from']=min((s['business_date'] for s in sales),default=None)
    metadata['period_closed']=last<datetime.now(ZoneInfo(get_settings().business_tz)).date()
    result=dict(kind=kind,date_from=start,date_to=end,point_id=str(point) if point else None,
        previous_start=previous_start,previous_end=previous_end,complete_source=False,
        last_sync=dict(sync) if sync else None,generated_at=datetime.now(ZoneInfo(get_settings().business_tz)).isoformat(),
        metadata=metadata,total_rows=len(rows),rows=rows if export_all else rows[offset:offset+50],
        next_offset=offset+50 if offset+50<len(rows) and not export_all else None,note=NOTE)
    return result


TITLES = dict(zip(CORE_KINDS, ('Изменение суммы продаж: покупки и средняя покупка','Продажи по часам и дням недели',
    'Совместные покупки','Новые и вернувшиеся клиенты','Удержание по месячным когортам',
    'Возвраты','Скидки и фактические цены','Необычные изменения продаж')))
TITLES.update(dict(zip(insights.KINDS,('Допродажи','Динамика замещения товаров','Регулярность продаж товаров',
    'Локальный ассортимент','Структура покупок','Переходы клиентов между магазинами','Возвраты клиентов после перерыва',
    'Ценность клиентских групп','Концентрация продаж','Сопоставимые учётки и смены','Бонусы в покупках','Трафик и конверсия'))))
LABELS = {
    'point_id':'магазин', 'seller_id':'учётка', 'purchases':'покупок', 'amount':'сумма продаж',
    'average':'средняя покупка', 'returns':'возвраты', 'previous':'база', 'current':'текущий',
    'amount_delta':'изменение суммы','purchase_count_effect':'вклад числа покупок','average_purchase_effect':'вклад средней покупки',
    'previous_per_day':'база в день','current_per_day':'текущий в день','business_weekday':'бизнес-день недели (1=пн)',
    'local_hour':'местный час','calendar_weekdays':'таких дней в календаре','observed_days':'дней с записями',
    'amount_per_calendar_weekday':'сумма на календарный день недели','joint_purchases':'совместных покупок',
    'eligible_purchases':'всего покупок','support':'доля совместных покупок','confidence_a_to_b':'доля B среди покупок A',
    'confidence_b_to_a':'доля A среди покупок B','lift':'отношение к независимому сочетанию',
    'segment':'группа','customers':'клиентов','cohort':'месяц первого наблюдения','month':'месяц визита',
    'cohort_size':'размер когорты','active_customers':'приходили','calendar_month_closed':'месяц закрыт',
    'retention':'доля вернувшихся','return_records':'возвратных продаж','return_amount_to_sales':'возвраты / сумма продаж',
    'quantity':'количество','unit':'единица','discount_known':'скидка известна','discount_unknown':'скидка неизвестна',
    'explicit_discount':'явная скидка','purchases_with_discount':'покупок со скидкой','average_price':'средняя фактическая цена',
    'date':'дата','metric':'показатель','value':'значение','baseline_median':'медиана базы',
    'observed_comparable_days':'дней в базе','relative_change':'относительное изменение','candidate':'требует внимания',
    'product_a':'товар A ID','product_b':'товар B ID','name_a':'товар A','name_b':'товар B','product_key':'товар ID',
    'section':'раздел',
    'rule':'правило','attachment_rate':'доля допродажи','item_amount':'сумма строк',
    'group':'группа','period':'период','days_with_sales':'дней с продажами','calendar_days':'календарных дней',
    'observed_store_days':'дней с продажами магазина','first_observed':'первое наблюдение','last_observed':'последнее наблюдение',
    'selling_day_share':'доля наблюдаемых дней с товаром','local_amount_share':'доля суммы в магазине',
    'network_amount_share':'доля суммы в сети','affinity':'отношение долей магазин/сеть','dimension':'разрез',
    'purchase_share':'доля покупок','from_point':'из магазина','to_point':'в магазин','customer_id':'ID клиента',
    'return_date':'дата возвращения','gap_days':'перерыв дней','followup_30d_closed':'окно 30 дней закрыто',
    'another_visit_30d':'ещё визит за 30 дней','known_customer_amount_share':'доля суммы известных клиентов',
    'top_n':'первые N','groups':'всего групп','covered_amount':'сумма базы','share':'доля',
    'weekday':'день недели','shift':'смена','distinct_products_per_purchase':'разных товаров на покупку',
    'peers_average_purchase':'средняя покупка остальных учёток','peer_purchases':'покупок остальных учёток',
    'customers_multiple_visit_days':'клиентов с повторными днями визита','bonus_debit_points':'известно списано баллов',
    'bonus_to_accrue_points':'известно к начислению баллов','known_debit_values':'известных значений списания',
    'known_accrual_values':'известных значений начисления','name':'товар','observed_sales':'наблюдаемых продаж',
    'crosssell':'доли допродаж по правилам','confirmed_responses':'подтверждённых ответов',
    'coverage':'покрытие бонусными ответами','observed_debit_purchases':'покупок с известным списанием',
    'observed_debit_purchase_share':'доля покупок с известным списанием',
}


def render(result):
    values={'new_observed':'впервые наблюдаемые','returning_observed':'повторные наблюдаемые',
        'frequent_4plus_days':'частые: от 4 дней посещения','infrequent_large_1000plus':'редкие, средняя покупка от 1000 ₽',
        'other_returning':'остальные повторные','no_customer_no_card':'без клиента — без карты',
        'unconfirmed':'бонусы не подтверждены','debit_observed':'известно списание',
        'to_accrue_observed':'известно к начислению','response_without_positive_operation':'ответ без положительной операции',
        'daily_coverage':'дневное покрытие','unknown_or_empty':'нет известных товарных ID',
        'products':'товары','customers':'клиенты','amount':'сумма покупки','previous':'база','current':'текущий',
        'base':'основная группа','target':'дополнительная группа','combined':'обе группы',
        'product_returns':'возвраты товаров','effective_prices':'фактические цены','NETWORK':'сеть'}
    def display(v):
        if v is None: return 'н/д'
        if isinstance(v,bool): return 'да' if v else 'нет'
        if isinstance(v,Decimal): return str(v.quantize(Decimal('0.0001'))).rstrip('0').rstrip('.') if v else '0'
        if isinstance(v,dict): return ', '.join(f'{LABELS.get(k,k)}: {display(x)}' for k,x in v.items())
        return values.get(str(v),str(v))
    lines=[f"{TITLES[result['kind']]}: {result['date_from']} → {result['date_to']}",
        'Область: '+str(result['point_id'] or 'сеть'),
        'Доли в диапазоне 0…1; 0.25 = 25%.',result['metadata'].get('interpretation','')]
    if result.get('previous_start'): lines.append(f"База: {result['previous_start']} → {result['previous_end']}")
    lines.append('Период завершён: '+display(result['metadata'].get('period_closed')))
    lines.append('Последняя загрузка: '+str((result.get('last_sync') or {}).get('status','неизвестно')))
    if result['metadata'].get('status'): lines.append('Состояние: '+result['metadata']['status'])
    if 'absent_here_active_elsewhere' in result['metadata']:
        lines.append('Не покупали в выбранном магазине, но покупали в других: '+str(result['metadata']['absent_here_active_elsewhere']))
    lines.extend(display(r) for r in result['rows'])
    lines.extend([f"Строк: {result['total_rows']}; следующая страница offset={result['next_offset']}",NOTE])
    return '\n'.join(lines)
