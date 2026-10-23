import asyncio
import json
from datetime import date,datetime,timezone
from unittest.mock import AsyncMock

import pytest
from scripts import diagnose_sale as diag
from app.services import detail_reports as d
from test_detail_reports import Conn,Pool


def sale():
    return dict(point_id=398,sale_id=25522,store_name='Магазин',business_date=date(2026,10,23),
                sale_datetime=datetime(2026,10,23,11,22,tzinfo=timezone.utc),raw_json={
                    'Number':999,'Customer':123,'CustomerName':'PRIVATE NAME','Phone':'PRIVATE PHONE',
                    'Card':'PRIVATE CARD','DiscountInfo':[],
                    'SaleNomenclatures':[{'Name':'Тара 1л','Quantity':1,'IsModifier':True,'UnitName':'шт',
                                         'SaleNomenclature':11,'Nomenclature':22}]})


def test_safe_selection_details_and_internal_vs_display_number():
    output=diag.details(sale())
    assert output['internal_sale_id']=='25522' and output['display_number']=='999'
    assert output['products'][0]['is_modifier'] is True
    assert output['products'][0]['quantity']==1
    assert output['customer_present'] is True and output['discount_info_count']==0
    assert 'PRIVATE' not in json.dumps(output,default=str)


@pytest.mark.parametrize('flag',[None,'true',1,False])
def test_unconfirmed_modifier_values_are_not_true(flag):
    row=sale()
    row['raw_json']['SaleNomenclatures'][0]['IsModifier']=flag
    assert diag.details(row)['products'][0]['is_modifier'] is (False if flag is False else None)


def test_search_number_uses_date_and_does_not_confuse_sale_id(monkeypatch):
    class C(Conn):
        async def fetch(self,sql,*args):
            assert "s.raw_json->>'Number'=$3" in sql
            assert args[:4]==(None,None,'25522',date(2026,10,23))
            return [sale()]
    monkeypatch.setattr(diag,'pool',lambda:Pool(C()))
    monkeypatch.setattr(diag,'init_db',AsyncMock())
    monkeypatch.setattr(diag,'close_db',AsyncMock())
    result=asyncio.run(diag.run(number=25522,calendar_date='2026-10-23'))
    assert result['status']=='SELECTED_LOCAL_SALE'


def test_number_without_date_does_not_query_database(monkeypatch):
    init=AsyncMock()
    monkeypatch.setattr(diag,'init_db',init)
    with pytest.raises(ValueError): asyncio.run(diag.run(number=25522))
    init.assert_not_called()


def test_modifier_report_filters_json_boolean_and_preserves_quantity(monkeypatch):
    class C(Conn):
        async def fetch(self,sql,*args):
            if "i.raw_json->'IsModifier'='true'::jsonb" in sql:
                rows=await super().fetch(d.ITEMS,*args)
                return rows[:1]
            return await super().fetch(sql,*args)
    monkeypatch.setattr(d,'pool',lambda:Pool(C()))
    result=asyncio.run(d.report('modifiers','2026-09-01','2026-09-01',query='тара'))
    assert result['rows'][0]['quantity']==6
    assert result['rows'][0]['return_quantity']==1
    assert 'не умножается' in result['report_text']
