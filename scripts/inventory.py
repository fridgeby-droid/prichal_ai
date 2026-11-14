"""Explicit inventory probes/loads. No auto-history reload, no remote writes."""
import argparse
import asyncio
import json
from pathlib import Path
import httpx
from app.db.database import init_db,close_db
from app.services import inventory_sources as source,product_costs,stock_history
from app.services.saby import _response_shape


async def run(args):
    if args.action=='receipt':
        return await source.find_receipt(args.date,args.number)
    if args.action in ('companies','warehouses'):
        path='/retail/company/list' if args.action=='companies' else '/retail/company/warehouses'
        data=await source.request(path,{} if args.action=='companies' else {'companyId':int(source.identifier(args.company))})
        # Only known identity/name fields, never full company tax/contact records.
        values=data if isinstance(data,list) else data.get('companies' if args.action=='companies' else 'warehouses',[]) if isinstance(data,dict) else []
        rows=[{k:r[k] for k in ('id','name') if k in r} for r in values if isinstance(r,dict)] if isinstance(values,list) else []
        return {'response_shape':_response_shape(data),'rows':rows,'mapping_confirmed':False}
    await init_db(prepare_schema=False)
    try:
        if args.action=='costs':
            return await product_costs.report(args.start,args.end,args.point_id,basis=args.cost_basis)
        if args.action=='stock-load':
            config=json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
            # Mapping is an explicit operator-confirmed configuration, not inferred from a name.
            if config.get('mapping_confirmed') is not True: raise ValueError('Подтвердите привязку магазина, организации и склада в конфигурации')
            units=config.get('products')
            if not isinstance(units,dict) or not units or not all(isinstance(u,str) and u.strip() for u in units.values()):
                raise ValueError('products: объект ID товара → базовая единица складского учёта')
            from app.services.period_reports import point_filter
            await point_filter(str(config['point_id']))
            snapshot=await source.fetch_balances(config['company_id'],config['warehouse_id'],list(units))
            if args.apply: return await stock_history.save(config['point_id'],snapshot,units)
            return {'mode':'PREVIEW',**snapshot,'units':units,'point_id':str(config['point_id'])}
        return await stock_history.report(args.start,args.end,args.point_id,args.action)
    finally:
        await close_db()


def main():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='action',required=True)
    sub.add_parser('companies')
    w=sub.add_parser('warehouses');w.add_argument('company')
    r=sub.add_parser('receipt');r.add_argument('--date',required=True);r.add_argument('--number',required=True)
    s=sub.add_parser('stock-load');s.add_argument('config');s.add_argument('--apply',action='store_true')
    for kind in ('costs','stock','stockouts'):
        q=sub.add_parser(kind);q.add_argument('start');q.add_argument('end');q.add_argument('--point-id',default='')
        if kind=='costs': q.add_argument('--cost-basis',default='unconfirmed',choices=['unconfirmed','same_as_total_price','revenue_excludes_vat'])
    try:
        result=asyncio.run(run(p.parse_args()))
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
    except Exception as exc:
        error={'status':'FAILED','error_type':type(exc).__name__,'complete_snapshot':False}
        if isinstance(exc,ValueError): error['message']=str(exc)[:250]
        if isinstance(exc,httpx.HTTPStatusError): error['http_status']=exc.response.status_code
        print(json.dumps(error,ensure_ascii=False));raise SystemExit(1)


if __name__=='__main__':main()
