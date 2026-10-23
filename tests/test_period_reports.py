import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from app.services import period_reports as m


def test_period_payment_sql_no_double_join_and_return_convention():
    conn=sqlite3.connect(':memory:'); conn.row_factory=sqlite3.Row
    conn.executescript('''
    CREATE TABLE sales(point_id INT,sale_id INT,deleted BOOL);
    CREATE TABLE sale_payments(point_id INT,sale_id INT,business_date TEXT,business_shift_type TEXT,is_return BOOL,amount NUMERIC,source TEXT);
    CREATE VIEW retail_sales AS SELECT * FROM sales WHERE point_id<>23109;
    CREATE VIEW retail_sale_payments AS SELECT * FROM sale_payments WHERE point_id<>23109;
    INSERT INTO sales VALUES(1,1,0),(1,2,0),(2,1,0),(1,3,1),(23109,1,0);
    INSERT INTO sale_payments VALUES
    (1,1,'2026-07-01','DAY',0,100,'saby_payment'),
    (1,1,'2026-07-02','NIGHT',0,50,'saby_payment'),
    (1,2,'2026-07-02','NIGHT',1,-30,'saby_payment'),
    (2,1,'2026-07-02','DAY',0,70,'sale_total_fallback'),
    (1,3,'2026-07-02','DAY',0,1000,'saby_payment'),
    (23109,1,'2026-07-02','DAY',0,2000,'saby_payment'),
    (1,1,'2026-07-03','DAY',0,999,'saby_payment');
    ''')
    sql=m.PAYMENTS_SQL.replace('$3::BIGINT','$3')
    rows=[dict(r) for r in conn.execute(sql,{'1':'2026-07-01','2':'2026-07-02','3':None})]
    total=m.totals(rows)
    assert total['revenue']==220 and total['checks']==3 and total['returns']==30
    assert total['average']==Decimal(220)/3 and total['fallback']==1
    one=m.totals([dict(r) for r in conn.execute(sql,{'1':'2026-07-01','2':'2026-07-02','3':1})])
    assert one['revenue']==150 and one['checks']==2


def test_product_sql_keeps_units_ids_and_no_payment_join():
    conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
    conn.executescript('''
    CREATE TABLE sales(point_id INT,sale_id INT,deleted BOOL,business_date TEXT);
    CREATE TABLE sale_items(point_id INT,sale_id INT,item_key TEXT,product_uuid TEXT,product_id INT,name TEXT,unit_name TEXT,quantity NUMERIC,total_price NUMERIC,is_return BOOL,refused BOOL);
    CREATE VIEW retail_sales AS SELECT * FROM sales WHERE point_id<>23109;
    CREATE VIEW retail_sale_items AS SELECT * FROM sale_items WHERE point_id<>23109;
    INSERT INTO sales VALUES(1,1,0,'2026-07-01'),(1,2,1,'2026-07-01'),(23109,1,0,'2026-07-01');
    INSERT INTO sale_items VALUES
    (1,1,'a','',7,'Same','кг',0.5,100,0,0),
    (1,1,'b','',7,'Same','шт',1,50,0,0),
    (1,1,'c','',8,'Same','кг',1,60,0,0),
    (1,1,'d','',7,'Same','кг',-0.2,-40,1,0),
    (1,1,'e','',7,'Same','кг',1,999,0,1),
    (1,2,'f','',7,'Same','кг',1,999,0,0),
    (23109,1,'g','',7,'Same','кг',1,999,0,0);
    ''')
    sql=m.PRODUCTS_SQL.replace('$3::BIGINT','$3')
    for key in ('product_id','point_id','sale_id'):
        sql=sql.replace(f'i.{key}::TEXT',f'CAST(i.{key} AS TEXT)')
    rows=[dict(r) for r in conn.execute(sql,{'1':'2026-07-01','2':'2026-07-02','3':None})]
    assert len(rows)==3
    row=next(r for r in rows if r['product_key']=='id:7' and r['unit']=='кг')
    assert row['quantity']==0.5 and row['revenue']==100 and row['returns']==40


@pytest.mark.parametrize('start,end',[('2026-07-02','2026-07-01'),('2026-01-01','2026-07-01'),('2999-01-01','2999-01-01')])
def test_invalid_periods(start,end):
    with pytest.raises(ValueError): m.period(start,end)


def test_changes_zero_and_negative():
    assert m.delta(100,0)=={'absolute':100,'percent':None}
    assert m.delta(60,100)=={'absolute':-40,'percent':-40}


