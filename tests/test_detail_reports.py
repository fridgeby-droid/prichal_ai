import asyncio
from contextlib import asynccontextmanager
from datetime import date,timedelta
from decimal import Decimal as D

import pytest
from app.services import detail_reports as d
from app.services import management_reports as m
from scripts.diagnose_sale import structure


def test_weighted_average_and_null_base():
    rows=[dict(revenue=100,checks=1,returns=20,fallback=0),dict(revenue=900,checks=3,returns=0,fallback=1)]
    assert d.sums(rows)==dict(revenue=1000,checks=4,average=250,returns=20,fallback=1)
    assert d.sums([])['average'] is None


def test_client_recency_boundaries_and_frequency():
    today=date(2026,10,23)
    rows=[dict(customer_id=i,first_purchase=today-timedelta(days=i+30),last_purchase=today-timedelta(days=i),visit_days=4,purchases=5) for i in [0,29,30,59,60,89,90]]
    selected=d.recency(rows,today,30,59)
    assert [r['customer_id'] for r in selected]==['30','59']
    assert all(r['segment']=='30-59' and r['mean_days_between_visits']==10 for r in selected)
    assert [r['segment'] for r in d.recency(rows,today)]==['0-29','0-29','30-59','30-59','60-89','60-89','90+']


def test_diagnostic_no_card_phone_name_or_arbitrary_keys():
    data={'CustomerName':'SECRET NAME','Phone':'79990000000','Card':123456789,'Modifiers':[{'Name':'Secret item','Quantity':1}],'BonusAmount':9,'79990000000':'secret'}
    result=structure(data)
    rendered=str(result)
    assert all(s not in rendered for s in ['SECRET NAME','Secret item','79990000000','123456789'])
    assert result['$.BonusAmount']['numeric_values']==[9]
    assert result['$.Modifiers[].Quantity']['numeric_values']==[1]


class Conn:
    def __init__(self): self.calls=[]
    @asynccontextmanager
    async def transaction(self,**kwargs):
        assert kwargs==dict(isolation='repeatable_read',readonly=True)
        yield
    async def fetchval(self,*args): return date(2026,9,1)
    async def fetch(self,sql,*args):
        self.calls.append((sql,args))
        if 'FROM retail_stores' in sql: return [{'point_id':1,'name':'Магазин'}]
        if sql==d.PAYMENTS: return [dict(point_id=1,seller_id=7,seller_name='Тёзка',business_date=date(2026,9,1),shift_type='DAY',checks=2,revenue=300,returns=10,fallback=0),dict(point_id=1,seller_id=8,seller_name='Тёзка',business_date=date(2026,9,1),shift_type='DAY',checks=1,revenue=50,returns=0,fallback=0)]
        if sql==d.ITEMS: return [dict(point_id=1,seller_id=7,seller_name='Тёзка',product_key='id:42',name='Пакет',unit='шт',quantity=D(6),return_quantity=1,revenue=30,purchases=2),dict(point_id=1,seller_id=7,seller_name='Тёзка',product_key='id:43',name='Пиво',unit='л',quantity=D('1.5'),return_quantity=0,revenue=100,purchases=1)]
        if sql==d.BASKETS: return [dict(point_id=1,sale_id=1,lines=2,products=2),dict(point_id=1,sale_id=2,lines=1,products=1),dict(point_id=1,sale_id=3,lines=0,products=0)]
        if sql==d.CLIENTS: return [dict(customer_id=9,first_purchase=date(2026,8,1),last_purchase=date(2026,8,31),visit_days=2,purchases=3,amount=400)]
        if sql==d.SALES: return [dict(point_id=1,seller_id=7,seller_name='Тёзка',revenue=330,checks=2,returns=0,fallback=0)]
        return []


class Pool:
    def __init__(self,c): self.c=c
    @asynccontextmanager
    async def acquire(self): yield self.c


@pytest.mark.parametrize('kind',['averages','quantities','basket','trend','clients'])
def test_detail_reports(kind,monkeypatch):
    c=Conn()
    monkeypatch.setattr(d,'pool',lambda:Pool(c))
    result=asyncio.run(d.report(kind,'2026-09-01','2026-09-30'))
    assert not result['complete_source'] and 'предварительные' in result['report_text']
    if kind=='averages':
        assert len(result['groups']['sellers'])==2
        assert result['totals']['average']==D(350)/3
        assert result['sale_averages']['network'][0]['average']==165
    if kind=='quantities':
        assert result['rows'][1]['average_unit_price']==5
        assert result['rows'][0]['point_id']=='1'
    if kind=='basket':
        assert result['purchases_without_items']==1
        assert result['lines_per_purchase']==1
        assert result['quantity_per_purchase_by_unit']=={'шт':2,'л':D('.5')}
    if kind=='clients':
        assert result['rows'][0]['days_since_purchase']==30
        assert result['rows'][0]['customer_id']=='9'
        assert result['selected_clients']==1
    if kind=='trend':
        assert result['groups']['weekdays'][0]['calendar_occurrences']==5


