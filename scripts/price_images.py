"""Prepare a private image manifest once, retry individual images without price reload."""
import argparse
import asyncio
import hashlib
import json
import logging
from pathlib import Path
from app.services.price_lists import get_price,download_image,ExportError


def save(path,data):
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(path)


async def run(args):
    path=Path(args.manifest)
    if args.action=='prepare':
        if path.exists(): raise ValueError('MANIFEST_EXISTS')
        data=await get_price(args.point_id,args.price_id)
        if data.get('complete_snapshot') is not True: raise ValueError('INCOMPLETE_PRICE')
        images={i['id']:{'download_path':i['download_path'],'status':'PENDING'} for p in data['products'] for i in p['images']}
        save(path,{'schema_version':1,'price_list':data['price_list'],'revision':data['revision'],'images':images})
        return {'images':len(images),'manifest':str(path.resolve())}
    data=json.loads(path.read_text(encoding='utf-8'))
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    saved=errors=skipped=0
    for reference,item in data['images'].items():
        filename=hashlib.sha256(reference.encode()).hexdigest()+'.image'
        target=output/filename
        if not args.recheck and item['status']=='OK' and target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest()==item.get('sha256'):
            skipped+=1;continue
        try:
            route=item['download_path']
            if not route.startswith('/api/v1/core/price-list-images/'): raise ValueError('INVALID_IMAGE_PATH')
            raw,mime,checksum=await download_image(route.rsplit('/',1)[1])
            temporary=target.with_suffix('.tmp');temporary.write_bytes(raw);temporary.replace(target)
            item.update(status='OK',sha256=checksum,mime=mime,file=filename)
            item.pop('error',None);saved+=1
        except Exception as exc:
            item.update(status='ERROR',error=exc.code if isinstance(exc,ExportError) else type(exc).__name__)
            errors+=1
        save(path,data)
        if item.get('error')=='SABY_IMAGE_RATE_LIMITED': break
        await asyncio.sleep(3)
    return {'saved':saved,'skipped':skipped,'errors':errors,'status':'PARTIAL_ERRORS' if errors else 'COMPLETED'}


def main():
    p=argparse.ArgumentParser()
    p.add_argument('action',choices=['prepare','download'])
    p.add_argument('--manifest',required=True)
    p.add_argument('--point-id',default='')
    p.add_argument('--price-id',default='')
    p.add_argument('--output',default='price-images')
    p.add_argument('--recheck',action='store_true')
    args=p.parse_args()
    logging.getLogger('httpx').setLevel(logging.WARNING)
    try: result=asyncio.run(run(args))
    except Exception as exc:
        print(json.dumps({'status':'FAILED','error_type':type(exc).__name__}));raise SystemExit(1)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if result.get('errors'): raise SystemExit(1)


if __name__=='__main__': main()
