import asyncio
from decimal import Decimal as D
import pytest
from app.services import inventory_sources as src, product_costs as costs, stock_history as stock


def sale(lines,returned=False,point=5635):
    return {'point_id':point,'sale_id':1,'is_return':returned,'raw_json':{'Return':returned,'SaleNomenclatures':lines}}


def line(**kw):
    return {'SaleNomenclature':1,'Nomenclature':10,'Name':'Товар','UnitName':'шт','Quantity':2,
        'TotalPrice':120,'TotalCost':80,**kw}


def test_actual_cost_is_total_not_unit_and_return_separate():
    c=costs.Costs('same_as_total_price');c.add(sale([line()]));c.add(sale([line(TotalPrice=-60,TotalCost=-40)],True))
    r,m=c.result();r=r[0]
    assert r['known_cost']==80 and r['gross_margin']==40
    assert r['known_return_cost']==40 and r['known_return_revenue']==60
    assert m['net_profit_calculated'] is False


def test_missing_cost_never_replaced_by_planned_or_zero():
    c=costs.Costs('same_as_total_price');c.add(sale([line(TotalCost=None,PlannedCost=1)]))
    r,_=c.result();assert r[0]['cost_missing_lines']==1 and r[0]['gross_margin'] is None


def test_vat_basis_unknown_blocks_margin_and_explicit_tax_subtraction():
    c=costs.Costs();c.add(sale([line(TotalVAT=20)]));assert c.result()[0][0]['gross_margin'] is None
    c=costs.Costs('revenue_excludes_vat');c.add(sale([line(TotalVAT=20)]))
    assert c.result()[0][0]['gross_margin']==20
    c=costs.Costs('revenue_excludes_vat');c.add(sale([line()]))
    assert c.result()[0][0]['gross_margin'] is None


def test_deduplicated_lines_units_and_partial_coverage():
    c=costs.Costs('same_as_total_price')
    c.add(sale([line(),line(),line(SaleNomenclature=2,TotalCost=None),line(SaleNomenclature=3,UnitName='кг')]))
    rows,_=c.result();assert len(rows)==2
    r=next(r for r in rows if r['unit']=='шт')
    assert r['sales_lines']==2 and r['known_revenue']==240 and r['cost_coverage_percent']==50
    assert r['margin_covered_revenue']==120 and r['gross_margin']==40


def test_nested_and_modifiers_never_double_counted():
    c=costs.Costs('same_as_total_price')
    child=line(SaleNomenclature=2,IsModifier=True)
    c.add(sale([line(Positions=[child]),child,line(SaleNomenclature=3,Refused=True)]))
    rows,m=c.result();assert rows==[] and m['exclusions']['excluded_complex_lines']==2


@pytest.mark.parametrize('value',[True,'NaN','Infinity',-4])
def test_invalid_cost_unknown(value):
    c=costs.Costs('same_as_total_price');c.add(sale([line(TotalCost=value)]))
    assert c.result()[0][0]['cost_missing_lines']==1


def test_zero_is_explicit_and_flagged():
    c=costs.Costs('same_as_total_price');c.add(sale([line(TotalCost=0)]))
    assert c.result()[0][0]['zero_cost_lines']==1
    assert c.result()[0][0]['gross_margin'] is None


def test_balances_full_scope_and_negative_anomaly():
    assert src.balances([{'nomenclature':1,'balance':0},{'nomenclature':2,'balance':-1}],['1','2'])=={'1':'0','2':'-1'}


@pytest.mark.parametrize('data',[
    [],{}, {'balances':[],'outcome':{'hasMore':True}},
    [{'nomenclature':1,'balance':None}], [{'nomenclature':1,'balance':True}],
    [{'nomenclature':2,'balance':0}],
    [{'nomenclature':1,'balance':0},{'nomenclature':1,'balance':0}],
    {'balances':[{'nomenclature':1,'balance':0}],'error':{'code':1}},
])
def test_missing_duplicate_partial_error_balances_fail(data):
    with pytest.raises(ValueError): src.balances(data,['1'])


def test_stock_observation_does_not_invent_continuous_outage():
    r={'point_id':5635,'company_id':'3','warehouse_id':'4','observed_at':'2026-11-14T00:00:00Z',
        'payload':{'complete_requested_scope':True,'balances':{'1':'0','2':'-2','3':'5'},'units':{'1':'шт','2':'л','3':'кг'}}}
    rows=stock.observations([r],True)
    assert len(rows)==1 and rows[0]['continuous_outage_confirmed'] is False
    assert stock.observations([r])[1]['availability']=='NEGATIVE_ANOMALY'


def test_receipt_pagination_rejects_partial_and_missing_markers():
    assert src.document_page({'Документ':[],'Навигация':{'ЕстьЕще':'Нет'}})==([],False)
    for data in ({'Документ':[]},{'Документ':[],'Навигация':{'ЕстьЕще':'Да'}},
                 {'Документ':[],'Навигация':{'ЕстьЕще':'Нет'},'ЧастичныеДанные':'Да'}):
        with pytest.raises(ValueError):src.document_page(data)


def test_receipt_number_exact_and_pagination_does_not_assert_posting(monkeypatch):
    calls=[]
    async def fake(path,params,method):
        calls.append((params,method))
        if method=='СБИС.ПрочитатьДокумент':
            return {'Идентификатор':'abc','Вложение':[{'Файл':{'Ссылка':'https://secret'}}]}
        return {'Документ':[{'Идентификатор':'abc','Номер':'УТ-121230','Дата':'13.11.2026','Тип':'ДокОтгрВх'}],
            'Навигация':{'ЕстьЕще':'Нет'}}
    monkeypatch.setattr(src,'request',fake)
    result=asyncio.run(src.find_receipt('2026-11-13','УТ-121230'))
    assert len(calls)==2 and result['posting_confirmed'] is False
    assert 'https://secret' not in str(result)


def test_mutating_rpc_blocked_before_auth():
    with pytest.raises(ValueError,match='READ_ONLY_METHOD'):
        asyncio.run(src.request(None,{},'СБИС.ЗаписатьДокумент'))


def test_inventory_menu_and_csv_registered():
    from app.bot import menu,menu_reports
    from app.agent import executive
    assert {'product_costs','stock','stockouts'}<=set(menu.ITEMS['products'])
    assert {'product_costs','stock','stockouts'}<=menu_reports.CSV_KINDS
    assert hasattr(executive,'get_inventory_report')
    args=menu_reports.memory_report({'report':'stockouts','start':'2026-11-13','end':'2026-11-13','point':'5635'})
    assert args['kind']=='inventory' and args['arguments']['point_id']=='5635'
