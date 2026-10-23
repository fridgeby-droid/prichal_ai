"""One consistent read, deterministic numbers and rendering for management reports."""
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from app.db.database import pool
from app.services import period_reports as periods
from app.services import catalog_categories as catalog_service
from app.version import APP_VERSION
from app.services.store_calendar import cohort

KINDS = {'summary','comparison','drivers','daynight','products','categories','comparable'}
LATEST_SQL = '''SELECT id,status,started_at,finished_at,date_from,date_to
 FROM sync_runs ORDER BY id DESC LIMIT 1'''
ACTIVE_SQL = "SELECT EXISTS(SELECT 1 FROM sync_runs WHERE status='RUNNING')"


def dec(value):
    return Decimal(str(value or 0))


def stamp(value):
    return value.isoformat() if hasattr(value,'isoformat') else value


def signature(latest, active):
    return {'latest':{k:stamp(v) for k,v in dict(latest).items()} if latest else None,
            'active':bool(active),'report_version':APP_VERSION}


def check_key(point, day):
    return f'reports:reconcile:v1:{point or "network"}:{day}'


def days(first,last):
    return [first+timedelta(days=i) for i in range((last-first).days+1)]


def matching_quality(q, first,last,cache,mark,point):
    checked, missing, stale = [],[],[]
    for day in days(first,last):
        item=cache.get(check_key(point,day))
        if not item:
            missing.append(day.isoformat())
        elif mark['active'] or item['sync_signature']!=mark:
            stale.append(day.isoformat())
        else:
            checked.append({'date':day.isoformat(),'status':item['status'],'checked_at':item['checked_at']})
    state='NOT_CHECKED'
    if any(x['status']=='REVIEW' for x in checked): state='REVIEW'
    elif stale: state='STALE'
    elif missing: state='PARTIAL' if checked else 'NOT_CHECKED'
    elif checked: state='NO_DATA' if any(x['status']=='NO_DATA' for x in checked) else 'OK'
    return {**q,'reconciliation':state,'checked_dates':checked,'unchecked_dates':missing,
            'stale_dates':stale,'sync_in_progress':mark['active']}


def measures(rows):
    total=periods.totals(rows)
    total['average']=total['revenue']/total['checks'] if total['checks'] else None
    total['has_records']=bool(total['records'])
    return total


def comparison(current, previous, current_days, previous_days, available=True):
    current,previous=dec(current),dec(previous)
    return {'current':current,'previous':previous,
            'current_per_day':current/current_days,'previous_per_day':previous/previous_days,
            'change':periods.delta(current,previous) if available else None,
            'per_day_change':periods.delta(current/current_days,previous/previous_days) if available else None}


def dimension(a,b,cd,pd,has_previous,limit=10):
    """Amounts and omitted rows always reconcile; missing observations stay explicit."""
    total_a=sum((dec(r['revenue']) for r in a.values()),Decimal(0))
    total_b=sum((dec(r['revenue']) for r in b.values()),Decimal(0))
    available=has_previous and bool(a) and bool(b)
    total=comparison(total_a,total_b,cd,pd,available)
    rows=[]
    for key in a.keys() | b.keys():
        x,y=a.get(key),b.get(key)
        item=comparison(x['revenue'] if x else 0,y['revenue'] if y else 0,cd,pd,available)
        item.update(id=key,name=(x or y)['name'],observed_current=x is not None,observed_previous=y is not None,
                    quantity_current=x.get('quantity') if x else None,quantity_previous=y.get('quantity') if y else None,
                    unit=(x or y).get('unit'))
        for side,source in [('current',x),('previous',y)]:
            quantity=source.get('quantity') if source else None
            item['average_unit_price_'+side]=dec(source['revenue'])/dec(quantity) if quantity is not None and dec(quantity) else None
        # A missing group can contribute to the observed total but is not proof of zero sales.
        item['observation_warning']=has_previous and (x is None or y is None)
        item['change_percent']=item['change']['percent'] if item['change'] and x and y else None
        item['contribution_percent_of_total_change']=(item['change']['absolute']/total['change']['absolute']*100
            if item['change'] and total['change']['absolute'] else None)
        rows.append(item)
    rows.sort(key=lambda r:(-abs(r['change']['absolute']) if r['change'] else -r['current'],r['id']))
    selected=rows[:limit]
    other=comparison(total_a-sum((r['current'] for r in selected),Decimal(0)),
                     total_b-sum((r['previous'] for r in selected),Decimal(0)),cd,pd,available)
    assert sum((r['current'] for r in selected),Decimal(0))+other['current']==total_a
    assert sum((r['previous'] for r in selected),Decimal(0))+other['previous']==total_b
    return {'totals':total,'rows':selected,'other':other,'omitted_groups':max(0,len(rows)-limit),
            'total_groups':len(rows),'observed_delta_only':True,
            'has_current_records':bool(a),'has_previous_records':bool(b)}


