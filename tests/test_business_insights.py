import asyncio
from datetime import date,timedelta
from decimal import Decimal as D
import json
import pytest
from app.services import business_insights as b,advanced_reports as a
from test_advanced_reports import sale,item,A,Z
from test_detail_reports import Conn,Pool

RULE={'id':'beer_snack','base':['id:1'],'target':['id:2']}
CONFIG={'crosssell':[RULE],'substitution':[RULE]}

def run(kind,ss,ii=[],**kw):
    return b.calculate(kind,ss,ii,A,Z,config=CONFIG,**kw)

def test_crosssell_counts_receipts_once_and_unknown_rule_state():
    rows,_=run('crosssell',[sale(1),sale(2),sale(3)],[item(1),item(1),item(1,'id:2'),item(2)])
    assert rows[0]['eligible_purchases']==2 and rows[0]['joint_purchases']==1
    assert rows[0]['attachment_rate']==D('.5')
    rows,m=b.calculate('crosssell',[],[],A,Z,config={})
    assert rows==[] and m['status']=='RULES_REQUIRED'

def test_substitution_preserves_group_totals_no_inferred_missing():
    ss=[sale(1,date(2026,8,1)),sale(2,A)]
    rows,_=run('substitution',ss,[item(1),item(2,'id:2')],base_first=date(2026,8,1),base_last=date(2026,8,31))
    assert rows[0]['item_amount']==10 and rows[1]['item_amount'] is None
    assert rows[2]['item_amount']==rows[5]['item_amount']==10

def test_regularity_repeated_lines_one_day_and_units_separate():
    rows,_=run('regularity',[sale(1),sale(2,A+timedelta(days=2))],[item(1),item(1),item(1,unit='кг')])
    assert len(rows)==2
    assert all(r['days_with_sales']==1 and r['observed_store_days']==2 and r['selling_day_share']==D('.5') for r in rows)

def test_local_affinity_uses_network_even_when_shop_selected():
    i2={**item(2,'id:2'),'point_id':2}
    rows,_=run('local_assortment',[sale(1),sale(2,point=2)],[item(1),i2],point=1)
    assert len(rows)==1 and rows[0]['local_amount_share']==1
    assert rows[0]['network_amount_share']==D('.5') and rows[0]['affinity']==2

def test_basket_empty_unknown_not_one_product():
    rows,_=run('basket_structure',[sale(1),sale(2)],[item(1),item(1)])
    assert next(r for r in rows if r['segment']=='unknown_or_empty')['purchases']==1
    assert sum(r['purchases'] for r in rows if r['dimension']=='products')==2

def test_moves_cross_store_history_and_ambiguous_day():
    ss=[sale(1,date(2026,8,31)),sale(2,A,point=2),sale(3,A+timedelta(days=1)),sale(4,A+timedelta(days=1),point=3)]
    rows,m=run('customer_moves',ss,point=1)
    assert rows==[{'from_point':'1','to_point':'2','customers':1}]
    assert m['absent_here_active_elsewhere']==0

def test_customer_active_elsewhere_not_lost_from_network():
    rows,m=run('customer_moves',[sale(1,date(2026,8,1)),sale(2,A,point=2)],point=1)
    assert m['absent_here_active_elsewhere']==1

def test_comeback_gaps_and_censored_followup():
    rows,_=run('customer_comebacks',[sale(1,date(2026,7,1)),sale(2,A),sale(3,A+timedelta(days=1))])
    assert rows[0]['segment']=='60-89' and rows[0]['another_visit_30d'] is None
    rows,_=b.calculate('customer_comebacks',[sale(1,date(2026,7,1)),sale(2,A),sale(3,A+timedelta(days=1))],[],A,date(2026,10,1))
    assert rows[0]['another_visit_30d'] is True

def test_customer_value_exclusive_segments_and_denominator():
    ss=[sale(1,A,customer=1),sale(2,date(2026,8,1),customer=2),sale(3,A,amount=1200,customer=2),sale(4,A,customer=None)]
    rows,_=run('customer_value',ss)
    assert {r['segment'] for r in rows}=={'new_observed','infrequent_large_1000plus'}
    assert sum(r['customers'] for r in rows)==2
    assert sum(r['known_customer_amount_share'] for r in rows)==1

