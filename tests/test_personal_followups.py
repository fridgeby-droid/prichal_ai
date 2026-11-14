import asyncio
from datetime import datetime,timedelta,timezone,date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from decimal import Decimal
import pytest
from app.services import personal_followups as p

STAMP=datetime(2026,11,14,5,0,tzinfo=timezone.utc) # 10:00 Екатеринбург


def job(status='PENDING'):
    return dict(id='task1',store='Батумская',point_id='5635',shift='DAY',metric='revenue_per_day',
                baseline={'revenue_per_day':'100'},threshold_percent=0,
                baseline_from='2026-11-01',baseline_to='2026-11-07',date_from='2026-11-08',date_to='2026-11-14',
                status=status,next_at=STAMP.isoformat(),due_at=STAMP.isoformat(),claimed_at=STAMP.isoformat(),token='token')


def state():
    result=p.defaults();result.update(enabled=True,morning=False);result['tasks']={'task1':job()}
    return result


def test_actor_rejects_group_other_id_and_model_cannot_choose_recipient():
    assert p.require_actor({'user_id':1,'private':True})==1
    for actor in (None,{'user_id':1,'private':False},{'user_id':2,'private':True}):
        with pytest.raises(ValueError): p.require_actor(actor)


def test_defaults_and_independent_toggles():
    s=p.defaults()
    assert not s['enabled'] and s['time']=='10:00'
    p.preferences(s,'enabled','on');p.preferences(s,'morning','off')
    assert s['enabled'] and not s['morning'] and s['tasks_enabled']
    with pytest.raises(ValueError): p.preferences(s,'time','24:00')
    p.preferences(s,'time','11:30');assert s['time']=='11:30'


def test_morning_due_once_after_local_time():
    s=p.defaults();s['enabled']=True
    assert p.claim(s,STAMP-timedelta(seconds=1)) is None
    j=p.claim(s,STAMP)
    assert j['id']=='morning:2026-11-13'
    assert p.claim(s,STAMP) is None
    assert p.update_job(s,j,'SENDING')
    assert p.update_job(s,j,'DONE')
    assert p.claim(s,STAMP+timedelta(hours=1)) is None
    assert p.claim(s,STAMP+timedelta(days=1))['day']=='2026-11-14'


def test_disabled_delivery_stops_claim_and_prepare():
    s=state();s['enabled']=False
    assert p.claim(s,STAMP) is None
    s['enabled']=True;j=p.claim(s,STAMP)
    s['tasks_enabled']=False
    assert not p.update_job(s,j,'SENDING')
    assert s['tasks']['task1']['status']=='WAITING'


def test_pause_cancel_cannot_be_overwritten_by_running_worker():
    s=state();j=p.claim(s,STAMP)
    p.task_action(s,'pause','task1')
    assert not p.update_job(s,j,'SENDING')
    assert s['tasks']['task1']['status']=='PAUSED'
    p.task_action(s,'resume','task1')
    p.task_action(s,'cancel','task1')
    assert p.claim(s,STAMP+timedelta(days=10)) is None


def test_restart_unknown_send_never_retried_but_processing_recoverable():
    s=state();s['tasks']['task1']=job('SENDING')
    assert p.claim(s,STAMP+timedelta(minutes=11)) is None
    assert s['tasks']['task1']['status']=='DELIVERY_UNKNOWN'
    s['tasks']['task1']=job('PROCESSING')
    assert p.claim(s,STAMP+timedelta(minutes=11))['status']=='PROCESSING'


def test_ready_respects_missing_review_pending():
    info={'has_local_sales':True}
    assert p.ready(info,date(2026,11,1),date(2026,11,7))
    for key in ('missing_load','review','pending_reconcile'):
        assert not p.ready({**info,key:[{'start':'2026-11-07','end':'2026-11-08'}]},date(2026,11,1),date(2026,11,7))


def test_shift_filter_and_missing_days_not_zero():
    rows=[dict(business_date=date(2026,11,1),shift_type='DAY',checks=2,revenue=100),
          dict(business_date=date(2026,11,1),shift_type='NIGHT',checks=10,revenue=900)]
    result=p.aggregate(rows,date(2026,11,1),date(2026,11,2),'DAY')
    assert result['checks']==2 and result['revenue_per_day']=='50'
    assert not result['all_days_observed']
    assert p.aggregate([],date(2026,11,1),date(2026,11,1),'DAY')['average_check'] is None


def test_outcome_threshold_zero_base_and_change():
    j=job();j['threshold_percent']=10
    assert p.outcome(j,{'revenue_per_day':'105'})['conclusion']=='Рост есть, заданный порог не достигнут'
    assert p.outcome(j,{'revenue_per_day':'110'})['conclusion']=='Улучшение достигнуто'
    assert p.outcome(j,{'revenue_per_day':'90'})['conclusion']=='Показатель снизился'
    j['baseline']['revenue_per_day']='0'
    assert p.outcome(j,{'revenue_per_day':'100'})['percent'] is None