def payment_groups(rows,field,names):
    values=defaultdict(list)
    for r in rows:
        values[str(r[field] or 'UNKNOWN')].append(r)
    return {key:{'name':names.get(key,key),'revenue':measures(part)['revenue']} for key,part in values.items()}


def product_groups(rows):
    return {json.dumps([r['product_key'],r['unit']],ensure_ascii=False):{
        'name':r['name'],'revenue':r['revenue'],'quantity':r['quantity'],'unit':r['unit']} for r in rows}


async def report(start,end,previous_start='',previous_end='',point_id='',kind='summary',limit=10,level='top'):
    if kind not in KINDS or level not in ('top','leaf'):
        raise ValueError('Неизвестный вид отчёта или уровень категории.')
    first,last=periods.period(start,end)
    if bool(previous_start)!=bool(previous_end): raise ValueError('Нужны обе даты сравнения.')
    before=periods.period(previous_start,previous_end) if previous_start else None
    if kind in ('comparison','drivers','comparable') and before is None: raise ValueError('Для сравнения нужны два периода.')
    point=await periods.point_filter(point_id)
    limit=max(1,min(int(limit),50))
    needs_items=kind in ('drivers','products','categories')
    scope=str(point) if point is not None else ''
    async with pool().acquire() as conn:
        async with conn.transaction(isolation='repeatable_read',readonly=True):
            stores=[dict(r) for r in await conn.fetch('SELECT point_id,name FROM retail_stores WHERE ($1::BIGINT IS NULL OR point_id=$1) ORDER BY name',point)]
            latest=await conn.fetchrow(LATEST_SQL)
            active=await conn.fetchval(ACTIVE_SQL)
            current=[dict(r) for r in await conn.fetch(periods.PAYMENTS_SQL,first,last,point)]
            previous=[dict(r) for r in await conn.fetch(periods.PAYMENTS_SQL,*before,point)] if before else []
            current_items=[dict(r) for r in await conn.fetch(periods.PRODUCTS_SQL,first,last,point)] if needs_items else []
            previous_items=[dict(r) for r in await conn.fetch(periods.PRODUCTS_SQL,*before,point)] if needs_items and before else []
            catalog=await catalog_service.read_catalog(conn) if kind in ('categories','drivers') else None
            comparable_items=None
            if needs_items and before:
                included,_=cohort(stores,current,previous,first,last,*before)
                ids=[int(r['point_id']) for r in included]
                sql=periods.PRODUCTS_SQL.replace('GROUP BY product_key,i.unit_name','AND i.point_id=ANY($4::BIGINT[]) GROUP BY product_key,i.unit_name')
                comparable_items=([dict(r) for r in await conn.fetch(sql,first,last,point,ids)],
                                  [dict(r) for r in await conn.fetch(sql,*before,point,ids)])
            all_days=set(days(first,last)) | (set(days(*before)) if before else set())
            saved=await conn.fetch('SELECT key,value FROM app_meta WHERE key=ANY($1::TEXT[])',
                                   [check_key(scope,d) for d in sorted(all_days)])
            cache={r['key']:json.loads(r['value']) if isinstance(r['value'],str) else r['value'] for r in saved}
            mark=signature(latest,active)
            quality=[]
            for a,z,records in [(first,last,current)]+([(before[0],before[1],previous)] if before else []):
                success=await conn.fetchrow("SELECT status,finished_at,date_from,date_to FROM sync_runs WHERE status='OK' AND date_from<=$2 AND date_to>=$1 ORDER BY finished_at DESC LIMIT 1",a,z)
                q=periods.quality(records,stores,a,z,dict(latest) if latest else None,dict(success) if success else None)
                quality.append(matching_quality(q,a,z,cache,mark,scope))
    cd=(last-first).days+1
    pd=(before[1]-before[0]).days+1 if before else cd
    a,b=measures(current),measures(previous)
    metrics={key:comparison(a[key],b[key],cd,pd,bool(before and a['has_records'] and b['has_records']))
             for key in ('revenue','checks','return_checks','returns')}
    metrics['average']={'current':a['average'],'previous':b['average'],
                        'change':periods.delta(a['average'],b['average']) if before and a['average'] is not None and b['average'] is not None else None}
    names={str(s['point_id']):s['name'] for s in stores}
    dimensions={}
    if kind in ('summary','comparison','drivers'):
        dimensions['stores']=dimension(payment_groups(current,'point_id',names),payment_groups(previous,'point_id',names),cd,pd,bool(before),limit)
    if kind in ('daynight','drivers'):
        dimensions['daynight']=dimension(payment_groups(current,'shift_type',{}),payment_groups(previous,'shift_type',{}),cd,pd,bool(before),50)
    if kind in ('products','drivers'):
        dimensions['products']=dimension(product_groups(current_items),product_groups(previous_items),cd,pd,bool(before),limit)
    category_meta=None
    if kind in ('categories','drivers'):
        ca=catalog_service.aggregate(current_items,catalog,level)
        cb=catalog_service.aggregate(previous_items,catalog,level)
        dimensions['categories']=dimension(ca,cb,cd,pd,bool(before),limit)
        category_meta={'status':'AVAILABLE' if catalog else 'NOT_LOADED','fetched_at':catalog['fetched_at'] if catalog else None,
                       'revision':catalog['revision'] if catalog else None,'level':level,
                       'unmapped_current':ca.get('UNMAPPED',{}).get('revenue',0),
                       'unmapped_previous':cb.get('UNMAPPED',{}).get('revenue',0),
                       'returns_current':sum(dec(r['returns']) for r in ca.values()),
                       'returns_previous':sum(dec(r['returns']) for r in cb.values())}
    # Independent decompositions: never sum store/shift/product/category contributions together.
    for key in ('stores','daynight'):
        if key in dimensions:
            assert dimensions[key]['totals']['current']==a['revenue']
            assert dimensions[key]['totals']['previous']==b['revenue']
    result={'schema_version':1,'kind':kind,'date_from':start,'date_to':end,
            'previous_from':previous_start or None,'previous_to':previous_end or None,
            'point_id':scope or None,'current_days':cd,'previous_days':pd if before else None,
            'equal_duration':cd==pd if before else None,'has_previous':bool(before),
            'has_current_records':a['has_records'],'has_previous_records':b['has_records'],
            'generated_at':datetime.now(timezone.utc).isoformat(),'metrics':metrics,
            'dimensions':dimensions,'catalog':category_meta,
            'items':{'current_returns':sum(dec(r['returns']) for r in current_items),
                     'previous_returns':sum(dec(r['returns']) for r in previous_items)} if needs_items else None,
            'quality_current':quality[0],'quality_previous':quality[1] if before else None}
    if before:
        included, excluded = cohort(stores,current,previous,first,last,*before)
        ids={r['point_id'] for r in included}
        ca=[r for r in current if str(r['point_id']) in ids]
        cb=[r for r in previous if str(r['point_id']) in ids]
        ma,mb=measures(ca),measures(cb)
        result['comparable']={
            'included':included,'excluded':excluded,
            'metrics':{k:comparison(ma[k],mb[k],cd,pd,bool(ids)) for k in ('revenue','checks','returns')},
            'average':{'current':ma['average'],'previous':mb['average'],
                       'change':periods.delta(ma['average'],mb['average']) if ma['average'] is not None and mb['average'] is not None else None},
            'stores':dimension(payment_groups(ca,'point_id',names),payment_groups(cb,'point_id',names),cd,pd,True,50),
            'daynight':dimension(payment_groups(ca,'shift_type',{}),payment_groups(cb,'shift_type',{}),cd,pd,True,50),
            'excluded_revenue':comparison(a['revenue']-ma['revenue'],b['revenue']-mb['revenue'],cd,pd,bool(a['has_records'] and b['has_records']))}
        if comparable_items is not None:
            ia,ib=comparable_items
            result['comparable']['products']=dimension(product_groups(ia),product_groups(ib),cd,pd,True,10)
            if catalog is not None:
                result['comparable']['categories']=dimension(catalog_service.aggregate(ia,catalog,level),catalog_service.aggregate(ib,catalog,level),cd,pd,True,50)
        result['brief']={'network_per_day':metrics['revenue']['per_day_change'],
                         'comparable_per_day':result['comparable']['metrics']['revenue']['per_day_change'],
                         'top_observed_changes':dimensions.get('stores',{}).get('rows',[])[:3]}
    result['report_text']=format_report(result)
    return result


