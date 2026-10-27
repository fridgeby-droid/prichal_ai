from datetime import date,datetime,timedelta,timezone
from decimal import Decimal as D
import pytest
from app.services.advanced_reports import calculate,render
from app.services.traffic_import import parse

A=date(2026,9,1)
Z=date(2026,9,30)

def sale(id=1,day=A,amount=100,customer=1,returned=False,point=1,discount=None):
    return dict(point_id=point,sale_id=id,business_date=day,total_price=D(amount),customer_id=customer,
                is_return=returned,seller_id=7,sale_datetime=datetime(2026,9,1,23,tzinfo=timezone.utc),
                explicit_discount=discount)

def item(id,product='id:1',quantity=1,unit='шт',returned=False):
    return dict(point_id=1,sale_id=id,product_key=product,name=product,quantity=D(quantity),
                total_price=D(10),unit_name=unit,is_return=returned)

def test_symmetric_decomposition_reconciles_and_missing_is_unknown():
    sales=[sale(1,date(2026,8,1),100),sale(2,A,150),sale(3,A,250),sale(4,A,80,returned=True),sale(5,A,50,point=2)]
    rows,_=calculate('decomposition',sales,[],A,Z,date(2026,8,1),date(2026,8,31))
    network=rows[0]
    assert network['amount_delta']==350
    assert network['purchase_count_effect']+network['average_purchase_effect']==350
    assert rows[-1]['purchase_count_effect'] is None
    assert network['current']['returns']==80

def test_copurchases_unique_receipts_not_lines_and_no_returns():
    sales=[sale(1),sale(2),sale(3,returned=True),sale(4)]
    items=[item(1),item(1),item(1,'id:2'),item(2),item(3,'id:2')]
    rows,_=calculate('copurchases',sales,items,A,Z,min_pairs=1)
    assert len(rows)==1
    assert rows[0]['joint_purchases']==1 and rows[0]['eligible_purchases']==3
    assert rows[0]['confidence_a_to_b']==D('.5')
    assert rows[0]['lift']==D('1.5')

def test_customers_search_before_period_and_exclude_anonymous():
    sales=[sale(1,date(2026,8,1)),sale(2,A),sale(3,A,customer=2),sale(4,A,customer=None)]
    rows,m=calculate('customers',sales,[],A,Z)
    assert [r['customers'] for r in rows]==[1,1]
    assert m['purchases_without_customer']==1

def test_retention_never_fills_unfinished_month_with_zero():
    sales=[sale(1,A),sale(2,date(2026,10,2)),sale(3,A,customer=2)]
    rows,_=calculate('retention',sales,[],A,date(2026,10,23))
    assert rows[0]['retention']==1
    assert rows[1]['active_customers']==1 and rows[1]['retention'] is None
    rows,_=calculate('retention',sales,[],A,date(2026,10,31))
    assert rows[1]['retention']==D('.5')

def test_hour_is_local_but_weekday_is_business_date():
    rows,_=calculate('hours',[sale()],[],A,Z)
    assert rows[0]['local_hour']==4 and rows[0]['business_weekday']==2
    assert rows[0]['calendar_weekdays']==5 and rows[0]['observed_days']==1

def test_returns_not_subtracted_and_units_separate():
    ss=[sale(1),sale(2,returned=True)]
    rows,_=calculate('returns',ss,[item(2),item(2,unit='кг')],A,Z)
    assert rows[0]['amount']==100 and rows[0]['returns']==100
    assert len(rows)==3

def test_discount_absent_is_unknown_and_price_weighted():
    ss=[sale(1),sale(2,discount='0'),sale(3,discount='36')]
    rows,_=calculate('discounts',ss,[item(1,quantity=1),item(2,quantity=3)],A,Z)
    assert rows[0]['discount_unknown']==1 and rows[0]['explicit_discount']==36
    assert rows[1]['average_price']==5

def test_anomalies_require_four_same_weekdays_no_filled_zero():
    ss=[sale(1,A,200)]+[sale(k+1,A-timedelta(days=7*k),100) for k in range(1,5)]
    rows,_=calculate('anomalies',ss,[],A,A)
    r=next(r for r in rows if r['metric']=='amount')
    assert r['candidate'] and r['baseline_median']==100
    rows,_=calculate('anomalies',ss[:-1],[],A,A)
    assert not any(r['candidate'] for r in rows)

def traffic(content,encoding='cp1251'):
    return parse(('Дата\tВремя\tСобытие\tID\n'+content).encode(encoding),'counter1','398','Asia/Yekaterinburg','series1')

def test_traffic_encoding_business_date_and_repeat():
    r=traffic('07.02.2026\t5:28:16\tВыход\t1\n07.02.2026\t5:28:16\tВыход\t1\n17.10.2026\t8:46:00\tВход\t2\n')
    assert r['entries']==1 and r['exits']==1 and r['duplicate_rows']==1
    assert r['events'][0]['business_date']=='2026-02-06'
    assert r['dates']==['2026-02-07','2026-10-17'] and r['conversion'] is None
    assert r['coverage_confirmed'] is False

@pytest.mark.parametrize('content',[
    '17.10.2026\t8:46:00\tВход\t1\n17.10.2026\t8:46:01\tВыход\t1\n',
    '17.10.2026\t8:46:00\tЧто-то\t1\n',
    '17.10.2026\t25:46:00\tВход\t1\n'])
def test_traffic_conflicts_fail_entire_file(content):
    with pytest.raises(ValueError): traffic(content)

def test_traffic_utf8_and_empty_export():
    r=traffic('', 'utf-8-sig')
    assert r['event_count']==0 and not r['coverage_confirmed']

def test_traffic_dst_not_guessed():
    data='Дата\tВремя\tСобытие\tID\n25.10.2026\t02:30:00\tВход\t1\n'.encode()
    with pytest.raises(ValueError): parse(data,'c','1','Europe/Berlin','s')


@pytest.mark.parametrize('kind',['decomposition','hours','copurchases','customers','retention','returns','discounts','anomalies'])
def test_readonly_report_queries_and_pagination(kind,monkeypatch):
    import asyncio
    from test_detail_reports import Conn,Pool
    from app.services import advanced_reports as ar
    class C(Conn):
        async def fetch(self,sql,*args):
            self.calls.append((sql,args))
            if 'FROM retail_sales WHERE' in sql: return [sale(i,point=i) for i in range(1,61)]
            return []
        async def fetchrow(self,*args): return {'status':'OK','started_at':None,'finished_at':None}
    c=C()
    monkeypatch.setattr(ar,'pool',lambda:Pool(c))
    async def point(_): return 398
    monkeypatch.setattr(ar,'point_filter',point)
    kwargs=dict(previous_start='2026-08-01',previous_end='2026-08-31') if kind=='decomposition' else {}
    result=asyncio.run(ar.report(kind,'2026-09-01','2026-09-30','398',**kwargs))
    assert not result['complete_source'] and len(result['rows'])<=50
    assert all(args[-1]==398 for _,args in c.calls)
    query,args=c.calls[0]
    assert 'raw_json->>' in query and 'SELECT *' not in query
    assert args[0] is None if kind in ('customers','retention') else args[0] is not None
    assert 'Нет записей' in ar.render(result)
    if kind in ('hours','decomposition','returns','discounts'): assert result['next_offset']==50
