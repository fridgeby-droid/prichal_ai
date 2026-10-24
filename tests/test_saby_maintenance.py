import asyncio
import copy
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.services import saby_maintenance as m
from app.services.bonus_load import summarize
from test_detail_reports import Conn,Pool


def setup(monkeypatch):
    memory={}
    class C(Conn):
        async def fetch(self,*args): return []
        async def fetchval(self,sql,*args):
            if 'pg_try' in sql: return True
            return copy.deepcopy(memory.get(args[0])) if args else None
        async def execute(self,*args): pass
    async def save(key,value): memory[key]=copy.deepcopy(value)
    monkeypatch.setattr(m,'pool',lambda:Pool(C()))
    monkeypatch.setattr(m,'save',save)
    monkeypatch.setattr(m,'get_settings',lambda:SimpleNamespace(
        auto_sync_enabled=False,auto_sync_on_start=False,saby_price_list_point_ids=''))
    monkeypatch.setattr(m,'closed_day',lambda:date(2026,10,23))
    mocks={}
    for owner,name,result in [(m.sync_service,'sync_stores',[]),
        (m.backfill_service,'create_run',7),(m.backfill_service,'run',{'status':'COMPLETED'}),
        (m.payroll_service,'sync_seller_identities',{}),(m,'sync_catalog',{}),
        (m.bonus_load,'load',{'status':'LOCAL_SALES_PROCESSED'})]:
        mock=AsyncMock(return_value=result);monkeypatch.setattr(owner,name,mock);mocks[name]=mock
    return memory,mocks


def test_resume_skips_successful_stages_and_reuses_backfill(monkeypatch):
    memory,mocks=setup(monkeypatch)
    mocks['sync_catalog'].side_effect=[RuntimeError('secret'),{}]
    first=asyncio.run(m.update())
    assert first['status']=='ERROR' and first['current_stage']=='catalog'
    assert 'secret' not in str(first)
    resumed=asyncio.run(m.update(resume=True))
    assert resumed['status']=='COMPLETED'
    assert mocks['sync_stores'].await_count==1
    assert mocks['create_run'].await_count==1 and mocks['run'].await_count==1
    assert mocks['load'].await_args.kwargs=={'limit':0}


def test_reconcile_failure_does_not_reach_catalog_or_bonuses(monkeypatch):
    memory,mocks=setup(monkeypatch)
    mocks['run'].return_value={'status':'ERROR'}
    result=asyncio.run(m.update())
    assert result['status']=='ERROR'
    mocks['sync_catalog'].assert_not_called()
    mocks['load'].assert_not_called()
    mocks['run'].return_value={'status':'COMPLETED'}
    asyncio.run(m.update(resume=True))
    assert mocks['create_run'].await_count==1


def test_unfinished_run_requires_resume(monkeypatch):
    memory,mocks=setup(monkeypatch)
    memory[m.KEY]={'status':'RUNNING'}
    with pytest.raises(ValueError,match='USE_RESUME'): asyncio.run(m.update())
    mocks['sync_stores'].assert_not_called()


def test_no_customer_keeps_payment_denominator():
    result=summarize([dict(is_return=False,customer_id=None,status=None,fresh=None,
                          revenue=100,fallback=0,response=None)])
    assert result['payment_revenue']=='100'
    assert result['no_card_by_owner_rule']==1 and result['unconfirmed_purchases']==0
    assert result['card_purchases_with_payments']==0


def test_only_unloaded_days_are_scheduled():
    start=date(2026,9,1);end=date(2026,9,30)
    coverage=[dict(date_from=start,date_to=end,next_date=date(2026,9,8)),
              dict(date_from=date(2026,9,15),date_to=end,next_date=date(2026,10,1))]
    assert m.missing_ranges(start,end,coverage)==[{'start':'2026-09-08','end':'2026-09-14'}]
    assert m.missing_ranges(start,end,[dict(date_from=start,date_to=end,next_date=date(2026,10,1))])==[]


def test_incomplete_price_preserves_old_snapshot_and_can_resume(monkeypatch):
    memory,mocks=setup(monkeypatch)
    monkeypatch.setattr(m,'get_settings',lambda:SimpleNamespace(
        auto_sync_enabled=False,auto_sync_on_start=False,saby_price_list_point_ids='23109'))
    monkeypatch.setattr(m,'allowed_point',lambda p:p)
    monkeypatch.setattr(m,'list_prices',AsyncMock(return_value={
        'complete_snapshot':True,'price_lists':[{'id':'66'}]}))
    price=AsyncMock(side_effect=[{'complete_snapshot':False},
                               {'complete_snapshot':True,'products':[]}])
    monkeypatch.setattr(m,'get_price',price)
    key='maintenance:price:content:23109:66'
    memory[key]={'complete_snapshot':True,'products':[{'id':'old'}]}
    assert asyncio.run(m.update())['status']=='ERROR'
    assert memory[key]['products']==[{'id':'old'}]
    assert asyncio.run(m.update(resume=True))['status']=='COMPLETED'
    assert memory[key]['products']==[]