async def run_quality(start,end,point_id=''):
    first,last=periods.period(start,end)
    if (last-first).days>=7: raise ValueError('Проверка сверки: не более 7 дней за один запрос.')
    point=await periods.point_filter(point_id)
    scope=str(point) if point is not None else ''
    async with pool().acquire() as conn:
        initial=signature(await conn.fetchrow(LATEST_SQL),await conn.fetchval(ACTIVE_SQL))
    result=await periods.status(start,end,scope)
    checked_at=datetime.now(timezone.utc).isoformat()
    async with pool().acquire() as conn:
        async with conn.transaction():
            final=signature(await conn.fetchrow(LATEST_SQL),await conn.fetchval(ACTIVE_SQL))
            valid=initial==final and not initial['active']
            if valid:
                for item in result['checks']:
                    value={'status':item['status'],'checked_at':checked_at,'sync_signature':final}
                    await conn.execute('''INSERT INTO app_meta(key,value,updated_at) VALUES($1,$2::JSONB,NOW())
                        ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=NOW()''',
                        check_key(scope,item['date']),json.dumps(value))
    # Re-read the standard quality block; no recalculation by the language model.
    refreshed=await report(start,end,point_id=scope)
    result['quality']=refreshed['quality_current']
    result['saved_check']=valid
    result['checked_at']=checked_at
    result['report_text']='Проверка локальной сверки '+start+' → '+end+'\n'+format_quality(result['quality'])
    if not valid: result['report_text']+='\nВо время проверки менялась загрузка: результат не сохранён. Повторите после sync.'
    return result


