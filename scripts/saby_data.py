"""Local check or resumable update of all supported Saby sources."""
import argparse
import asyncio
import json
import logging
from datetime import date
from app.db.database import init_db,close_db
from app.services.saby_maintenance import check,update,COMPONENTS


async def run(args):
    stage='database_connect' if args.action=='check' else 'database_prepare'
    try:
        await init_db(prepare_schema=args.action not in ('check','status'))
        stage='saby_'+args.action
        if args.action=='check': return await check()
        if args.action=='status':
            from app.services.load_status import status
            return await status()
        component=args.action if args.action in COMPONENTS else args.component
        return await update(args.start,args.end,args.all_history,args.action=='resume',component)
    except TimeoutError:
        return {'status':'ERROR','error_type':'TimeoutError','stage':stage}
    finally: await close_db()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('check','status','update','resume',*COMPONENTS))
    parser.add_argument('--component',choices=COMPONENTS,help='Продолжить отдельное обновление: resume --component prices')
    parser.add_argument('--start',type=date.fromisoformat)
    parser.add_argument('--end',type=date.fromisoformat)
    parser.add_argument('--all-history',action='store_true')
    args=parser.parse_args()
    if args.component and args.action!='resume': parser.error('--component задаётся только для resume')
    if args.action not in ('update','sales') and (args.start or args.end or args.all_history):
        parser.error('Период задаётся только для update или sales')
    if args.all_history and args.start: parser.error('Выберите start или all-history')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    try: result=asyncio.run(run(args))
    except Exception as exc:
        # Only local, controlled validation messages, never arbitrary exception text.
        code=str(exc) if type(exc) is ValueError and str(exc) in {
            'DISABLE_AUTO_SYNC_FOR_MAINTENANCE','MAINTENANCE_ALREADY_RUNNING',
            'UNFINISHED_MAINTENANCE_USE_RESUME','NO_MAINTENANCE_TO_RESUME',
            'EMPTY_DB_SPECIFY_START_AND_END','ONLY_CLOSED_BUSINESS_DAYS',
            'PRICE_POINTS_NOT_CONFIGURED'} else type(exc).__name__
        print(json.dumps({'status':'ERROR','code':code})); raise SystemExit(1)
    if args.action=='status' and result.get('status')!='ERROR':
        from app.services.load_status import render
        print(render(result))
    else:
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
    if result.get('status') in ('ERROR','PARTIAL'): raise SystemExit(1)


if __name__=='__main__': main()
