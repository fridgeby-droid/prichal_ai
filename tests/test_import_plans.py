import asyncio
import json
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from scripts.import_plans import resolve_bundle, classify, import_bundle


def bundle_and_stores():
    path = Path(__file__).resolve().parents[1] / 'data/plans-2026-07-09.json'
    bundle = json.loads(path.read_text(encoding='utf-8'))
    # Synthetic ID for the lookup-only store; never shipped in the plan data.
    stores = [dict(point_id=v['point_id'] or 999999, name=v['name']) for v in bundle['stores'].values()]
    return bundle, stores


def test_all_approved_amounts_and_month_boundaries():
    bundle, stores = bundle_and_stores()
    plans = resolve_bundle(bundle, stores)
    assert len(plans) == 64
    for month, day_total, night_total, count, last_day in [
        (7, 210000, 470000, 20, 31), (8, 190000, 545000, 22, 31), (9, 190000, 550000, 22, 30)
    ]:
        rows = [p for p in plans if p['valid_from'].month == month]
        assert len(rows) == count
        assert sum(p['amount'] for p in rows if p['shift_type'] == 'DAY') == day_total
        assert sum(p['amount'] for p in rows if p['shift_type'] == 'NIGHT') == night_total
        assert all(p['valid_to'] == date(2026, month, last_day) for p in rows)
    assert not any(p['store_alias'] == 'КАМСКАЯ' and p['valid_from'].month == 7 for p in plans)


def test_unresolved_store_blocks_entire_bundle():
    bundle, stores = bundle_and_stores()
    with pytest.raises(ValueError, match='КАМСКАЯ'):
        resolve_bundle(bundle, [s for s in stores if s['point_id'] != 999999])


def test_same_plan_skips_and_different_plan_conflicts():
    bundle, stores = bundle_and_stores()
    plan = resolve_bundle(bundle, stores)[0]
    existing = dict(valid_from=plan['valid_from'], valid_to=plan['valid_to'], plan_amount=plan['amount'])
    assert classify(plan, [existing]) == 'skip'
    with pytest.raises(ValueError, match='Конфликт'):
        classify(plan, [dict(existing, plan_amount=999)])


def test_preview_no_writes_and_repeat_apply_no_duplicates():
    bundle, stores = bundle_and_stores()
    class Connection:
        def __init__(self):
            self.rows = {}
            self.inserts = 0
        async def fetch(self, sql, *args):
            if 'FROM stores' in sql:
                return stores
            return self.rows.get(tuple(args), [])
        async def execute(self, sql, *args):
            if sql.startswith('INSERT'):
                self.inserts += 1
                pid, shift, amount, start, end, _ = args
                self.rows[(pid, shift, start, end)] = [dict(valid_from=start, valid_to=end, plan_amount=amount)]
    conn = Connection()
    preview = asyncio.run(import_bundle(conn, bundle))
    assert preview['to_insert'] == 64 and conn.inserts == 0
    assert asyncio.run(import_bundle(conn, bundle, True))['inserted'] == 64
    assert asyncio.run(import_bundle(conn, bundle, True))['already_present'] == 64
    assert conn.inserts == 64


def test_conflict_late_in_bundle_prevents_any_insert():
    bundle, stores = bundle_and_stores()
    class Connection:
        calls = 0
        execute = AsyncMock()
        async def fetch(self, sql, *args):
            if 'FROM stores' in sql:
                return stores
            self.calls += 1
            if self.calls == 64:
                return [dict(valid_from=date(2026, 1, 1), valid_to=None, plan_amount=1)]
            return []
    conn = Connection()
    with pytest.raises(ValueError, match='Конфликт'):
        asyncio.run(import_bundle(conn, bundle, True))
    assert all('INSERT' not in call.args[0] for call in conn.execute.await_args_list)
