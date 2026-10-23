import argparse
import asyncio
import json
from app.db.database import init_db,close_db
from app.services.detail_reports import report


async def run(args):
    await init_db()
    try:
        return await report(**vars(args))
    finally:
        await close_db()


def main():
    p=argparse.ArgumentParser()
    p.add_argument('kind',choices=['averages','quantities','basket','trend','clients'])
    p.add_argument('--start',default='')
    p.add_argument('--end',default='')
    p.add_argument('--point-id',default='')
    p.add_argument('--seller-id',default='')
    p.add_argument('--shift',default='')
    p.add_argument('--query',default='')
    p.add_argument('--min-days',type=int,default=0)
    p.add_argument('--max-days',type=int)
    p.add_argument('--offset',type=int,default=0)
    try:
        print(json.dumps(asyncio.run(run(p.parse_args())),ensure_ascii=False,indent=2,default=str))
    except Exception as exc:
        print(json.dumps({'error':type(exc).__name__,'message':'Не удалось получить отчёт. Проверьте параметры и доступность БД.'},ensure_ascii=False))
        raise SystemExit(1)


if __name__=='__main__': main()