def test_concentration_denominators_are_not_all_revenue():
    rows,_=run('concentration',[sale(1),sale(2,amount=900,customer=None)],[item(1),item(2,'id:2')])
    customer=next(r for r in rows if r['dimension']=='customers')
    product=next(r for r in rows if r['dimension']=='products')
    assert customer['covered_amount']==100 and product['covered_amount']==20

def test_seller_peers_same_shop_weekday_shift_not_self():
    ss=[{**sale(1),'business_shift_type':'DAY'},
        {**sale(2,amount=200),'business_shift_type':'DAY','seller_id':8},
        {**sale(3,amount=999),'business_shift_type':'NIGHT','seller_id':8}]
    rows,_=run('seller_comparison',ss,[item(1),item(1,'id:2')])
    row=next(r for r in rows if r['seller_id']=='7')
    assert row['peers_average_purchase']==200 and row['peer_purchases']==1
    assert row['crosssell']['beer_snack']==1

def test_loyalty_unknown_not_zero_stale_excluded_and_no_double_group():
    ss=[{**sale(1),'bonus_current':True,'bonus_response':{'bonusDec':36,'bonusInc':9}},
        {**sale(2),'bonus_current':False,'bonus_response':{'bonusDec':999}},sale(3,customer=None)]
    rows,_=run('loyalty',ss)
    debit=next(r for r in rows if r.get('segment')=='debit_observed')
    unknown=next(r for r in rows if r.get('segment')=='unconfirmed')
    assert debit['bonus_debit_points']==36 and debit['bonus_to_accrue_points']==9
    assert unknown['bonus_debit_points'] is None
    assert sum(r['purchases'] for r in rows if 'segment' in r)==3
    daily=next(r for r in rows if r.get('section')=='daily_coverage')
    assert daily['coverage']==D('.5') and daily['observed_debit_purchases']==1

def test_traffic_not_connected_never_fake_conversion():
    rows,m=run('traffic',[])
    assert not rows and m['status']=='NOT_CONNECTED'

@pytest.mark.parametrize('kind',b.KINDS)
def test_all_new_routes_readonly_scopes_and_render(kind,monkeypatch):
    class C(Conn):
        async def fetch(self,sql,*args):
            self.calls.append((sql,args))
            if 'FROM retail_sales WHERE' in sql: return [sale(1)]
            return []
        async def fetchrow(self,*args): return {'status':'OK'}
    c=C();monkeypatch.setattr(a,'pool',lambda:Pool(c))
    async def point(_): return 1
    monkeypatch.setattr(a,'point_filter',point)
    kw={'previous_start':'2026-08-01','previous_end':'2026-08-31'} if kind=='substitution' else {}
    result=asyncio.run(a.report(kind,'2026-09-01','2026-09-30','1',**kw))
    assert result['kind']==kind and not result['complete_source']
    assert 'Полнота' in a.render(result) or 'полнота' in a.render(result)
    assert c.calls[0][1][-1]==(None if kind in b.NETWORK_KINDS else 1)
    if kind=='loyalty': assert any("b.fingerprint=md5" in sql for sql,_ in c.calls)

def test_rules_reject_overlap_and_duplicate_rule_ids(tmp_path,monkeypatch):
    p=tmp_path/'rules.json';monkeypatch.setattr(b,'RULE_PATH',p)
    for rules in ([RULE,RULE],[{**RULE,'target':['id:1']}],[{**RULE,'base':[]} ]):
        p.write_text(json.dumps({'crosssell':rules}))
        with pytest.raises(ValueError): b.rules()


def test_crosssell_streams_modifiers_without_money_double_count(monkeypatch):
    class C(Conn):
        async def fetch(self,sql,*args):
            if 'FROM retail_sales WHERE' in sql: return [sale(1)]
            if 'FROM retail_sale_items' in sql: return [item(1)]
            return []
        async def fetchrow(self,*args): return {'status':'OK'}
        async def cursor(self,*args,**kwargs):
            assert kwargs['prefetch']==100
            yield dict(point_id=1,sale_id=1,raw_json={'SaleNomenclatures':[
                {'SaleNomenclature':11,'IsModifier':True,'Nomenclature':2},
                {'SaleNomenclature':12,'IsModifier':True,'Nomenclature':3,'Refused':True}]})
    monkeypatch.setattr(a,'pool',lambda:Pool(C()))
    monkeypatch.setattr(b,'rules',lambda:CONFIG)
    result=asyncio.run(a.report('crosssell','2026-09-01','2026-09-30'))
    assert result['rows'][0]['attachment_rate']==1
    assert result['metadata']['totals']['amount']==100
