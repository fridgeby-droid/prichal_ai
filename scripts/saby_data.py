"""Local check or resumable update of all supported Saby sources."""
import argparse
import asyncio
import json
import logging
from datetime import date
from app.db.database import init_db,close_db
from app.services.saby_maintenance import check,update


async def run(args):
    stage='database_connect' if args.action=='check' else 'database_prepare'
    try:
        await init_db(prepare_schema=args.action!='check')
        stage='saby_'+args.action
        if args.action=='check': return await check()
        return await update(args.start,args.end,args.all_history,args.action=='resume')
    except TimeoutError:
        return {'status':'ERROR','error_type':'TimeoutError','stage':stage}
    finally: await close_db()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('check','update','resume'))
    parser.add_argument('--start',type=date.fromisoformat)
    parser.add_argument('--end',type=date.fromisoformat)
    parser.add_argument('--all-history',action='store_true')
    args=parser.parse_args()
    if args.action!='update' and (args.start or args.end or args.all_history):
        parser.error('Период задаётся только для update')
    if args.all_history and args.start: parser.error('Выберите start или all-history')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    try: result=asyncio.run(run(args))
    except Exception as exc:
        # Only local, controlled validation messages, never arbitrary exception text.
        code=str(exc) if type(exc) is ValueError and str(exc) in {
            'DISABLE_AUTO_SYNC_FOR_MAINTENANCE','MAINTENANCE_ALREADY_RUNNING',
            'UNFINISHED_MAINTENANCE_USE_RESUME','NO_MAINTENANCE_TO_RESUME',
            'EMPTY_DB_SPECIFY_START_AND_END','ONLY_CLOSED_BUSINESS_DAYS'} else type(exc).__name__
        print(json.dumps({'status':'ERROR','code':code})); raise SystemExit(1)
    print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
    if result.get('status')=='ERROR': raise SystemExit(1)


if __name__=='__main__': main()
