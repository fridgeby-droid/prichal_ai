import asyncio
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.bot import menu as ui,menu_reports as r


def message():
    return SimpleNamespace(chat=SimpleNamespace(id=1,type='private'),from_user=SimpleNamespace(id=999),
        message_thread_id=None,message_id=40,answer=AsyncMock(),answer_document=AsyncMock())


def setup(monkeypatch):
    monkeypatch.setattr(ui,'save',AsyncMock())
    from app.agent import conversations
    monkeypatch.setattr(conversations,'load',AsyncMock(return_value={'messages':[],'scope':{}}))
    monkeypatch.setattr(conversations,'save',AsyncMock())
    return message(),SimpleNamespace(id=1)


def test_menu_complete_and_callback_payload_small():
    s={'nonce':'1234567890'}
    buttons=[(v,'section',k) for k,v in ui.SECTIONS.items()]
    keyboard=ui.keyboard(s,buttons)
    assert len(ui.SECTIONS)==9
    assert all(len(b.callback_data.encode())<=64 for row in keyboard.inline_keyboard for b in row)


def test_callback_bound_to_nonce_and_visible_actions():
    s={'nonce':'fresh','actions':[['pref','enabled,off']]}
    assert ui.valid_callback(s,'ui:fresh:pref:enabled,off')
    assert not ui.valid_callback(s,'ui:old:pref:enabled,off')
    assert not ui.valid_callback(s,'ui:fresh:pref:enabled,on')
    assert not ui.valid_callback(s,'ui:fresh:taskact:cancel')


def test_session_isolated_by_user_and_topic():
    m=message()
    assert ui.session(SimpleNamespace(id=1),m)!=ui.session(SimpleNamespace(id=2),m)
    a=ui.session(SimpleNamespace(id=1),m);m.message_thread_id=99
    assert a!=ui.session(SimpleNamespace(id=1),m)


def test_periods_and_month_boundary():
    stamp=datetime(2026,11,14,5,tzinfo=timezone.utc)
    assert r.period('day',stamp)==('2026-11-13','2026-11-13')
    assert r.period('week',stamp)==('2026-11-07','2026-11-13')
    assert r.period('month',stamp)==('2026-10-01','2026-10-31')
    assert r.period('month',datetime(2026,11,1,5,tzinfo=timezone.utc))==('2026-10-01','2026-10-31')
    assert r.previous('2026-10-01','2026-10-31')==('2026-09-01','2026-09-30')
    assert r.previous('2026-10-02','2026-10-08')==('2026-09-25','2026-10-01')


def test_report_scope_and_filters_reach_calculation(monkeypatch):
    mock=AsyncMock(return_value={'kind':'quantities','rows':[]})
    monkeypatch.setattr(r.d,'report',mock)
    s=dict(report='quantities',point='5635',shift='DAY',query='тара 1',start='2026-10-01',end='2026-10-31',offset=50)
    asyncio.run(r.calculate(s))
    assert mock.await_args.kwargs['point_id']=='5635' and mock.await_args.kwargs['shift']=='DAY'
    assert mock.await_args.kwargs['query']=='тара 1' and mock.await_args.kwargs['offset']==50
    asyncio.run(r.calculate(s,export=True))
    assert mock.await_args.kwargs['offset']==0 and mock.await_args.kwargs['export_all']


def test_comparison_uses_previous_calendar_month(monkeypatch):
    mock=AsyncMock(return_value={});monkeypatch.setattr(r.m,'report',mock)
    asyncio.run(r.calculate(dict(report='products',start='2026-10-01',end='2026-10-31',point='5635',compare=True)))
    assert mock.await_args.args==('2026-10-01','2026-10-31','2026-09-01','2026-09-30')
    assert mock.await_args.kwargs['point_id']=='5635'


def test_csv_is_full_filtered_selection(monkeypatch):
    mock=AsyncMock(return_value={'kind':'clients','rows':[{'customer_id':str(i)} for i in range(80)]})
    monkeypatch.setattr(r.d,'report',mock)
    content,name,count=asyncio.run(r.export(dict(report='clients',start='2026-10-01',end='2026-10-31',point='',min_days=30,max_days=59,offset=50)))
    assert name=='prichal-clients.csv' and count==80 and b'79' in content
    assert mock.await_args.kwargs['min_days']==30 and mock.await_args.kwargs['max_days']==59


