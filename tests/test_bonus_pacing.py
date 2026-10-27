import asyncio
from unittest.mock import AsyncMock
import httpx
import pytest
from app.services import bonus_pacing as p,bonus_load as b


def test_only_clean_streaks_accelerate_and_never_below_two():
    pace=p.BonusPacer()
    for _ in range(49): pace.success()
    assert pace.delay==3
    pace.success()
    assert pace.delay==2.75
    for _ in range(1000): pace.success()
    assert pace.delay==2
    pace.failure()
    assert pace.delay==3 and pace.streak==0
    pace.failure(limited=True)
    assert pace.delay>=6
    for _ in range(30): pace.failure()
    assert pace.delay==10


def test_speed_includes_elapsed_time_and_errors_remain_pending(monkeypatch):
    monkeypatch.setattr(p,'monotonic',lambda:0)
    pace=p.BonusPacer()
    monkeypatch.setattr(p,'monotonic',lambda:3600)
    data=pace.metrics(dict(saved=90,unsupported=10,errors=5),200)
    assert data['resolved_per_hour']==100
    assert data['estimated_pending']==100 and data['estimated_remaining_hours']==1
    assert pace.metrics(dict(saved=1,unsupported=0,errors=0),200)['estimated_remaining_hours'] is None
    assert pace.metrics(dict(saved=90,unsupported=10,errors=0),200,True)['estimated_remaining_hours'] is None


@pytest.mark.parametrize('limited',[False,True])
def test_http_500_is_not_automatically_rate_limit(limited,monkeypatch):
    response=httpx.Response(500,request=httpx.Request('GET','https://example.test'),
        json={'error':{'details':'Метод вызывается слишком часто. Время блокировки: 120' if limited else 'internal error'}})
    error=httpx.HTTPStatusError('error',request=response.request,response=response)
    monkeypatch.setattr(b.saby_client,'_get',AsyncMock(side_effect=[error,{'ok':True}]))
    monkeypatch.setattr(b,'normalize',lambda x:x)
    sleeper=AsyncMock();monkeypatch.setattr(b.asyncio,'sleep',sleeper)
    pace=p.BonusPacer()
    token=p.current_pacer.set(pace)
    try: assert asyncio.run(b.read_bonus('example'))=={'ok':True}
    finally: p.current_pacer.reset(token)
    assert pace.failures==1
    assert pace.delay==(6 if limited else 4)
    assert sleeper.call_args.args[0]>120 if limited else sleeper.call_args.args[0]==4


def test_long_cooldown_stops_instead_of_shortening(monkeypatch):
    response=httpx.Response(429,headers={'Retry-After':'900'},request=httpx.Request('GET','https://example.test'))
    error=httpx.HTTPStatusError('error',request=response.request,response=response)
    monkeypatch.setattr(b.saby_client,'_get',AsyncMock(side_effect=error))
    sleeper=AsyncMock();monkeypatch.setattr(b.asyncio,'sleep',sleeper)
    with pytest.raises(ValueError,match='RETRY_LATER'): asyncio.run(b.read_bonus('example'))
    sleeper.assert_not_called()
