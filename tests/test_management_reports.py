import asyncio
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal as D
from unittest.mock import AsyncMock

import pytest
from app.services import management_reports as m


def group(value):
    return {'name':'Товар', 'revenue':D(str(value))}


def test_unequal_days_and_zero_base():
    result=m.comparison(300,310,30,31)
    assert result['change']['absolute']==-10
    assert result['per_day_change']['absolute']==0
    assert m.comparison(5,0,1,1)['change']['percent'] is None


def test_contributions_other_and_missing_observations():
    result=m.dimension({'a':group(120),'b':group(5),'new':group(30)},
                       {'a':group(100),'b':group(20),'old':group(10)},30,31,True,2)
    assert sum(r['change']['absolute'] for r in result['rows'])+result['other']['change']['absolute']==25
    assert sum(r['current'] for r in result['rows'])+result['other']['current']==155
    new=next(r for r in result['rows'] if r['id']=='new')
    assert new['observation_warning'] and new['change_percent'] is None
    assert new['contribution_percent_of_total_change']==120


def test_opposing_changes_and_no_data():
    result=m.dimension({'a':group(150),'b':group(50)}, {'a':group(100),'b':group(100)},1,1,True)
    assert all(r['contribution_percent_of_total_change'] is None for r in result['rows'])
    missing=m.dimension({}, {'a':group(100)},1,1,True)
    assert missing['totals']['change'] is None
    assert missing['rows'][0]['observation_warning']


@pytest.mark.parametrize('active,changed,expected',[(False,False,'OK'),(False,True,'STALE'),(True,False,'STALE')])
def test_quality_signature_invalidates(active,changed,expected):
    day=date(2026,9,1)
    saved=m.signature({'id':1},False)
    mark=m.signature({'id':2 if changed else 1},active)
    cache={m.check_key('',day):{'status':'OK','checked_at':'2026-10-01','sync_signature':saved}}
    q=m.matching_quality({'completeness':'UNCONFIRMED'},day,day,cache,mark,'')
    assert q['reconciliation']==expected
    assert q['completeness']=='UNCONFIRMED'
    if expected=='OK': assert q['checked_dates'][0]['date']=='2026-09-01'


def test_scope_missing_dates_and_version_change():
    first,last=date(2026,9,1),date(2026,9,2)
    mark=m.signature(None,False)
    cache={m.check_key('',first):{'status':'REVIEW','checked_at':'now','sync_signature':mark}}
    q=m.matching_quality({},first,last,cache,mark,'')
    assert q['reconciliation']=='REVIEW' and q['unchecked_dates']==['2026-09-02']
    assert m.matching_quality({},first,last,cache,mark,'1')['reconciliation']=='NOT_CHECKED'
    changed={**mark,'report_version':'new'}
    assert m.matching_quality({},first,first,cache,changed,'')['reconciliation']=='STALE'


class Connection:
    def __init__(self):
        self.transactions=[]
        self.depth=0
        self.executed=[]
        self.ids=iter([1]*20)

    @asynccontextmanager
    async def transaction(self,**kwargs):
        self.transactions.append(kwargs)
        self.depth+=1
        yield
        self.depth-=1

    async def fetch(self,sql,*args):
        assert self.depth==1
        if 'FROM retail_stores' in sql: return [{'point_id':1,'name':'Магазин'}]
        if sql==m.periods.PAYMENTS_SQL:
            return [dict(point_id=1,business_date=args[0],shift_type='DAY',records=2,
                         checks=2,return_checks=0,revenue=D(300 if args[0].month==9 else 310),returns=0,fallback=0)]
        if sql==m.periods.PRODUCTS_SQL:
            return [dict(product_key='id:1',name='Товар',unit='шт',quantity=1,revenue=D('0.005'),returns=2),
                    dict(product_key='id:2',name='Товар 2',unit='шт',quantity=1,revenue=D('0.005'),returns=3)]
        return []

    async def fetchrow(self,sql,*args):
        return {'id':next(self.ids),'status':'OK'} if sql==m.LATEST_SQL else None

    async def fetchval(self,sql,*args): return False

    async def execute(self,*args): self.executed.append(args)


class Pool:
    def __init__(self,conn): self.conn=conn
    @asynccontextmanager
    async def acquire(self): yield self.conn


def test_report_consistent_snapshot_rounding_and_source_bases(monkeypatch):
    conn=Connection()
    monkeypatch.setattr(m,'pool',lambda:Pool(conn))
    monkeypatch.setattr(m.catalog_service,'read_catalog',AsyncMock(return_value=None))
    result=asyncio.run(m.report('2026-09-01','2026-09-30','2026-08-01','2026-08-31',kind='drivers'))
    assert conn.transactions==[{'isolation':'repeatable_read','readonly':True}]
    assert result['metrics']['revenue']['per_day_change']['absolute']==0
    assert result['dimensions']['stores']['totals']['current']==300
    assert result['dimensions']['categories']['totals']['current']==D('.01')
    assert result['items']['current_returns']==5
    assert 'Поправка округления (текущий): -0,01' in result['report_text']
    assert result['quality_current']['reconciliation']=='NOT_CHECKED'
    assert len(result['quality_current']['store_dates_without_records'][0]['dates'])==29
    assert 'НЕ ПОДТВЕРЖДЕНА' in result['report_text']


@pytest.mark.parametrize('changed',[False,True])
def test_save_quality_only_for_stable_sync(monkeypatch,changed):
    conn=Connection()
    conn.ids=iter([1,2 if changed else 1])
    monkeypatch.setattr(m,'pool',lambda:Pool(conn))
    monkeypatch.setattr(m.periods,'status',AsyncMock(return_value={'checks':[{'date':'2026-09-01','status':'OK'}]}))
    monkeypatch.setattr(m,'report',AsyncMock(return_value={'quality_current':{'period_closed':True,'reconciliation':'NOT_CHECKED'}}))
    result=asyncio.run(m.run_quality('2026-09-01','2026-09-01'))
    assert result['saved_check'] is (not changed)
    assert len(conn.executed)==(0 if changed else 1)


def test_quality_limit_and_telegram_chunks():
    with pytest.raises(ValueError,match='7 дней'):
        asyncio.run(m.run_quality('2026-09-01','2026-09-08'))
    text='я'*10000+'\n'+'строка\n'*2000
    parts=m.chunks(text)
    assert all(len(part)<=3800 for part in parts)
    assert ''.join(parts).replace('\n','')==text.replace('\n','')


def test_plain_commands_are_authorized_and_before_ai():
    from pathlib import Path
    source=Path('app/bot/router.py').read_text(encoding='utf-8')
    section=source[source.index('@router.message(Command("sales"'):source.index('@router.message(Command("daily"')]
    assert 'allowed(' in section and 'ask_executive_agent' not in section
    assert 'parse_mode=None' in section
    for name in ('compare','drivers','daynight','products','reportquality'):
        assert '"'+name+'"' in section
