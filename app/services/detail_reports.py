"""Read-only detailed reports. No Saby calls and no inferred loyalty fields."""
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db.database import pool
from app.services import period_reports as p
from app.services.management_reports import dec, money
from app.services.sale_tree import modifiers
from app.services.report_helpers import quantity,product_totals,frequency


PAYMENTS = '''SELECT p.point_id,p.seller_id,MAX(p.seller_name) seller_name,
 p.business_date,p.business_shift_type shift_type,
 COUNT(*) FILTER(WHERE NOT p.is_return) checks,
 SUM(CASE WHEN p.is_return THEN 0 ELSE ABS(p.amount) END) revenue,
 SUM(CASE WHEN p.is_return THEN ABS(p.amount) ELSE 0 END) returns,
 COUNT(*) FILTER(WHERE p.source='sale_total_fallback') fallback
 FROM retail_sale_payments p JOIN retail_sales s USING(point_id,sale_id)
 WHERE NOT s.deleted AND p.business_date BETWEEN $1 AND $2
 AND ($3::BIGINT IS NULL OR p.point_id=$3)
 AND ($4::BIGINT IS NULL OR p.seller_id=$4)
 AND ($5::TEXT IS NULL OR p.business_shift_type=$5)
 GROUP BY p.point_id,p.seller_id,p.business_date,p.business_shift_type'''

ITEMS = '''SELECT s.point_id,s.seller_id,MAX(s.seller_name) seller_name,
 CASE WHEN NULLIF(i.product_uuid,'') IS NOT NULL THEN 'uuid:'||i.product_uuid
 WHEN i.product_id IS NOT NULL THEN 'id:'||i.product_id::TEXT
 ELSE 'unknown:'||i.point_id::TEXT||':'||i.sale_id::TEXT||':'||i.item_key END product_key,
 MAX(i.name) name,i.unit_name unit,
 SUM(CASE WHEN i.is_return OR s.is_return THEN 0 ELSE i.quantity END) quantity,
 SUM(CASE WHEN i.is_return OR s.is_return THEN ABS(i.quantity) ELSE 0 END) return_quantity,
 SUM(CASE WHEN i.is_return OR s.is_return THEN ABS(i.total_price) ELSE 0 END) return_revenue,
 SUM(CASE WHEN i.is_return OR s.is_return THEN 0 ELSE i.total_price END) revenue,
 COUNT(DISTINCT (i.point_id,i.sale_id)) FILTER(WHERE NOT i.is_return AND NOT s.is_return) purchases
 FROM retail_sale_items i JOIN retail_sales s USING(point_id,sale_id)
 WHERE NOT s.deleted AND NOT i.refused AND s.business_date BETWEEN $1 AND $2
 AND ($3::BIGINT IS NULL OR s.point_id=$3)
 AND ($4::BIGINT IS NULL OR s.seller_id=$4)
 AND ($5::TEXT IS NULL OR s.business_shift_type=$5)
 AND ($6::TEXT IS NULL OR POSITION(LOWER($6) IN LOWER(i.name))>0
 OR i.product_id::TEXT=$6 OR i.product_uuid=$6)
 GROUP BY s.point_id,s.seller_id,product_key,i.unit_name'''

BASKETS = '''SELECT s.point_id,s.sale_id,
 COUNT(i.item_key) FILTER(WHERE NOT i.refused AND NOT i.is_return) lines,
 COUNT(DISTINCT CASE WHEN NULLIF(i.product_uuid,'') IS NOT NULL THEN 'uuid:'||i.product_uuid
 WHEN i.product_id IS NOT NULL THEN 'id:'||i.product_id::TEXT ELSE 'line:'||i.item_key END)
 FILTER(WHERE NOT i.refused AND NOT i.is_return) products
 FROM retail_sales s LEFT JOIN retail_sale_items i USING(point_id,sale_id)
 WHERE NOT s.deleted AND NOT s.is_return AND s.business_date BETWEEN $1 AND $2
 AND ($3::BIGINT IS NULL OR s.point_id=$3)
 AND ($4::BIGINT IS NULL OR s.seller_id=$4)
 AND ($5::TEXT IS NULL OR s.business_shift_type=$5)
 GROUP BY s.point_id,s.sale_id'''

