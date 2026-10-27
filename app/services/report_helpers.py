"""Pure report formatting and rollups; unique sales already grouped by owner."""
import csv
import io
import json
from collections import defaultdict
from decimal import Decimal


def quantity(value):
    if value is None: return 'н/д'
    return format(Decimal(str(value)).quantize(Decimal('.001')), 'f').rstrip('0').rstrip('.').replace('.', ',') or '0'


def frequency(row):
    if row['purchases']==1: return 'one_purchase'
    return 'repeat_visits' if row['visit_days']>1 else 'multiple_one_day'


def product_totals(rows, denominators, by_store=False):
    groups=defaultdict(list)
    for row in rows:
        key=(str(row['point_id']) if by_store else '',row['product_key'],row['unit'])
        groups[key].append(row)
    result=[]
    for (point,key,unit),values in sorted(groups.items()):
        total=sum((Decimal(str(r['quantity'])) for r in values),Decimal(0))
        revenue=None if any(r['revenue'] is None for r in values) else sum((Decimal(str(r['revenue'])) for r in values),Decimal(0))
        base=sum(r['checks'] for r in denominators if not by_store or str(r['point_id'])==point)
        purchases=sum(r['purchases'] for r in values)
        result.append(dict(point_id=point or None,product_key=key,unit=unit,name=values[0]['name'],
            quantity=total,return_quantity=sum((Decimal(str(r['return_quantity'])) for r in values),Decimal(0)),
            return_revenue=None if any(r.get('return_revenue') is None for r in values) else sum((Decimal(str(r['return_revenue'])) for r in values),Decimal(0)),
            revenue=revenue,purchases=purchases,eligible_purchases=base,
            purchase_share_percent=Decimal(purchases)*100/base if base else None,
            average_unit_price=revenue/total if revenue is not None and total else None))
    return result


def compare_products(current, previous, days, base_days):
    def keyed(rows): return {(r['point_id'],r['product_key'],r['unit']):r for r in rows}
    cur,prev=keyed(current),keyed(previous)
    result=[]
    for key in sorted(cur.keys()|prev.keys(),key=str):
        a,b=cur.get(key),prev.get(key)
        row={'point_id':key[0],'product_key':key[1],'unit':key[2],'name':(a or b)['name'],
             'current_present':a is not None,'previous_present':b is not None}
        for metric in ('quantity','return_quantity','return_revenue','revenue','average_unit_price','purchase_share_percent'):
            x=a.get(metric) if a else None;y=b.get(metric) if b else None
            row[metric+'_current']=x;row[metric+'_previous']=y
            row[metric+'_delta']=x-y if x is not None and y is not None else None
        row['quantity_per_day_current']=a['quantity']/days if a else None
        row['quantity_per_day_previous']=b['quantity']/base_days if b else None
        result.append(row)
    return result


def csv_bytes(rows):
    """Complete in-memory export, safe for opening in spreadsheet software."""
    headers=list(dict.fromkeys(k for row in rows for k in row)) or ['no_rows']
    buffer=io.StringIO(newline='')
    writer=csv.DictWriter(buffer,fieldnames=headers,delimiter=';')
    writer.writeheader()
    for row in rows:
        clean={}
        for key,value in row.items():
            numeric=isinstance(value,(int,float,Decimal)) and not isinstance(value,bool)
            if value is None: value=''
            elif isinstance(value,(dict,list)): value=json.dumps(value,ensure_ascii=False,default=str)
            else: value=str(value)
            if not numeric and (value.startswith(('\t','\r')) or value.lstrip().startswith(('=','+','-','@'))): value="'"+value
            clean[key]=value
        writer.writerow(clean)
    return ('\ufeff'+buffer.getvalue()).encode('utf-8')
