import asyncio
import httpx
import pytest
from app.services import order_transport as o


def response(code,body=None,headers=None):
    return httpx.Response(code,json=body or {},headers=headers or {})


def execute(monkeypatch,events):
    now=[1000.0]
    calls=[]
    monkeypatch.setattr(o,'monotonic',lambda:now[0])
    async def sleep(delay): now[0]+=delay
    monkeypatch.setattr(o.asyncio,'sleep',sleep)
    async def send():
        calls.append(now[0])
        item=events.pop(0)
        if isinstance(item,Exception): raise item
        return item
    return send,calls


def test_http500_retries_then_returns_real_response(monkeypatch):
    send,calls=execute(monkeypatch,[response(500),response(200,{'orders':[{'Sale':1}]})])
    result=asyncio.run(o.OrderTransport().request(send,{'pointId':5598}))
    assert result.json()['orders']==[{'Sale':1}]
    assert len(calls)==2 and calls[1]-calls[0]>=3


def test_antibot_http500_honors_120_seconds(monkeypatch):
    send,calls=execute(monkeypatch,[response(500,{'error':{'details':'Метод вызывается слишком часто и будет заблокирован на 120 секунд'}}),response(200)])
    asyncio.run(o.OrderTransport().request(send,{}))
    assert calls[1]-calls[0]>=121


def test_permanent_failure_bounded_and_no_body_leak(monkeypatch):
    send,calls=execute(monkeypatch,[response(500,{'error':{'details':'SECRET'}}) for _ in range(4)])
    with pytest.raises(RuntimeError,match='UPSTREAM_ERROR') as error:
        asyncio.run(o.OrderTransport().request(send,{'pointId':5598}))
    assert len(calls)==4 and 'SECRET' not in str(error.value)


def test_retry_after_and_network_failure(monkeypatch):
    send,calls=execute(monkeypatch,[httpx.ReadError('secret'),response(429,headers={'Retry-After':'180'}),response(200)])
    asyncio.run(o.OrderTransport().request(send,{}))
    assert calls[2]-calls[1]>=181


def test_nonretryable_and_cancellation(monkeypatch):
    send,calls=execute(monkeypatch,[response(403)])
    assert asyncio.run(o.OrderTransport().request(send,{})).status_code==403
    async def cancel(): raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): asyncio.run(o.OrderTransport().request(cancel,{}))


def test_shared_reader_spacing(monkeypatch):
    send,calls=execute(monkeypatch,[response(200),response(200)])
    async def run():
        transport=o.OrderTransport()
        return await asyncio.gather(transport.request(send,{}),transport.request(send,{}))
    asyncio.run(run())
    assert calls[1]-calls[0]>=3


def test_unparsed_retry_after_does_not_retry_early(monkeypatch):
    send,calls=execute(monkeypatch,[response(503,headers={'Retry-After':'invalid'})])
    with pytest.raises(RuntimeError): asyncio.run(o.OrderTransport().request(send,{}))
    assert len(calls)==1
