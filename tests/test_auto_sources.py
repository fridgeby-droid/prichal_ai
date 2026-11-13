import asyncio
import copy
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.services import auto_sources as a, saby_maintenance as m, bonus_load as b
from test_saby_maintenance import setup
from test_detail_reports import Conn,Pool

AT=datetime(2026,11,13,12,tzinfo=timezone.utc)


def settings(**kw):
    return SimpleNamespace(**(dict(auto_sync_enabled=True,auto_sync_on_start=True,
        auto_sources_enabled=True,sync_interval_minutes=60,sync_recent_days=3,
        auto_bonus_interval_minutes=10,auto_source_interval_hours=24,
        auto_bonus_batch_size=50,saby_price_list_point_ids='23109')|kw))


def future():
    return {job:{'status':'OK','next_run_at':(AT+timedelta(hours=1)).isoformat()} for job in a.JOBS}


def harness(monkeypatch,state=None):
    memory={a.KEY:copy.deepcopy(state or {})}
    class C(Conn):
        async def fetchval(self,sql,*args):
            return True if 'pg_try' in sql else False
        async def execute(self,*args): pass
    async def read(key): return copy.deepcopy(memory.get(key))
    async def save(key,value): memory[key]=copy.deepcopy(value)
    monkeypatch.setattr(a,'pool',lambda:Pool(C()))
    monkeypatch.setattr(a,'read_state',read)
    monkeypatch.setattr(a.maintenance,'save',save)
    monkeypatch.setattr(a,'get_settings',settings)
    monkeypatch.setattr(a,'now',lambda:AT)
    execute=AsyncMock(return_value={'status':'COMPLETED'})
    monkeypatch.setattr(a,'execute_job',execute)
    return memory,execute


def test_sales_first_and_background_requires_success():
    assert a.select_job({},settings(),AT)=='sales'
    state={'sales':{'status':'ERROR','next_run_at':(AT+timedelta(minutes=5)).isoformat()}}
    assert a.select_job(state,settings(),AT) is None
    state['sales']['status']='OK'
    assert a.select_job(state,settings(),AT)=='bonuses_new'
    assert a.select_job(state,settings(auto_sources_enabled=False),AT) is None


def test_oldest_due_source_not_starved_and_errors_last():
    state=future()
    state['prices']['next_run_at']=(AT-timedelta(minutes=3)).isoformat()
    state['bonuses_new']['next_run_at']=(AT-timedelta(minutes=1)).isoformat()
    state['bonuses_errors']['next_run_at']=(AT-timedelta(days=2)).isoformat()
    assert a.select_job(state,settings(),AT)=='prices'
    state['sales']['next_run_at']=AT.isoformat()
    assert a.select_job(state,settings(),AT)=='sales'


def test_persistent_schedule_no_duplicate_after_restart(monkeypatch):
    memory,execute=harness(monkeypatch,future())
    memory[a.KEY]['sales']['next_run_at']=AT.isoformat()
    asyncio.run(a.tick())
    assert execute.await_count==1
    asyncio.run(a.tick())
    assert execute.await_count==1
    assert memory[a.KEY]['sales']['last_success_at']==AT.isoformat()


def test_error_preserves_other_jobs_and_defers_one_hour(monkeypatch):
    state=future();state['catalog']['next_run_at']=AT.isoformat()
    memory,execute=harness(monkeypatch,state)
    execute.side_effect=RuntimeError('secret upstream text')
    asyncio.run(a.tick())
    result=memory[a.KEY]
    assert result['catalog']['status']=='ERROR'
    assert result['catalog']['next_run_at']==(AT+timedelta(hours=1)).isoformat()
    assert result['sales']==state['sales'] and 'secret' not in str(result)


def test_shutdown_persists_interrupted_and_resumes(monkeypatch):
    state=future();state['prices']['next_run_at']=AT.isoformat()
    memory,execute=harness(monkeypatch,state)
    execute.side_effect=asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): asyncio.run(a.tick())
    assert memory[a.KEY]['prices']['status']=='INTERRUPTED'
    execute.side_effect=None;execute.return_value={'status':'PENDING'}
    asyncio.run(a.tick())
    assert memory[a.KEY]['prices']['status']=='PENDING'


def test_bonus_error_retry_is_daily_and_new_queue_separate(monkeypatch):
    state=future();state['bonuses_errors']['next_run_at']=AT.isoformat()
    memory,execute=harness(monkeypatch,state)
    execute.return_value={'status':'STOPPED','error':'HTTP_500','errors':5}
    asyncio.run(a.tick())
    assert memory[a.KEY]['bonuses_errors']['next_run_at']==(AT+timedelta(days=1)).isoformat()
    assert memory[a.KEY]['bonuses_new']==state['bonuses_new']


def test_automatic_prices_checkpoint_and_resume(monkeypatch):
    memory,mocks=setup(monkeypatch)
    monkeypatch.setattr(m,'get_settings',settings)
    monkeypatch.setattr(m,'allowed_point',lambda p:p)
    listing=AsyncMock(return_value={'complete_snapshot':True,'price_lists':[{'id':'81'},{'id':'82'}]})
    reader=AsyncMock(return_value={'complete_snapshot':True,'products':[]})
    monkeypatch.setattr(m,'list_prices',listing);monkeypatch.setattr(m,'get_price',reader)
    first=asyncio.run(m.update(component='prices',automatic=True,max_stages=1))
    assert first['status']=='PENDING' and reader.await_count==0
    second=asyncio.run(m.update(component='prices',automatic=True,max_stages=1,resume=True))
    assert second['status']=='PENDING' and reader.await_count==1
    third=asyncio.run(m.update(component='prices',automatic=True,max_stages=1,resume=True))
    assert third['status']=='COMPLETED' and reader.await_count==2 and listing.await_count==1
    assert m.KEY not in memory and m.state_key('prices') not in memory


@pytest.mark.parametrize('queue,fragment',[('new','AND b.point_id IS NULL'),('errors',"INTERVAL '24 hours'")])
def test_bonus_queue_filters_used_in_count_and_fetch(monkeypatch,queue,fragment):
    queries=[]
    class C(Conn):
        async def fetchval(self,sql,*args): queries.append(sql);return 1
        async def fetch(self,sql,*args): queries.append(sql);return []
        async def execute(self,*args): pass
    monkeypatch.setattr(b,'pool',lambda:Pool(C()))
    asyncio.run(b.load(queue_mode=queue,coordinated=True))
    reads=[q for q in queries if 'retail_sales' in q]
    assert len(reads)==2 and all(fragment in q for q in reads)


def test_disabled_worker_makes_no_db_calls(monkeypatch):
    monkeypatch.setattr(a,'get_settings',lambda:settings(auto_sync_enabled=False))
    monkeypatch.setattr(a,'pool',lambda:pytest.fail('disabled worker opened database'))
    asyncio.run(a.tick())


def test_competing_worker_makes_no_requests(monkeypatch):
    memory,execute=harness(monkeypatch)
    class C(Conn):
        async def fetchval(self,*args): return False
    monkeypatch.setattr(a,'pool',lambda:Pool(C()))
    asyncio.run(a.tick());execute.assert_not_called()
