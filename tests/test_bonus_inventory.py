import asyncio
from unittest.mock import AsyncMock
from app.services import bonus_load as b
from test_detail_reports import Conn,Pool

KEY='11111111-1111-4111-8111-111111111111'


def test_inventory_counts_changes_without_scheduling_repeat():
    def row(status=None,fresh=None,key=KEY,customer=15):
        return dict(status=status,fresh=fresh,raw_json={'Key':key},customer_id=customer)
    result=b.inventory([row('OK',True),row('OK',False),row('UNSUPPORTED',False),
                        row(),row('ERROR'),row(key=None),row(customer=15),row(customer=None)])
    assert result['local_sales']==8
    assert result['saved_ok']==2 and result['changed_saved']==2
    assert result['pending_requests']==3 and result['pending_without_uuid']==1
    assert result['pending_with_customer']==3 and result['saby_requests_made']==0
    assert result['excluded_without_customer']==1


def test_plan_never_calls_saby(monkeypatch):
    class C(Conn):
        async def fetch(self,*args): return []
        async def fetchrow(self,sql,*args):
            assert 'raw_json' not in sql and 'md5' not in sql
            return {'pending_requests':0}
    monkeypatch.setattr(b,'pool',lambda:Pool(C()))
    get=AsyncMock()
    monkeypatch.setattr(b.saby_client,'_get',get)
    assert asyncio.run(b.plan())['pending_requests']==0
    get.assert_not_called()


def test_loader_does_not_automatically_schedule_changed_saved(monkeypatch):
    class C(Conn):
        async def fetchval(self,*args): return True
        async def execute(self,*args): pass
        async def fetch(self,sql,*args):
            assert 's.customer_id IS NOT NULL' in sql
            assert 'b.fingerprint<>' not in sql
            assert "b.status='ERROR'" in sql and 'b.point_id IS NULL' in sql
            assert args[3] is False
            return []
    monkeypatch.setattr(b,'pool',lambda:Pool(C()))
    get=AsyncMock()
    monkeypatch.setattr(b,'read_bonus',get)
    assert asyncio.run(b.load())['status']=='LOCAL_SALES_PROCESSED'
    get.assert_not_called()
