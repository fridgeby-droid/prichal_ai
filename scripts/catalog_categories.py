"""Preview or apply a full current Saby category catalog, using server credentials."""
import argparse
import asyncio
import json
import httpx

from app.db.database import init_db, close_db
from app.services.catalog_categories import sync_catalog
from app.services.price_lists import ExportError


async def run(apply):
    if apply:
        await init_db()
    try:
        print(json.dumps(await sync_catalog(apply),ensure_ascii=False,indent=2,default=str))
    finally:
        if apply:
            await close_db()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',help='Save the complete validated catalog to AI PostgreSQL')
    args=parser.parse_args()
    try:
        asyncio.run(run(args.apply))
    except Exception as exc:
        error={'complete_snapshot':False,'error':exc.code if isinstance(exc,ExportError) else type(exc).__name__}
        if isinstance(exc,httpx.HTTPStatusError):
            error['http_status']=exc.response.status_code
        print(json.dumps(error))
        raise SystemExit(1) from None


if __name__=='__main__':
    main()
