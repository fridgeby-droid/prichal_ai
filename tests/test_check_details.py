import asyncio
import re
import sqlite3
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from app.services import check_details as m


@pytest.fixture
def database(monkeypatch):
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
    db.executescript('''
    CREATE TABLE retail_stores(point_id INT PRIMARY KEY,name TEXT);
    INSERT INTO retail_stores VALUES(1,'One'),(2,'Two');
    CREATE TABLE retail_sales(point_id INT,sale_id INT,raw_json TEXT,sale_number TEXT,
      sale_datetime TEXT,business_date TEXT,business_shift_type TEXT,seller_id INT,
      seller_name TEXT,total_price NUMERIC,is_return BOOL,deleted BOOL);
    INSERT INTO retail_sales VALUES
    (1,11,'{"Number":"123","Customer":"PRIVATE"}','x','2026-10-01','2026-10-01','DAY',9,'Seller',500,0,0),
    (1,12,'{"Number":"124"}','x','2026-10-01','2026-10-01','NIGHT',8,'Seller2',900,0,0),
    (2,11,'{"Number":"123"}','x','2026-10-02','2026-10-02','DAY',9,'Seller',1000,0,0),
    (1,13,'{}','return','2026-10-02','2026-10-02','DAY',9,'Seller',500,1,0),
    (1,14,'{}','deleted','2026-10-02','2026-10-02','DAY',9,'Seller',500,0,1),
    (23109,1,'{}','rc','2026-10-02','2026-10-02','DAY',9,'Seller',500,0,0);
    CREATE TABLE retail_sale_items(point_id INT,sale_id INT,item_key TEXT,product_id INT,
      name TEXT,quantity NUMERIC,unit_name TEXT,total_price NUMERIC,is_return BOOL,refused BOOL);
    INSERT INTO retail_sale_items VALUES
    (1,11,'a',1,'Bag',1,'шт',10,0,0),(1,11,'b',1,'Bag',1,'шт',10,0,0),
    (1,12,'c',1,'Bag',1,'шт',10,0,1);
    CREATE TABLE retail_sale_payments(point_id INT,sale_id INT,payment_key TEXT,
      check_number TEXT,carried_at TEXT,amount NUMERIC,signed_amount NUMERIC,
      is_return BOOL,nonfiscal BOOL,source TEXT);
    INSERT INTO retail_sale_payments VALUES
    (1,11,'p1','555','2026-10-01',200,200,0,0,'saby_payment'),
    (1,11,'p2','556','2026-10-01',300,300,0,0,'saby_payment');
    ''')
    def execute(sql,args):
        sql=re.sub(r'::(?:BIGINT|NUMERIC|TEXT|DATE)','',sql)
        sql=sql.replace('POSITION(LOWER($8) IN LOWER(i.name))','INSTR(LOWER(i.name),LOWER($8))')
        params={str(i+1):float(a) if isinstance(a,Decimal) else a if isinstance(a,(str,int,float,type(None))) else str(a) for i,a in enumerate(args)}
        return db.execute(sql,params)
    async def fetch(sql,*args):return execute(sql,args).fetchall()
    async def fetchval(sql,*args):return execute(sql,args).fetchone()[0]
    async def fetchrow(sql,*args):return execute(sql,args).fetchone()
    @asynccontextmanager
    async def transaction(**kwargs):yield
    @asynccontextmanager
    async def acquire():yield NS(fetch=fetch,fetchval=fetchval,fetchrow=fetchrow,transaction=transaction)
    monkeypatch.setattr(m,'pool',lambda:NS(acquire=acquire))
    async def point(value):return int(value) if value else None
    monkeypatch.setattr(m,'point_filter',point)
    yield db
    db.close()


def search(**kwargs):return asyncio.run(m.search('2026-10-01','2026-10-31',**kwargs))


def test_range_top_pagination(database):
    r=search(minimum='500',maximum='1000',limit=1)
    assert r['total_matches']==3 and r['next_offset']==1
    assert r['rows'][0]['point_id']=='2'
    assert search(offset=1,limit=1)['rows'][0]['sale_id']=='12'
    assert search(order='amount_asc')['rows'][0]['total_price']==500
    assert search(point_id='1',seller_id='9',shift='DAY')['total_matches']==1


def test_number_ambiguous_and_product_exists_not_double_counted(database):
    assert search(sale_number='123')['total_matches']==2
    assert search(sale_number='123',point_id='1')['total_matches']==1
    assert search(product='Bag')['total_matches']==1
    assert search(sale_number='11')['total_matches']==0
    assert search(returns=True)['total_matches']==1


def test_card_private_fields_and_payments_separate(database):
    r=asyncio.run(m.card('1','11'))
    assert r['purchase']['total_price']==500
    assert len(r['items'])==2 and len(r['payments'])==2
    assert 'PRIVATE' not in str(r) and 'raw_json' not in str(r)
    assert asyncio.run(m.card('2','12'))['status']=='NOT_FOUND'
    assert asyncio.run(m.card('1','14'))['status']=='NOT_FOUND'


@pytest.mark.parametrize('args',[{'minimum':'900','maximum':'500'},{'minimum':'NaN'},
    {'order':'bad'},{'date_basis':'bad'},{'shift':'bad'},{'limit':51},{'offset':-1}])
def test_invalid(database,args):
    with pytest.raises(ValueError):search(**args)


def test_calendar_uses_local_timezone(monkeypatch):
    captured=[]
    async def fetchval(sql,*args):captured.append((sql,args));return 0
    @asynccontextmanager
    async def transaction(**kwargs):yield
    @asynccontextmanager
    async def acquire():yield NS(fetchval=fetchval,fetch=AsyncMock(return_value=[]),transaction=transaction)
    monkeypatch.setattr(m,'pool',lambda:NS(acquire=acquire))
    monkeypatch.setattr(m,'point_filter',AsyncMock(return_value=None))
    assert search(date_basis='calendar')['date_basis']=='calendar'
    assert 'AT TIME ZONE $11' in captured[0][0]
    assert captured[0][1][-1]=='Asia/Yekaterinburg'
