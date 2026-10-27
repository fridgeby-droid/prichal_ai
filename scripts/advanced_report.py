import argparse
import asyncio
import json
from pathlib import Path
from app.db.database import init_db,close_db
from app.services.advanced_reports import KINDS,report
from app.services.report_helpers import csv_bytes


async def run(args):
    await init_db(prepare_schema=False)
    try:
        result=await report(args.kind,args.start,args.end,args.point_id,args.previous_start,args.previous_end,
                            args.offset,args.min_pairs,bool(args.csv))
        if args.csv:
            Path(args.csv).write_bytes(csv_bytes([{**r,'date_from':args.start,'date_to':args.end,
                'previous_start':args.previous_start,'previous_end':args.previous_end,
                'complete_source':False,'basis':'unique_sale_total_price'} for r in result['rows']]))
            return dict(csv=str(Path(args.csv).resolve()),rows=result['total_rows'],metadata=result['metadata'])
        return result
    finally:
        await close_db()


def main():
    p=argparse.ArgumentParser()
    p.add_argument('kind',choices=KINDS)
    p.add_argument('start');p.add_argument('end')
    for key in ('point-id','previous-start','previous-end'):
        p.add_argument('--'+key,default='')
    p.add_argument('--offset',type=int,default=0)
    p.add_argument('--min-pairs',type=int,default=3)
    p.add_argument('--csv')
    try:
        print(json.dumps(asyncio.run(run(p.parse_args())),ensure_ascii=False,indent=2,default=str))
    except Exception as exc:
        print(json.dumps({'status':'FAILED','error_type':type(exc).__name__,
            'message':str(exc) if isinstance(exc,ValueError) else 'См. журнал; секреты скрыты'},ensure_ascii=False))
        raise SystemExit(1)


if __name__=='__main__': main()
