import asyncio
from datetime import date
from unittest.mock import AsyncMock
import httpx
import pytest
from app.services import bonus_probe as b
from test_detail_reports import Conn,Pool


@pytest.mark.parametrize('action,key,value',[(1,'bonusInc',108),(-1,'bonusDec',36)])
def test_actual_fields_match_not_discount_or_available(action,key,value):
    result=b.describe({'bonusAction':action,key:value,'cardId':123456789},action,str(value))
    assert result['status']=='REFERENCE_VALUES_MATCH'
    assert not result['mapping_confirmed']
    assert '123456789' not in str(result)
    missing=b.describe({'bonusAction':action,'availableBonusInc':108,'userBonusDec':36},action,str(value))
    assert missing['status']=='REFERENCE_NOT_MATCHED'


@pytest.mark.parametrize('body',[{},[],{'bonusAction':True},{'bonusAction':0},{'error':'secret'},
    {'bonusAction':1,'bonusInc':'nan'},{'bonusAction':1,'bonusInc':-1},{'bonusBalanceError':'secret'}])
def test_invalid_is_not_zero_or_success(body):
    result=b.describe(body,1,'108')
    assert result['status'] not in ('REFERENCE_VALUES_MATCH',)
    assert 'secret' not in str(result)


def test_uuid_only_not_path_or_numeric_id():
    assert b.candidate_key({'Key':'../bonus-write'}) is None
    assert b.candidate_key({'Key':211869}) is None
    assert b.candidate_key({'Key':'00000000-0000-0000-0000-000000000000'}) is None


def test_two_known_gets_only_no_save(monkeypatch):
    class C(Conn):
        async def fetch(self,sql,*args):
            return [dict(point_id=398,sale_id=s,business_date=date(2026,10,23),raw_json={'Number':n,'Key':f'11111111-1111-4111-8111-{s:012d}'}) for s,n in [(211869,25529),(212324,25576)]]
    monkeypatch.setattr(b,'pool',lambda:Pool(C()))
    get=AsyncMock(side_effect=[{'bonusAction':1,'bonusInc':108},{'bonusAction':-1,'bonusDec':36}])
    monkeypatch.setattr(b.saby_client,'_get',get)
    monkeypatch.setattr(b.asyncio,'sleep',AsyncMock())
    result=asyncio.run(b.probe())
    assert get.await_count==2 and not result['facts_saved'] and not result['complete_history']
    assert all(call.args[0].endswith('/bonus-read') and call.args[1]=={} for call in get.await_args_list)
    assert all(row['status']=='REFERENCE_VALUES_MATCH' for row in result['results'])
