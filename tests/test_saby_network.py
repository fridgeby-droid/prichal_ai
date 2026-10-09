import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services import saby
from app.services.async_utils import gather_or_cancel


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(saby.httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))


def test_dns_failure_recovers_without_empty_result(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            raise httpx.ConnectError("No address associated with hostname", request=request)
        return httpx.Response(200, json={"orders": [{"Sale": 123}]})
    transport(monkeypatch, handler)
    sleep = AsyncMock()
    monkeypatch.setattr(saby.asyncio, "sleep", sleep)
    client = saby.SabyClient()
    monkeypatch.setattr(client, "_authenticate", AsyncMock(return_value="test"))
    data = asyncio.run(client._get("/retail/order/list", {}))
    assert data["orders"][0]["Sale"] == 123
    assert [c.args[0] for c in sleep.await_args_list] == [1, 2]


def test_permanent_dns_failure_is_bounded(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("dns", request=request)
    transport(monkeypatch, handler)
    monkeypatch.setattr(saby.asyncio, "sleep", AsyncMock())
    with pytest.raises(RuntimeError, match="4 попыток"):
        asyncio.run(saby.SabyClient()._request("GET", saby.API_BASE))
    assert len(calls) == 4


def test_rate_limit_respects_retry_after(monkeypatch):
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(429, headers={"Retry-After": "5"}) if count == 1 else httpx.Response(200)
    transport(monkeypatch, handler)
    sleep = AsyncMock()
    monkeypatch.setattr(saby.asyncio, "sleep", sleep)
    assert asyncio.run(saby.SabyClient()._request("GET", saby.API_BASE)).status_code == 200
    sleep.assert_awaited_once_with(5.0)


def test_permission_error_not_network_retried(monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(403))
    sleep = AsyncMock()
    monkeypatch.setattr(saby.asyncio, "sleep", sleep)
    assert asyncio.run(saby.SabyClient()._request("GET", saby.API_BASE)).status_code == 403
    sleep.assert_not_awaited()


def test_total_http_concurrency_is_bounded(monkeypatch):
    active = peak = 0
    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        return httpx.Response(200)
    transport(monkeypatch, handler)
    async def run():
        client = saby.SabyClient()
        await gather_or_cancel(*(client._request("GET", saby.API_BASE) for _ in range(30)))
    asyncio.run(run())
    assert peak <= 4


def test_failed_batch_cancels_and_drains_siblings():
    async def run():
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def sibling():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async def failure():
            await started.wait()
            raise RuntimeError("failed")
        with pytest.raises(RuntimeError, match="failed"):
            await gather_or_cancel(sibling(), failure())
        assert cancelled.is_set()
    asyncio.run(run())