CLIENTS = '''SELECT customer_id,MIN(business_date) first_purchase,MAX(business_date) last_purchase,
 COUNT(*) purchases,COUNT(DISTINCT business_date) visit_days,SUM(total_price) amount
 FROM retail_sales WHERE NOT deleted AND NOT is_return AND customer_id IS NOT NULL
 AND business_date<=$1 AND ($2::BIGINT IS NULL OR point_id=$2)
 GROUP BY customer_id ORDER BY last_purchase,customer_id'''

SALES = '''SELECT point_id,seller_id,MAX(seller_name) seller_name,
 COUNT(*) FILTER(WHERE NOT is_return) checks,
 SUM(CASE WHEN is_return THEN 0 ELSE total_price END) revenue,
 SUM(CASE WHEN is_return THEN ABS(total_price) ELSE 0 END) returns,0 fallback
 FROM retail_sales WHERE NOT deleted AND business_date BETWEEN $1 AND $2
 AND ($3::BIGINT IS NULL OR point_id=$3) AND ($4::BIGINT IS NULL OR seller_id=$4)
 AND ($5::TEXT IS NULL OR business_shift_type=$5)
 GROUP BY point_id,seller_id'''

MODIFIER_SALES = '''SELECT point_id,sale_id,seller_id,seller_name,raw_json
 FROM retail_sales WHERE NOT deleted AND business_date BETWEEN $1 AND $2
 AND ($3::BIGINT IS NULL OR point_id=$3) AND ($4::BIGINT IS NULL OR seller_id=$4)
 AND ($5::TEXT IS NULL OR business_shift_type=$5)'''


def number(value):
    if not value: return None
    if not str(value).isdecimal() or not 0<int(value)<2**63: raise ValueError('Нужен положительный числовой ID.')
    return int(value)


def sums(rows):
    revenue=sum((dec(r['revenue']) for r in rows),Decimal(0))
    checks=sum(r['checks'] for r in rows)
    return {'revenue':revenue,'checks':checks,'average':revenue/checks if checks else None,
            'returns':sum((dec(r['returns']) for r in rows),Decimal(0)),
            'fallback':sum(r['fallback'] for r in rows)}


def grouped(rows, key):
    groups=defaultdict(list)
    for row in rows: groups[key(row)].append(row)
    return [{'id':k,**sums(v)} for k,v in sorted(groups.items())]


def recency(rows, as_of, minimum=0, maximum=None):
    result=[]
    for row in rows:
        age=(as_of-row['last_purchase']).days
        if age<minimum or maximum is not None and age>maximum: continue
        gap=(row['last_purchase']-row['first_purchase']).days
        result.append({**dict(row),'customer_id':str(row['customer_id']),'days_since_purchase':age,
                       'segment':'0-29' if age<30 else '30-59' if age<60 else '60-89' if age<90 else '90+',
                       'mean_days_between_visits':Decimal(gap)/(row['visit_days']-1) if row['visit_days']>1 else None})
    return result


