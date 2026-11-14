"""Workflows join existing evidence; unavailable external sources stay explicit."""
from decimal import Decimal as D
from app.services import management_reports as m,advanced_reports as a
from app.services.assistant_briefs import trim


async def report(kind,start,end,point_id='',previous_start='',previous_end=''):
    if kind=='data_sources':
        return {'kind':kind,'point_id':point_id or None,'rows':[
            {'source':'Трафик','status':'NOT_CONNECTED','required':'Счётчик → магазин, часовой пояс, интервалы, входы/выходы и покрытие.'},
            {'source':'Остатки и поступления','status':'SETUP_REQUIRED','required':'Загрузчик текущих остатков подготовлен: нужны подтверждённые ID организации/склада, привязка к магазину и единицы. Поступления: требуется проверка документа УТ-121230 от 13.11.2026; проведение и склад пока не подтверждены.'},
            {'source':'Себестоимость','status':'LOCAL_CHECK_REQUIRED','required':'Отчёт читает фактическую TotalCost из сохранённых продаж. Требуется проверить наличие значений и согласовать базу НДС; PlannedCost не используется.'},
            {'source':'Фактический график Core','status':'NOT_USED_IN_THESE_REPORTS','required':'Проверенная привязка сотрудников и назначений; сравнения здесь по учёткам Saby.'}]}
    if kind=='basket_diagnosis':
        result={'kind':kind,'point_id':point_id or None,'date_from':start,'date_to':end,'sections':{},'errors':[]}
        for name,source in [('purchase_amount','decomposition'),('product_price_quantity','basket_change'),('basket_structure','basket_structure')]:
            try:
                base=(previous_start,previous_end) if source!='basket_structure' else ('','')
                result['sections'][name]=trim(await a.report(source,start,end,point_id,*base))
            except Exception as exc: result['errors'].append({'section':name,'error_type':type(exc).__name__})
        result['complete']=not result['errors'];return result
    if kind!='attention': raise ValueError('Неизвестный сценарий')
    data=await m.report(start,end,previous_start,previous_end,point_id,'comparison',50)
    eligible=data.get('comparable',{}).get('stores',{}).get('rows',[])
    rows=[]
    for row in eligible:
        change=row.get('per_day_change')
        if not change or row.get('observation_warning') or change['absolute']>=0: continue
        rows.append({'point_id':row['id'],'name':row['name'],'revenue_per_day_delta':change['absolute'],
                     'revenue_per_day_percent':change['percent'],'checks_per_day_current':row.get('checks_per_day_current'),
                     'average_check_current':row.get('average_current'),
                     'suggestion':'Разобрать смены, число платежей и товары. Наличие и трафик проверить отдельно.'})
    rows.sort(key=lambda r:r['revenue_per_day_delta'])
    return {'kind':kind,'point_id':point_id or None,'date_from':start,'date_to':end,'rows':rows[:3],
            'quality_current':data['quality_current'],'quality_previous':data['quality_previous'],
            'selection':'Три крупнейших снижения выручки в день среди сопоставимых по локальному покрытию магазинов.',
            'source_complete':False,'causal':False}


async def attachment_metrics(first,last,point,shift,rule):
    from app.services.personal_followups import metrics
    coverage=await metrics(first,last,point,shift)
    data=await a.report('crosssell',str(first),str(last),point_id=point,export_all=True,rules_config={'crosssell':[rule]})
    rows=[r for r in data['rows'] if r.get('dimension')=='seller_shift_weekday' and (not shift or r['shift']==shift)]
    eligible=sum(r['eligible_purchases'] for r in rows);both=sum(r['joint_purchases'] for r in rows)
    return {**coverage,'attachment_rate':str(D(both)*100/eligible) if eligible else None,
            'eligible_purchases':eligible,'joint_purchases':both,'rate_unit':'percent'}
