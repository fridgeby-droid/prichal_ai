"""Business questions over local immutable receipts. No external requests."""
import json
from collections import defaultdict, Counter
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

KINDS=('crosssell','substitution','regularity','local_assortment','basket_structure',
       'customer_moves','customer_comebacks','customer_value','concentration','seller_comparison','loyalty','traffic')
ITEM_KINDS={'crosssell','substitution','regularity','local_assortment','basket_structure','concentration','seller_comparison'}
HISTORY_KINDS={'customer_moves','customer_comebacks','customer_value'}
NETWORK_KINDS={'local_assortment','customer_moves'}
RULE_PATH=Path(__file__).resolve().parents[2]/'data'/'analytics-rules.json'


def rules():
    data=json.loads(RULE_PATH.read_text(encoding='utf-8'))
    for kind in ('crosssell','substitution'):
        seen=set()
        for rule in data.get(kind,[]):
            if not isinstance(rule,dict) or not isinstance(rule.get('id'),str) or not rule['id'] or rule['id'] in seen:
                raise ValueError('Некорректный или повторный ID правила аналитики')
            seen.add(rule['id'])
            for field in ('base','target'):
                values=rule.get(field)
                if not isinstance(values,list) or not values or any(not isinstance(v,str) or not v.startswith(('id:','uuid:')) for v in values):
                    raise ValueError('Правило требует непустые base и target со стабильными id:/uuid:')
            if set(rule['base']) & set(rule['target']):
                raise ValueError('Группы base и target не должны пересекаться')
    return data


