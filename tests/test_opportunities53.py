import asyncio
from datetime import date,timedelta
from decimal import Decimal as D
from unittest.mock import AsyncMock
import pytest
from app.services import opportunity_calculations as o,business_insights as b,management_opportunities as work
from app.services.opportunity_rules import validated
from test_advanced_reports import sale,item,A,Z

BASE=date(2026,8,1);END=date(2026,8,31)


def test_customer_habit_requires_four_distinct_days_and_not_four_receipts():
    ss=[sale(i,day=date(2026,8,1)+timedelta(days=7*i)) for i in range(4)]
    ss += [sale(20+i,date(2026,8,1),customer=2) for i in range(10)]
    rows,meta=o.calculate('customer_risk',ss,[],A,Z)
    assert len(rows)==1 and rows[0]['customer_id']=='1'
    assert rows[0]['typical_interval_days']==7 and rows[0]['observed_visit_days']==4
    assert rows[0]['days_since_purchase']==39 and meta['insufficient_history_clients']==1


def test_customer_risk_excludes_returns_future_and_other_store():
    ss=[sale(i,day=BASE+timedelta(days=7*i),point=2) for i in range(4)]
    ss += [sale(40+i,day=BASE+timedelta(days=7*i),returned=True) for i in range(4)]
    rows,_=o.calculate('customer_risk',ss,[],A,Z,point=1)
    assert rows==[]


def test_gap_requires_same_stable_id_unit_and_elsewhere_current_sale():
    ss=[sale(1,BASE),sale(2,A,point=2),sale(3,A,point=1)]
    ii=[item(1),{**item(2),'point_id':2},item(3,'id:2')]
    rows,_=o.calculate('assortment_gaps',ss,ii,A,Z,BASE,END,1)
    assert len(rows)==1 and rows[0]['product_key']=='id:1'
    assert rows[0]['current_item_amount'] is None and rows[0]['other_stores_with_sales']==1
    assert rows[0]['observed_store_days']==1
    ii[1]['unit_name']='кг'
    assert o.calculate('assortment_gaps',ss,ii,A,Z,BASE,END,1)[0]==[]


def test_price_quantity_arithmetic_reconciles_and_missing_stays_unknown():
    ss=[sale(1,BASE),sale(2,A),sale(3,BASE)]
    ii=[{**item(1,quantity=2),'total_price':D(20)},
        {**item(2,quantity=3),'total_price':D(45)},item(3,'id:2')]
    rows,_=o.calculate('basket_change',ss,ii,A,Z,BASE,END,1)
    row=next(r for r in rows if r['product_key']=='id:1')
    assert row['price_effect']+row['quantity_effect']==25
    assert row['average_price_previous']==10 and row['average_price_current']==15
    missing=next(r for r in rows if r['product_key']=='id:2')
    assert missing['amount_current'] is None and missing['price_effect'] is None


def test_basket_keeps_units_separate_and_excludes_returns():
    ss=[sale(1,BASE),sale(2,A),sale(3,A,returned=True)]
    ii=[item(1),item(2,unit='кг'),item(3)]
    rows,_=o.calculate('basket_change',ss,ii,A,Z,BASE,END,1)
    assert len(rows)==2 and all(r['price_effect'] is None for r in rows)


def test_crosssell_store_and_seller_dimensions_never_sum_together():
    ss=[{**sale(1),'business_shift_type':'DAY'},
        {**sale(2),'business_shift_type':'DAY','seller_id':8},
        {**sale(3),'business_shift_type':'NIGHT','seller_id':8}]
    ii=[item(1),item(1,'id:2'),item(2),item(3),item(3,'id:2')]
    rows,_=b.calculate('crosssell',ss,ii,A,Z,config={'crosssell':[{'id':'r','base':['id:1'],'target':['id:2']}]})
    store=next(r for r in rows if r['dimension']=='store')
    seller=next(r for r in rows if r.get('seller_id')=='7')
    assert store['joint_purchases']==2 and store['eligible_purchases']==3
    assert seller['peer_attachment_rate']==0 and seller['peer_eligible_purchases']==1
    assert seller['sample_status']=='LOW_SAMPLE'


def test_rules_reject_intersection_names_as_ids_and_malformed_uuid():
    for base,target in [(['id:1'],['id:1']),(['пиво'],['id:2']),(['uuid:'+'-'*36],['id:2'])]:
        with pytest.raises(ValueError): validated('crosssell','rule',base,target)
    assert validated('crosssell',' rule ',['id:1','id:1'],['id:2'])['base']==['id:1']


def test_attention_only_ranks_comparable_negative_daily_changes(monkeypatch):
    rows=[{'id':'1','name':'А','per_day_change':{'absolute':D(-20),'percent':-10}},
          {'id':'2','name':'Б','per_day_change':{'absolute':D(10),'percent':5}},
          {'id':'3','name':'В','per_day_change':{'absolute':D(-50),'percent':-20},'observation_warning':True}]
    monkeypatch.setattr(work.m,'report',AsyncMock(return_value={'comparable':{'stores':{'rows':rows}},'quality_current':{},'quality_previous':{}}))
    result=asyncio.run(work.report('attention','2026-09-01','2026-09-30','','2026-08-01','2026-08-31'))
    assert [r['point_id'] for r in result['rows']]==['1']


