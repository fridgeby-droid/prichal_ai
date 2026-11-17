import asyncio
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.services import check_amounts as m


@pytest.mark.parametrize('comparison,expected', [('gt',2), ('gte',3), ('lt',1), ('lte',2), ('eq',1)])
def test_real_query_boundaries_and_exclusions(monkeypatch, comparison, expected):
    db=sqlite3.connect(':memory:'); db.row_factory=sqlite3.Row
    db.executescript('''CREATE TABLE retail_sales(point_id INT,sale_id INT,total_price NUMERIC,
        business_date TEXT,deleted BOOL,is_return BOOL);
        CREATE TABLE retail_stores(point_id INT PRIMARY KEY,name TEXT);
        INSERT INTO retail_stores VALUES(1,'One'),(2,'Two');
        INSERT INTO retail_sales VALUES
        (1,1,499.99,'2026-10-01',0,0),(1,2,500,'2026-10-31',0,0),
        (1,3,500.01,'2026-10-10',0,0),(2,3,700,'2026-10-10',0,0),
        (1,4,NULL,'2026-10-10',0,0),(1,5,800,'2026-10-10',0,1),
        (1,6,800,'2026-10-10',1,0),(23109,1,800,'2026-10-10',0,0),
        (1,7,900,'2026-11-01',0,0);''')
    async def fetch(sql, first, last, point, amount):
        return db.execute(sql.replace('$3::BIGINT','$3'),
            {'1':str(first),'2':str(last),'3':point,'4':float(amount)}).fetchall()
    @asynccontextmanager
    async def acquire(): yield SimpleNamespace(fetch=fetch)
    monkeypatch.setattr(m,'pool',lambda:SimpleNamespace(acquire=acquire))
    monkeypatch.setattr(m,'point_filter',AsyncMock(return_value=None))
    r=asyncio.run(m.report('2026-10-01','2026-10-31','500',comparison))
    assert r['matched_purchases']==expected
    assert r['total_purchases']==5 and r['unknown_amount']==1
    assert [x['point_id'] for x in r['stores']]==['1','2']
    monkeypatch.setattr(m,'point_filter',AsyncMock(return_value=2))
    r=asyncio.run(m.report('2026-10-01','2026-10-31','500','gt','2'))
    assert r['matched_purchases']==1 and r['total_purchases']==1
    r=asyncio.run(m.report('2026-09-01','2026-09-30'))
    assert r['matched_purchases'] is None and r['status']=='NO_LOCAL_RECORDS'
    db.close()


@pytest.mark.parametrize('amount,op',[('NaN','gt'),('Infinity','gt'),('-1','gt'),('abc','gt'),('500','> OR 1=1')])
def test_reject_invalid(amount,op):
    with pytest.raises(ValueError): m.parameters(amount,op)
