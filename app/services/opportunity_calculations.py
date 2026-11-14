"""Observed opportunities, never inventory, causality or profit estimates."""
from collections import defaultdict
from decimal import Decimal as D
from statistics import median

KINDS=('customer_risk','assortment_gaps','basket_change')


def calculate(kind,sales,items,first,last,base_first=None,base_last=None,point=None):
    valid={(s['point_id'],s['sale_id']):s for s in sales if not s['is_return']}
    current=[s for s in valid.values() if first<=s['business_date']<=last and (point is None or s['point_id']==point)]
    rows=[];meta={'basis':'local_unique_sales_and_item_rows','causal':False,'source_complete':False}
    if kind=='customer_risk':
        histories=defaultdict(list)
        for s in valid.values():
            if s.get('customer_id') is not None and s['business_date']<=last and (point is None or s['point_id']==point): histories[str(s['customer_id'])].append(s)
        insufficient=0
        for cid,ss in histories.items():
            days=sorted({s['business_date'] for s in ss})
            if len(days)<4: insufficient+=1;continue
            gaps=[(z-a).days for a,z in zip(days,days[1:])]
            normal=D(str(median(gaps)));elapsed=(last-days[-1]).days
            if elapsed<normal*2: continue
            amount=sum((D(str(s['total_price'])) for s in ss if (last-s['business_date']).days<90),D(0))
            rows.append(dict(customer_id=cid,last_purchase=str(days[-1]),days_since_purchase=elapsed,
                             typical_interval_days=normal,delay_ratio=D(elapsed)/normal,
                             observed_visit_days=len(days),amount_90d=amount,purchases=len(ss)))
        rows.sort(key=lambda r:(-r['amount_90d'],-r['delay_ratio'],r['customer_id']))
        meta.update(insufficient_history_clients=insufficient,minimum_visit_days=4,delay_multiplier=2,
                    interpretation='Задержка относительно медианного интервала наблюдаемых посещений. Не подтверждённый уход. Область — выбранный магазин или сеть; первые даты истории не означают начало отношений.')
        return rows,meta
    grouped=defaultdict(list)
    for i in items:
        s=valid.get((i['point_id'],i['sale_id']))
        if s and not i['is_return'] and i.get('product_key'):
            grouped[(i['point_id'],i['product_key'],i['unit_name'])].append((s,i))
    if kind=='assortment_gaps':
        active=defaultdict(set);observed_by_store=defaultdict(set)
        for s in current: observed_by_store[s['point_id']].add(s['business_date'])
        for (pid,key,unit),values in grouped.items():
            if any(first<=s['business_date']<=last for s,_ in values): active[(key,unit)].add(pid)
        for (pid,key,unit),values in grouped.items():
            if point is not None and pid!=point: continue
            old=[(s,i) for s,i in values if base_first<=s['business_date']<=base_last]
            cur=[(s,i) for s,i in values if first<=s['business_date']<=last]
            if not old or cur: continue
            other=active[(key,unit)]-{pid}
            if not other: continue
            observed=observed_by_store[pid]
            rows.append(dict(point_id=str(pid),product_key=key,unit=unit,name=old[-1][1]['name'],
                previous_item_amount=sum((D(str(i['total_price'])) for _,i in old),D(0)),
                current_item_amount=None,last_observed=str(max(s['business_date'] for s,_ in old)),
                other_stores_with_sales=len(other),observed_store_days=len(observed),calendar_days=(last-first).days+1,
                action='Проверить наличие и выкладку; отсутствие продаж не доказывает отсутствие товара.'))
        rows.sort(key=lambda r:-r['previous_item_amount'])
        meta['interpretation']='Раньше товар продавался здесь, сейчас записи есть в других магазинах. Наличие на полке неизвестно. Стабильные ID и одинаковая единица, без объединения по названию.'
        return rows,meta
    # Exact price/quantity arithmetic for matching products, separately by unit.
    merged=defaultdict(list)
    for (pid,key,unit),values in grouped.items():
        if point is None or pid==point: merged[(key,unit)].extend(values)
    for (key,unit),values in merged.items():
        a=[i for s,i in values if base_first<=s['business_date']<=base_last]
        z=[i for s,i in values if first<=s['business_date']<=last]
        qa=sum((D(str(i['quantity'])) for i in a),D(0)) if a else None
        qz=sum((D(str(i['quantity'])) for i in z),D(0)) if z else None
        ra=sum((D(str(i['total_price'])) for i in a),D(0)) if a else None
        rz=sum((D(str(i['total_price'])) for i in z),D(0)) if z else None
        pa=ra/qa if qa is not None and qa>0 else None;pz=rz/qz if qz is not None and qz>0 else None
        rows.append(dict(product_key=key,unit=unit,name=values[-1][1]['name'],quantity_previous=qa,quantity_current=qz,
            amount_previous=ra,amount_current=rz,average_price_previous=pa,average_price_current=pz,
            quantity_effect=(qz-qa)*(pa+pz)/2 if pa is not None and pz is not None else None,
            price_effect=(pz-pa)*(qa+qz)/2 if pa is not None and pz is not None else None))
    rows.sort(key=lambda r:-abs((r['amount_current'] or 0)-(r['amount_previous'] or 0)))
    meta['interpretation']='Разложение изменения сумм товарных строк, не платёжного среднего чека. Цена — средняя фактическая сумма на единицу, включая скидки и состав продаж. Неизвестные стороны не нули; единицы раздельны.'
    return rows,meta