def test_basket_diagnosis_keeps_failed_section_explicit(monkeypatch):
    async def report(kind,*args,**kwargs):
        if kind=='basket_change': raise TimeoutError('private')
        return {'kind':kind,'rows':[]}
    monkeypatch.setattr(work.a,'report',report)
    result=asyncio.run(work.report('basket_diagnosis','2026-09-01','2026-09-30','1','2026-08-01','2026-08-31'))
    assert not result['complete'] and len(result['sections'])==2
    assert result['errors']==[{'section':'product_price_quantity','error_type':'TimeoutError'}]


def test_attachment_task_uses_frozen_rule_and_only_selected_shift(monkeypatch):
    from app.services import personal_followups as p
    monkeypatch.setattr(p,'metrics',AsyncMock(return_value={'all_days_observed':True}))
    mock=AsyncMock(return_value={'rows':[
        {'dimension':'store','eligible_purchases':100,'joint_purchases':90},
        {'dimension':'seller_shift_weekday','shift':'DAY','eligible_purchases':10,'joint_purchases':2},
        {'dimension':'seller_shift_weekday','shift':'DAY','eligible_purchases':10,'joint_purchases':8},
        {'dimension':'seller_shift_weekday','shift':'NIGHT','eligible_purchases':50,'joint_purchases':50}]})
    monkeypatch.setattr(work.a,'report',mock)
    rule={'id':'frozen','base':['id:1'],'target':['id:2']}
    result=asyncio.run(work.attachment_metrics(A,Z,'1','DAY',rule))
    assert result['attachment_rate']=='50' and result['eligible_purchases']==20
    assert mock.await_args.kwargs['rules_config']=={'crosssell':[rule]}


def test_unavailable_external_sources_are_explicit():
    result=asyncio.run(work.report('data_sources','',''))
    assert len(result['rows'])==4
    assert all(r['status'] in ('NOT_CONNECTED','NOT_USED_IN_THESE_REPORTS','SETUP_REQUIRED','LOCAL_CHECK_REQUIRED') for r in result['rows'])


def test_new_tools_and_menu_actions_are_registered():
    from app.agent.executive import executive_agent
    from app.bot.menu import ITEMS
    assert {'opportunity_rules','get_opportunity_report'} <= {t.name for t in executive_agent.tools}
    assert {'crosssell','assortment_gaps','substitution','rules'} <= set(ITEMS['products'])
    assert 'customer_risk' in ITEMS['clients'] and 'attention' in ITEMS['network']


def test_rule_picker_uses_selected_id_and_requires_save_confirmation(monkeypatch):
    from app.bot import menu
    from test_button_menu import setup
    from app.services import opportunity_rules as rules
    message,user=setup(monkeypatch)
    s={'rule_kind':'crosssell','rule_name':'Закуска','rule_base':[], 'rule_target':[],
       'rule_side':'base','rule_candidates':[{'product_key':'id:1','name':'Пиво'}]}
    save=AsyncMock();monkeypatch.setattr(rules,'save',save)
    asyncio.run(menu.action(message,'key',s,user,'rulepick','0'))
    assert s['rule_base']==['id:1'] and s['rule_side']=='target'
    s['rule_candidates']=[{'product_key':'id:2','name':'Закуска'}]
    asyncio.run(menu.action(message,'key',s,user,'rulepick','0'))
    assert s['rule_target']==['id:2'] and ['rulesave',''] in s['actions']
    save.assert_not_awaited()


def test_action_task_stores_note_and_rule_snapshot(monkeypatch):
    from app.services import personal_followups as p,opportunity_rules as rules
    from test_personal_followups import install_state
    state=p.defaults();install_state(monkeypatch,state)
    rule={'id':'закуска','base':['id:1'],'target':['id:2']}
    monkeypatch.setattr(rules,'load',AsyncMock(return_value={'crosssell':[rule]}))
    monkeypatch.setattr(p.periods,'point_filter',AsyncMock(return_value=1))
    monkeypatch.setattr(p.periods,'stores',AsyncMock(return_value={'stores':[{'point_id':'1','name':'Магазин'}]}))
    baseline=AsyncMock(return_value={'attachment_rate':'20','all_days_observed':True})
    monkeypatch.setattr(p,'baseline_when_ready',baseline)
    result=asyncio.run(p.create_task({'user_id':1,'private':True,'message_id':10},'1','DAY','attachment_rate',7,0,'закуска','Предлагаем закуску'))
    assert result['rule_snapshot']==rule and result['action_note']=='Предлагаем закуску'
    assert result['action_recorded_at'] and baseline.await_args.kwargs['rule']==rule


def test_rules_load_merges_file_and_saved_by_id(monkeypatch):
    from app.services import opportunity_rules as rules
    from test_detail_reports import Conn,Pool
    class C(Conn):
        async def fetchval(self,*args): return {'crosssell':[{'id':'r','base':['id:1'],'target':['id:3']}]}
    monkeypatch.setattr(rules,'pool',lambda:Pool(C()))
    monkeypatch.setattr(b,'rules',lambda:{'crosssell':[{'id':'r','base':['id:1'],'target':['id:2']}],'substitution':[]})
    result=asyncio.run(rules.load())
    assert len(result['crosssell'])==1 and result['crosssell'][0]['target']==['id:3']
