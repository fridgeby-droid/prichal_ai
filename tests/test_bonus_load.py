import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock
import httpx
import pytest
from app.services import bonus_load as b
from test_detail_reports import Conn,Pool


def test_allowed_fields_and_unknown_not_zero():
    result=b.normalize({'bonusAction':-1,'bonusDec':36,'cardId':12,'phone':'secret','availableBonusInc':89})
    assert result['cardId']=='12' and 'phone' not in result and 'bonusInc' not in result
    with pytest.raises(ValueError): b.normalize({'bonusAction':1,'bonusInc':-1})
    with pytest.raises(ValueError): b.normalize({'bonusAction':1,'cardId':True})


def test_report_no_payment_duplication_unknown_and_stale():
    def row(**kw):
        return dict(is_return=False,status='OK',fresh=True,revenue=Decimal('1750.15'),
                    fallback=0,response={'bonusAction':-1,'bonusDec':36,'cardId':'1'},**kw)
    first=row()
    stale={**first,'fresh':False,'revenue':Decimal('100')}
    returns={**first,'is_return':True}
    result=b.summarize([first,stale,returns])
    assert result['bonus_debit_known']=='36'
    assert result['bonus_to_accrue_known'] is None
    assert result['card_purchase_average']=='1750.15'
    assert result['payment_revenue']=='1850.15' and result['unconfirmed_purchases']==1
    assert b.summarize([])['bonus_debit_known'] is None


def test_retry_and_no_retry_for_404(monkeypatch):
    req=httpx.Request('GET','https://example.com')
    error=httpx.HTTPStatusError('hidden',request=req,response=httpx.Response(500,request=req))
    get=AsyncMock(side_effect=[error,{'bonusAction':1,'bonusInc':108}])
    monkeypatch.setattr(b.saby_client,'_get',get)
    monkeypatch.setattr(b.asyncio,'sleep',AsyncMock())
    assert asyncio.run(b.read_bonus('uuid'))['bonusInc']==108
    assert get.await_count==2
    get.side_effect=httpx.HTTPStatusError('hidden',request=req,response=httpx.Response(404,request=req))
    with pytest.raises(ValueError,match='HTTP_404'): asyncio.run(b.read_bonus('uuid'))


def test_loader_continues_after_error_and_unlocks(monkeypatch):
    class C(Conn):
        def __init__(self): self.calls=[]; self.n=0
        async def fetchval(self,*args): return True
        async def fetch(self,*args):
            self.n+=1
            assert 'ANY($6::text[])' in args[0]
            if self.n>=3: assert '398:2' in args[-1]
            if self.n==4: return []
            return [dict(point_id=398,sale_id=self.n,fingerprint='hash',raw_json={
                'Key':f'11111111-1111-4111-8111-{self.n:012d}'})]
        async def execute(self,*args): self.calls.append(args)
    conn=C()
    monkeypatch.setattr(b,'pool',lambda:Pool(conn))
    monkeypatch.setattr(b.asyncio,'sleep',AsyncMock())
    monkeypatch.setattr(b,'read_bonus',AsyncMock(side_effect=[{'bonusAction':1,'bonusInc':108},ValueError('HTTP_500'),{'bonusAction':-1,'bonusDec':36}]))
    result=asyncio.run(b.load(limit=0))
    assert result['saved']==2 and result['status']=='PARTIAL_ERRORS' and result['errors']==1
    writes=[c for c in conn.calls if 'INSERT INTO' in c[0]]
    assert [c[4] for c in writes]==['OK','ERROR','OK']
    assert 'pg_advisory_unlock' in conn.calls[-1][0]
    assert 'ELSE bonus_observations.response' in writes[-1][0]


@pytest.mark.parametrize('error,attempts',[('HTTP_500',5),('HTTP_401',1),('RETRY_LATER',1)])
def test_global_failure_stops_without_running_through_account(monkeypatch,error,attempts):
    class C(Conn):
        n=0
        async def fetchval(self,*args): return True
        async def execute(self,*args): pass
        async def fetch(self,*args):
            self.n+=1
            return [dict(point_id=398,sale_id=self.n,fingerprint='hash',raw_json={
                'Key':f'11111111-1111-4111-8111-{self.n:012d}'})]
    monkeypatch.setattr(b,'pool',lambda:Pool(C()))
    monkeypatch.setattr(b.asyncio,'sleep',AsyncMock())
    get=AsyncMock(side_effect=ValueError(error))
    monkeypatch.setattr(b,'read_bonus',get)
    result=asyncio.run(b.load(limit=0))
    assert result['status']=='STOPPED' and get.await_count==attempts


def test_competing_loader_does_not_request(monkeypatch):
    class C(Conn):
        async def fetchval(self,*args): return False
    monkeypatch.setattr(b,'pool',lambda:Pool(C()))
    get=AsyncMock()
    monkeypatch.setattr(b,'read_bonus',get)
    with pytest.raises(ValueError,match='ALREADY_RUNNING'): asyncio.run(b.load())
    get.assert_not_called()
