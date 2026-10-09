"""Import the approved July–September store plans; preview unless --apply."""
from __future__ import annotations

import argparse
import asyncio
import calendar
import json
from datetime import date
from decimal import Decimal


# Approved source table is embedded: no dependency on a deployment data directory.
APPROVED_PLANS_JSON = '{"stores":{"БАТУМСКАЯ":{"name":"Батумская 5","point_id":5635},"БЕЛИНСКОГО":{"name":"Белинского 6б","point_id":5604},"ВОЛЬСКАЯ":{"name":"Вольская 62","point_id":5617},"ВОРОНЕЖСКАЯ":{"name":"Воронежская 13","point_id":5629},"ГОЗНАК":{"name":"Шоссе космонавтов 115а","point_id":398},"КАМСКАЯ":{"name":"Камская 2/1","point_id":null},"МАКАРЕНКО":{"name":"Макаренко 48","point_id":5611},"МАРШАЛА":{"name":"Маршала рыбалко 99","point_id":5590},"МЕДОВЫЙ":{"name":"Яблокова 14","point_id":5598},"ПАРКОВЫЙ":{"name":"Парковый 8","point_id":5583},"СЕРЕБРИСТАЯ":{"name":"Серебристая 7","point_id":5623}},"plans":[{"store_alias":"БАТУМСКАЯ","shift_type":"DAY","weekday":null,"plan_amount":20000,"valid_from":"2026-07-01","valid_to":"2026-07-31"},{"store_alias":"БАТУМСКАЯ","shift_type":"NIGHT","weekday":null,"plan_amount":35000,"valid_from":"2026-07-01","valid_to":"2026-07-31"},{"store_alias":"БЕЛИНСКОГО","shift_type":"DAY","weekday":null,"plan_amount":15000,"valid_from":"2026-07-01","valid_to":"2026-07-31"},{"store_alias":"БЕЛИНСКОГО","shift_type":"NIGHT","weekday":null,"plan_amount":30000,"valid_from":"2026-07-01","valid_to":"2026-07-31"},{"store_alias":"ВОЛЬСКАЯ","shift_type":"DAY","weekday":null,"plan_amount":20000,"valid_from":"2026-07-01","valid_to":"2026-07-31"},{"store_alias":"ВОЛЬСКАЯ","shift_type":"NIGHT","weekday":null,"plan_amount":35000,"valid_from":"2026-07-01","valid_to":"2026-07-31"},{"store_alias":"ВОРОНЕЖСКАЯ","shift_type":"DAY","weekday":null,"plan_amount":15000,"valid_from":"2026-07-01","valid_to":"2026-07-31"},{"store_alias":"ВОРОНЕЖСКАЯ","shift_type":"NIGHT","weekday":null,"plan_amount":25000,"valid_from":"2026-07-01","valid_to":"2026-07-31"},{"store_alias":"ГОЗНАК","shift_type":"DAY","weekday":null,"plan_amount":25000,"valid_from":"2026-07-01","valid_to":"2026-07-31"},{"store_alias":"ГОЗНАК","shift_type":"NIGHT","weekday":null,"plan_amount":60000,"valid_from":"2026-07-01","valid_to":"2026-07-31"},{"store_alias":"МАКАРЕНКО","shift_type":"DAY","weekday":null,"plan_amount":20000,"valid_from":"2026-07-01","valid_to":"2026-07-31"},{"store_alias":"МАКАРЕНКО","shift_type":"NIGHT","weekday":null,"plan_amount":35000,"valid_from":"2026-07-01","valid_to":"2026-07-31"},{"store_alias":"МАРШАЛА","shift_type":"DAY","weekday":null,"plan_amount":20000,"valid_from":"2026-07-01","valid_to":"2026-07-31"},{"store_alias":"МАРШАЛА","shift_type":"NIGHT","weekday":null,"plan_amount":50000,"valid_from":"2026-07-01","valid_to":"2026-07-31"},{"store_alias":"МЕДОВЫЙ","shift_type":"DAY","weekday":null,"plan_amount":25000,"valid_from":"2026-07-01","valid_to":"2026-07-31"},{"store_alias":"МЕДОВЫЙ","shift_type":"NIGHT","weekday":null,"plan_amount":60000,"valid_from":"2026-07-01","valid_to":"2026-07-31"},{"store_alias":"ПАРКОВЫЙ","shift_type":"DAY","weekday":null,"plan_amount":20000,"valid_from":"2026-07-01","valid_to":"2026-07-31"},{"store_alias":"ПАРКОВЫЙ","shift_type":"NIGHT","weekday":null,"plan_amount":60000,"valid_from":"2026-07-01","valid_to":"2026-07-31"},{"store_alias":"СЕРЕБРИСТАЯ","shift_type":"DAY","weekday":null,"plan_amount":30000,"valid_from":"2026-07-01","valid_to":"2026-07-31"},{"store_alias":"СЕРЕБРИСТАЯ","shift_type":"NIGHT","weekday":null,"plan_amount":80000,"valid_from":"2026-07-01","valid_to":"2026-07-31"},{"store_alias":"БАТУМСКАЯ","shift_type":"DAY","weekday":null,"plan_amount":15000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"БАТУМСКАЯ","shift_type":"NIGHT","weekday":null,"plan_amount":60000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"БЕЛИНСКОГО","shift_type":"DAY","weekday":null,"plan_amount":10000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"БЕЛИНСКОГО","shift_type":"NIGHT","weekday":null,"plan_amount":30000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"ВОЛЬСКАЯ","shift_type":"DAY","weekday":null,"plan_amount":15000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"ВОЛЬСКАЯ","shift_type":"NIGHT","weekday":null,"plan_amount":35000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"ВОРОНЕЖСКАЯ","shift_type":"DAY","weekday":null,"plan_amount":10000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"ВОРОНЕЖСКАЯ","shift_type":"NIGHT","weekday":null,"plan_amount":25000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"ГОЗНАК","shift_type":"DAY","weekday":null,"plan_amount":20000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"ГОЗНАК","shift_type":"NIGHT","weekday":null,"plan_amount":65000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"КАМСКАЯ","shift_type":"DAY","weekday":null,"plan_amount":15000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"КАМСКАЯ","shift_type":"NIGHT","weekday":null,"plan_amount":30000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"МАКАРЕНКО","shift_type":"DAY","weekday":null,"plan_amount":15000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"МАКАРЕНКО","shift_type":"NIGHT","weekday":null,"plan_amount":45000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"МАРШАЛА","shift_type":"DAY","weekday":null,"plan_amount":15000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"МАРШАЛА","shift_type":"NIGHT","weekday":null,"plan_amount":50000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"МЕДОВЫЙ","shift_type":"DAY","weekday":null,"plan_amount":20000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"МЕДОВЫЙ","shift_type":"NIGHT","weekday":null,"plan_amount":65000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"ПАРКОВЫЙ","shift_type":"DAY","weekday":null,"plan_amount":25000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"ПАРКОВЫЙ","shift_type":"NIGHT","weekday":null,"plan_amount":60000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"СЕРЕБРИСТАЯ","shift_type":"DAY","weekday":null,"plan_amount":30000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"СЕРЕБРИСТАЯ","shift_type":"NIGHT","weekday":null,"plan_amount":80000,"valid_from":"2026-08-01","valid_to":"2026-08-31"},{"store_alias":"БАТУМСКАЯ","shift_type":"DAY","weekday":null,"plan_amount":15000,"valid_from":"2026-09-01","valid_to":"2026-09-30"},{"store_alias":"БАТУМСКАЯ","shift_type":"NIGHT","weekday":null,"plan_amount":65000,"valid_from":"2026-09-01","valid_to":"2026-09-30"},{"store_alias":"БЕЛИНСКОГО","shift_type":"DAY","weekday":null,"plan_amount":10000,"valid_from":"2026-09-01","valid_to":"2026-09-30"},{"store_alias":"БЕЛИНСКОГО","shift_type":"NIGHT","weekday":null,"plan_amount":30000,"valid_from":"2026-09-01","valid_to":"2026-09-30"},{"store_alias":"ВОЛЬСКАЯ","shift_type":"DAY","weekday":null,"plan_amount":15000,"valid_from":"2026-09-01","valid_to":"2026-09-30"},{"store_alias":"ВОЛЬСКАЯ","shift_type":"NIGHT","weekday":null,"plan_amount":35000,"valid_from":"2026-09-01","valid_to":"2026-09-30"},{"store_alias":"ВОРОНЕЖСКАЯ","shift_type":"DAY","weekday":null,"plan_amount":10000,"valid_from":"2026-09-01","valid_to":"2026-09-30"},{"store_alias":"ВОРОНЕЖСКАЯ","shift_type":"NIGHT","weekday":null,"plan_amount":25000,"valid_from":"2026-09-01","valid_to":"2026-09-30"},{"store_alias":"ГОЗНАК","shift_type":"DAY","weekday":null,"plan_amount":20000,"valid_from":"2026-09-01","valid_to":"2026-09-30"},{"store_alias":"ГОЗНАК","shift_type":"NIGHT","weekday":null,"plan_amount":65000,"valid_from":"2026-09-01","valid_to":"2026-09-30"},{"store_alias":"КАМСКАЯ","shift_type":"DAY","weekday":null,"plan_amount":15000,"valid_from":"2026-09-01","valid_to":"2026-09-30"},{"store_alias":"КАМСКАЯ","shift_type":"NIGHT","weekday":null,"plan_amount":30000,"valid_from":"2026-09-01","valid_to":"2026-09-30"},{"store_alias":"МАКАРЕНКО","shift_type":"DAY","weekday":null,"plan_amount":15000,"valid_from":"2026-09-01","valid_to":"2026-09-30"},{"store_alias":"МАКАРЕНКО","shift_type":"NIGHT","weekday":null,"plan_amount":45000,"valid_from":"2026-09-01","valid_to":"2026-09-30"},{"store_alias":"МАРШАЛА","shift_type":"DAY","weekday":null,"plan_amount":15000,"valid_from":"2026-09-01","valid_to":"2026-09-30"},{"store_alias":"МАРШАЛА","shift_type":"NIGHT","weekday":null,"plan_amount":50000,"valid_from":"2026-09-01","valid_to":"2026-09-30"},{"store_alias":"МЕДОВЫЙ","shift_type":"DAY","weekday":null,"plan_amount":20000,"valid_from":"2026-09-01","valid_to":"2026-09-30"},{"store_alias":"МЕДОВЫЙ","shift_type":"NIGHT","weekday":null,"plan_amount":65000,"valid_from":"2026-09-01","valid_to":"2026-09-30"},{"store_alias":"ПАРКОВЫЙ","shift_type":"DAY","weekday":null,"plan_amount":25000,"valid_from":"2026-09-01","valid_to":"2026-09-30"},{"store_alias":"ПАРКОВЫЙ","shift_type":"NIGHT","weekday":null,"plan_amount":60000,"valid_from":"2026-09-01","valid_to":"2026-09-30"},{"store_alias":"СЕРЕБРИСТАЯ","shift_type":"DAY","weekday":null,"plan_amount":30000,"valid_from":"2026-09-01","valid_to":"2026-09-30"},{"store_alias":"СЕРЕБРИСТАЯ","shift_type":"NIGHT","weekday":null,"plan_amount":80000,"valid_from":"2026-09-01","valid_to":"2026-09-30"}]}'


