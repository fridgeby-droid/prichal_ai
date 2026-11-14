import asyncio
from datetime import datetime,timezone,timedelta,date
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.agent import conversations as c,exports
from app.agent.report_scope import request_scope,resolve_point
from app.services import assistant_briefs as b


def test_memory_identity_and_expiration_and_bounds():
    assert len({c.key(1,2),c.key(1,3),c.key(2,2),c.key(1,2,10)})==4
    state={'updated_at':datetime.now(timezone.utc).isoformat(),
           'messages':[{'role':'user','content':'x'*7000} for _ in range(30)],
           'scope':{'point_id':'398','attachments':['secret'],'last_report':{}}}
    value=c.bounded(state)
    assert len(value['messages'])==12 and len(value['messages'][0]['content'])==6000
    assert 'attachments' not in value['scope']
    state['updated_at']=(datetime.now(timezone.utc)-timedelta(days=8)).isoformat()
    assert c.bounded(state)=={'messages':[],'scope':{}}


def test_conversation_serializes_only_same_session():
    async def run():
        active=0
        async def task():
            nonlocal active
            async with c.serial('test'):
                active+=1
                assert active==1
                await asyncio.sleep(0)
                active-=1
        await asyncio.gather(task(),task())
        assert 'test' not in c._locks
    asyncio.run(run())


def test_network_switch_persists_and_prevents_old_store_export():
    token=request_scope.set({'point_id':'398','last_report':{'kind':'detail','arguments':{'point_id':'398'}}})
    try:
        assert resolve_point('network')=='' and resolve_point('')==''
        with pytest.raises(ValueError,match='Область'): asyncio.run(exports.create())
    finally: request_scope.reset(token)


def test_real_csv_keeps_group_and_full_rows(monkeypatch):
    from app.services import detail_reports
    report=AsyncMock(return_value={'date_from':'2026-07-01','date_to':'2026-10-31',
                                  'rows':[{'customer_id':str(i)} for i in range(80)]})
    monkeypatch.setattr(detail_reports,'report',report)
    state={'point_id':'network'}
    token=request_scope.set(state)
    try:
        exports.remember('detail',dict(kind='clients',start='2026-07-01',end='2026-10-31',
                                      point_id='',min_days=30,max_days=59,offset=50))
        result=asyncio.run(exports.create())
        assert result['rows']==80 and result['status']=='FILE_READY'
        assert report.await_args.kwargs['export_all'] is True
        assert report.await_args.kwargs['min_days']==30 and 'offset' not in report.await_args.kwargs
        assert state['attachments'][0]['content'].startswith(b'\xef\xbb\xbf')
        assert b'79' in state['attachments'][0]['content']
    finally: request_scope.reset(token)


def test_failed_export_never_queues_file(monkeypatch):
    from app.services import detail_reports
    monkeypatch.setattr(detail_reports,'report',AsyncMock(side_effect=TimeoutError))
    state={}
    token=request_scope.set(state)
    try:
        exports.remember('detail',{'kind':'clients'})
        with pytest.raises(TimeoutError): asyncio.run(exports.create())
        assert not state.get('attachments')
    finally: request_scope.reset(token)


def test_morning_closed_day_and_same_weekday(monkeypatch):
    monkeypatch.setattr(b,'closed_day',lambda:date(2026,11,13))
    report=AsyncMock(return_value={'point_id':None,'report_text':'large','metrics':{}})
    monkeypatch.setattr(b.management,'report',report)
    result=asyncio.run(b.build('morning'))
    assert result['date_from']=='2026-11-13' and result['previous_start']=='2026-11-06'
    assert report.await_args.args[:4]==('2026-11-13','2026-11-13','2026-11-06','2026-11-06')
    assert 'report_text' not in result['sections']['drivers']


def test_investigation_partial_failure_preserves_evidence(monkeypatch):
    monkeypatch.setattr(b.management,'report',AsyncMock(return_value={'point_id':'398','metrics':{'amount':10}}))
    monkeypatch.setattr(b.detail,'report',AsyncMock(side_effect=TimeoutError('secret')))
    result=asyncio.run(b.build('investigation','2026-10-01','2026-10-31','398'))
    assert not result['complete'] and result['sections']['drivers']['metrics']['amount']==10
    assert result['previous_start']=='2026-08-31' and result['previous_end']=='2026-09-30'
    assert result['errors']==[{'section':'trend','error_type':'TimeoutError'}]
    assert 'secret' not in str(result)