def money(value):
    if value is None: return 'н/д'
    return f"{dec(value).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP):,.2f}".replace(',',' ').replace('.',',')


def date_ranges(values):
    values=sorted(set(values))
    if not values: return 'нет'
    spans=[]
    start=previous=periods.date.fromisoformat(values[0])
    for value in values[1:]:
        day=periods.date.fromisoformat(value)
        if day!=previous+timedelta(days=1):
            spans.append(str(start) if start==previous else f'{start}…{previous}')
            start=day
        previous=day
    spans.append(str(start) if start==previous else f'{start}…{previous}')
    return ', '.join(spans)


def format_quality(q):
    lines=['Качество: предварительные данные; полнота Saby НЕ ПОДТВЕРЖДЕНА.',
           'Период: '+('завершён' if q['period_closed'] else 'НЕ ЗАВЕРШЁН'),
           'Локальная сверка: '+q['reconciliation']]
    checked=q.get('checked_dates',[])
    for status in ('OK','REVIEW','NO_DATA'):
        group=[x for x in checked if x['status']==status]
        if group: lines.append(f"Проверено {status}: {date_ranges([x['date'] for x in group])}; проверка до {max(x['checked_at'] for x in group)}")
    if q.get('unchecked_dates'): lines.append('Не проверены: '+date_ranges(q['unchecked_dates']))
    if q.get('stale_dates'): lines.append('Сверка устарела после sync: '+date_ranges(q['stale_dates']))
    latest=q.get('last_run')
    lines.append('Последний sync: '+(str(latest['status']) if latest else 'нет запусков'))
    success=q.get('last_success_overlapping_period')
    if success: lines.append(f"Успешный sync пересекающегося периода: {success['finished_at']}; {success['date_from']}…{success['date_to']}; возраст {q['last_success_age_hours']} ч.")
    else: lines.append('Успешная загрузка пересекающегося периода не найдена.')
    for row in q.get('store_dates_without_records',[]):
        lines.append(f"Без записей, point_id {row['point_id']}: {date_ranges(row['dates'])}")
    for row in q.get('expected_before_opening',[]):
        lines.append(f"До открытия {row['opening_date']}, point_id {row['point_id']}: {date_ranges(row['dates'])} — отсутствие ожидаемо.")
    for row in q.get('records_before_opening',[]):
        lines.append(f"ПРОВЕРИТЬ: записи до открытия, point_id {row['point_id']}: {date_ranges(row['dates'])}. Данные сохранены в итогах.")
    if q.get('fallback_records'): lines.append(f"Платёжных записей fallback: {q['fallback_records']}")
    if q.get('sync_in_progress'): lines.append('В БД есть незавершённый sync (RUNNING). Работа процесса не подтверждена: проверьте журнал загрузки. Сверка пока не считается актуальной.')
    lines.append('Нет записей ≠ нулевые продажи. OK сверки не подтверждает полноту источника.')
    return '\n'.join(lines)


