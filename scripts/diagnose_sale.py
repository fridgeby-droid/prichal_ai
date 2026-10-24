"""Inspect saved Saby field structure; never export customer names, cards or phones."""
import argparse
import asyncio
import json
import re
from datetime import date
from app.config import get_settings
from app.db.database import init_db,close_db,pool
from app.services.sale_tree import nodes


def structure(value, path='$', output=None, depth=0):
    output=output if output is not None else {}
    if depth>10: return output
    entry=output.setdefault(path,{'types':[],'occurrences':0})
    kind=type(value).__name__
    if kind not in entry['types']: entry['types'].append(kind)
    entry['occurrences']+=1
    if isinstance(value,dict):
        for key,child in value.items():
            safe=key if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,63}',str(key)) else '[redacted-key]'
            structure(child,path+'.'+safe,output,depth+1)
    elif isinstance(value,list):
        for child in value: structure(child,path+'[]',output,depth+1)
    # Only quantity and explicit bonus amounts; no identifiers, contact or card values.
    elif type(value) in (int,float) and re.fullmatch(r'(Quantity|BonusAmount|BonusSum|AccruedBonuses|SpentBonuses)',path.split('.')[-1]):
        entry.setdefault('numeric_values',[])
        if value not in entry['numeric_values'] and len(entry['numeric_values'])<20: entry['numeric_values'].append(value)
    return output


def overview(row):
    raw=json.loads(row['raw_json']) if isinstance(row['raw_json'],str) else row['raw_json']
    return {'point_id':str(row['point_id']),'store':row['store_name'],
            'internal_sale_id':str(row['sale_id']),'display_number':str(raw.get('Number')) if raw.get('Number') is not None else None,
            'sale_datetime':str(row['sale_datetime']),'business_date':str(row['business_date']),
            'source_date':raw.get('DateWTZ'),'opened_at':raw.get('OpenedWTZ'),'closed_at':raw.get('ClosedWTZ')}


def details(row):
    raw=json.loads(row['raw_json']) if isinstance(row['raw_json'],str) else row['raw_json']
    products=[]
    for node in nodes(raw):
        item=node['item']
        products.append({'line_key':node['line_key'],'parent_key':node['parent_key'],'path':node['path'],
                         'line_id':str(item['SaleNomenclature']) if item.get('SaleNomenclature') is not None else None,
                         'product_id':str(item['Nomenclature']) if item.get('Nomenclature') is not None else None,
                         'name':item.get('Name'),'unit':item.get('UnitName'),
                         'quantity':item.get('Quantity'),'is_modifier':item.get('IsModifier') if type(item.get('IsModifier')) is bool else None,
                         'is_return':node['is_return'],'refused':node['refused']})
    return {**overview(row),'customer_present':raw.get('Customer') is not None,
            'discount_info_count':len(raw['DiscountInfo']) if isinstance(raw.get('DiscountInfo'),list) else None,
            'products':products,'fields':structure(raw)}


async def run(sale_id=None,point_id=None,number=None,calendar_date=None):
    if (sale_id is None)==(number is None): raise ValueError('Выберите внутренний ID или --number.')
    day=date.fromisoformat(calendar_date) if calendar_date else None
    if number is not None and day is None: raise ValueError('Для номера продажи нужна --date YYYY-MM-DD.')
    await init_db()
    try:
        async with pool().acquire() as conn:
            rows=await conn.fetch('''SELECT s.point_id,s.sale_id,s.raw_json,s.sale_datetime,s.business_date,
                COALESCE(t.name,'') store_name FROM retail_sales s LEFT JOIN retail_stores t USING(point_id)
                WHERE ($1::BIGINT IS NULL OR s.sale_id=$1)
                AND ($2::BIGINT IS NULL OR s.point_id=$2)
                AND ($3::TEXT IS NULL OR s.raw_json->>'Number'=$3)
                AND ($4::DATE IS NULL OR (s.sale_datetime AT TIME ZONE $5)::DATE=$4)
                ORDER BY s.point_id,s.sale_id LIMIT 21''',sale_id,point_id,str(number) if number is not None else None,day,get_settings().business_tz)
        if len(rows)!=1:
            return {'status':'NOT_FOUND' if not rows else 'AMBIGUOUS',
                    'candidates':[overview(r) for r in rows[:20]],'truncated':len(rows)>20,
                    'next':'При неоднозначности укажите --point-id. При NOT_FOUND проверьте дату, номер и загрузку; это не доказывает отсутствие продажи в Saby.'}
        row=rows[0]
        return {'status':'SELECTED_LOCAL_SALE',**details(row),
                'note':'Сверьте магазин, номер, дату и товары с чеком. Контакты, карты и имя клиента скрыты. Отсутствие бонусных полей не означает нулевые бонусы.'}
    finally:
        await close_db()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('sale_id',type=int,nargs='?')
    parser.add_argument('--point-id',type=int)
    parser.add_argument('--number',type=int)
    parser.add_argument('--date',dest='calendar_date')
    args=parser.parse_args()
    try:
        result=asyncio.run(run(args.sale_id,args.point_id,args.number,args.calendar_date))
    except ValueError as exc:
        print(json.dumps({'error':'INVALID_ARGUMENT','message':str(exc)},ensure_ascii=False))
        raise SystemExit(1)
    except Exception as exc:
        print(json.dumps({'error':type(exc).__name__,'message':'Диагностика не выполнена; исходные данные не выводятся.'},ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