@pytest.mark.parametrize('hour,closed',[(7,False),(8,True)])
def test_closed_period_boundary_and_unknown_completeness(monkeypatch,hour,closed):
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None): return cls(2026,7,3,hour,tzinfo=tz)
    monkeypatch.setattr(m,'datetime',Clock)
    q=m.quality([], [{'point_id':1}],date(2026,7,2),date(2026,7,2),None,None)
    assert q['period_closed'] is closed
    assert q['completeness']=='UNCONFIRMED'
    assert q['store_dates_without_records']==[{'point_id':'1','dates':['2026-07-02']}]


def test_product_comparison_includes_disappeared_and_new(monkeypatch):
    class Conn:
        @asynccontextmanager
        async def transaction(self,**kwargs):
            assert kwargs['readonly']; yield
        async def fetch(self,sql,start,end,point):
            return [{'product_key':'id:1' if start.day==1 else 'id:2','unit':'шт','name':'Same',
                     'revenue':Decimal(100 if start.day==1 else 250),'quantity':1,'returns':0}]
    class Pool:
        @asynccontextmanager
        async def acquire(self): yield Conn()
    monkeypatch.setattr(m,'pool',lambda:Pool())
    data=asyncio.run(m.products('2026-07-01','2026-07-01',previous_start='2026-07-02',previous_end='2026-07-02'))
    assert data['products'][0]['changes']['absolute']==-250
    assert data['products'][0]['current'] is None
    assert data['products'][1]['changes']['absolute']==100
    assert data['categories_available'] is False


def test_empty_reconcile_is_not_ok(monkeypatch):
    monkeypatch.setattr(m,'snapshot',AsyncMock(return_value=(date(2026,7,1),date(2026,7,1),[],[],{})))
    monkeypatch.setattr(m.analytics_service,'reconcile',AsyncMock(return_value={'ok':True,'stores':[]}))
    data=asyncio.run(m.status('2026-07-01','2026-07-01'))
    assert data['quality']['reconciliation']=='NO_DATA'
    with pytest.raises(ValueError): asyncio.run(m.status('2026-07-01','2026-07-09'))


def test_agent_registers_tools_and_numeric_schema():
    from app.agent.executive import executive_agent
    names={t.name for t in executive_agent.tools}
    assert {'list_stores','get_period_summary','compare_sales_periods','get_day_night_breakdown',
            'get_product_sales','get_load_reconcile_status'} <= names


def test_exact_store_id_and_ambiguity(monkeypatch):
    class Conn:
        async def fetch(self,*args): return [dict(point_id=1,name='Shop',address='1',locality=''),dict(point_id=11,name='Shop',address='11',locality='')]
    class Pool:
        @asynccontextmanager
        async def acquire(self): yield Conn()
    monkeypatch.setattr(m,'pool',lambda:Pool())
    assert len(asyncio.run(m.stores('1'))['stores'])==1
    assert asyncio.run(m.stores('Shop'))['ambiguous'] is True
    with pytest.raises(ValueError): asyncio.run(m.point_filter('23109'))


def test_comparison_does_not_turn_missing_data_into_decline(monkeypatch):
    blank=m.totals([])
    populated={**blank,'records':2,'checks':2,'revenue':Decimal(150),'average':Decimal(75)}
    monkeypatch.setattr(m,'summary',AsyncMock(side_effect=[
        {'totals':blank,'stores':[{'point_id':'1','name':'Shop',**blank}]},
        {'totals':populated,'stores':[{'point_id':'1','name':'Shop',**populated}]}]))
    result=asyncio.run(m.compare('2026-07-08','2026-07-14','2026-07-01','2026-07-07'))
    assert result['changes'] is None
    assert result['store_changes'][0]['changes'] is None
    assert result['equal_duration'] is True


def test_unknown_shift_is_visible_and_totals_are_weighted(monkeypatch):
    rows=[dict(point_id=1,business_date=date(2026,7,1),shift_type=label,records=checks,
               checks=checks,return_checks=0,revenue=Decimal(revenue),returns=Decimal(0),fallback=0)
          for label,checks,revenue in [('DAY',1,100),('NIGHT',3,600),(None,1,20)]]
    monkeypatch.setattr(m,'snapshot',AsyncMock(return_value=(date(2026,7,1),date(2026,7,1),rows,[{'point_id':1,'name':'Shop'}],{})))
    summary=asyncio.run(m.summary('2026-07-01','2026-07-01'))
    shifts=asyncio.run(m.shifts('2026-07-01','2026-07-01'))
    assert summary['totals']['average']==144
    assert shifts['shifts']['UNKNOWN']['revenue']==20
    assert sum(s['revenue'] for s in shifts['shifts'].values())==summary['totals']['revenue']