def change_text(change):
    if change is None: return 'не сравниваем: нет записей'
    percent=money(change['percent'])+'%' if change['percent'] is not None else 'процент не определён: база 0'
    return money(change['absolute'])+' ('+percent+')'


def format_report(data):
    prev=data['has_previous']
    lines=[f"Отчёт {data['date_from']} → {data['date_to']} ({data['current_days']} дней)",
           'Область: '+('точка '+data['point_id'] if data['point_id'] else 'сеть без РЦ')]
    if prev:
        lines.append(f"База: {data['previous_from']} → {data['previous_to']} ({data['previous_days']} дней)")
        if not data['equal_duration']: lines.append('Длительность различается. Сравнивайте также среднее за календарный бизнес-день.')
    if data.get('brief'):
        lines.extend(['Кратко, по доступным данным:',
                      'Вся сеть, выручка в день: '+change_text(data['brief']['network_per_day']),
                      'Сопоставимая сеть, выручка в день: '+change_text(data['brief']['comparable_per_day']),
                      'Полнота источника не подтверждена; это наблюдаемые изменения, не доказанные причины.'])
    for key,label in [('revenue','Платёжная выручка'),('checks','Чеки (платёжные записи)'),('average','Средний чек'),('returns','Возвраты отдельно'),('return_checks','Возвратные записи')]:
        item=data['metrics'][key]
        current=money(item['current']) if data['has_current_records'] else 'нет записей'
        line=label+': '+current
        if prev:
            prior=money(item['previous']) if data['has_previous_records'] else 'нет записей'
            line=label+': '+prior+' → '+current+'; Δ '+change_text(item['change'])
        lines.append(line)
        if key in ('revenue','checks'):
            line='В среднем за день: '+(money(item['current_per_day']) if data['has_current_records'] else 'нет записей')
            if prev: line+='; база '+(money(item['previous_per_day']) if data['has_previous_records'] else 'нет записей')+'; Δ '+change_text(item['per_day_change'])
            lines.append(line)
    labels={'stores':'Магазины — платёжная выручка','daynight':'DAY/NIGHT — по времени платежа',
            'categories':'Категории — суммы товарных строк','products':'Товары — суммы товарных строк'}
    for key,block in data['dimensions'].items():
        lines.append('\n'+labels[key])
        entries=[(r['name']+(f" [{r['unit']}]" if r.get('unit') else ''),r) for r in block['rows']]
        if block['omitted_groups']: entries.append((f"Остальные группы ({block['omitted_groups']})",block['other']))
        for name,row in entries:
            name=' '.join(name.split())[:160]
            line=name+': '+money(row['current'])
            if prev:
                line=name+': '+money(row['previous'])+' → '+money(row['current'])+'; вклад Δ '+(money(row['change']['absolute']) if row['change'] else 'н/д')
                if 'change_percent' in row:
                    line+='; '+(money(row['change_percent'])+'%' if row['change_percent'] is not None else '% н/д')
                if row.get('observation_warning'): line+=' [нет записей в одном периоде; не доказательство нуля]'
                if not data['equal_duration']:
                    line+='; в день '+money(row['previous_per_day'])+' → '+money(row['current_per_day'])
            lines.append(line)
            if key=='products' and row.get('quantity_current') is not None:
                lines.append('Количество: '+(str(row.get('quantity_previous'))+' → ' if prev else '')+str(row['quantity_current'])+
                             '; средняя цена за единицу: '+(money(row.get('average_unit_price_previous'))+' → ' if prev else '')+money(row.get('average_unit_price_current')))
        # Explicit decimal rounding residual, never a made-up remainder.
        for side,label in [('current','текущий')]+([('previous','база')] if prev else []):
            shown=sum((dec(r[side]).quantize(Decimal('.01'),rounding=ROUND_HALF_UP) for _,r in entries),Decimal(0))
            target=dec(block['totals'][side]).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
            adjustment=target-shown
            if adjustment: lines.append(f'Поправка округления ({label}): {money(adjustment)}')
        lines.append('Итого раздела: '+(money(block['totals']['previous'])+' → ' if prev else '')+money(block['totals']['current']))
        if not block['has_current_records'] or (prev and not block['has_previous_records']):
            lines.append('В одном из периодов нет записей этого источника. Нулевая сумма записей НЕ подтверждает нулевые продажи.')
        if prev:
            lines.append('Изменение раздела: '+change_text(block['totals']['change']))
            lines.append('Среднее за день: '+money(block['totals']['previous_per_day'])+' → '+money(block['totals']['current_per_day'])+'; Δ '+change_text(block['totals']['per_day_change']))
    if data.get('comparable'):
        lines.extend(format_comparable(data['comparable']))
    cat=data['catalog']
    if data.get('items'):
        lines.append('Возвраты по товарным строкам отдельно: '+(money(data['items']['previous_returns'])+' → ' if prev else '')+money(data['items']['current_returns']))
    if cat:
        lines.extend([f"Каталог: {cat['status']}; снимок {cat['fetched_at']}; текущая, НЕ историческая классификация.",
                      'Товары без найденной категории (входят в итог): '+(money(cat['unmapped_previous'])+' → ' if prev else '')+money(cat['unmapped_current'])])
    lines.extend(['\nКАЧЕСТВО ТЕКУЩЕГО ПЕРИОДА',format_quality(data['quality_current'])])
    if prev: lines.extend(['\nКАЧЕСТВО БАЗЫ СРАВНЕНИЯ',format_quality(data['quality_previous'])])
    lines.append('Вклад — изменение доступных записей, не доказанная причина. Разрезы не складываются между собой. Суммы товаров/категорий не равны платёжной выручке. Среднее за день не исправляет пропуски и сезонность.')
    return '\n'.join(lines)


