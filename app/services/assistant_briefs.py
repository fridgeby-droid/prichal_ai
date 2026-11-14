"""Deterministic multi-report orchestration. Reads only; partial failures explicit."""
from datetime import timedelta
from decimal import Decimal
from app.services import management_reports as management,detail_reports as detail,advanced_reports as advanced
from app.services.saby_maintenance import closed_day


def trim(value):
    if isinstance(value,dict): return {k:trim(v) for k,v in value.items() if k not in ('report_text','brief')}
    if isinstance(value,list): return [trim(v) for v in value]
    return value


def payment_effects(report):
    """Exact symmetric arithmetic, separate total and daily effects; not causality."""
    m=report.get('metrics',{})
    if not report.get('has_current_records') or not report.get('has_previous_records'):
        return {'status':'NOT_COMPARABLE'}
    avg=m.get('average',{})
    if avg.get('current') is None or avg.get('previous') is None: return {'status':'NOT_COMPARABLE'}
    a,z=Decimal(str(avg['previous'])),Decimal(str(avg['current']))
    output={'status':'AVAILABLE','basis':'payment_records','causal':False}
    for label,cur,prev in [('total','current','previous'),('per_day','current_per_day','previous_per_day')]:
        ca,cz=Decimal(str(m['checks'][prev])),Decimal(str(m['checks'][cur]))
        output[label]={'count_effect':(cz-ca)*(a+z)/2,'average_effect':(z-a)*(ca+cz)/2}
    return output


async def build(kind,start='',end='',point_id='',previous_start='',previous_end=''):
    if kind not in ('investigation','store_card','morning','sellers'): raise ValueError('Неизвестный обзор')
    if kind=='store_card' and not point_id: raise ValueError('Для карточки нужен один магазин')
    if kind=='morning':
        day=closed_day()
        start=end=str(day)
        previous_start=previous_end=str(day-timedelta(days=7))
    first,last=management.periods.period(start,end)
    if bool(previous_start)!=bool(previous_end): raise ValueError('Нужны обе даты базы')
    if not previous_start:
        previous_end=str(first-timedelta(days=1))
        previous_start=str(first-timedelta(days=(last-first).days+1))
    management.periods.period(previous_start,previous_end)
    result={'kind':kind,'point_id':point_id or None,'date_from':start,'date_to':end,
            'previous_start':previous_start,'previous_end':previous_end,'sections':{},'errors':[],
            'complete':True,'causal_claims_confirmed':False,
            'comparison_rule':'same_weekday_last_week' if kind=='morning' else 'explicit_or_previous_equal_length',
            'consistency':'Separate local reads; examine timestamps and quality if sync overlaps.'}
    async def section(name,call):
        try:
            data=await call()
            if str(data.get('point_id') or '')!=str(point_id or ''):
                raise ValueError('REPORT_SCOPE_MISMATCH')
            result['sections'][name]=trim(data)
        except Exception as exc:
            result['complete']=False
            result['errors'].append({'section':name,'error_type':type(exc).__name__})
    # Sequential reads avoid multiplying DB load and memory use.
    if kind=='sellers':
        await section('sellers',lambda:advanced.report('seller_comparison',start,end,point_id=point_id))
    else:
        await section('drivers',lambda:management.report(start,end,previous_start,previous_end,point_id,'drivers',10))
        if 'drivers' in result['sections']:
            result['payment_effects']=payment_effects(result['sections']['drivers'])
        if kind!='morning':
            await section('trend',lambda:detail.report('trend',start,end,point_id=point_id))
        if kind=='store_card':
            await section('clients',lambda:detail.report('clients',start,end,point_id=point_id))
            await section('loyalty',lambda:advanced.report('loyalty',start,end,point_id=point_id))
    return result
