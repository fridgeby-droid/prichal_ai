from decimal import Decimal
import asyncio
from unittest.mock import AsyncMock
import pytest
from app.services.catalog_categories import format_current_report


def data(values,total,unmapped=0,truncated=False):
    return {'date_from':'2026-09-01','date_to':'2026-09-30',
            'categories':[{'name':'Category '+str(i),'current':{'revenue':v}} for i,v in enumerate(values)],
            'current_totals':{'revenue':total,'returns':5,'category_mapped_revenue':total-unmapped,
                              'unmapped_revenue':unmapped,'unmapped_product_groups':int(unmapped!=0)},
            'truncated':truncated,'catalog_status':'AVAILABLE','catalog_fetched_at':'2026-10-23',
            'quality':{'period_closed':True}}


def test_unmapped_does_not_equal_unreconciled_total():
    text=format_current_report(data([150,20],170,20))
    assert 'ИТОГО: 170,00' in text
    assert 'без найденной категории: 20,00' in text
    assert 'с найденной связью с каталогом: 150,00' in text
    assert 'Сверка этим отчётом не выполнялась' in text


def test_zero_unknown_is_explicit_and_truncated_other_is_computed():
    text=format_current_report(data([100],170,truncated=True))
    assert 'вне списка: 70,00' in text
    assert 'без найденной категории: 0,00' in text


def test_rounding_and_arithmetic_mismatch():
    text=format_current_report(data([Decimal('1.005'),Decimal('1.005')],Decimal('2.010')))
    assert 'Поправка округления: -0,01' in text
    assert 'ИТОГО: 2,01' in text
    with pytest.raises(ValueError,match='Итог не совпадает'):
        format_current_report(data([21802361.90],21799361.90))


def test_command_access_control_and_no_model():
    from app.bot import router
    from pathlib import Path
    message=type('Message',(),{'from_user':None,'answer':AsyncMock()})()
    asyncio.run(router.category_summary(message))
    assert 'Доступ' in message.answer.call_args.args[0]
    source=Path(router.__file__).read_text(encoding='utf-8')
    section=source[source.index('async def category_summary'):source.index('@router.message(Command("daily"))')]
    assert 'ask_executive_agent' not in section
    assert source.index('Command("categories")') < source.index('@router.message(F.text)')
