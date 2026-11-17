import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from app.services import purchase_loyalty as m


def row(**changes):
    return dict(dict(point_id=1,sale_id=1,sale_number='20',business_date='2026-10-01',
        customer_id=10,status='OK',fresh=True,response={'cardId':'123','bonusInc':'10'},
        total_price=Decimal(600),seller_id=9,seller_name='Seller'),**changes)


@pytest.mark.parametrize('changes,expected',[
    ({},'with_card'),({'customer_id':None},'without_card'),({'status':'ERROR'},'unknown'),
    ({'status':'UNSUPPORTED'},'unknown'),({'fresh':False},'unknown'),
    ({'response':{}},'unknown'),({'response':'broken'},'unknown'),
    ({'response':{'personalCardId':'5','bonusDec':'36'}},'with_card')])
def test_classification(changes,expected):
    assert m.classify(row(**changes))[0]==expected


def test_streamed_summary_and_private_fields(monkeypatch):
    rows=[row(),row(sale_id=2,customer_id=None),row(sale_id=3,status='ERROR'),
          row(sale_id=4,response={'cardId':'777','bonusDec':'36','availableBonusInc':'100'})]
    async def cursor(sql,*args,**kwargs):
        assert 'NOT s.is_return' in sql and 's.point_id<>23109' in sql
        assert 'md5(s.raw_json::text)' in sql and kwargs['prefetch']==200
        assert args[4]==Decimal(500) and args[3] is False
        for r in rows:yield r
    @asynccontextmanager
    async def transaction(**kwargs):yield
    @asynccontextmanager
    async def acquire():yield NS(cursor=cursor,transaction=transaction)
    monkeypatch.setattr(m,'pool',lambda:NS(acquire=acquire))
    monkeypatch.setattr(m,'point_filter',AsyncMock(return_value=None))
    result=asyncio.run(m.report('2026-10-01','2026-10-31',amount='500',loyalty='with_card'))
    assert result['selected']['purchases']==2
    assert result['selected']['purchase_share_percent']==50
    assert result['selected']['average']==600
    assert result['groups']['unknown']['purchases']==1
    assert result['groups']['without_card']['bonus_spent'] is None
    assert result['selected']['bonus_to_accrue']==10
    assert 'cardId' not in str(result) and '777' not in str(result)
    result=asyncio.run(m.report('2026-10-01','2026-10-31',amount='500',operation='debit'))
    assert result['selected']['purchases']==1 and result['selected']['bonus_spent']==36
    assert result['selected']['bonus_to_accrue'] is None


def test_unknown_amount_not_zero_average():
    g=m.empty();m.add(g,row(total_price=None),None,None)
    result=m.finish(g,1,Decimal(0))
    assert result['average'] is None and result['unknown_amount_count']==1
    assert result['bonus_spent'] is None