def test_store_card_uses_same_scope_all_sections(monkeypatch):
    mock=AsyncMock(return_value={'point_id':'398'})
    monkeypatch.setattr(b.management,'report',mock)
    detail=AsyncMock(return_value={'point_id':'398'})
    advanced=AsyncMock(return_value={'point_id':'398'})
    monkeypatch.setattr(b.detail,'report',detail)
    monkeypatch.setattr(b.advanced,'report',advanced)
    result=asyncio.run(b.build('store_card','2026-10-01','2026-10-31','398'))
    assert set(result['sections'])=={'drivers','trend','clients','loyalty'}
    assert all(call.kwargs['point_id']=='398' for call in detail.await_args_list+advanced.await_args_list)
    with pytest.raises(ValueError): asyncio.run(b.build('store_card','2026-10-01','2026-10-31'))


def test_scope_mismatch_is_not_presented(monkeypatch):
    monkeypatch.setattr(b.management,'report',AsyncMock(return_value={'point_id':None}))
    monkeypatch.setattr(b.detail,'report',AsyncMock(return_value={'point_id':'398'}))
    result=asyncio.run(b.build('investigation','2026-10-01','2026-10-31','398'))
    assert 'drivers' not in result['sections'] and not result['complete']


def test_agent_followup_loads_history_and_returns_files_without_persisting_bytes(monkeypatch):
    from app.agent import executive as e
    load=AsyncMock(return_value={'messages':[{'role':'user','content':'Батумская октябрь'}],
                                'scope':{'point_id':'5635'}})
    save=AsyncMock()
    monkeypatch.setattr(c,'load',load)
    monkeypatch.setattr(c,'save',save)
    async def run(agent,messages,**kwargs):
        assert messages[0]['content']=='Батумская октябрь' and messages[-1]['content']=='А ночью?'
        assert resolve_point('')=='5635'
        request_scope.get()['attachments'].append({'filename':'test.csv','content':b'csv'})
        return SimpleNamespace(final_output='Ночная смена')
    monkeypatch.setattr(e.Runner,'run',run)
    result=asyncio.run(e.ask_with_files('А ночью?','test'))
    assert result['attachments'][0]['content']==b'csv'
    assert save.await_args.args[1][-1]['role']=='assistant'
    assert request_scope.get() is None


def test_seller_small_sample_not_ranking():
    from app.services.business_insights import calculate
    from test_advanced_reports import sale,A,Z
    rows,meta=calculate('seller_comparison',[sale(1)],[],A,Z,config={})
    assert rows[0]['sample_status']=='NO_PEERS'
    assert meta['sample_rule']['minimum_own_purchases']==30


def test_payment_effects_sum_to_exact_changes():
    from decimal import Decimal as D
    report={'has_current_records':True,'has_previous_records':True,
            'metrics':{'average':{'previous':D(100),'current':D(120)},
                       'checks':{'previous':D(20),'current':D(10),'previous_per_day':D(2),'current_per_day':D(1)}}}
    result=b.payment_effects(report)
    assert sum(result['total'].values())==D(-800)
    assert sum(result['per_day'].values())==D(-80)
    report['has_previous_records']=False
    assert b.payment_effects(report)=={'status':'NOT_COMPARABLE'}


def test_all_new_tools_registered_with_sdk():
    from app.agent.executive import executive_agent
    assert {'get_management_brief','export_last_report','get_short_load_status'} <= {t.name for t in executive_agent.tools}


def test_unrelated_management_report_cannot_export_old_clients():
    from app.agent.tools import _dump
    state={'last_report':{'kind':'detail','arguments':{'kind':'clients'}}}
    token=request_scope.set(state)
    try:
        _dump({'kind':'products','point_id':'398'})
        with pytest.raises(ValueError,match='выгрузки'): asyncio.run(exports.create())
    finally: request_scope.reset(token)


def test_telegram_sends_actual_file(monkeypatch):
    import importlib
    router=importlib.import_module('app.bot.router')
    monkeypatch.setattr(router,'_allowed',lambda m:True)
    monkeypatch.setattr(router,'ask_with_files',AsyncMock(return_value={'text':'Готово','attachments':[{'filename':'clients.csv','content':b'a;b'}]}))
    status=SimpleNamespace(edit_text=AsyncMock())
    message=SimpleNamespace(text='Пришли файлом',message_id=123,chat=SimpleNamespace(id=1,type='private'),from_user=SimpleNamespace(id=2),
                            message_thread_id=None,answer=AsyncMock(return_value=status),answer_document=AsyncMock())
    asyncio.run(router.ai_text(message))
    doc=message.answer_document.await_args.args[0]
    assert doc.filename=='clients.csv' and doc.data==b'a;b'
    assert router.ask_with_files.await_args.args[1]==c.key(1,2)
