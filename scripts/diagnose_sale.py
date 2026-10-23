"""Inspect saved Saby field structure; never export customer names, cards or phones."""
import argparse
import asyncio
import json
import re
from app.db.database import init_db,close_db,pool


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


async def run(sale_id,point_id=None):
    await init_db()
    try:
        async with pool().acquire() as conn:
            rows=await conn.fetch('SELECT point_id,sale_id,raw_json FROM retail_sales WHERE sale_id=$1 AND ($2::BIGINT IS NULL OR point_id=$2)',sale_id,point_id)
        if len(rows)!=1:
            return {'status':'NOT_FOUND' if not rows else 'AMBIGUOUS','sale_id':str(sale_id),
                    'point_ids':[str(r['point_id']) for r in rows],
                    'next':'Укажите --point-id при неоднозначности; для NOT_FOUND дождитесь загрузки продажи.'}
        row=rows[0]
        raw=json.loads(row['raw_json']) if isinstance(row['raw_json'],str) else row['raw_json']
        return {'status':'STRUCTURE_ONLY','point_id':str(row['point_id']),'sale_id':str(sale_id),
                'fields':structure(raw),'note':'Отсутствие поля не доказывает отсутствие бонусов/модификаторов. Значения строк скрыты.'}
    finally:
        await close_db()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('sale_id',type=int)
    parser.add_argument('--point-id',type=int)
    args=parser.parse_args()
    try:
        result=asyncio.run(run(args.sale_id,args.point_id))
    except Exception as exc:
        print(json.dumps({'error':type(exc).__name__,'message':'Диагностика не выполнена; исходные данные не выводятся.'},ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
