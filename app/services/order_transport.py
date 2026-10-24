"""Bounded, serialized retail/order/list reads; errors never become empty sales."""
import asyncio
import logging
import math
import re
from time import monotonic
import httpx

logger=logging.getLogger(__name__)


def cooldown(response):
    limited=response.status_code==429
    seconds=120.0 if limited else 0.0
    try:
        body=response.json()
    except ValueError:
        body=None
    error=body.get('error') if isinstance(body,dict) else None
    detail=error.get('details') if isinstance(error,dict) else None
    if response.status_code==500 and isinstance(detail,str) and 'метод вызывается слишком часто' in detail.lower():
        limited=True
        match=re.search(r'(?:на\s+|Время блокировки:\s*)(\d+)(?:\s+секунд|\b)',detail,re.I)
        seconds=max(120.0,float(match.group(1))) if match else 120.0
    header=response.headers.get('Retry-After')
    if header:
        try:
            requested=float(header)
            if not math.isfinite(requested) or requested<0: raise ValueError()
            seconds=max(seconds,requested)
        except ValueError:
            # An unparsed header must never cause an early retry.
            return limited,None
    return limited,seconds


class OrderTransport:
    def __init__(self):
        self.gate=asyncio.Lock()
        self.next_request=0.0

    async def request(self,send,params):
        context=f"point_id={params.get('pointId')}; from={params.get('fromDateTime')}; to={params.get('toDateTime')}"
        async with self.gate:
            for attempt in range(4):
                remaining=self.next_request-monotonic()
                if remaining>0: await asyncio.sleep(remaining)
                self.next_request=monotonic()+3.0
                try:
                    response=await send()
                except (httpx.ConnectError,httpx.TimeoutException,httpx.ReadError,httpx.RemoteProtocolError) as exc:
                    if attempt==3:
                        raise RuntimeError(f'SABY_ORDERS_UNAVAILABLE: {type(exc).__name__}; 4 attempts; {context}') from None
                    delay=2**(attempt+1)
                    reason=type(exc).__name__
                else:
                    if response.status_code not in {429,500,502,503,504}:
                        return response
                    limited,requested=cooldown(response)
                    reason='RATE_LIMIT' if limited else 'UPSTREAM_ERROR'
                    delay=max(2**(attempt+1),requested+1 if requested else 0)
                    self.next_request=max(self.next_request,monotonic()+delay)
                    logger.warning('Saby order read: %s HTTP=%s attempt=%s/4; %s',reason,response.status_code,attempt+1,context)
                    if attempt==3 or requested is None or delay>600:
                        raise RuntimeError(f'SABY_ORDERS_{reason}: HTTP {response.status_code}; attempts={attempt+1}; {context}; source data not confirmed') from None
                self.next_request=max(self.next_request,monotonic()+delay)
                logger.warning('Saby order read retry in at least %.0fs: %s',delay,reason)
        raise RuntimeError('SABY_ORDERS_RETRY_EXHAUSTED')