def calculate(kind,sales,items,first,last,base_first=None,base_last=None,point=None,config=None):
    from app.services.advanced_reports import stats,ratio,dec
    current=[s for s in sales if first<=s['business_date']<=last and not s['is_return']]
    selected=[s for s in current if point is None or s['point_id']==point]
    valid={(s['point_id'],s['sale_id']):s for s in sales if not s['is_return']}
    by_sale=defaultdict(list)
    for i in items:
        if (i['point_id'],i['sale_id']) in valid and not i['is_return']:
            by_sale[(i['point_id'],i['sale_id'])].append(i)
    def products(s): return {i['product_key'] for i in by_sale[(s['point_id'],s['sale_id'])] if i['product_key']}
    def rule_products(s): return products(s)|set(s.get('modifier_keys',[]))
    metadata={'basis':'unique_sale_total_price','totals':stats(selected)}
    rows=[]
    if kind in ('crosssell','substitution'):
        configured=(config if config is not None else rules()).get(kind,[])
        if not configured:
            return [],dict(metadata,status='RULES_REQUIRED',interpretation='Настройте data/analytics-rules.json: стабильные ID товаров base и target. Названия не угадываются.')
        for rule in configured:
            a,b=set(rule['base']),set(rule['target'])
            if kind=='crosssell':
                for pid in sorted({s['point_id'] for s in selected}):
                    ss=[s for s in selected if s['point_id']==pid]
                    eligible=[s for s in ss if rule_products(s)&a]
                    both=sum(bool(rule_products(s)&b) for s in eligible)
                    rows.append(dict(rule=rule['id'],point_id=str(pid),eligible_purchases=len(eligible),
                        joint_purchases=both,attachment_rate=ratio(both,len(eligible))))
            else:
                for period,start,end in (('previous',base_first,base_last),('current',first,last)):
                    ss=[s for s in sales if start<=s['business_date']<=end and not s['is_return'] and (point is None or s['point_id']==point)]
                    for label,keys in (('base',a),('target',b),('combined',a|b)):
                        matching=[i for s in ss for i in by_sale[(s['point_id'],s['sale_id'])] if i['product_key'] in keys]
                        rows.append(dict(rule=rule['id'],period=period,group=label,
                            item_amount=sum((dec(i['total_price']) for i in matching),D(0)) if matching else None,
                            purchases=sum(bool(products(s)&keys) for s in ss),observed_sales=len(ss)))
        metadata['interpretation']='Правила по стабильным ID. Совместные покупки и динамика групп — наблюдение, не доказательство замещения или эффекта продавца. Ноль среди загруженных записей не подтверждает полноту.'
    elif kind in ('regularity','local_assortment'):
        groups=defaultdict(list);names={};store_days=defaultdict(set)
        for s in current:
            store_days[s['point_id']].add(s['business_date'])
            for i in by_sale[(s['point_id'],s['sale_id'])]:
                if i['product_key']:
                    key=(s['point_id'],i['product_key'],i['unit_name'])
                    groups[key].append((s,i));names[key]=i['name']
        network=Counter(); total=sum((dec(i['total_price']) for ss in groups.values() for _,i in ss),D(0))
        store_amount=defaultdict(lambda:D(0))
        for (pid,product,unit),ss in groups.items():
            amount=sum((dec(i['total_price']) for _,i in ss),D(0))
            network[(product,unit)]+=amount;store_amount[pid]+=amount
        for key,ss in sorted(groups.items()):
            pid,product,unit=key
            if point is not None and pid!=point: continue
            amount=sum((dec(i['total_price']) for _,i in ss),D(0))
            ds={s['business_date'] for s,_ in ss}
            observed=store_days[pid]
            row=dict(point_id=str(pid),product_key=product,name=names[key],unit=unit,
                quantity=sum((dec(i['quantity']) for _,i in ss),D(0)),item_amount=amount,
                days_with_sales=len(ds),calendar_days=(last-first).days+1,observed_store_days=len(observed),
                first_observed=min(ds).isoformat(),last_observed=max(ds).isoformat(),
                selling_day_share=ratio(len(ds),len(observed)))
            if kind=='local_assortment':
                local_share=ratio(amount,store_amount[pid]);net_share=ratio(network[(product,unit)],total)
                row.update(local_amount_share=local_share,network_amount_share=net_share,
                           affinity=ratio(local_share,net_share))
            rows.append(row)
        metadata['interpretation']='Только товары с продажами; дни без записей не означают отсутствия товара на полке. Регулярность по дням с наблюдаемыми продажами магазина. Привязанность — отношение долей суммы товарных строк в магазине и сети, не оценка спроса при одинаковой доступности.'
    elif kind=='basket_structure':
        groups=defaultdict(list)
        for s in selected:
            n=len(products(s)); amount=dec(s['total_price'])
            groups[('products','unknown_or_empty' if n==0 else '1' if n==1 else '2-3' if n<=3 else '4+')].append(s)
            groups[('amount','<300' if amount<300 else '300-999.99' if amount<1000 else '1000+')].append(s)
        rows=[dict(dimension=k[0],segment=k[1],**stats(ss),purchase_share=ratio(len(ss),len(selected))) for k,ss in sorted(groups.items())]
        metadata['interpretation']='Два независимых разреза: число разных товаров и сумма покупки. Между разрезами строки не складываются. Границы суммы фиксированы 300/1000 ₽, это не универсальная оценка ценности.'
    elif kind in ('customer_moves','customer_comebacks','customer_value'):
        history=defaultdict(list)
        for s in sales:
            if not s['is_return'] and s.get('customer_id') is not None:
                history[str(s['customer_id'])].append(s)
        if kind=='customer_moves':
            moves=defaultdict(set)
            for cid,ss in history.items():
                visits=defaultdict(set)
                for s in ss: visits[s['business_date']].add(s['point_id'])
                days=sorted(visits)
                for before,after in zip(days,days[1:]):
                    if not first<=after<=last or len(visits[before])!=1 or len(visits[after])!=1: continue
                    a,b=next(iter(visits[before])),next(iter(visits[after]))
                    if a!=b and (point is None or point in (a,b)): moves[(a,b)].add(cid)
            rows=[dict(from_point=str(a),to_point=str(b),customers=len(ids)) for (a,b),ids in sorted(moves.items())]
            if point is not None:
                old={cid for cid,ss in history.items() if any(s['point_id']==point and s['business_date']<first for s in ss)}
                elsewhere={cid for cid in old if not any(s['point_id']==point and first<=s['business_date']<=last for s in history[cid])
                           and any(s['point_id']!=point and first<=s['business_date']<=last for s in history[cid])}
                metadata['absent_here_active_elsewhere']=len(elsewhere)
            metadata['interpretation']='Переход между последовательными днями посещения; дни с несколькими магазинами исключены как неоднозначные. Клиент может входить в несколько направлений. Это не доказательство окончательного ухода.'
        elif kind=='customer_comebacks':
            for cid,ss in sorted(history.items()):
                days=sorted({s['business_date'] for s in ss})
                for before,after in zip(days,days[1:]):
                    gap=(after-before).days
                    if first<=after<=last and gap>=30:
                        follow_end=after+timedelta(days=30)
                        returned_again=any(after<d<=follow_end for d in days)
                        rows.append(dict(customer_id=cid,return_date=after.isoformat(),gap_days=gap,
                            segment='90+' if gap>=90 else '60-89' if gap>=60 else '30-59',
                            followup_30d_closed=follow_end<=last,another_visit_30d=returned_again if follow_end<=last else None))
            metadata['interpretation']='Каждый возврат после перерыва — событие, один клиент может вернуться несколько раз. Повторный визит оценивается за 30 дней только при завершённом окне. История неполна.'
        else:
            groups=defaultdict(list);customers=Counter()
            for cid,ss in history.items():
                current_ss=[s for s in ss if first<=s['business_date']<=last]
                if not current_ss: continue
                visits=len({s['business_date'] for s in current_ss})
                average=stats(current_ss)['average']
                label=('new_observed' if min(s['business_date'] for s in ss)>=first else
                       'frequent_4plus_days' if visits>=4 else 'infrequent_large_1000plus' if average>=1000 else 'other_returning')
                groups[label].extend(current_ss);customers[label]+=1
            known_amount=sum((dec(s['total_price']) for s in selected if s.get('customer_id') is not None),D(0))
            rows=[dict(segment=k,customers=customers[k],**stats(ss),known_customer_amount_share=ratio(stats(ss)['amount'],known_amount)) for k,ss in sorted(groups.items())]
            metadata['interpretation']='Непересекающиеся группы: новые, затем ≥4 дней посещений, затем редкие со средней покупкой ≥1000 ₽, остальные. Доля только суммы покупок с Customer; не пожизненная ценность и не прибыль.'
    elif kind=='concentration':
        for dimension in ('customers','products'):
            amounts=defaultdict(lambda:D(0))
            for s in selected:
                if dimension=='customers' and s.get('customer_id') is not None:
                    amounts[str(s['customer_id'])]+=dec(s['total_price'])
                elif dimension=='products':
                    for i in by_sale[(s['point_id'],s['sale_id'])]:
                        if i['product_key']: amounts[i['product_key']]+=dec(i['total_price'])
            ranked=sorted(amounts.values(),reverse=True);total=sum(ranked,D(0))
            for n in (1,5,10,20): rows.append(dict(dimension=dimension,top_n=n,groups=len(ranked),
                amount=sum(ranked[:n],D(0)),covered_amount=total,share=ratio(sum(ranked[:n],D(0)),total)))
        metadata['interpretation']='Клиенты: только покупки с Customer; товары: только строки со стабильным ID. Знаменатели различаются. Показатель зависимости, не прогноз риска.'
    elif kind=='seller_comparison':
        configured=(config if config is not None else rules()).get('crosssell',[])
        groups=defaultdict(list);strata=defaultdict(list)
        for s in selected:
            key=(s['point_id'],s['business_date'].isoweekday(),s.get('business_shift_type'))
            groups[(*key,s.get('seller_id'))].append(s);strata[key].append(s)
        stratum_stats={k:stats(v) for k,v in strata.items()}
        for (pid,weekday,shift,seller),ss in sorted(groups.items(),key=lambda x:str(x[0])):
            own=stats(ss);total=stratum_stats[(pid,weekday,shift)]
            peer_count=total['purchases']-own['purchases']
            basket_sizes=[len(products(s)) for s in ss]
            rows.append(dict(point_id=str(pid),weekday=weekday,shift=shift,seller_id=str(seller) if seller is not None else None,
                **stats(ss),observed_days=len({s['business_date'] for s in ss}),
                distinct_products_per_purchase=ratio(sum(basket_sizes),len(ss)),
                peers_average_purchase=ratio(total['amount']-own['amount'],peer_count),peer_purchases=peer_count,
                crosssell={r['id']:ratio(sum(bool(rule_products(s)&set(r['target'])) for s in ss if rule_products(s)&set(r['base'])),
                    sum(bool(rule_products(s)&set(r['base'])) for s in ss)) for r in configured}))
        metadata['crosssell_rules_configured']=len(configured)
        metadata['interpretation']='Сравнение внутри магазина, одинаковых бизнес-дней недели и DAY/NIGHT. Остальные учётки — база сравнения. Не рейтинг сотрудников: нет фактического графика, длительности работы, трафика и контроля ассортимента.'
    elif kind=='loyalty':
        groups=defaultdict(list);visit_days=defaultdict(set)
        for s in selected:
            if s.get('customer_id') is not None: visit_days[s['customer_id']].add(s['business_date'])
            b=s.get('bonus_response') or {}
            if isinstance(b,str): b=json.loads(b)
            if s.get('customer_id') is None: label='no_customer_no_card'
            elif not s.get('bonus_current'): label='unconfirmed'
            elif dec(b.get('bonusDec'))>0: label='debit_observed'
            elif dec(b.get('bonusInc'))>0: label='to_accrue_observed'
            else: label='response_without_positive_operation'
            groups[label].append((s,b))
        for label,values in sorted(groups.items()):
            ss=[s for s,_ in values];confirmed=label not in ('unconfirmed','no_customer_no_card')
            debits=[dec(b['bonusDec']) for _,b in values if b.get('bonusDec') is not None] if confirmed else []
            accruals=[dec(b['bonusInc']) for _,b in values if b.get('bonusInc') is not None] if confirmed else []
            active_ids={s['customer_id'] for s in ss if s.get('customer_id') is not None}
            repeat=sum(len(visit_days[cid])>1 for cid in active_ids)
            rows.append(dict(segment=label,**stats(ss),purchase_share=ratio(len(ss),len(selected)),
                customers=len(active_ids),customers_multiple_visit_days=repeat,
                bonus_debit_points=sum(debits,D(0)) if debits else None,
                bonus_to_accrue_points=sum(accruals,D(0)) if accruals else None,
                known_debit_values=len(debits),known_accrual_values=len(accruals)))
        daily=defaultdict(list)
        for s in selected: daily[s['business_date']].append(s)
        for day,ss in sorted(daily.items()):
            eligible=[s for s in ss if s.get('customer_id') is not None]
            known=[s for s in eligible if s.get('bonus_current')]
            debit_count=0
            for s in known:
                b=s.get('bonus_response') or {}
                if isinstance(b,str): b=json.loads(b)
                debit_count+=dec(b.get('bonusDec'))>0
            rows.append(dict(section='daily_coverage',date=day.isoformat(),purchases=len(ss),
                eligible_purchases=len(eligible),confirmed_responses=len(known),coverage=ratio(len(known),len(eligible)),
                observed_debit_purchases=debit_count,observed_debit_purchase_share=ratio(debit_count,len(ss))))
        metadata['interpretation']='Только OK ответы с актуальным fingerprint. Группы приоритетные: списание, затем к начислению; один чек один раз. bonusInc не подтверждает проведённое начисление. Повторные визиты — любые покупки клиента внутри периода, группы клиентов пересекаются. Баллы не переводятся в рубли без подтверждённого курса; неизвестные операции не нули. Это связь, не эффект бонусной программы.'
    elif kind=='traffic':
        metadata.update(status='NOT_CONNECTED',interpretation='Счётчики ещё не подключены: нет подтверждённой привязки и покрытия. Конверсия неизвестна. Используйте scripts.traffic для офлайн-проверки файла.')
    else: raise ValueError('Неизвестный отчёт')
    return rows,metadata