def install_state(monkeypatch,s):
    async def edit(uid,fn):
        assert uid==1
        return fn(s)
    monkeypatch.setattr(p,'edit',edit)
    monkeypatch.setattr(p,'now',lambda:STAMP)


def test_creation_freezes_baseline_periods_and_is_idempotent(monkeypatch):
    s=p.defaults();install_state(monkeypatch,s)
    monkeypatch.setattr(p.periods,'point_filter',AsyncMock(return_value=5635))
    monkeypatch.setattr(p.periods,'stores',AsyncMock(return_value={'stores':[{'point_id':'5635','name':'Батумская'}]}))
    monkeypatch.setattr(p,'coverage',AsyncMock(return_value={'has_local_sales':True}))
    monkeypatch.setattr(p,'metrics',AsyncMock(return_value={'all_days_observed':True,'revenue_per_day':'100'}))
    actor={'user_id':1,'private':True,'message_id':5}
    first=asyncio.run(p.create_task(actor,'5635','DAY'))
    second=asyncio.run(p.create_task(actor,'5635','DAY'))
    assert first['id']==second['id'] and len(s['tasks'])==1
    assert first['baseline_from']=='2026-11-07' and first['baseline_to']=='2026-11-13'
    assert first['date_from']=='2026-11-14' and first['date_to']=='2026-11-20'
    assert not first['delivery_enabled']


def test_process_delivers_once_and_persists_result(monkeypatch):
    s=state();install_state(monkeypatch,s)
    j=p.claim(s,STAMP)
    monkeypatch.setattr(p,'evaluate',AsyncMock(return_value=('result',{'conclusion':'ok'})))
    bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=55)))
    asyncio.run(p.process(bot,1,j))
    assert s['tasks']['task1']['status']=='DONE'
    assert s['tasks']['task1']['telegram_message_id']==55
    assert bot.send_message.await_args.args==(1,'result')
    asyncio.run(p.process(bot,1,j))
    assert bot.send_message.await_count==1


def test_send_timeout_does_not_cause_automatic_duplicate(monkeypatch):
    s=state();install_state(monkeypatch,s);j=p.claim(s,STAMP)
    monkeypatch.setattr(p,'evaluate',AsyncMock(return_value=('result',{'conclusion':'ok'})))
    bot=SimpleNamespace(send_message=AsyncMock(side_effect=TimeoutError))
    asyncio.run(p.process(bot,1,j))
    assert s['tasks']['task1']['status']=='DELIVERY_UNKNOWN'
    assert p.claim(s,STAMP+timedelta(days=1)) is None


def test_not_ready_waits_without_false_improvement(monkeypatch):
    s=state();install_state(monkeypatch,s);j=p.claim(s,STAMP)
    monkeypatch.setattr(p,'evaluate',AsyncMock(return_value=None))
    bot=SimpleNamespace(send_message=AsyncMock())
    asyncio.run(p.process(bot,1,j))
    assert s['tasks']['task1']['status']=='WAITING'
    bot.send_message.assert_not_awaited()


def test_three_day_missing_data_notifies_once_as_unavailable(monkeypatch):
    s=state();s['tasks']['task1']['due_at']=(STAMP-timedelta(days=4)).isoformat()
    install_state(monkeypatch,s);j=p.claim(s,STAMP)
    monkeypatch.setattr(p,'evaluate',AsyncMock(return_value=None))
    bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=8)))
    asyncio.run(p.process(bot,1,j))
    assert s['tasks']['task1']['result']['status']=='INSUFFICIENT_DATA'
    assert 'Итог не рассчитан' in bot.send_message.await_args.args[1]


def test_business_day_boundary_and_changed_morning_time_no_duplicate():
    assert p.closed(datetime(2026,11,14,2,59,tzinfo=timezone.utc))==date(2026,11,12)
    assert p.closed(datetime(2026,11,14,3,0,tzinfo=timezone.utc))==date(2026,11,13)
    s=p.defaults();s['enabled']=True
    j=p.claim(s,STAMP);p.update_job(s,j,'DONE')
    p.preferences(s,'time','11:00')
    assert p.claim(s,STAMP+timedelta(hours=1)) is None


def test_other_owners_task_id_cannot_be_managed():
    with pytest.raises(ValueError,match='вашем списке'):
        p.task_action(p.defaults(),'cancel','another-user-task')


def test_personal_tools_registered():
    from app.agent.executive import executive_agent
    assert {'personal_notifications','create_followup_task','manage_followup_tasks'} <= {t.name for t in executive_agent.tools}


def test_morning_empty_current_records_waits(monkeypatch):
    from app.services import management_reports
    monkeypatch.setattr(p,'coverage',AsyncMock(return_value={'has_local_sales':True}))
    monkeypatch.setattr(management_reports,'report',AsyncMock(return_value={'has_current_records':False}))
    assert asyncio.run(p.evaluate({'id':'morning:2026-11-13','day':'2026-11-13'})) is None
