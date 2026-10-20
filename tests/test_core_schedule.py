import asyncio
import copy
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import date
from unittest.mock import AsyncMock
import httpx
import pytest
from pydantic import ValidationError, SecretStr
from app.services import core_schedule as module


def sample():
    return dict(schema_version=1,source='prichal_core',authority='actual_schedule',generated_at='2026-10-09T12:00:00Z',date_from='2026-10-01',date_to='2026-10-31',complete_snapshot=True,count=1,shifts=[dict(core_assignment_id='1',core_employee_id='10',employee_name='Seller',core_store_id='2',store_name='Store',saby_point_id='5598',position='seller',work_date='2026-10-08',shift_type='NIGHT',slot=1)])


@pytest.mark.parametrize('mutation',[lambda d:d.update(complete_snapshot=False),lambda d:d.update(count=2),lambda d:d['shifts'][0].update(work_date='2026-09-01'),lambda d:d['shifts'][0].update(saby_point_id='23109'),lambda d:d.update(shifts=d['shifts']*2,count=2)])
def test_invalid_snapshots_rejected(mutation):
    d=sample();mutation(d)
    with pytest.raises(ValidationError):module.Snapshot.model_validate(d)


class Connection:
    def __init__(self):
        self.db=sqlite3.connect(':memory:')
        self.db.executescript("""CREATE TABLE core_schedule_assignments(source_url TEXT,core_assignment_id TEXT,core_employee_id TEXT,employee_name TEXT,core_store_id TEXT,store_name TEXT,saby_point_id INTEGER,position TEXT,work_date TEXT,shift_type TEXT,slot INTEGER,cancelled BOOLEAN,snapshot_at TEXT,synced_at TEXT,PRIMARY KEY(source_url,core_assignment_id));
          CREATE TABLE app_meta(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT);""")
    def sql(self,s):return s.replace('::jsonb','').replace('NOW()','CURRENT_TIMESTAMP')
    def args(self,args):return {str(i+1):str(v) if hasattr(v,'isoformat') else v for i,v in enumerate(args)}
    async def execute(self,s,*args):
        if 'pg_advisory' in s:return
        self.db.execute(self.sql(s),self.args(args))
    async def executemany(self,s,rows):
        self.db.executemany(self.sql(s),[self.args(r) for r in rows])
    @asynccontextmanager
    async def transaction(self):
        try:yield
        except BaseException:self.db.rollback();raise
        else:self.db.commit()


def test_repeat_substitution_cancellation_and_range_preservation():
    c=Connection();d=sample()
    async def apply(payload):
        async with c.transaction():return await module.apply_snapshot(c,'https://core.example',module.Snapshot.model_validate(payload))
    asyncio.run(apply(d));asyncio.run(apply(d))
    assert c.db.execute('SELECT COUNT(*) FROM core_schedule_assignments').fetchone()[0]==1
    old=copy.deepcopy(d);old.update(date_from='2026-09-01',date_to='2026-09-30');old['shifts'][0].update(core_assignment_id='9',work_date='2026-09-08')
    asyncio.run(apply(old))
    d['shifts'][0].update(core_assignment_id='2',core_employee_id='11',saby_point_id=None)
    result=asyncio.run(apply(d));assert result['unmapped_core_store_ids']==['2']
    assert c.db.execute("SELECT core_assignment_id,cancelled FROM core_schedule_assignments ORDER BY core_assignment_id").fetchall()==[('1',1),('2',0),('9',0)]
    d.update(count=0,shifts=[]);asyncio.run(apply(d))
    assert c.db.execute("SELECT cancelled FROM core_schedule_assignments WHERE core_assignment_id='2'").fetchone()==(1,)
    c.db.close()


def test_failed_write_rolls_back_cancellations(monkeypatch):
    c=Connection()
    async def run():
        async with c.transaction():await module.apply_snapshot(c,'https://core.example',module.Snapshot.model_validate(sample()))
        monkeypatch.setattr(c,'executemany',AsyncMock(side_effect=RuntimeError('write failed')))
        with pytest.raises(RuntimeError):
            async with c.transaction():await module.apply_snapshot(c,'https://core.example',module.Snapshot.model_validate(sample()))
    asyncio.run(run())
    assert c.db.execute('SELECT cancelled FROM core_schedule_assignments').fetchone()==(0,)


def test_failed_fetch_never_applies(monkeypatch):
    c=Connection()
    class Pool:
        @asynccontextmanager
        async def acquire(self):yield c
    monkeypatch.setattr(module,'pool',lambda:Pool())
    monkeypatch.setattr(module,'configured_source',lambda:('https://core.example','x'*40))
    monkeypatch.setattr(module,'fetch_snapshot',AsyncMock(side_effect=ValueError('Core HTTP 502')))
    apply=AsyncMock();monkeypatch.setattr(module,'apply_snapshot',apply)
    with pytest.raises(ValueError):asyncio.run(module.sync_schedule(date(2026,10,1),date(2026,10,31)))
    apply.assert_not_awaited()


def test_fetch_contract_and_no_redirect(monkeypatch):
    original=httpx.AsyncClient
    def handler(request):
        assert request.url.path=='/api/v1/ai/schedule'
        assert request.headers['Authorization']=='Bearer '+('x'*40)
        return httpx.Response(200,json=sample())
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    result=asyncio.run(module.fetch_snapshot('https://core.example','x'*40,date(2026,10,1),date(2026,10,31)))
    assert result.count==1
    with pytest.raises(ValueError,match='другой период'):
        asyncio.run(module.fetch_snapshot('https://core.example','x'*40,date(2026,9,1),date(2026,9,30)))