def test_quality_never_silently_checks_only_part_of_month():
    with pytest.raises(ValueError,match='7 дней'):
        asyncio.run(r.calculate(dict(report='quality',start='2026-10-01',end='2026-10-31',point='5635')))


def test_task_creation_requires_confirmation_and_real_callback_owner(monkeypatch):
    m,u=setup(monkeypatch)
    s={'section':'tasks','task_draft':True,'point':'5635','store_name':'Батумская','shift':'DAY','metric':'revenue_per_day'}
    create=AsyncMock(return_value={'id':'abc','delivery_enabled':True})
    monkeypatch.setattr(ui.followups,'create_task',create)
    monkeypatch.setattr(ui,'task_card',AsyncMock())
    asyncio.run(ui.action(m,'key',s,u,'days','7'))
    create.assert_not_awaited()
    assert ['createtask',''] in s['actions']
    asyncio.run(ui.action(m,'key',s,u,'createtask',''))
    assert create.await_args.args[0]['user_id']==1
    assert create.await_args.args[1:]==('5635','DAY','revenue_per_day',7)


def test_cancel_is_separate_confirmation(monkeypatch):
    m,u=setup(monkeypatch);s={'task_id':'abc'}
    change=AsyncMock();monkeypatch.setattr(ui.followups,'tasks',change)
    asyncio.run(ui.action(m,'key',s,u,'cancelask',''))
    change.assert_not_awaited()
    assert ['taskact','cancel'] in s['actions']


def test_private_preferences_not_called_with_bot_identity(monkeypatch):
    m,u=setup(monkeypatch)
    prefs=AsyncMock(return_value={'enabled':True,'morning':True,'tasks_enabled':True,'time':'10:00'})
    monkeypatch.setattr(ui.followups,'configure',prefs)
    asyncio.run(ui.action(m,'key',{},u,'pref','morning,off'))
    assert all(c.args[0]['user_id']==1 for c in prefs.await_args_list)
    assert prefs.await_args_list[0].args[1:]==('morning','off')


def test_old_keyboard_rejected_without_operation(monkeypatch):
    m,u=setup(monkeypatch)
    monkeypatch.setattr(ui,'load',AsyncMock(return_value={'nonce':'new','actions':[['taskact','cancel']]}))
    operation=AsyncMock();monkeypatch.setattr(ui,'action',operation)
    cb=SimpleNamespace(from_user=u,message=m,data='ui:old:taskact:cancel',answer=AsyncMock())
    asyncio.run(ui.click(cb));operation.assert_not_awaited()
    assert cb.answer.await_args.kwargs['show_alert']


def test_report_selection_clears_client_filter_before_other_report(monkeypatch):
    m,u=setup(monkeypatch);s={'section':'network','min_days':90,'max_days':999}
    monkeypatch.setattr(ui,'after_scope',AsyncMock())
    asyncio.run(ui.action(m,'key',s,u,'report','sales'))
    assert s['point']=='' and s['min_days']==0 and s['max_days'] is None


def test_task_and_card_picker_never_offers_network(monkeypatch):
    m,_=setup(monkeypatch)
    monkeypatch.setattr(ui.period_reports,'stores',AsyncMock(return_value={'stores':[{'point_id':'5635','name':'Батумская'}]}))
    for s in ({'task_draft':True},{'report':'card'}):
        asyncio.run(ui.scopes(m,'key',s))
        assert ['point','network'] not in s['actions'] and ['point','5635'] in s['actions']


def test_button_result_updates_text_followup_memory(monkeypatch):
    m,_=setup(monkeypatch)
    from app.agent import conversations
    monkeypatch.setattr(r,'calculate',AsyncMock(return_value={'kind':'clients','rows':[],'selected_clients':0}))
    s=dict(report='clients',point='5635',start='2026-10-01',end='2026-10-31',min_days=30,max_days=59)
    asyncio.run(ui.result(m,'assistant:menu:v1:abc',s))
    assert conversations.save.await_args.args[0]=='assistant:conversation:v1:abc'
    scope=conversations.save.await_args.args[2]
    assert scope['point_id']=='5635' and scope['last_report']['arguments']['min_days']==30
