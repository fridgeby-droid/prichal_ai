"""Preview or apply a full current Saby category catalog, using server credentials."""
import argparse
import asyncio
import json
import httpx
import re

from app.config import get_settings
from app.services.saby import saby_client

from app.db.database import init_db, close_db
from app.services.catalog_categories import sync_catalog
from app.services.price_lists import ExportError


def safe_http_error(exc):
    """Only selected error fields; no raw response, headers, URLs or tracebacks."""
    settings = get_settings()
    secrets = [settings.saby_app_client_id, settings.saby_app_secret,
               settings.saby_secret_key, saby_client._token.token]
    secrets += [v for k,v in exc.request.headers.items()
                if k.lower() in {'authorization','x-sbisaccesstoken'}]
    def redact(text):
        for secret in sorted((s for s in secrets if s),key=len,reverse=True):
            text=text.replace(secret,'[REDACTED]')
        text=re.sub(r'https?://[^\s<>"\']+', '[URL]', text)
        text=re.sub(r'(?i)(token|secret|password|authorization|api[_-]?key)\s*[=:]\s*[^\s,;]+',r'\1=[REDACTED]',text)
        text=re.sub(r'[A-Za-z0-9_+/=-]{40,}','[REDACTED]',text)
        return text[:800]
    result={'http_status':exc.response.status_code}
    try:
        body=exc.response.json()
    except ValueError:
        return result | {'response_format':'non_json'}
    if not isinstance(body,dict):
        return result | {'response_format':'unexpected_json'}
    for label,block in [('response',body),('error',body.get('error'))]:
        if not isinstance(block,dict):
            continue
        fields={}
        for key in ('code','errorCode','message','details','type'):
            value=block.get(key)
            if type(value) is int:
                fields[key]=value
            elif isinstance(value,str):
                fields[key]=redact(value)
        if fields:
            result[label]=fields
    return result


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
            error['saby_error']=safe_http_error(exc)
        print(json.dumps(error,ensure_ascii=False,indent=2))
        raise SystemExit(1) from None


if __name__=='__main__':
    main()
