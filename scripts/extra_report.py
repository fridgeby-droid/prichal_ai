import argparse
import asyncio
import json
from pathlib import Path
from app.db.database import init_db,close_db
from app.services.extra_reports import product_comparison,customer_card,payment_audit
from app.services.report_helpers import csv_bytes


async def run(args):
    await init_db(prepare_schema=False)
    try:
        if args.kind=='customer': result=await customer_card(args.customer_id,args.end,args.offset)
        elif args.kind=='payments': result=await payment_audit(args.start,args.end,args.point_id,args.offset)
        else: result=await product_comparison(args.start,args.end,args.previous_start,args.previous_end,
            args.point_id,args.seller_id,args.shift,args.query,args.match,args.modifiers,args.offset)
        if args.csv:
            rows=[{**r,'complete_source':False,'report_start':args.start,'report_end':args.end,
                   'previous_start':args.previous_start,'previous_end':args.previous_end} for r in result['rows']]
            Path(args.csv).write_bytes(csv_bytes(rows))
            return {'csv':str(Path(args.csv).resolve()),'rows':len(rows),'complete_source':False}
        return result
    finally: await close_db()


def main():
    p=argparse.ArgumentParser()
    p.add_argument('kind',choices=['compare','customer','payments'])
    for key in ('start','end','previous-start','previous-end','point-id','seller-id','shift','query','customer-id'):
        p.add_argument('--'+key,default='')
    p.add_argument('--match',choices=['contains','exact','id'],default='contains')
    p.add_argument('--modifiers',action='store_true')
    p.add_argument('--offset',type=int,default=0)
    p.add_argument('--csv')
    args=p.parse_args()
    try: print(json.dumps(asyncio.run(run(args)),ensure_ascii=False,indent=2,default=str))
    except Exception as exc:
        print(json.dumps({'status':'FAILED','error_type':type(exc).__name__}))
        raise SystemExit(1)


if __name__=='__main__': main()
