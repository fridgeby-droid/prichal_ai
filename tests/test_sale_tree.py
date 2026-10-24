import copy
from decimal import Decimal
import pytest
from app.services.sale_tree import nodes,modifiers
from scripts.diagnose_sale import details


def example():
    child={'SaleNomenclature':2,'Nomenclature':99,'Name':'Тара 1л','UnitName':'шт','Quantity':1,'TotalPrice':None,'IsModifier':True}
    parent={'SaleNomenclature':1,'Nomenclature':288,'Name':'BLACK QUEEN','UnitName':'л','Quantity':2,'TotalPrice':500,'IsModifier':False,'Positions':[child]}
    return dict(point_id=398,sale_id=211800,seller_id=7,seller_name='S',raw_json={'SaleNomenclatures':[parent]})


def test_nested_modifier_count_not_multiplied_and_amount_unknown():
    sale=example()
    tree=nodes(sale['raw_json'])
    assert tree[1]['parent_key']=='id:1'
    rows=modifiers([sale])
    assert len(rows)==1 and rows[0]['quantity']==1
    assert rows[0]['revenue'] is None and rows[0]['unknown_amount_lines']==1
    assert rows[0]['purchases']==1


def test_duplicate_top_level_and_nested_count_once():
    sale=example()
    sale['raw_json']['SaleNomenclatures'].append(copy.deepcopy(sale['raw_json']['SaleNomenclatures'][0]['Positions'][0]))
    assert len(nodes(sale['raw_json']))==2
    assert modifiers([sale])[0]['quantity']==1


def test_conflict_not_silently_accepted():
    sale=example()
    duplicate=copy.deepcopy(sale['raw_json']['SaleNomenclatures'][0]['Positions'][0])
    duplicate['Quantity']=4
    sale['raw_json']['SaleNomenclatures'].append(duplicate)
    with pytest.raises(ValueError,match='CONFLICT'): modifiers([sale])


def test_inherited_return_and_refusal():
    sale=example()
    sale['raw_json']['Return']=True
    row=modifiers([sale])[0]
    assert row['quantity']==0 and row['return_quantity']==1
    sale['raw_json']['SaleNomenclatures'][0]['Refused']=True
    assert modifiers([sale])==[]


def test_missing_quantity_and_nonboolean_flag():
    sale=example()
    child=sale['raw_json']['SaleNomenclatures'][0]['Positions'][0]
    child['Quantity']=None
    with pytest.raises(ValueError,match='QUANTITY_UNKNOWN'): modifiers([sale])
    child['IsModifier']='true'
    assert modifiers([sale])==[]


def test_ids_scoped_to_sale_and_units_not_merged():
    a=example()
    b=copy.deepcopy(a)
    b['sale_id']=211801
    assert modifiers([a,b])[0]['quantity']==2
    b['raw_json']['SaleNomenclatures'][0]['Positions'][0]['UnitName']='упак'
    assert len(modifiers([a,b]))==2


def test_diagnostic_includes_nested_names_and_parent():
    sale=example()
    sale.update(store_name='Shop',sale_datetime='2026-10-23',business_date='2026-10-23')
    result=details(sale)
    assert result['products'][1]['name']=='Тара 1л'
    assert result['products'][1]['parent_key']=='id:1'
