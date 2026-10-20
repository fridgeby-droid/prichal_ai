import argparse
import asyncio
import json
from datetime import date
from app.db.database import init_db,close_db
from app.services.core_schedule import sync_schedule,read_schedule,default_range

async def run(args):
    await init_db()
    try:
        start,end=(args.date_from,args.date_to) if args.date_from else default_range()
        result=await (sync_schedule(start,end) if args.command=='sync' else read_schedule(start,end))
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
    finally:
        await close_db()

def main():
    parser=argparse.ArgumentParser(description='Core actual schedule')
    parser.add_argument('command',choices=['sync','report'])
    parser.add_argument('date_from',type=date.fromisoformat,nargs='?')
    parser.add_argument('date_to',type=date.fromisoformat,nargs='?')
    args=parser.parse_args()
    if bool(args.date_from)!=bool(args.date_to):
        parser.error('Укажите обе даты или ни одной')
    try:
        asyncio.run(run(args))
    except Exception as exc:
        parser.exit(1,f'График Core не обновлён: {exc}\n')

if __name__=='__main__':
    main()
