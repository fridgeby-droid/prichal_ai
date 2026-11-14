"""Button reports reuse existing calculations; no model or Saby calls."""
from datetime import date,timedelta
from app.services import management_reports as m,detail_reports as d,advanced_reports as a
from app.services.assistant_briefs import build
from app.services.personal_followups import closed,now
from app.services.report_helpers import export_rows,csv_bytes

TITLES={'sales':'Итоги продаж','comparison':'Сравнение периодов','drivers':'Разбор изменений',
        'card':'Карточка магазина','daynight':'Дневная и ночная смены','averages':'Средний чек',
        'sellers':'Сравнение учёток','products':'Лидеры товаров','categories':'Категории',
        'quantities':'Количество товаров','modifiers':'Модификаторы','basket':'Наполняемость покупки',
        'returns':'Возвраты','clients':'Группы клиентов','loyalty':'Бонусы','customers':'Новые и повторные клиенты',
        'quality':'Сверка данных'}
DETAIL={'averages','quantities','modifiers','basket','clients'}
ADVANCED={'returns','loyalty','customers','sellers'}
CSV_KINDS=(DETAIL-{'basket'})|ADVANCED
COMPARE_KINDS={'sales','comparison','drivers','products','categories','daynight'}


def period(choice,stamp=None):
    end=closed(stamp or now())
    if choice=='day': first=end
    elif choice=='week': first=end-timedelta(days=6)
    elif choice=='month':
        if (end+timedelta(days=1)).day!=1: end=end.replace(day=1)-timedelta(days=1)
        first=end.replace(day=1)
    elif choice=='mtd': first=end.replace(day=1)
    else: raise ValueError('Неизвестный период')
    return str(first),str(end)


def previous(start,end):
    first,last=date.fromisoformat(start),date.fromisoformat(end)
    if first.day==1 and (last+timedelta(days=1)).day==1:
        prev=first-timedelta(days=1)
        return str(prev.replace(day=1)),str(prev)
    return str(first-timedelta(days=(last-first).days+1)),str(first-timedelta(days=1))


async def calculate(s,export=False):
    kind=s['report'];start,end=s['start'],s['end'];point=s.get('point','')
    offset=0 if export else s.get('offset',0)
    if kind in DETAIL:
        args=dict(kind=kind,start=start,end=end,point_id=point,shift=s.get('shift',''),
                  query=s.get('query',''),min_days=s.get('min_days',0),max_days=s.get('max_days'),
                  match='contains',offset=offset,export_all=export)
        return await d.report(**args)
    if kind in ADVANCED:
        return await a.report('seller_comparison' if kind=='sellers' else kind,start,end,point_id=point,offset=offset,export_all=export)
    if kind=='card': return await build('store_card',start,end,point)
    if kind=='quality':
        if (date.fromisoformat(end)-date.fromisoformat(start)).days>=7:
            raise ValueError('Для сверки выберите не более 7 дней.')
        return await m.run_quality(start,end,point)
    before=previous(start,end) if s.get('compare') or kind in ('comparison','drivers') else ('','')
    return await m.report(start,end,*before,point_id=point,kind='summary' if kind=='sales' else kind,limit=10)


def compact(data,title):
    if 'metrics' in data:
        x=data['metrics'];lines=[title]
        if data.get('previous_from'): lines.append(f"База: {data['previous_from']} → {data['previous_to']}")
        for key,label in [('revenue','Выручка'),('checks','Платёжные записи'),('average','Средний чек'),('returns','Возвраты')]:
            row=x[key];value=row['current'] if data.get('has_current_records',True) else None
            line=f'{label}: {m.money(value)}'+(' ₽' if key!='checks' else '')
            if row.get('change'): line+=f" · Δ {m.money(row['change']['percent'])}%"
            lines.append(line)
        if data.get('has_previous'):
            revenue=x['revenue']
            lines.append(f"Выручка в день: {m.money(revenue['previous_per_day'])} → {m.money(revenue['current_per_day'])} ₽")
            if not data.get('equal_duration'): lines.append('Периоды разной длины: сравнивайте также среднее за день.')
        for name,group in data.get('dimensions',{}).items():
            lines.append({'stores':'По магазинам','products':'Суммы товаров','categories':'Суммы категорий','daynight':'По сменам'}.get(name,name))
            for r in group['rows'][:10]:
                label={'DAY':'Дневная смена','NIGHT':'Ночная смена'}.get(r['name'],r['name'])
                line=f"• {label}: {m.money(r['current']) if r['observed_current'] else 'нет записей'}"
                if r.get('change') and not r.get('observation_warning'): line+=f" · Δ {m.money(r['change']['absolute'])}"
                elif r.get('observation_warning'): line+=' · сравнение недоступно'
                lines.append(line)
                if name=='daynight':
                    lines.append(f"  Платежей: {m.money(r.get('checks_current'))} · средний чек: {m.money(r.get('average_current'))} ₽")
        return '\n'.join(lines)
    if 'sections' in data:
        lines=[title,f"{data.get('date_from','')} → {data.get('date_to','')}"]
        for name,section in data['sections'].items():
            if name=='drivers': lines.append(compact(section,'Продажи'))
            elif name=='clients': lines.append('Клиенты по давности: '+str(section.get('segments',{})))
            elif name=='loyalty': lines.append(advanced_preview(section,'Бонусы'))
        if data.get('errors'): lines.append('Недоступные разделы: '+', '.join(e['section'] for e in data['errors']))
        return '\n\n'.join(lines)
    if 'metadata' in data:
        return advanced_preview(data,title)
    if data.get('kind')=='averages':
        lines=[title]
        for group,rows in data.get('groups',{}).items():
            lines.append({'network':'Выборка','stores':'Магазины','sellers':'Учётки'}.get(group,group))
            for r in rows[:10]: lines.append(f"• {r.get('name') or r['id']}: {m.money(r['average'])} ₽ · {r['checks']} платежей")
        return '\n'.join(lines)
    if data.get('kind') in ('quantities','modifiers'):
        if not data.get('rows'): return title+'\nНет записей по выбранным фильтрам.'
        return title+'\n'+'\n'.join(f"• {r['name']} · {r.get('seller_name') or 'учётка не указана'}: {r['quantity']} {r['unit']} · {m.money(r['revenue'])} ₽" for r in data.get('rows',[])[:10])
    if data.get('kind')=='clients':
        lines=[title,f"В выбранной группе: {data['selected_clients']}",'Сначала дольше не покупавшие']
        lines += [f"• Клиент {r['customer_id']}: {r['days_since_purchase']} дней · последняя покупка {r['last_purchase']}" for r in data['rows'][:10]]
        return '\n'.join(lines)
    return title+'\n'+data.get('report_text','Нет записей')


