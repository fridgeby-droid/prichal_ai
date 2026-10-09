import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock

import pytest
from app.services import daily_report as module


def test_query_excludes_rc_deleted_sales_and_separates_returns():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE sales(point_id INTEGER, sale_id INTEGER, deleted BOOLEAN);
    CREATE TABLE sale_payments(point_id INTEGER, sale_id INTEGER, business_date TEXT, business_shift_type TEXT,
      is_return BOOLEAN, amount NUMERIC, source TEXT);
    CREATE VIEW retail_sales AS SELECT * FROM sales WHERE point_id<>23109;
    CREATE VIEW retail_sale_payments AS SELECT * FROM sale_payments WHERE point_id<>23109;
    INSERT INTO sales VALUES (1,1,0),(1,2,0),(2,1,0),(1,3,1),(23109,1,0);
    INSERT INTO sale_payments VALUES
      (1,1,'2026-10-08','DAY',0,100,'saby_payment'),
      (1,1,'2026-10-08','DAY',0,50,'saby_payment'),
      (1,2,'2026-10-08','NIGHT',1,-30,'saby_payment'),
      (2,1,'2026-10-08','NIGHT',0,70,'sale_total_fallback'),
      (1,3,'2026-10-08','DAY',0,1000,'saby_payment'),
      (23109,1,'2026-10-08','DAY',0,1596,'saby_payment'),
      (1,1,'2026-10-07','DAY',0,80,'saby_payment'),
      (1,1,'2026-10-01','DAY',0,60,'saby_payment'),
      (1,1,'2026-09-01','DAY',0,9999,'saby_payment');
    """)
    rows = [dict(r) for r in conn.execute(module.DAILY_SQL, {'1':'2026-10-08','2':'2026-10-07','3':'2026-10-01'})]
    today = module.totals([r for r in rows if r['business_date']=='2026-10-08'])
    assert today['revenue'] == 220
    assert today['returns'] == 30
    assert today['checks'] == 3  # Two payments on same sale retained, as in reconcile.
    assert today['return_checks'] == 1
    assert today['fallback'] == 1
    assert len(rows) == 5
    assert {r['point_id'] for r in rows} == {1, 2}
    conn.close()


def test_comparison_never_invents_percent_from_empty_or_zero_base():
    blank = module.totals([])
    positive = dict(blank, records=1, revenue=Decimal(150))
    zero = dict(blank, records=1)
    previous = dict(blank, records=1, revenue=Decimal(100))
    assert module.change(positive, previous) == '+50.0%'
    assert 'нет данных' in module.change(blank, previous)
    assert 'база сравнения 0' in module.change(positive, zero)
    assert 'день не завершён' in module.change(positive, previous, False)


@pytest.mark.parametrize('hour,expected', [(7, date(2026,10,7)),(8, date(2026,10,8))])
def test_default_last_closed_business_day(monkeypatch,hour,expected):
    class Conn:
        @asynccontextmanager
        async def transaction(self, **kwargs):
            assert kwargs == dict(isolation='repeatable_read', readonly=True)
            yield
        async def fetch(self, sql, *args):
            if args:
                assert args == (expected, expected-module.timedelta(days=1), expected-module.timedelta(days=7))
            return []
        async def fetchrow(self, *args):
            return None
    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Conn()
    monkeypatch.setattr(module, 'pool', lambda: Pool())
    data=asyncio.run(module.daily_report(now=datetime(2026,10,9,hour,0,tzinfo=ZoneInfo('Asia/Yekaterinburg'))))
    assert data['day']==expected and data['complete']


def test_future_date_rejected_before_db():
    with pytest.raises(ValueError, match='будущем'):
        asyncio.run(module.daily_report('2026-10-10',now=datetime(2026,10,9,12,tzinfo=ZoneInfo('Asia/Yekaterinburg'))))


def test_output_handles_missing_stores_and_keeps_cards_together():
    day=date(2026,10,8); dates=(day,date(2026,10,7),date(2026,10,1))
    data=dict(day=day, dates=dates,complete=False,stores=[dict(point_id=i,name=f'Магазин {i}') for i in range(100)],periods={d:[] for d in dates},latest={'status':'ERROR'},success=None)
    chunks=module.format_report(data)
    text='\n'.join(chunks)
    assert all(len(c)<=3800 for c in chunks)
    assert 'предварительные' in text and 'не подтверждает отсутствие продаж' in text
    assert 'Последний запуск загрузки: ERROR' in text
    assert 'Магазин 99 — нет записей' in text
    assert 'РЦ исключён' in text


def test_daily_handler_requires_access_and_is_before_ai():
    from app.bot import router
    from pathlib import Path
    message=type('Message',(),{'from_user':None,'answer':AsyncMock()})()
    asyncio.run(router.daily_summary(message))
    assert 'Доступ' in message.answer.call_args.args[0]
    source=Path(router.__file__).read_text(encoding='utf-8')
    assert source.index('Command("daily")') < source.index('@router.message(F.text)')
