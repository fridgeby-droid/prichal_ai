import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from app.services import price_lists as s


def error(details='Метод вызывается слишком часто и будет заблокирован на 120 секунд [-1]', status=500):
    response=httpx.Response(status,request=httpx.Request('GET','https://api.sbis.ru/retail/v2/nomenclature/list'),
                            json={'error':{'code':-32000,'details':details}})
    return httpx.HTTPStatusError('failed',request=response.request,response=response)


@pytest.fixture
def clock(monkeypatch):
    state={'now':0.0,'sleeps':[]}
    async def sleep(delay):
        state['sleeps'].append(delay)
        state['now']+=delay
    monkeypatch.setattr(s,'monotonic',lambda:state['now'])
    monkeypatch.setattr(s.asyncio,'sleep',sleep)
    monkeypatch.setattr(s,'_catalog_gate',asyncio.Lock())
    monkeypatch.setattr(s,'_catalog_next_request',0.0)
    monkeypatch.setattr(s,'get_settings',lambda:SimpleNamespace(catalog_min_request_interval_seconds=3))
    return state


def test_observed_500_waits_full_cooldown_and_same_page(monkeypatch,clock):
    calls=[]
    async def get(path,params):
        calls.append((clock['now'],path,dict(params)))
        if len(calls)==1:
            raise error()
        return {'ok':True}
    monkeypatch.setattr(s.saby_client,'_get',get)
    assert asyncio.run(s.price_get('/retail/v2/nomenclature/list',{'position':123}))=={'ok':True}
    assert calls[1][0]-calls[0][0]>=120
    assert calls[0][1:]==calls[1][1:]
    assert clock['sleeps']==[121.0]


def test_normal_pages_paced_under_observed_limit(monkeypatch,clock):
    calls=[]
    async def get(*args):
        calls.append(clock['now'])
        return {}
    monkeypatch.setattr(s.saby_client,'_get',get)
    async def run():
        for page in range(60):
            await s.price_get('/retail/v2/nomenclature/list',{'position':page})
    asyncio.run(run())
    assert all(b-a>=3 for a,b in zip(calls,calls[1:]))
    assert max(sum(start<=t<start+60 for t in calls) for start in calls)<=20


def test_persistent_block_bounded_and_gate_retained(monkeypatch,clock):
    get=AsyncMock(side_effect=error())
    monkeypatch.setattr(s.saby_client,'_get',get)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(s.price_get('/retail/v2/nomenclature/list',{}))
    assert get.await_count==3
    assert clock['sleeps']==[121.0,121.0]
    assert s._catalog_next_request>=clock['now']+120


def test_longer_block_is_not_shortened():
    assert s.rate_limit_delay(error('Метод вызывается слишком часто. Время блокировки: 300.').response)==300
    assert s.rate_limit_delay(error('ordinary server failure').response) is None


def test_429_retry_after():
    response=error(status=429).response
    response.headers['Retry-After']='240'
    assert s.rate_limit_delay(response)==240