def load_bundle():
    return json.loads(APPROVED_PLANS_JSON)


def normalize(value):
    return ''.join(str(value).casefold().replace('ё', 'е').split())


def resolve_bundle(bundle, stores):
    resolved = []
    targets = {}
    for alias, mapping in bundle['stores'].items():
        if mapping.get('point_id') is not None:
            matches = [s for s in stores if s['point_id'] == mapping['point_id']]
        else:
            matches = [s for s in stores if normalize(s['name']) == normalize(mapping['name'])]
        if len(matches) != 1:
            raise ValueError(f'Магазин {alias} ({mapping["name"]}) не найден однозначно. Импорт отменён.')
        targets[alias] = matches[0]
    if len({s['point_id'] for s in targets.values()}) != len(targets):
        raise ValueError('Разные названия сопоставлены одному магазину. Импорт отменён.')
    keys = set()
    for item in bundle['plans']:
        store = targets[item['store_alias']]
        start, end = date.fromisoformat(item['valid_from']), date.fromisoformat(item['valid_to'])
        if start.day != 1 or end != date(start.year, start.month, calendar.monthrange(start.year, start.month)[1]):
            raise ValueError('План должен действовать ровно один календарный месяц.')
        amount = Decimal(str(item['plan_amount']))
        if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal('.01')):
            raise ValueError('Некорректная сумма плана.')
        if item['shift_type'] not in ('DAY', 'NIGHT') or item['weekday'] is not None:
            raise ValueError('Ожидаются DAY/NIGHT и ALL.')
        key = (store['point_id'], item['shift_type'], start)
        if key in keys:
            raise ValueError('Дубликат плана в файле.')
        keys.add(key)
        resolved.append(dict(point_id=store['point_id'], store=store['name'],
                             store_alias=item['store_alias'], shift_type=item['shift_type'],
                             amount=amount, valid_from=start, valid_to=end))
    return resolved