def advanced_preview(data,title):
    segments={'new_observed':'Впервые наблюдаемые клиенты','returning_observed':'Повторные клиенты',
              'no_customer_no_card':'Без карты','unconfirmed':'Без подтверждённого бонусного ответа',
              'debit_observed':'Покупки со списанием','to_accrue_observed':'Покупки с суммой к начислению',
              'response_without_positive_operation':'Без положительной бонусной операции'}
    fields={'customers':'Клиентов','purchases':'Покупок','amount':'Сумма продаж, ₽','average':'Средняя покупка, ₽',
            'returns':'Возвраты, ₽','return_records':'Возвратных записей','quantity':'Количество',
            'bonus_debit_points':'Списано баллов','bonus_to_accrue_points':'К начислению баллов',
            'confirmed_responses':'Подтверждённых ответов','eligible_purchases':'Покупок с клиентом',
            'observed_days':'Дней с записями','peer_purchases':'Покупок остальных учёток',
            'peers_average_purchase':'Средняя покупка остальных, ₽'}
    lines=[title]
    for row in data.get('rows',[])[:10]:
        name=segments.get(row.get('segment')) or row.get('seller_name') or row.get('date')
        if not name: name='Учётка '+str(row['seller_id']) if row.get('seller_id') else 'Товар '+str(row['product_key']) if row.get('product_key') else 'Выборка'
        if row.get('shift'): name+=' · '+{'DAY':'Дневная смена','NIGHT':'Ночная смена'}.get(row['shift'],row['shift'])
        if row.get('weekday'): name+=' · '+['Пн','Вт','Ср','Чт','Пт','Сб','Вс'][row['weekday']-1]
        lines.append('\n'+name)
        for key,label in fields.items():
            if key in row: lines.append(f'{label}: {m.money(row[key])}')
        if row.get('sample_status') in ('LOW_SAMPLE','NO_PEERS'): lines.append('Недостаточно сопоставимых наблюдений для рейтинга.')
    if not data.get('rows'): lines.append('Нет записей по выбранным фильтрам.')
    if data.get('metadata',{}).get('status'): lines.append('Состояние: '+str(data['metadata']['status']))
    return '\n'.join(lines)


async def export(s):
    if s['report'] not in CSV_KINDS: raise ValueError('CSV недоступен для этого отчёта')
    data=await calculate(s,export=True)
    filters={k:s.get(k) for k in ('report','point','shift','query','min_days','max_days','start','end')}
    rows=export_rows(data,filters)
    if 'metadata' in data:
        rows=[{**r,'basis':data['metadata'].get('basis')} for r in rows]
    content=csv_bytes(rows)
    if len(content)>45*1024*1024: raise ValueError('Файл больше 45 МБ; сузьте период или магазин.')
    return content,f"prichal-{s['report']}.csv",len(rows)


def memory_report(s):
    if s['report'] in DETAIL-{'basket'}:
        return {'kind':'detail','arguments':dict(kind=s['report'],start=s['start'],end=s['end'],
                point_id=s.get('point',''),shift=s.get('shift',''),query=s.get('query',''),
                min_days=s.get('min_days',0),max_days=s.get('max_days'))}
    if s['report'] in ADVANCED:
        return {'kind':'advanced','arguments':dict(kind='seller_comparison' if s['report']=='sellers' else s['report'],
                start=s['start'],end=s['end'],point_id=s.get('point',''))}
    return {'kind':'unsupported','arguments':{'kind':s['report'],'point_id':s.get('point','')}}
