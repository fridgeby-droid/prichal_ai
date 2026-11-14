import asyncio
from datetime import date,datetime,timedelta,timezone
from unittest.mock import AsyncMock
import pytest
from app.services import daily_reconcile as d, auto_sources as a
from test_auto_sources import harness,future,AT
from test_detail_reports import Conn,Pool

DAY=date(2026,11,13)


def sync(**kw):
    return dict(id=1,status='OK',date_from=DAY,date_to=DAY+timedelta(days=1),
                started_at=datetime(2026,11,14,4,tzinfo=timezone.utc),
                finished_at=datetime(2026,11,14,4,10,tzinfo=timezone.utc))|kw


def classify(syncs,checks=None,backfills=None):
    return d.classify(DAY,DAY,backfills or [],syncs,checks or {},'Asia/Yekaterinburg',8)


def test_loaded_day_is_pending_reconcile_not_missing_data():
    result=classify([sync()])
    assert result['missing_load']==[] and result['candidates']==[(DAY,1)]
    assert result['pending_reconcile']==[{'start':str(DAY),'end':str(DAY)}]


@pytest.mark.parametrize('row',[
    sync(date_to=DAY),sync(status='ERROR'),sync(status='RUNNING',finished_at=None),
    sync(started_at=datetime(2026,11,14,2,tzinfo=timezone.utc)),
])
def test_incomplete_or_before_close_load_never_certified(row):
    result=classify([row])
    assert result['missing_load'] and result['candidates']==[]


def test_new_sync_invalidates_check_and_review_is_not_ok():
    checks={str(DAY):{'sync_run_id':1,'status':'OK'}}
    assert classify([sync()],checks)['pending_reconcile']==[]
    assert classify([sync(id=2),sync()],checks)['candidates']==[(DAY,2)]
    checks[str(DAY)]['status']='REVIEW'
    result=classify([sync()],checks)
    assert result['review'] and not result['candidates']


def test_completed_backfill_not_reloaded_or_rechecked():
    result=classify([],backfills=[dict(date_from=DAY,date_to=DAY,next_date=DAY+timedelta(days=1))])
    assert not result['missing_load'] and not result['candidates']


@pytest.mark.parametrize('changed',[True,False])
def test_persist_check_only_if_load_generation_unchanged(monkeypatch,changed):
    monkeypatch.setattr(d,'inspect',AsyncMock(return_value={'candidates':[(DAY,1)]}))
    monkeypatch.setattr(d.analytics_service,'reconcile',AsyncMock(return_value={'ok':False,'stores':[]}))
    writes=[]
    class C(Conn):
        async def fetchrow(self,*args): return {'id':2 if changed else 1,'status':'OK'}
        async def execute(self,*args): writes.append(args)
    monkeypatch.setattr(d,'pool',lambda:Pool(C()))
    result=asyncio.run(d.run_next())
    if changed:
        assert result['status']=='PENDING' and not writes
    else:
        assert result['reconcile_status']=='REVIEW' and len(writes)==1
        assert result['source_complete'] is False


@pytest.mark.parametrize('result,expected,seconds',[
    ({'status':'PARTIAL_ERRORS','saved':15,'errors':1},'PARTIAL',60),
    ({'status':'PARTIAL_ERRORS','saved':2,'errors':2},'PARTIAL',60),
    ({'status':'PARTIAL_ERRORS','saved':0,'errors':4},'ERROR',3600),
    ({'status':'PARTIAL_ERRORS','saved':15,'errors':5},'ERROR',3600),
    ({'status':'STOPPED','saved':15,'errors':1,'error':'RETRY_LATER'},'ERROR',3600),
    ({'status':'STOPPED','saved':15,'errors':5,'error':'HTTP_500'},'ERROR',3600),
])
def test_bonus_partial_progress_without_losing_failure_backoff(monkeypatch,result,expected,seconds):
    state=future();state['bonuses_new']['next_run_at']=AT.isoformat()
    memory,execute=harness(monkeypatch,state)
    execute.return_value=result
    asyncio.run(a.tick())
    record=memory[a.KEY]['bonuses_new']
    assert record['status']==expected and record['result']==result
    assert record['next_run_at']==(AT+timedelta(seconds=seconds)).isoformat()
