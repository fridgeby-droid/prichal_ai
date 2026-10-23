"""Current Saby catalog folders, never inferred categories or historical assignments."""
import asyncio
import json
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from uuid import UUID

from app.config import get_settings
from app.db.database import pool
from app.services.price_lists import ExportError, identifier, page_data, price_get, digest, text_field
from app.services.period_reports import PRODUCTS_SQL, period, point_filter, delta

META_KEY = 'analytics:catalog_categories:v1'


def canonical_uuid(value):
    return str(UUID(value))


def build_catalog(rows):
    nodes = {}
    for row in rows:
        node_id = identifier(row.get('hierarchicalId'))
        if node_id in nodes:
            raise ExportError('CATALOG_DUPLICATE_CURSOR')
        if 'hierarchicalParent' not in row or 'isParent' not in row:
            raise ExportError('CATALOG_HIERARCHY_MISSING')
        marker = row['isParent']
        if marker is not None and type(marker) is not bool:
            raise ExportError('CATALOG_PARENT_FLAG_INVALID')
        parent = None if row['hierarchicalParent'] is None else identifier(row['hierarchicalParent'])
        nodes[node_id] = {'id':node_id, 'parent':parent, 'folder':marker is True,
                          'name':text_field(row,'name'), 'raw':row}
    paths = {}
    def folder_path(node_id):
        if node_id in paths:
            return paths[node_id]
        path, seen = [], set()
        cursor = node_id
        while cursor is not None:
            if cursor in seen:
                raise ExportError('CATALOG_HIERARCHY_CYCLE')
            seen.add(cursor)
            node = nodes.get(cursor)
            if node is None or not node['folder']:
                raise ExportError('CATALOG_PARENT_NOT_FOUND')
            path.append({'id':node['id'],'name':node['name']})
            cursor = node['parent']
        paths[node_id] = list(reversed(path))
        return paths[node_id]
    for node in nodes.values():
        if node['folder']:
            folder_path(node['id'])
    mapping = {}
    count = 0
    for node in nodes.values():
        if node['folder']:
            continue
        row = node['raw']
        product_id = identifier(row.get('id'))
        path = folder_path(node['parent']) if node['parent'] else []
        assignment = {'path':path}
        aliases = ['id:'+product_id]
        if row.get('externalId') not in (None,''):
            try:
                aliases.append('uuid:'+canonical_uuid(row['externalId']))
            except (ValueError, TypeError, AttributeError):
                raise ExportError('CATALOG_UUID_INVALID') from None
        for alias in aliases:
            if alias in mapping and mapping[alias] != assignment:
                raise ExportError('CATALOG_CATEGORY_CONFLICT')
            mapping[alias] = assignment
        count += 1
    return {'folders':list(paths.values()),'mapping':mapping,'product_nodes':count,
            'folder_count':len(paths),'revision':digest(mapping)}


async def fetch_catalog():
    started = datetime.now(timezone.utc).isoformat()
    params = {'order':'after','pageSize':25,'noStopList':False,
              'withBalance':False,'withBarcode':False}
    rows, seen = [], set()
    for page in range(max(1,get_settings().price_lists_max_pages)):
        # No priceListId: the documented account catalog, not supplier price sections.
        data = await price_get('/retail/v2/nomenclature/list',params)
        batch, more = page_data(data,'nomenclatures')
        for row in batch:
            cursor = identifier(row.get('hierarchicalId'))
            if cursor in seen:
                raise ExportError('CATALOG_DUPLICATE_CURSOR')
            seen.add(cursor)
        rows.extend(batch)
        if not more:
            return {**build_catalog(rows),'schema_version':1,'complete_snapshot':True,
                    'started_at':started,'fetched_at':datetime.now(timezone.utc).isoformat(),
                    'source':'saby_account_catalog_current_folders'}
        params['position'] = int(batch[-1]['hierarchicalId'])
    raise ExportError('CATALOG_PAGE_LIMIT')


async def sync_catalog(apply=False):
    async with asyncio.timeout(600):
        data = await fetch_catalog()
    saved = False
    if apply:
        async with pool().acquire() as conn:
            # One atomic replacement, only after all pages and graph validation.
            result = await conn.execute('''
                INSERT INTO app_meta(key,value,updated_at) VALUES($1,$2::JSONB,NOW())
                ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at
                WHERE app_meta.value->>'started_at' < EXCLUDED.value->>'started_at'
                   OR NOT (app_meta.value ? 'started_at')
            ''', META_KEY,json.dumps(data,ensure_ascii=False))
            saved = result != 'INSERT 0 0'
    return {k:v for k,v in data.items() if k not in ('mapping','folders')} | {
        'mode':'applied' if saved else 'superseded' if apply else 'preview',
        'folder_examples':[path for path in data['folders'][:10]]}