def classify(plan, overlaps):
    if not overlaps:
        return 'insert'
    if len(overlaps) == 1:
        row = overlaps[0]
        if (row['valid_from'] == plan['valid_from'] and row['valid_to'] == plan['valid_to']
                and Decimal(str(row['plan_amount'])) == plan['amount']):
            return 'skip'
    raise ValueError(f'Конфликт действующего ALL-плана: {plan["store"]} '
                     f'{plan["shift_type"]} {plan["valid_from"]}. Существующие планы не изменены.')


async def import_bundle(conn, bundle, apply=False):
    if apply:
        # Prevent duplicate inserts from simultaneous imports/manual setplan.
        await conn.execute('LOCK TABLE shift_plans IN SHARE ROW EXCLUSIVE MODE')
    stores = await conn.fetch('SELECT point_id, name FROM stores ORDER BY point_id')
    plans = resolve_bundle(bundle, stores)
    actions = []
    for plan in plans:
        overlaps = await conn.fetch(
            '''SELECT valid_from, valid_to, plan_amount FROM shift_plans
               WHERE point_id=$1 AND shift_type=$2 AND active=TRUE AND weekday IS NULL
                 AND valid_from <= $4 AND (valid_to IS NULL OR valid_to >= $3)''',
            plan['point_id'], plan['shift_type'], plan['valid_from'], plan['valid_to'])
        actions.append((plan, classify(plan, overlaps)))
    # Validate all stores/conflicts before inserting any plan.
    if apply:
        for plan, action in actions:
            if action == 'insert':
                await conn.execute(
                    '''INSERT INTO shift_plans(point_id,shift_type,weekday,plan_amount,
                       valid_from,valid_to,active,source,note)
                       VALUES($1,$2,NULL,$3,$4,$5,TRUE,'MANUAL_IMPORT',$6)''',
                    plan['point_id'], plan['shift_type'], plan['amount'],
                    plan['valid_from'], plan['valid_to'],
                    'Approved monthly plans 2026-07..09; alias=' + plan['store_alias'])
    return {'mode': 'applied' if apply else 'preview', 'total': len(plans),
            'inserted' if apply else 'to_insert': sum(a == 'insert' for _, a in actions),
            'already_present': sum(a == 'skip' for _, a in actions),
            'plans': [dict(plan, action=action) for plan, action in actions],
            'note': 'ALL; месячные границы включительно. Existing weekday overrides сохранены.'}


async def run(apply):
    from app.db.database import init_db, close_db, pool
    bundle = load_bundle()
    await init_db()
    try:
        async with pool().acquire() as conn:
            async with conn.transaction():
                result = await import_bundle(conn, bundle, apply)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        await close_db()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Сохранить планы одной транзакцией')
    args = parser.parse_args()
    try:
        asyncio.run(run(args.apply))
    except Exception as exc:
        parser.exit(1, f'Импорт остановлен: {exc}\n')


if __name__ == '__main__':
    main()
