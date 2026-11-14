import asyncio
import ast
import json
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from app.agent.report_scope import request_scope,resolve_point,remember_store,validate_scope
from app.services.report_presentation import clean_message
from app.services.management_reports import product_groups,dimension


def test_empty_scope_never_silently_means_network():
    with pytest.raises(ValueError): resolve_point('')
    assert resolve_point('network')==''


def test_selected_store_survives_tool_task_and_isolated_between_requests():
    async def run():
        async def tool(): remember_store({'stores':[{'point_id':'5635'}]},'Батумская')
        token=request_scope.set({})
        try:
            await asyncio.create_task(tool())
            assert resolve_point('')=='5635'
            assert resolve_point('network')==''
        finally: request_scope.reset(token)
        with pytest.raises(ValueError): resolve_point('')
    asyncio.run(run())


def test_ambiguous_store_requires_selection():
    token=request_scope.set({})
    try:
        remember_store({'stores':[{'point_id':'1'},{'point_id':'2'}]},'магазин')
        with pytest.raises(ValueError): resolve_point('')
        assert resolve_point('2')=='2'
    finally: request_scope.reset(token)


def test_network_response_cannot_be_labelled_store():
    with pytest.raises(ValueError,match='SCOPE_MISMATCH'):
        validate_scope({'point_id':None},'5635')


def test_product_tool_passes_selected_store_to_service(monkeypatch):
    from app.agent import tools
    # Execute the real function body without the SDK transport decorator.
    tree=ast.parse(Path(tools.__file__).read_text(encoding='utf-8'))
    fn=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='get_product_sales')
    fn.decorator_list=[]
    namespace=dict(vars(tools))
    exec(compile(ast.Module(body=[fn],type_ignores=[]),tools.__file__,'exec'),namespace)
    report=AsyncMock(return_value={'point_id':'5635','dimensions':{}})
    monkeypatch.setattr(tools.management_reports,'report',report)
    token=request_scope.set({'point_id':'5635'})
    try:
        result=json.loads(asyncio.run(namespace['get_product_sales']('2026-10-01','2026-10-31')))
        assert report.await_args.args[4]=='5635'
        assert result['scope_contract']['kind']=='store'
        report.return_value={'point_id':None}
        with pytest.raises(ValueError,match='SCOPE_MISMATCH'):
            asyncio.run(namespace['get_product_sales']('2026-10-01','2026-10-31'))
    finally: request_scope.reset(token)


def test_shift_names_and_tables_clean_but_commands_unchanged():
    text='☀️ DAY — -7%\n🌙 NIGHT — -3%\n\n| A | B |\n\n| 1 | 2 |\n\n\n`shift=DAY`\n```\nshift=NIGHT\n```'
    result=clean_message(text)
    assert 'Дневная смена' in result and 'Ночная смена' in result
    assert '| A | B |\n| 1 | 2 |' in result
    assert '`shift=DAY`' in result and 'shift=NIGHT' in result


def test_product_dimension_keeps_purchases_and_returns():
    rows=[dict(product_key='id:1',name='Товар',unit='шт',revenue=100,quantity=10,
               purchases=8,returns=20,return_quantity=2)]
    row=dimension(product_groups(rows),{},31,31,False)['rows'][0]
    assert row['purchases_current']==8 and row['return_quantity_current']==2
    assert row['returns_current']==20
