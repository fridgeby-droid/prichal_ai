import argparse
import asyncio
import json
from pathlib import Path
from app.db.database import init_db,close_db
from app.services.detail_reports import report


async def run(args):
    await init_db(prepare_schema=False)
    try:
        options=vars(args).copy()
        output=options.pop('csv',None)
        result=await report(**options,export_all=bool(output))
        if output:
            from app.services.report_helpers import csv_bytes
            rows=result.get('rows',[])
            for row in rows:
                row.update(report_date_from=result.get('date_from'),report_date_to=result.get('date_to'),complete_source=False)
            Path(output).write_bytes(csv_bytes(rows))
            return {'csv':str(Path(output).resolve()),'rows':len(rows),'complete_source':False}
        return result
    finally:
        await close_db()


def main():
    p=argparse.ArgumentParser()
    p.add_argument('kind',choices=['averages','quantities','modifiers','basket','trend','clients'])
    p.add_argument('--start',default='')
    p.add_argument('--end',default='')
    p.add_argument('--point-id',default='')
    p.add_argument('--seller-id',default='')
    p.add_argument('--shift',default='')
    p.add_argument('--query',default='')
    p.add_argument('--min-days',type=int,default=0)
    p.add_argument('--max-days',type=int)
    p.add_argument('--offset',type=int,default=0)
    p.add_argument('--match',choices=['contains','exact','id'],default='contains')
    p.add_argument('--frequency-filter',choices=['','one_purchase','multiple_one_day','repeat_visits'],default='')
    p.add_argument('--csv',help='Полный CSV без ограничения страниц; только quantities/modifiers/clients')
    args=p.parse_args()
    if args.csv and args.kind not in ('quantities','modifiers','clients'):
        p.error('CSV доступен для quantities, modifiers, clients')
    try:
        print(json.dumps(asyncio.run(run(args)),ensure_ascii=False,indent=2,default=str))
    except Exception as exc:
        print(json.dumps({'error':type(exc).__name__,'message':'Не удалось получить отчёт. Проверьте параметры и доступность БД.'},ensure_ascii=False))
        raise SystemExit(1)


if __name__=='__main__': main()
