import asyncio
from datetime import date, timedelta
from app.services import store_calendar as c
from app.services import management_reports as m
from test_management_reports import Connection, Pool


def rows(point, start, end):
    return [dict(point_id=point,business_date=start+timedelta(days=i)) for i in range((end-start).days+1)]


def test_opening_boundary_and_expected_absence():
    q=c.opening_quality([], [{'point_id':33446}], date(2026,8,17),date(2026,8,19))
    assert q['expected_before_opening'][0]['dates']==['2026-08-17']
    assert q['store_dates_without_records'][0]['dates']==['2026-08-18','2026-08-19']
    assert not q['records_before_opening']


def test_before_opening_records_flagged_not_silently_removed():
    data=rows(37462,date(2026,9,23),date(2026,9,24))
    q=c.opening_quality(data,[{'point_id':37462}],date(2026,9,23),date(2026,9,24))
    assert q['records_before_opening'][0]['dates']==['2026-09-23']
    assert not q['store_dates_without_records'] and not q['expected_before_opening']


def test_cohort_excludes_openings_and_missing_days_without_inventing_dates():
    shops=[{'point_id':i,'name':str(i)} for i in [1,2,33446,37462]]
    a,b=date(2026,9,1),date(2026,9,30)
    x,y=date(2026,8,1),date(2026,8,31)
    current=sum([rows(i,a,b) for i in [1,2,33446,37462]],[])
    previous=sum([rows(i,x,y) for i in [1,33446,37462]],[])
    included,excluded=c.cohort(shops,current,previous,a,b,x,y)
    assert [s['point_id'] for s in included]==['1']
    assert included[0]['opening_date'] is None
    assert 'PREVIOUS_DATES_WITHOUT_RECORDS' in excluded[0]['reasons']
    assert all('OPENED_AFTER_PERIOD_START' in s['reasons'] for s in excluded[1:])


def test_opening_on_first_day_is_eligible():
    day=date(2026,9,24)
    included,excluded=c.cohort([{'point_id':37462,'name':'Shop'}],rows(37462,day,day),rows(37462,day,day),day,day,day,day)
    assert included and not excluded


def test_comparable_report_bridge_and_empty_selection(monkeypatch):
    conn=Connection()
    monkeypatch.setattr(m,'pool',lambda:Pool(conn))
    result=asyncio.run(m.report('2026-09-01','2026-09-01','2026-08-01','2026-08-01',kind='comparable'))
    block=result['comparable']
    assert len(block['included'])==1
    for side in ('current','previous'):
        assert block['metrics']['revenue'][side]+block['excluded_revenue'][side]==result['metrics']['revenue'][side]
        assert block['daynight']['totals'][side]==block['metrics']['revenue'][side]
    assert 'СОПОСТАВИМАЯ СЕТЬ' in result['report_text']
    empty=asyncio.run(m.report('2026-09-01','2026-09-30','2026-08-01','2026-08-31',kind='comparable'))
    assert empty['comparable']['metrics']['revenue']['change'] is None
    assert 'Нет магазинов для сопоставимого сравнения' in empty['report_text']


def test_running_does_not_claim_process_is_alive():
    text=m.format_quality({'period_closed':True,'reconciliation':'NOT_CHECKED','sync_in_progress':True})
    assert 'Работа процесса не подтверждена' in text
    assert 'Сейчас выполняется sync' not in text