def test_quantity_filters_are_bound_not_interpolated(monkeypatch):
    c=Conn()
    monkeypatch.setattr(d,'pool',lambda:Pool(c))
    asyncio.run(d.report('quantities','2026-09-01','2026-09-04',seller_id='7',shift='NIGHT',query="' OR true --"))
    call=next(x for x in c.calls if x[0]==d.ITEMS)
    assert call[1][3:]==(7,'NIGHT',"' OR true --")
    assert "' OR true --" not in call[0]


def test_client_unsupported_filter_rejected():
    with pytest.raises(ValueError): asyncio.run(d.report('clients',seller_id='7'))


def test_quantity_comparison_zero_quantity():
    row=m.dimension({'x':{'name':'X','revenue':100,'quantity':0,'unit':'шт'}}, {'x':{'name':'X','revenue':50,'quantity':2,'unit':'шт'}},1,1,True)['rows'][0]
    assert row['average_unit_price_current'] is None
    assert row['average_unit_price_previous']==25


def test_item_and_basket_sql_no_payment_multiplication():
    import sqlite3
    conn=sqlite3.connect(':memory:')
    conn.row_factory=sqlite3.Row
    conn.executescript('''
    CREATE TABLE retail_sales(point_id INT,sale_id INT,seller_id INT,seller_name TEXT,deleted BOOL,is_return BOOL,business_date TEXT,business_shift_type TEXT);
    CREATE TABLE retail_sale_items(point_id INT,sale_id INT,item_key TEXT,product_uuid TEXT,product_id INT,name TEXT,unit_name TEXT,quantity NUMERIC,total_price NUMERIC,is_return BOOL,refused BOOL);
    INSERT INTO retail_sales VALUES(1,1,7,'Same',0,0,'2026-09-01','DAY'),(1,2,7,'Same',0,1,'2026-09-01','DAY'),(1,3,8,'Same',0,0,'2026-09-01','NIGHT'),(1,4,7,'Same',0,0,'2026-09-01','DAY');
    INSERT INTO retail_sale_items VALUES(1,1,'a','',42,'Bag','шт',6,30,0,0),(1,1,'b','',43,'Beer','л',1.5,100,0,0),(1,2,'c','',42,'Bag','шт',-1,-5,1,0),(1,3,'d','',42,'Bag','шт',100,500,0,0),(1,1,'e','',42,'Bag','шт',999,999,0,1);
    ''')
    def portable(sql):
        import re
        sql=re.sub(r'(\$\d+)::(?:BIGINT|TEXT)',r'\1',sql)
        sql=re.sub(r'(i\.[a-z_]+)::TEXT',r'CAST(\1 AS TEXT)',sql)
        sql=sql.replace('COUNT(DISTINCT (i.point_id,i.sale_id))',"COUNT(DISTINCT i.point_id||':'||i.sale_id)")
        sql=sql.replace('POSITION(LOWER($6) IN LOWER(i.name))','INSTR(LOWER(i.name),LOWER($6))')
        return sql
    params={'1':'2026-09-01','2':'2026-09-01','3':None,'4':7,'5':'DAY','6':None}
    items=[dict(r) for r in conn.execute(portable(d.ITEMS),params)]
    bag=next(r for r in items if r['product_key']=='id:42')
    assert bag['quantity']==6 and bag['return_quantity']==1 and bag['purchases']==1 and bag['revenue']==30
    baskets=[dict(r) for r in conn.execute(portable(d.BASKETS),params)]
    assert len(baskets)==2 and sum(r['lines'] for r in baskets)==2
    assert sum(r['products'] for r in baskets)==2


def test_comparable_items_filtered_inside_snapshot(monkeypatch):
    from test_management_reports import Connection,Pool as MP
    from unittest.mock import AsyncMock
    class C(Connection):
        async def fetch(self,sql,*args):
            if 'ANY($4::BIGINT[])' in sql:
                assert self.depth==1 and args[3]==[1]
                return await super().fetch(m.periods.PRODUCTS_SQL,*args[:3])
            return await super().fetch(sql,*args)
    c=C()
    monkeypatch.setattr(m,'pool',lambda:MP(c))
    monkeypatch.setattr(m.catalog_service,'read_catalog',AsyncMock(return_value={'mapping':{},'revision':'x','fetched_at':'now'}))
    r=asyncio.run(m.report('2026-09-01','2026-09-01','2026-08-01','2026-08-01',kind='drivers'))
    assert r['comparable']['products']['totals']['current']==D('.01')
    assert r['comparable']['categories']['totals']['current']==D('.01')
    assert 'Товары сопоставимой сети' in r['report_text']
    assert 'Количество:' in r['report_text']
