"""Per-loader conservative pacing; no parallel requests or persisted speed claims."""
from contextvars import ContextVar
from time import monotonic
import asyncio

current_pacer=ContextVar('bonus_pacer',default=None)
MIN_DELAY=1.5
INITIAL_DELAY=3.0


class BonusPacer:
    def __init__(self):
        self.delay=INITIAL_DELAY
        self.streak=0
        self.failures=0
        self.started=monotonic()
        self.next_start=self.started

    async def before_request(self):
        remaining=self.next_start-monotonic()
        if remaining>0:
            await asyncio.sleep(remaining)
        # No catch-up bursts: schedule from the actual start, not an old deadline.
        self.next_start=monotonic()+self.delay

    def success(self):
        self.streak+=1
        if self.streak>=50:
            self.delay=max(MIN_DELAY,self.delay-0.25)
            self.streak=0

    def failure(self,limited=False):
        self.failures+=1
        self.streak=0
        self.delay=min(10.0,max(6.0 if limited else 3.0,self.delay+1.0))
        # Error backoff is from the error, even when the failed call was slow.
        self.next_start=max(self.next_start,monotonic()+self.delay)

    def metrics(self,counts,initial_pending,refresh=False):
        elapsed=max(0.0,monotonic()-self.started)
        # Deferred errors are still pending; do not call attempts queue progress.
        resolved=counts['saved']+counts['unsupported']
        rate=resolved/elapsed*3600 if elapsed>0 and resolved>=25 else None
        remaining=max(0,initial_pending-resolved)
        return dict(pause_seconds=self.delay,request_start_interval_seconds=self.delay,
                    pacing_mode='START_TO_START',elapsed_seconds=round(elapsed,1),
                    resolved_per_hour=round(rate,1) if rate else None,
                    estimated_remaining_hours=round(remaining/rate,2) if rate and not refresh else None,
                    estimated_pending=remaining,
                    estimate_note='По скорости текущего запуска, включая ответы и повторы. Ошибки остаются в очереди; новые продажи после старта не учтены. Не гарантия срока.')
