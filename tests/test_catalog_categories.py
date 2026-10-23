import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from app.services import catalog_categories as c


UUID='12345678-1234-1234-1234-123456789abc'


def folder(n,parent=None,name='Same'):
    return {'hierarchicalId':n,'hierarchicalParent':parent,'isParent':True,'name':name,'id':None}


def product(n=3,parent=2,**extra):
    return {'hierarchicalId':n,'hierarchicalParent':parent,'isParent':None,'name':'Item',
            'id':10,'externalId':UUID,**extra}


def catalog():
    return c.build_catalog([product(),folder(2,1),folder(1)])


def row(key='id:10',unit='шт',revenue=100,quantity=1):
    return {'product_key':key,'unit':unit,'revenue':Decimal(revenue),'quantity':Decimal(str(quantity)),'returns':Decimal(5)}


def test_graph_not_order_dependent_and_uuid_alias():
    data=catalog()
    assert data['mapping']['id:10']==data['mapping']['uuid:'+UUID]
    assert [p['id'] for p in data['mapping']['id:10']['path']]==['1','2']
    assert data['folder_count']==2
    assert data['revision']==c.build_catalog([folder(1),folder(2,1),product()])['revision']


@pytest.mark.parametrize('rows',[
    [folder(1,2),folder(2,1)],
    [folder(1,99)],
    [folder(1),folder(1)],
    [product(parent=99)],
    [product(parent=2),product(2,parent=None,id=20,externalId=None)],
    [folder(1),folder(2),product(3,parent=1),product(4,parent=2)],
    [product(parent=None,externalId='not-uuid')],
    [product(parent=None,isParent='false')],
])
def test_invalid_catalog_rejected(rows):
    with pytest.raises(c.ExportError): c.build_catalog(rows)


def test_aggregate_uses_ids_keeps_unknown_and_units():
    data=catalog()
    rows=[row(unit='кг',quantity=0.5),row('uuid:'+UUID.upper()),row('id:999',revenue=30)]
    top=c.aggregate(rows,data,'top')
    assert top['1']['revenue']==200
    assert top['1']['quantity_by_unit']=={'кг':Decimal('0.5'),'шт':1}
    assert top['UNMAPPED']['revenue']==30
    assert sum(r['revenue'] for r in top.values())==230
    assert c.aggregate(rows,data,'leaf')['2']['revenue']==200
    assert c.aggregate(rows,None,'top')['UNMAPPED']['revenue']==230


def test_root_is_not_unmatched_and_same_names_do_not_merge():
    data=c.build_catalog([folder(1),folder(2),product(parent=1),product(4,parent=2,id=11,externalId=None),product(5,parent=None,id=12,externalId=None)])
    groups=c.aggregate([row('id:10'),row('id:11'),row('id:12')],data,'top')
    assert set(groups)=={'1','2','ROOT'}


def test_catalog_fetch_cursor_no_price_and_partial_failure(monkeypatch):
    fetch=AsyncMock(side_effect=[{'nomenclatures':[folder(1)],'outcome':{'hasMore':True}},
                               {'nomenclatures':[product(parent=1)],'outcome':{'hasMore':False}}])
    monkeypatch.setattr(c,'price_get',fetch)
    result=asyncio.run(c.fetch_catalog())
    assert result['complete_snapshot']
    assert 'priceListId' not in fetch.call_args.args[1]
    assert fetch.call_args.args[1]['position']==1
    monkeypatch.setattr(c,'fetch_catalog',AsyncMock(side_effect=c.ExportError('FAIL')))
    pool=AsyncMock()
    monkeypatch.setattr(c,'pool',pool)
    with pytest.raises(c.ExportError): asyncio.run(c.sync_catalog(True))
    pool.assert_not_called()


def test_atomic_apply_and_preview_no_writes(monkeypatch):
    data={**catalog(),'schema_version':1,'complete_snapshot':True,'started_at':'2026-10-23T01:00:00+00:00','fetched_at':'2026-10-23T01:01:00+00:00'}
    monkeypatch.setattr(c,'fetch_catalog',AsyncMock(return_value=data))
    execute=AsyncMock(return_value='INSERT 0 1')
    class Conn:
        async def execute(self,*args): return await execute(*args)
    class Pool:
        @asynccontextmanager
        async def acquire(self): yield Conn()
    monkeypatch.setattr(c,'pool',lambda:Pool())
    assert asyncio.run(c.sync_catalog())['mode']=='preview'
    execute.assert_not_called()
    assert asyncio.run(c.sync_catalog(True))['mode']=='applied'
    assert execute.await_count==1
    assert 'ON CONFLICT' in execute.call_args.args[0]
    execute.return_value='INSERT 0 0'
    assert asyncio.run(c.sync_catalog(True))['mode']=='superseded'


def test_report_consistent_snapshot_and_category_change(monkeypatch):
    data={**catalog(),'fetched_at':'2026-10-23T01:00:00Z'}
    monkeypatch.setattr(c,'read_catalog',AsyncMock(return_value=data))
    class Conn:
        @asynccontextmanager
        async def transaction(self,**kwargs):
            assert kwargs=={'isolation':'repeatable_read','readonly':True}
            yield
        async def fetch(self,sql,start,end,point):
            return [row(revenue=150 if start.day==8 else 100),row('id:999',revenue=20)]
    class Pool:
        @asynccontextmanager
        async def acquire(self): yield Conn()
    monkeypatch.setattr(c,'pool',lambda:Pool())
    result=asyncio.run(c.report('2026-07-08','2026-07-14',previous_start='2026-07-01',previous_end='2026-07-07'))
    assert result['categories'][0]['change']=={'absolute':50,'percent':50}
    assert result['current_totals']['revenue']==170
    assert result['current_totals']['unmapped_revenue']==20
    assert result['attribution']=='CURRENT_CATALOG_NOT_HISTORICAL'


def test_category_tool_registered():
    from app.agent.executive import executive_agent
    assert 'get_category_sales' in {t.name for t in executive_agent.tools}