def format_comparable(block):
    lines=['\nСОПОСТАВИМАЯ СЕТЬ — платёжный слой',
           'Отбор: нет известного открытия внутри периодов; есть записи каждого дня обоих периодов. Полнота источника и история закрытий не подтверждены.',
           'Включено магазинов: '+str(len(block['included']))]
    reasons={'OPENED_AFTER_PERIOD_START':'открыт после начала одного из периодов',
             'CURRENT_DATES_WITHOUT_RECORDS':'есть дни без записей текущего периода',
             'PREVIOUS_DATES_WITHOUT_RECORDS':'есть дни без записей базового периода'}
    for row in block['excluded']:
        lines.append('Исключён: '+row['name']+' (ID '+row['point_id']+'): '+
                     '; '.join(reasons[r] for r in row['reasons'])+
                     ('; открыт '+row['opening_date'] if row['opening_date'] else ''))
    if not block['included']:
        lines.append('Нет магазинов для сопоставимого сравнения; изменение не рассчитывается.')
        return lines
    for key,label in [('revenue','Выручка'),('checks','Платёжные записи'),('returns','Возвраты отдельно')]:
        row=block['metrics'][key]
        lines.append(label+': '+money(row['previous'])+' → '+money(row['current'])+'; Δ '+change_text(row['change']))
        lines.append('В день: '+money(row['previous_per_day'])+' → '+money(row['current_per_day'])+'; Δ '+change_text(row['per_day_change']))
    row=block['average']
    lines.append('Средний чек: '+money(row['previous'])+' → '+money(row['current'])+'; Δ '+change_text(row['change']))
    for key,label in [('stores','Магазины сопоставимой сети'),('daynight','DAY/NIGHT сопоставимой сети')]:
        lines.append(label)
        for row in block[key]['rows']:
            lines.append(' '.join(row['name'].split())[:160]+': '+money(row['previous'])+' → '+money(row['current'])+
                         '; Δ '+change_text(row['change'])+'; в день Δ '+change_text(row['per_day_change']))
    row=block['excluded_revenue']
    lines.append('Остальная сеть (новые/исключённые из сравнения магазины): '+money(row['previous'])+' → '+money(row['current'])+'; Δ '+change_text(row['change']))
    lines.append('Выручка всей сети = сопоставимые + остальные магазины. Остальная сеть не означает только эффект открытий.')
    for key in ('products','categories'):
        if key not in block: continue
        section=block[key]
        lines.append(('Товары' if key=='products' else 'Категории')+' сопоставимой сети — суммы товарных строк')
        for row in section['rows']:
            lines.append(' '.join(row['name'].split())[:160]+' ['+str(row.get('unit') or '')+']: '+money(row['previous'])+' → '+money(row['current'])+'; Δ '+change_text(row['change']))
            if key=='products': lines.append('Количество: '+str(row['quantity_previous'])+' → '+str(row['quantity_current'])+'; средняя цена: '+money(row['average_unit_price_previous'])+' → '+money(row['average_unit_price_current']))
        if section['omitted_groups']:
            lines.append('Остальные: '+money(section['other']['previous'])+' → '+money(section['other']['current']))
        shown=section['rows']+([section['other']] if section['omitted_groups'] else [])
        for side in ('previous','current'):
            rounding=dec(section['totals'][side]).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)-sum((dec(r[side]).quantize(Decimal('.01'),rounding=ROUND_HALF_UP) for r in shown),Decimal(0))
            if rounding: lines.append('Поправка округления '+side+': '+money(rounding))
        lines.append('Итого товарного раздела: '+money(section['totals']['previous'])+' → '+money(section['totals']['current']))
    return lines


def chunks(text):
    result=['']
    for line in text.splitlines():
        # Long data labels cannot break the Telegram limit.
        for start in range(0,max(1,len(line)),3500):
            part=line[start:start+3500]
            if len(result[-1])+len(part)+1>3800: result.append('')
            result[-1]+=part+'\n'
    return result
