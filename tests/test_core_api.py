import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import sqlite3

import httpx
import pytest
from pydantic import SecretStr
from fastapi import FastAPI
from app import core_api
from app.services.core_directories import assemble_sellers, ACTIVITY_SQL


def snapshot():
    return dict(schema_version=1, generated_at=datetime.now(timezone.utc), last_successful_sync_at=None,
                scope='retail_excluding_rc', seller_source='observed_saby_payment_accounts', complete_snapshot=True,
                stores=[], sellers=[], store_count=0, seller_count=0)


def request(monkeypatch, key='x'*40, header=None, payload=None):
    monkeypatch.setattr(core_api, 'get_settings', lambda: SimpleNamespace(core_directory_api_key=SecretStr(key)))
    fetch=AsyncMock(return_value=payload if payload is not None else snapshot())
    monkeypatch.setattr(core_api, 'get_directories', fetch)
    app=FastAPI(); app.include_router(core_api.router)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            return await client.get('/api/v1/core/directories', headers={'Authorization':header} if header else {})
    return asyncio.run(run()), fetch


@pytest.mark.parametrize('key,header,status', [('',None,503),('short','Bearer short',503),('x'*40,None,401),('x'*40,'Basic xxx',401),('x'*40,'Bearer wrong',401)])
def test_rejects_before_database(monkeypatch,key,header,status):
    response, fetch=request(monkeypatch,key,header)
    assert response.status_code==status
    assert response.headers['cache-control']=='no-store'
    fetch.assert_not_awaited()


def test_authenticated_response_and_string_ids(monkeypatch):
    data=snapshot()
    data['stores']=[dict(saby_point_id='9007199254740993',name='Store',address='',locality='',updated_at=datetime.now(timezone.utc))]
    data['store_count']=1
    response,fetch=request(monkeypatch,header='Bearer '+'x'*40,payload=data)
    assert response.status_code==200
    assert response.json()['stores'][0]['saby_point_id']=='9007199254740993'
    assert response.headers['cache-control']=='no-store'
    fetch.assert_awaited_once()


def test_db_error_does_not_leak_details(monkeypatch):
    monkeypatch.setattr(core_api, 'get_settings', lambda: SimpleNamespace(core_directory_api_key=SecretStr('x'*40)))
    monkeypatch.setattr(core_api, 'get_directories', AsyncMock(side_effect=RuntimeError('private database string')))
    app=FastAPI();app.include_router(core_api.router)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            return await client.get('/api/v1/core/directories',headers={'Authorization':'Bearer '+'x'*40})
    response=asyncio.run(run())
    assert response.status_code==503
    assert 'private' not in response.text


def test_distinct_ids_homonyms_aliases_and_missing_names():
    rows=[dict(seller_id=sid,seller_name=name,point_id=point,first_seen=first,last_seen=last) for sid,name,point,first,last in [
        (7,'Same name',1,date(2026,7,1),date(2026,8,1)),
        (7,'New name',1,date(2026,9,1),date(2026,10,1)),
        (7,'Same name',2,date(2026,8,2),date(2026,9,1)),
        (8,'Same name',1,date(2026,7,1),date(2026,7,1)),
        (9,'',2,None,None)]]
    result=assemble_sellers(rows)
    assert len(result)==3
    assert result[0]['display_name']=='New name'
    assert result[0]['observed_names']==['New name','Same name']
    assert result[0]['stores'][0]['first_seen']==date(2026,7,1)
    assert result[0]['last_seen']==date(2026,10,1)
    assert result[1]['saby_seller_id']=='8'
    assert result[2]['display_name'] is None
    assert result==assemble_sellers(list(reversed(rows)))


def test_activity_query_excludes_rc_deleted_and_missing_ids():
    conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
    conn.executescript("""
    CREATE TABLE stores(point_id INTEGER);
    CREATE TABLE sales(point_id INTEGER,sale_id INTEGER,deleted BOOLEAN);
    CREATE TABLE sale_payments(point_id INTEGER,sale_id INTEGER,seller_id INTEGER,seller_name TEXT,business_date TEXT);
    CREATE VIEW retail_stores AS SELECT * FROM stores WHERE point_id<>23109;
    CREATE VIEW retail_sales AS SELECT * FROM sales WHERE point_id<>23109;
    CREATE VIEW retail_sale_payments AS SELECT * FROM sale_payments WHERE point_id<>23109;
    INSERT INTO stores VALUES(1),(23109);
    INSERT INTO sales VALUES(1,1,0),(1,2,1),(23109,1,0);
    INSERT INTO sale_payments VALUES(1,1,7,' Name ','2026-10-08'),(1,2,8,'Deleted','2026-10-08'),
    (23109,1,9,'RC','2026-10-08'),(1,1,NULL,'Unknown','2026-10-08');
    """)
    rows=[dict(r) for r in conn.execute(ACTIVITY_SQL)]
    assert len(rows)==1
    assert rows[0]['seller_id']==7 and rows[0]['seller_name']=='Name'
    conn.close()


def test_route_registered_in_real_app():
    from app.web import app
    assert '/api/v1/core/directories' in app.openapi()['paths']
