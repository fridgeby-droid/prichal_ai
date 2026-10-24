import asyncio
import json
import logging
from app.db.database import init_db,close_db
from app.services.bonus_probe import probe


async def run():
    await init_db()
    try: return await probe()
    finally: await close_db()


def main():
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    try: result=asyncio.run(run())
    except Exception as exc:
        print(json.dumps({'status':'PROBE_FAILED','error_type':type(exc).__name__}))
        raise SystemExit(1)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
