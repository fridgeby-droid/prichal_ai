"""Read-only export of retail stores and observed Saby seller accounts."""
from datetime import date, datetime, timezone
from app.db.database import pool

ACTIVITY_SQL = """
SELECT p.seller_id, TRIM(p.seller_name) AS seller_name, p.point_id,
       MIN(p.business_date) AS first_seen, MAX(p.business_date) AS last_seen
FROM retail_sale_payments p
JOIN retail_sales s ON s.point_id=p.point_id AND s.sale_id=p.sale_id
JOIN retail_stores st ON st.point_id=p.point_id
WHERE s.deleted=FALSE AND p.seller_id IS NOT NULL
GROUP BY p.seller_id, TRIM(p.seller_name), p.point_id
ORDER BY p.seller_id, p.point_id, seller_name
"""


def assemble_sellers(rows):
    accounts = {}
    for row in rows:
        sid = str(row['seller_id'])
        item = accounts.setdefault(sid, {'saby_seller_id': sid, '_names': {}, '_stores': {}})
        name = (row['seller_name'] or '').strip()
        first, last = row['first_seen'], row['last_seen']
        if name:
            item['_names'][name] = max(item['_names'].get(name, date.min), last or date.min)
        point = str(row['point_id'])
        activity = item['_stores'].setdefault(point, {'saby_point_id': point, 'first_seen': None, 'last_seen': None})
        if first is not None:
            activity['first_seen'] = min(activity['first_seen'] or first, first)
        if last is not None:
            activity['last_seen'] = max(activity['last_seen'] or last, last)
    result = []
    for sid in sorted(accounts, key=int):
        item = accounts[sid]
        names = item.pop('_names')
        item['observed_names'] = sorted(names)
        item['display_name'] = sorted(names, key=lambda n: (-names[n].toordinal(), n))[0] if names else None
        item['stores'] = sorted(item.pop('_stores').values(), key=lambda s: int(s['saby_point_id']))
        first = [s['first_seen'] for s in item['stores'] if s['first_seen'] is not None]
        last = [s['last_seen'] for s in item['stores'] if s['last_seen'] is not None]
        item['first_seen'] = min(first) if first else None
        item['last_seen'] = max(last) if last else None
        result.append(item)
    return result


async def get_directories():
    async with pool().acquire() as conn:
        async with conn.transaction(isolation='repeatable_read', readonly=True):
            stores = await conn.fetch('SELECT point_id, name, address, locality, updated_at FROM retail_stores ORDER BY point_id')
            rows = await conn.fetch(ACTIVITY_SQL)
            sync = await conn.fetchrow("SELECT finished_at FROM sync_runs WHERE status='OK' ORDER BY finished_at DESC NULLS LAST LIMIT 1")
    sellers = assemble_sellers(rows)
    return {
        'schema_version': 1,
        'generated_at': datetime.now(timezone.utc),
        'last_successful_sync_at': sync['finished_at'] if sync else None,
        'scope': 'retail_excluding_rc',
        'seller_source': 'observed_saby_payment_accounts',
        'complete_snapshot': True,
        'stores': [dict(saby_point_id=str(s['point_id']), name=s['name'], address=s['address'], locality=s['locality'], updated_at=s['updated_at']) for s in stores],
        'sellers': sellers,
        'store_count': len(stores),
        'seller_count': len(sellers),
    }
