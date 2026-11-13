import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import asyncpg
from app.services import saby_maintenance as m
from app.services.lock_guard import keep_lock_alive, release_lock
from test_saby_maintenance import setup
from test_bootstrap import setup_run
from app.services import backfill


def test_catalog_independent_of_old_bonus_failure(monkeypatch):
    memory,mocks=setup(monkeypatch)
    old={'id':'old','status':'ERROR','error_code':'BONUS_LOAD_INCOMPLETE'}
    memory[m.KEY]=copy.deepcopy(old)
    result=asyncio.run(m.update(component='catalog'))
    assert result['status']=='COMPLETED' and result['done']==['catalog']
    assert memory[m.KEY]==old
    mocks['load'].assert_not_called()
    mocks['run'].assert_not_called()
    mocks['sync_stores'].assert_not_called()
    asyncio.run(m.update(component='catalog'))
    assert mocks['sync_catalog'].await_count==2
    assert memory[f'maintenance:saby:history:{result["id"]}']['status']=='COMPLETED'


def test_prices_resume_preserves_full_snapshot_and_skips_finished(monkeypatch):
    memory,mocks=setup(monkeypatch)
    monkeypatch.setattr(m,'get_settings',lambda:SimpleNamespace(
        auto_sync_enabled=False,auto_sync_on_start=False,saby_price_list_point_ids='23109'))
    monkeypatch.setattr(m,'allowed_point',lambda p:p)
    listing=AsyncMock(return_value={'complete_snapshot':True,'price_lists':[{'id':'81'},{'id':'82'}]})
    monkeypatch.setattr(m,'list_prices',listing)
    reader=AsyncMock(side_effect=[{'complete_snapshot':True,'products':[{'id':'new81'}]},
                                 {'complete_snapshot':False},
                                 {'complete_snapshot':True,'products':[]}])
    monkeypatch.setattr(m,'get_price',reader)
    key='maintenance:price:content:23109:82'
    memory[key]={'complete_snapshot':True,'products':[{'id':'old82'}]}
    assert asyncio.run(m.update(component='prices'))['status']=='ERROR'
    assert memory[key]['products']==[{'id':'old82'}]
    with pytest.raises(ValueError,match='USE_RESUME'):
        asyncio.run(m.update(component='prices'))
    result=asyncio.run(m.update(component='prices',resume=True))
    assert result['status']=='COMPLETED' and memory[key]['products']==[]
    assert listing.await_count==1 and reader.await_count==3
    mocks['load'].assert_not_called()
    mocks['sync_catalog'].assert_not_called()
    mocks['run'].assert_not_called()


def test_bonus_partial_not_full_completion_and_new_update_allowed(monkeypatch):
    memory,mocks=setup(monkeypatch)
    mocks['load'].return_value={'status':'STOPPED','errors':135}
    first=asyncio.run(m.update())
    assert first['status']=='PARTIAL'
    assert 'bonuses_all_local_customer_sales' not in first['done']
    mocks['load'].return_value={'status':'LOCAL_SALES_PROCESSED'}
    result=asyncio.run(m.update())
    assert result['status']=='COMPLETED'
    assert memory[f'maintenance:saby:history:{first["id"]}']['bonus_result']['errors']==135


def test_legacy_bonus_failure_can_start_new_general_update(monkeypatch):
    memory,mocks=setup(monkeypatch)
    memory[m.KEY]={'id':'old','status':'ERROR','error_code':'BONUS_LOAD_INCOMPLETE'}
    assert asyncio.run(m.update())['status']=='COMPLETED'
    assert memory['maintenance:saby:history:old']['status']=='ERROR'


def test_heartbeat_loss_stops_work():
    conn=SimpleNamespace(execute=AsyncMock(side_effect=asyncpg.InterfaceError('released')))
    async def work():
        async with keep_lock_alive(conn,interval=0.001):
            await asyncio.sleep(10)
    with pytest.raises(ValueError,match='DATABASE_LOCK_SESSION_LOST'):
        asyncio.run(work())


def test_external_cancellation_is_not_hidden():
    async def work():
        async with keep_lock_alive(SimpleNamespace(execute=AsyncMock())):
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): asyncio.run(work())


def test_backfill_completed_result_survives_released_cleanup_connection(monkeypatch):
    conn,service,sync=setup_run(monkeypatch)
    original=conn.execute
    async def execute(sql,*args):
        if 'pg_advisory_unlock' in sql:
            raise asyncpg.InterfaceError('connection has been released back to the pool')
        return await original(sql,*args)
    conn.execute=execute
    monkeypatch.setattr(backfill.analytics_service,'reconcile',AsyncMock(return_value={'ok':True,'stores':[]}))
    result=asyncio.run(service.run(1))
    assert result['status']=='COMPLETED' and len(conn.checkpoints)==2


def test_heartbeat_runs_and_is_stopped_after_work():
    async def work():
        called=asyncio.Event()
        async def execute(sql): called.set()
        conn=SimpleNamespace(execute=AsyncMock(side_effect=execute))
        async with keep_lock_alive(conn,interval=0.001):
            await asyncio.wait_for(called.wait(),timeout=2)
        count=conn.execute.await_count
        assert count>0
        await asyncio.sleep(0.01)
        assert conn.execute.await_count==count
    asyncio.run(work())