async def read_catalog(conn):
    value = await conn.fetchval('SELECT value FROM app_meta WHERE key=$1',META_KEY)
    if isinstance(value,str):
        value = json.loads(value)
    if value is not None and (value.get('schema_version')!=1 or value.get('complete_snapshot') is not True):
        raise ValueError('Некорректный снимок категорий: обновите каталог.')
    return value


def aggregate(rows, catalog, level):
    mapping = catalog['mapping'] if catalog else {}
    grouped = {}
    for row in rows:
        key = row['product_key']
        if key.startswith('uuid:'):
            try:
                key = 'uuid:'+canonical_uuid(key[5:])
            except (ValueError,TypeError,AttributeError):
                pass
        match = mapping.get(key)
        if match is None:
            category_id, name, path = 'UNMAPPED','Без сопоставления',[]
        elif not match['path']:
            category_id, name, path = 'ROOT','Без раздела каталога',[]
        else:
            path = match['path']
            category = path[0] if level=='top' else path[-1]
            category_id,name = category['id'],category['name']
            if level=='top':
                path=path[:1]
        group=grouped.setdefault(category_id,{'category_id':category_id,'name':name,'path':path,
            'revenue':0,'returns':0,'quantity_by_unit':{},'product_groups':0})
        group['revenue'] += row['revenue']
        group['returns'] += row['returns']
        unit=row['unit'] or 'UNKNOWN'
        group['quantity_by_unit'][unit]=group['quantity_by_unit'].get(unit,0)+row['quantity']
        group['product_groups'] += 1
    return grouped


async def report(start,end,point_id='',previous_start='',previous_end='',level='top',limit=30):
    first,last=period(start,end)
    if level not in ('top','leaf'):
        raise ValueError('Уровень категорий: top или leaf.')
    if bool(previous_start)!=bool(previous_end):
        raise ValueError('Нужны обе даты базы сравнения.')
    before=period(previous_start,previous_end) if previous_start else None
    point=await point_filter(point_id)
    async with pool().acquire() as conn:
        async with conn.transaction(isolation='repeatable_read',readonly=True):
            catalog=await read_catalog(conn)
            current=[dict(r) for r in await conn.fetch(PRODUCTS_SQL,first,last,point)]
            previous=[dict(r) for r in await conn.fetch(PRODUCTS_SQL,*before,point)] if before else []
    a,b=aggregate(current,catalog,level),aggregate(previous,catalog,level)
    rows=[]
    for key in a.keys() | b.keys():
        x,y=a.get(key),b.get(key)
        base=x or y
        rows.append({k:base[k] for k in ('category_id','name','path')} | {
            'current':x,'previous':y,
            'change':delta(x['revenue'] if x else 0,y['revenue'] if y else 0) if before and current and previous else None})
    rows.sort(key=lambda r:(-abs(r['change']['absolute']) if r['change'] is not None else -(r['current'] or r['previous'])['revenue'],r['category_id']))
    def total(groups):
        return {'revenue':sum(g['revenue'] for g in groups.values()),
                'returns':sum(g['returns'] for g in groups.values()),
                'unmapped_revenue':groups.get('UNMAPPED',{}).get('revenue',0),
                'unmapped_product_groups':groups.get('UNMAPPED',{}).get('product_groups',0)}
    limit=max(1,min(int(limit),50))
    settings=get_settings()
    now=datetime.now(ZoneInfo(settings.business_tz))
    current_day=(now-timedelta(hours=settings.business_day_start_hour)).date()
    return {'date_from':start,'date_to':end,'point_id':point_id or None,
        'previous_from':previous_start or None,'previous_to':previous_end or None,
        'level':level,'catalog_status':'AVAILABLE' if catalog else 'NOT_LOADED',
        'catalog_fetched_at':catalog['fetched_at'] if catalog else None,
        'catalog_revision':catalog['revision'] if catalog else None,
        'generated_at':now.isoformat(),'timezone':settings.business_tz,
        'business_day_start_hour':settings.business_day_start_hour,
        'equal_duration':(last-first)==(before[1]-before[0]) if before else None,
        'attribution':'CURRENT_CATALOG_NOT_HISTORICAL','source':'sale_items.total_price',
        'current_totals':total(a),'previous_totals':total(b) if before else None,
        'categories':rows[:limit],'total_groups':len(rows),'truncated':len(rows)>limit,
        'quality':{'completeness':'UNCONFIRMED','reconciliation':'NOT_CHECKED',
                   'period_closed':last<current_day,
                   'previous_period_closed':before[1]<current_day if before else None},
        'note':'Категории — разделы текущего каталога Saby, не прайса. Оба периода пересчитаны по одному снимку. Нет записей не означает ноль. Без сопоставления включено в итог; суммы позиций не равны платёжной выручке. Для проверки вызовите get_load_reconcile_status.'}
