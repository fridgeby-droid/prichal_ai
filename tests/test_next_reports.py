import asyncio
import csv
import io
from datetime import date
from decimal import Decimal as D
from unittest.mock import AsyncMock
import pytest
from app.services.report_helpers import quantity,frequency,product_totals,compare_products,csv_bytes
from app.services import detail_reports as d
from app.services import extra_reports as e
from app.services.sale_tree import modifiers
from test_detail_reports import Conn,Pool


def test_quantity_format_and_nonoverlapping_client_frequency():
    assert quantity(D('70.000000'))=='70'
    assert quantity(D('.135'))=='0,135'
    assert quantity(None)=='н/д'
    assert [frequency(r) for r in [dict(purchases=1,visit_days=1),dict(purchases=5,visit_days=1),dict(purchases=5,visit_days=2)]]==['one_purchase','multiple_one_day','repeat_visits']


def test_product_totals_weighted_price_denominator_and_units():
    def row(seller,q,purchases,revenue,unit='шт'):
        return dict(point_id=1,seller_id=seller,product_key='id:1',name='Пакет',unit=unit,
                    quantity=D(q),return_quantity=D(1),revenue=revenue,purchases=purchases)
    rows=[row(1,70,70,D('484.11')),row(2,67,66,D('458.97')),row(1,2,1,None,'кг')]
    bases=[dict(point_id=1,seller_id=1,checks=100),dict(point_id=1,seller_id=2,checks=100)]
    result=product_totals(rows,bases)
    pieces=next(r for r in result if r['unit']=='шт')
    assert pieces['quantity']==137 and pieces['revenue']==D('943.08')
    assert pieces['purchase_share_percent']==68 and pieces['average_unit_price']==D('943.08')/137
    assert next(r for r in result if r['unit']=='кг')['revenue'] is None


def test_comparison_missing_not_zero_and_per_day():
    row=dict(point_id='1',product_key='id:1',unit='шт',name='Пакет',quantity=D(20),
             return_quantity=D(1),revenue=D(100),average_unit_price=D(5),purchase_share_percent=D(10))
    result=compare_products([row],[],2,4)[0]
    assert result['quantity_previous'] is None and result['quantity_delta'] is None
    assert result['quantity_per_day_current']==10
    result=compare_products([row],[{**row,'quantity':D(40)}],2,4)[0]
    assert result['quantity_delta']==-20 and result['quantity_per_day_previous']==10


def test_csv_safe_complete_and_numeric():
    data=csv_bytes([{'name':'=HYPERLINK("bad")','quantity':D('-2'),'value':None},
                    {'name':'\t=1+1','quantity':3,'value':'строка;ещё'}])
    rows=list(csv.DictReader(io.StringIO(data.decode('utf-8-sig')),delimiter=';'))
    assert rows[0]['name'].startswith("'=") and rows[0]['quantity']=='-2'
    assert rows[0]['value']=='' and rows[1]['name'].startswith("'\t")


def test_exact_modifier_does_not_include_one_and_half():
    sale=dict(point_id=1,sale_id=1,seller_id=7,seller_name='A',raw_json={'SaleNomenclatures':[
        dict(SaleNomenclature=1,Nomenclature=50,Name='тара 1',UnitName='шт',Quantity=2,IsModifier=True),
        dict(SaleNomenclature=2,Nomenclature=51,Name='тара 1,5',UnitName='шт',Quantity=1,IsModifier=True)]})
    assert len(modifiers([sale],'тара 1','contains'))==2
    assert len(modifiers([sale],'тара 1','exact'))==1
    assert modifiers([sale],'id:50','id')[0]['quantity']==2


def test_exact_items_use_bound_predicate_and_client_export_full(monkeypatch):
    class C(Conn):
        async def fetch(self,sql,*args):
            if 'FROM retail_sale_items' in sql:
                assert 'LOWER(i.name)=LOWER($6)' in sql
                assert args[-1]=='тара 1'
                return []
            if sql==d.CLIENTS:
                return [dict(customer_id=n,first_purchase=date(2026,8,1),last_purchase=date(2026,8,1),
                             visit_days=1,purchases=1,amount=100) for n in range(120)]
            return await super().fetch(sql,*args)
    monkeypatch.setattr(d,'pool',lambda:Pool(C()))
    asyncio.run(d.report('quantities','2026-09-01','2026-09-04',query='тара 1',match='exact'))
    result=asyncio.run(d.report('clients','2026-07-01','2026-10-23',frequency_filter='one_purchase',export_all=True))
    assert len(result['rows'])==120 and result['frequency_groups']['one_purchase']==120


def test_customer_card_coverage_and_no_history(monkeypatch):
    class C(Conn):
        async def fetchrow(self,sql,*args):
            assert args==(9,date(2026,10,23))
            if 'bonus_observations' in sql:
                return dict(eligible_purchases=0,current_responses=0,known_debit=None,known_to_accrue=None,last_attempt=None)
            return dict(purchases=0,visit_days=0,first_purchase=None,last_purchase=None,amount=None,average_sale=None,stores=0)
        async def fetch(self,*args): return []
    monkeypatch.setattr(e,'pool',lambda:Pool(C()))
    result=asyncio.run(e.customer_card('9','2026-10-23'))
    assert result['summary']['days_since_purchase'] is None
    assert result['bonuses']['known_debit'] is None


def test_payment_audit_has_sale_granularity_and_no_raw_payload(monkeypatch):
    class C(Conn):
        async def fetch(self,sql,*args):
            if 'HAVING COUNT' in sql:
                assert 'raw_json' not in sql and 'GROUP BY s.point_id,s.sale_id' in sql
                assert args==(date(2026,10,23),date(2026,10,23),None)
                return [dict(point_id=1,sale_id=10,sale_number='N',seller_id=9,seller_name='A',payment_rows=2,
                    nonreturn_rows=2,return_rows=0,total_price=D(100),signed_payments=D(100),payments=[
                    dict(payment_key='a',amount=40,is_return=False,business_date='2026-10-23',seller_id=9,source='native'),
                    dict(payment_key='b',amount=60,is_return=False,business_date='2026-10-23',seller_id=9,source='native')])]
            return []
    monkeypatch.setattr(e,'pool',lambda:Pool(C()))
    result=asyncio.run(e.payment_audit('2026-10-23','2026-10-23'))
    assert result['total']==1 and len(result['rows'][0]['payments'])==2
    assert result['rows'][0]['sale_id']=='10'
