import argparse
import asyncio
import json
import logging
from datetime import date
from app.db.database import init_db,close_db
from app.services.bonus_load import load,report


async def run(args):
    await init_db()
    try:
        if args.action=='report': return await report(args.start,args.end,args.point_id)
        return await load(args.start,args.end,args.point_id,0 if args.all else args.limit,args.refresh)
    finally: await close_db()


def main():
    parser=argparse.ArgumentParser(description='Бонусы сохранённых продаж, без изменения Saby')
    parser.add_argument('action',choices=('load','report'))
    parser.add_argument('--start',type=date.fromisoformat)
    parser.add_argument('--end',type=date.fromisoformat)
    parser.add_argument('--point-id',type=int)
    parser.add_argument('--all',action='store_true',help='Вся локальная история без лимита пакета')
    parser.add_argument('--limit',type=int,default=100)
    parser.add_argument('--refresh',action='store_true',help='Повторно прочитать уже сохранённые наблюдения')
    args=parser.parse_args()
    if args.limit<1: parser.error('limit должен быть положительным')
    if args.start and args.end and args.start>args.end: parser.error('Неверный период')
    if args.action=='report' and not (args.start and args.end): parser.error('Для отчёта нужны start и end')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    try: result=asyncio.run(run(args))
    except Exception as exc:
        print(json.dumps({'status':'FAILED','error_type':type(exc).__name__}))
        raise SystemExit(1)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if result.get('status')=='STOPPED': raise SystemExit(1)


if __name__=='__main__': main()
