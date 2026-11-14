from datetime import date
from decimal import Decimal as D
from app.services.detail_reports import weeks,recency
from app.services.management_reports import dimension,payment_groups
from app.services.report_helpers import export_rows,csv_bytes
from app.services.load_status import compact,render


def test_partial_weeks_both_edges_and_unknown_empty_week():
    result=weeks([],date(2026,10,1),date(2026,10,31))
    assert [r['calendar_days'] for r in result]==[4,7,7,7,6]
    assert [r['partial_week'] for r in result]==[True,False,False,False,True]
    assert all(r['revenue_per_calendar_day'] is None for r in result)


def test_week_denominator_and_daily_peak_evidence():
    rows=[dict(business_date=date(2026,10,2),revenue=D(400),checks=8,returns=0,fallback=0)]
    result=weeks(rows,date(2026,10,1),date(2026,10,4))[0]
    assert result['revenue_per_calendar_day']==100
    assert result['checks_per_calendar_day']==2
    assert result['observed_days']==1


def test_missing_quantity_is_not_negative_previous():
    row=dimension({}, {'a':dict(name='ИСЕТСКОЕ',revenue=D(100),quantity=D('113.518'))},31,30,True)['rows'][0]
    assert row['quantity_current'] is None
    assert row['quantity_delta'] is None


def test_shift_payment_count_and_average():
    rows=[dict(shift_type='DAY',revenue=D(100),checks=4,returns=0,return_checks=0,records=4,fallback=0)]
    groups=payment_groups(rows,'shift_type',{})
    row=dimension(groups,groups,2,4,True)['rows'][0]
    assert row['checks_current']==4
    assert row['checks_per_day_current']==2
    assert row['checks_per_day_previous']==1
    assert row['average_current']==25


def test_csv_group_basis_filters_and_unknown():
    data={'kind':'averages','date_from':'2026-10-01','date_to':'2026-10-31',
          'groups':{'sellers':[{'id':'1','revenue':D(100),'checks':4,'average':D(25)}]},
          'sale_averages':{'sellers':[{'id':'1','average':None}]}}
    rows=export_rows(data,{'point_id':'5635'})
    assert len(rows)==2 and rows[0]['basis']=='payments' and rows[1]['basis']=='sales'
    assert rows[0]['filters']=={'point_id':'5635'}
    assert rows[1]['average'] is None
    assert b'5635' in csv_bytes(rows)


def test_client_order_by_age_then_numeric_id():
    rows=[dict(customer_id=i,first_purchase=date(2026,9,2),last_purchase=date(2026,9,2),visit_days=1,purchases=1) for i in (20,3)]
    result=recency(rows,date(2026,10,31))
    assert [r['customer_id'] for r in result]==['3','20']
    assert result[0]['days_since_purchase']==59


def test_short_status_retains_errors_and_due_time_without_price_payloads():
    result=compact({'summary':{'closed_day_reconcile':'REVIEW'},'prices':[{'large':'payload'}],
                    'automatic_updates':{'maintenance:saby:scheduler:v1':{'sales':{'status':'ERROR','next_run_at':'tomorrow','result':{'huge':True}}}}})
    assert 'prices' not in result and 'result' not in result['automatic_jobs']['sales']
    text=render(result)
    assert 'нужна проверка' in text and 'ошибка' in text and 'tomorrow' in text