async def report(kind,start='',end='',point_id='',seller_id='',shift='',query='',min_days=0,max_days=None,offset=0,
                 match='contains',frequency_filter='',export_all=False):
    if kind not in ('averages','quantities','modifiers','basket','trend','clients'): raise ValueError('Неизвестный отчёт.')
    if kind=='clients' and (seller_id or shift or query): raise ValueError('Давность клиентов: фильтры продавца, смены и товара не поддерживаются.')
    if query and kind not in ('quantities','modifiers'): raise ValueError('Поиск товара доступен в quantities/modifiers.')
    if match not in ('contains','exact','id'): raise ValueError('match: contains, exact или id')
    if frequency_filter not in ('','one_purchase','multiple_one_day','repeat_visits'): raise ValueError('Неизвестная группа посещений')
    if frequency_filter and kind!='clients': raise ValueError('Группа посещений доступна только для clients')
    point=await p.point_filter(point_id)
    seller=number(seller_id)
    shift=shift.upper() if shift else None
    if shift not in (None,'DAY','NIGHT'): raise ValueError('Смена: DAY или NIGHT.')
    if bool(start)!=bool(end): raise ValueError('Укажите обе даты или оставьте обе пустыми для всей локальной истории.')
    today=datetime.now(ZoneInfo(get_settings().business_tz)).date()
    first,last=(date.fromisoformat(start),date.fromisoformat(end)) if start else (None,today)
    if first and (first>last or last>today): raise ValueError('Некорректный период.')
    if min_days<0 or max_days is not None and max_days<min_days or offset<0: raise ValueError('Некорректные границы группы/страницы.')
    async with pool().acquire() as conn:
        async with conn.transaction(isolation='repeatable_read',readonly=True):
            if first is None:
                first=await conn.fetchval('SELECT MIN(business_date) FROM retail_sales WHERE NOT deleted AND ($1::BIGINT IS NULL OR point_id=$1)',point)
            names={str(r['point_id']):r['name'] for r in await conn.fetch('SELECT point_id,name FROM retail_stores')}
            if first is None: return {'report_text':'Нет локальных продаж. Это не подтверждает отсутствие продаж в Saby.','complete_source':False}
            args=(first,last,point,seller,shift)
            payment_rows=[dict(r) for r in await conn.fetch(PAYMENTS,*args)] if kind in ('averages','trend') else []
            sale_rows=[dict(r) for r in await conn.fetch(SALES,*args)] if kind in ('averages','quantities','modifiers') else []
            sync_rows=await conn.fetch('SELECT status,started_at,finished_at FROM sync_runs ORDER BY id DESC LIMIT 1')
            item_sql=ITEMS
            if match!='contains':
                predicate=('LOWER(i.name)=LOWER($6)' if match=='exact' else
                    "i.product_id::TEXT=$6 OR i.product_uuid=$6 OR 'id:'||i.product_id::TEXT=$6 OR 'uuid:'||i.product_uuid=$6")
                item_sql=ITEMS.replace('POSITION(LOWER($6) IN LOWER(i.name))>0\n OR i.product_id::TEXT=$6 OR i.product_uuid=$6',predicate)
            item_rows=[dict(r) for r in await conn.fetch(item_sql,*args,query or None)] if kind in ('quantities','basket') else []
            if kind=='modifiers':
                item_rows=modifiers(await conn.fetch(MODIFIER_SALES,*args),query,match)
            basket_rows=[dict(r) for r in await conn.fetch(BASKETS,*args)] if kind=='basket' else []
            client_rows=[dict(r) for r in await conn.fetch(CLIENTS,last,point)] if kind=='clients' else []
    result={'kind':kind,'date_from':first.isoformat(),'date_to':last.isoformat(),'point_id':str(point) if point else None,
            'seller_id':str(seller) if seller else None,'shift':shift,'complete_source':False,
            'last_sync':dict(sync_rows[0]) if sync_rows else None,'generated_at':datetime.now(ZoneInfo(get_settings().business_tz)).isoformat()}
    title={'averages':'Средние суммы покупок и платежей','quantities':'Количество товаров','modifiers':'Проданные модификаторы','basket':'Наполняемость покупки','trend':'Динамика продаж','clients':'Клиенты по давности покупок'}[kind]
    lines=[f'{title}; {first} → {last}',
           f'Точка: {point or "сеть"}; учётка продавца: {seller or "все"}; смена: {shift or "все"}']
    if kind in ('averages','trend'):
        result['totals']=sums(payment_rows)
        groups={'network':[{'id':'Сеть/выборка',**sums(payment_rows)}],
                'stores':grouped(payment_rows,lambda r:str(r['point_id'])),
                'sellers':grouped(payment_rows,lambda r:str(r['seller_id']) if r['seller_id'] is not None else 'UNKNOWN')}
        seller_names={str(r['seller_id']):r['seller_name'] for r in payment_rows if r['seller_id'] is not None}
        if kind=='trend':
            groups={'days':grouped(payment_rows,lambda r:r['business_date'].isoformat()),
                    'weekdays':grouped(payment_rows,lambda r:str(r['business_date'].weekday()))}
        result['groups']=groups
        for label, values in groups.items():
            lines.append({'network':'Сеть / выборка','stores':'Магазины','sellers':'Учётки продавцов','days':'Дни','weekdays':'Дни недели'}.get(label,label))
            for row in values:
                title=names.get(row['id'],row['id']) if label=='stores' else seller_names.get(row['id'],row['id']) if label=='sellers' else row['id']
                text=f"{title} [ID {row['id']}]: {money(row['revenue'])} ₽; платёжных записей {row['checks']}; среднее по платежам {money(row['average'])} ₽; возвраты {money(row['returns'])} ₽; fallback {row['fallback']}"
                if label=='weekdays':
                    count=sum((first+timedelta(days=i)).weekday()==int(row['id']) for i in range((last-first).days+1))
                    row['calendar_occurrences']=count
                    row['revenue_per_calendar_day']=row['revenue']/count
                    text+=f"; календарных дней {count}; в день {money(row['revenue_per_calendar_day'])} ₽ (ПН=0)"
                lines.append(text)
        lines.append('СЧ = невозвратная платёжная сумма / число невозвратных платёжных записей. Это не доказанное число уникальных фискальных чеков. Дни без строк не означают нулевые продажи.')
        if kind=='averages':
            result['sale_averages']={'network':[{'id':'Сеть/выборка',**sums(sale_rows)}],
                                     'stores':grouped(sale_rows,lambda r:str(r['point_id'])),
                                     'sellers':grouped(sale_rows,lambda r:str(r['seller_id']) if r['seller_id'] is not None else 'UNKNOWN')}
            lines.append('Отдельно: средняя сумма продажи Saby = TotalPrice / число продаж (point_id + sale_id). Не платёжная выручка, не подтверждённый фискальный чек.')
            for label,values in result['sale_averages'].items():
                for row in values:
                    title=names.get(row['id'],row['id']) if label=='stores' else seller_names.get(row['id'],row['id']) if label=='sellers' else row['id']
                    lines.append(f"{title} [ID {row['id']}]: продаж {row['checks']}; средняя сумма продажи {money(row['average'])} ₽")
    elif kind in ('quantities','modifiers'):
        result['product_totals']=product_totals(item_rows,sale_rows)
        result['store_product_totals']=product_totals(item_rows,sale_rows,True)
        for label,values in [('Итоги по выборке',result['product_totals']),('Итоги по магазинам',result['store_product_totals'])]:
            lines.append(label)
            for row in values[:50]:
                title=names.get(str(row['point_id']),'Сеть / выборка')
                lines.append(f"{title}: {row['name']} [{row['product_key']}] — {quantity(row['quantity'])} {row['unit']}; возврат {quantity(row['return_quantity'])}; сумма {money(row['revenue'])} ₽; покупок {row['purchases']} из {row['eligible_purchases']}; доля {money(row['purchase_share_percent'])}%")
            if len(values)>50: lines.append('Показаны первые 50 итогов; полный список доступен в CSV.')
        item_rows.sort(key=lambda r:(-dec(r['revenue']),str(r['point_id']),str(r['seller_id']),r['product_key'],r['unit']))
        result['total_groups']=len(item_rows)
        result['rows']=item_rows if export_all else item_rows[offset:offset+50]
        result['next_offset']=offset+50 if not export_all and offset+50<len(item_rows) else None
        for row in result['rows']:
            row['point_id']=str(row['point_id'])
            row['seller_id']=str(row['seller_id']) if row['seller_id'] is not None else None
            row['average_unit_price']=dec(row['revenue'])/dec(row['quantity']) if row['revenue'] is not None and dec(row['quantity']) else None
            base=sum(r['checks'] for r in sale_rows if str(r['point_id'])==row['point_id'] and r['seller_id']==(int(row['seller_id']) if row['seller_id'] is not None else None))
            row['eligible_purchases']=base
            row['purchase_share_percent']=Decimal(row['purchases'])*100/base if base else None
            if not export_all:
                lines.append(f"{row['name']} [{row['product_key']}; {row['unit']}], {names.get(str(row['point_id']),row['point_id'])}, {row['seller_name']} [ID {row['seller_id']}]: количество {quantity(row['quantity'])}; возврат количества {quantity(row['return_quantity'])}; сумма {money(row['revenue'])} ₽; средняя цена {money(row['average_unit_price'])}; покупок с товаром {row['purchases']} из {base}; доля {money(row['purchase_share_percent'])}%")
        lines.append(f"Групп {len(item_rows)}; следующая страница offset={result['next_offset']}. Количества разных единиц не суммируются. Средняя цена = сумма / количество, не прайс.")
        lines.append('Доля по каждому товару отдельно: уникальные покупки с товаром / все невозвратные продажи выбранной области. Доли разных товаров не складываются.')
        if kind=='modifiers':
            lines.append('Обход SaleNomenclatures и вложенных Positions из сохранённых продаж; только явный IsModifier=true. Повторы одного ID/Key внутри продажи считаются один раз; конфликт останавливает отчёт. Количество не умножается на основную позицию. Неизвестная сумма — н/д, не ноль; суммы модификаторов нельзя прибавлять к выручке родительских товаров. Пустой результат не доказывает отсутствие модификаторов.')
    elif kind=='basket':
        n=len(basket_rows)
        result['purchases']=n
        result['purchases_without_items']=sum(r['lines']==0 for r in basket_rows)
        result['lines_per_purchase']=Decimal(sum(r['lines'] for r in basket_rows))/n if n else None
        result['products_per_purchase']=Decimal(sum(r['products'] for r in basket_rows))/n if n else None
        units=defaultdict(Decimal)
        for row in item_rows: units[row['unit']]+=dec(row['quantity'])
        result['quantity_per_purchase_by_unit']={u:q/n for u,q in units.items()} if n else {}
        lines.extend([f"Покупок (point_id + sale_id): {n}; без товарных строк: {result['purchases_without_items']}",
                      f"Строк на покупку: {money(result['lines_per_purchase'])}; разных товаров: {money(result['products_per_purchase'])}",
                      'Количество на покупку: '+'; '.join(f'{quantity(q)} {u}' for u,q in result['quantity_per_purchase_by_unit'].items()),
                      'Это наполняемость продажи Saby, не платёжной записи. Покупки без строк включены в знаменатель; полнота не подтверждена.'])
    else:
        selected=recency(client_rows,last,min_days,max_days)
        result['frequency_groups']={key:sum(frequency(r)==key for r in selected) for key in ('one_purchase','multiple_one_day','repeat_visits')}
        if frequency_filter: selected=[r for r in selected if frequency(r)==frequency_filter]
        result['total_clients']=len(client_rows)
        result['selected_clients']=len(selected)
        result['rows']=selected if export_all else selected[offset:offset+50]
        result['next_offset']=offset+50 if not export_all and offset+50<len(selected) else None
        result['segments']={key:sum(r['segment']==key for r in recency(client_rows,last)) for key in ('0-29','30-59','60-89','90+')}
        lines.extend([f"Клиентов с ID в локальных продажах: {len(client_rows)}; в выбранной группе: {len(selected)}",'; '.join(f'{k} дней: {v}' for k,v in result['segments'].items()),
                      'В интервале давности, до фильтра посещений — одна покупка: '+str(result['frequency_groups']['one_purchase'])+'; несколько покупок в один день: '+str(result['frequency_groups']['multiple_one_day'])+'; повторные посещения: '+str(result['frequency_groups']['repeat_visits']),
                      'История до даты '+str(last)+' включительно; начало периода не ограничивает поиск последней покупки.'])
        for row in result['rows']:
            row['frequency_group']=frequency(row)
            if not export_all: lines.append(f"Клиент ID {row['customer_id']}: последняя покупка {row['last_purchase']}; дней {row['days_since_purchase']}; покупок {row['purchases']}; дней посещений {row['visit_days']}; средний интервал {money(row['mean_days_between_visits'])} дней")
        lines.append(f"Следующая страница offset={result['next_offset']}. Давность — по наблюдаемой истории. По правилу владельца продажи без клиента не имеют карты. Наличие клиента само по себе не доказывает бонусную операцию. Этот список не рассчитывает баланс, начисления и списания; смотрите карточку клиента с покрытием бонусных ответов.")
    if result['last_sync']:
        lines.append('Последний sync: '+str(result['last_sync']['status'])+'; старт '+str(result['last_sync']['started_at'])+'; завершён '+str(result['last_sync']['finished_at']))
        if result['last_sync']['status']=='RUNNING': lines.append('RUNNING — статус в БД; работа процесса не подтверждена.')
    lines.append('Продавец — учётка Saby, не подтверждённый фактический сотрудник. РЦ исключён. Источник — локальная БД; полнота не подтверждена, сверка этим отчётом не выполнялась. Данные предварительные.')
    result['report_text']='\n'.join(lines)
    return result
