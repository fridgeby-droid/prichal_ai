from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db.database import pool
from app.services.saby import saby_client

settings = get_settings()

# Payment ledger follows the existing revenue convention: returns are separate.
DAILY_SQL = """
SELECT p.point_id, p.business_date, p.business_shift_type AS shift_type,
       COUNT(*) AS records,
       COUNT(*) FILTER (WHERE p.is_return=FALSE) AS checks,
       COUNT(*) FILTER (WHERE p.is_return=TRUE) AS return_checks,
       COALESCE(SUM(CASE WHEN p.is_return THEN 0 ELSE ABS(p.amount) END), 0) AS revenue,
       COALESCE(SUM(CASE WHEN p.is_return THEN ABS(p.amount) ELSE 0 END), 0) AS returns,
       COUNT(*) FILTER (WHERE p.source='sale_total_fallback') AS fallback
FROM retail_sale_payments p
JOIN retail_sales s ON s.point_id=p.point_id AND s.sale_id=p.sale_id
WHERE s.deleted=FALSE AND p.business_date IN ($1, $2, $3)
GROUP BY p.point_id, p.business_date, p.business_shift_type
"""


def totals(rows):
    result = dict(records=0, checks=0, return_checks=0, revenue=Decimal(0), returns=Decimal(0), fallback=0)
    for row in rows:
        for key in result:
            result[key] += row[key]
    result['average'] = result['revenue'] / result['checks'] if result['checks'] else Decimal(0)
    return result


def percent(current, previous):
    if previous <= 0:
        return None
    return (current - previous) / previous * 100


def money(value):
    return f"{value:,.2f}".replace(',', ' ')


def change(current, previous, comparable=True):
    if not comparable:
        return 'не сравниваем: день не завершён'
    if not current['records'] or not previous['records']:
        return 'нет данных для сравнения'
    value = percent(current['revenue'], previous['revenue'])
    return f"{value:+.1f}%" if value is not None else 'база сравнения 0 ₽'


async def daily_report(value: str | None = None, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(ZoneInfo(settings.business_tz))
    now = now.astimezone(ZoneInfo(settings.business_tz))
    current_day = (now - timedelta(hours=settings.business_day_start_hour)).date()
    day = date.fromisoformat(saby_client.resolve_date(value)) if value else current_day - timedelta(days=1)
    if day > now.date():
        raise ValueError('Дата отчёта не может быть в будущем.')
    dates = (day, day - timedelta(days=1), day - timedelta(days=7))
    async with pool().acquire() as conn:
        async with conn.transaction(isolation='repeatable_read', readonly=True):
            stores = await conn.fetch('SELECT point_id, name FROM retail_stores ORDER BY name, point_id')
            rows = await conn.fetch(DAILY_SQL, *dates)
            latest = await conn.fetchrow('SELECT status, started_at, finished_at FROM sync_runs ORDER BY id DESC LIMIT 1')
            success = await conn.fetchrow("SELECT finished_at, date_from, date_to FROM sync_runs WHERE status='OK' ORDER BY finished_at DESC LIMIT 1")
    periods = {d: [dict(row) for row in rows if row['business_date'] == d] for d in dates}
    return dict(day=day, dates=dates, complete=day < current_day, stores=[dict(s) for s in stores],
                periods=periods, latest=dict(latest) if latest else None,
                success=dict(success) if success else None)


def format_report(data: dict) -> list[str]:
    day, previous, week = data['dates']
    periods = data['periods']
    current = totals(periods[day])
    hour = settings.business_day_start_hour
    blocks = [f"📊 Продажи — {day:%d.%m.%Y}\n"
              f"Окно: {day:%d.%m} {hour:02d}:00 → {day + timedelta(days=1):%d.%m} {hour:02d}:00\n"
              + ('День завершён; данные по состоянию последней загрузки.' if data['complete'] else '⏳ День не завершён — предварительные данные.')]
    if not current['records']:
        blocks.append('⚠️ За выбранный день нет платёжных записей. Это не подтверждает отсутствие продаж.')
    active = len({r['point_id'] for r in periods[day]})
    blocks.append(f"Сеть: данные по {active} из {len(data['stores'])} точек\n"
                  f"Выручка: {money(current['revenue'])} ₽\n"
                  f"Чеки: {current['checks']} | средний: {money(current['average'])} ₽\n"
                  f"Возвраты: {money(current['returns'])} ₽ ({current['return_checks']})\n"
                  f"Выручка к {previous:%d.%m}: {change(current, totals(periods[previous]), data['complete'])}\n"
                  f"К тому же дню недели {week:%d.%m}: {change(current, totals(periods[week]), data['complete'])}")
    for label in ('DAY', 'NIGHT', None):
        selected = [r for r in periods[day] if r['shift_type'] == label]
        if not selected and label is None:
            continue
        t = totals(selected)
        blocks.append(f"{label or 'Тип смены не определён'}: " +
                      (f"{money(t['revenue'])} ₽ | {t['checks']} чеков | возвраты {money(t['returns'])} ₽" if selected else 'нет записей'))
    for store in data['stores']:
        point = store['point_id']
        t = totals([r for r in periods[day] if r['point_id'] == point])
        if not t['records']:
            blocks.append(f"⚪ {store['name']} — нет записей за день")
            continue
        prior = totals([r for r in periods[previous] if r['point_id'] == point])
        earlier = totals([r for r in periods[week] if r['point_id'] == point])
        blocks.append(f"{store['name']}\n{money(t['revenue'])} ₽ | {t['checks']} чеков | средний {money(t['average'])} ₽\n"
                      f"Возвраты: {money(t['returns'])} ₽\n"
                      f"К {previous:%d.%m}: {change(t, prior, data['complete'])} | к {week:%d.%m}: {change(t, earlier, data['complete'])}")
    if current['fallback']:
        blocks.append(f"⚠️ Записей с суммой из продажи вместо платежа: {current['fallback']}. Проверьте /reconcile {day}.")
    latest = data['latest']
    success = data['success']
    sync_text = f"Последний запуск загрузки: {latest['status']}" if latest else 'Запусков загрузки нет.'
    if success and success['finished_at']:
        stamp = success['finished_at'].astimezone(ZoneInfo(settings.business_tz))
        sync_text += f"\nПоследний успешный: {stamp:%d.%m.%Y %H:%M} ({settings.business_tz}), период {success['date_from']} → {success['date_to']}"
    blocks.append(sync_text)
    blocks.append('Выручка без вычитания возвратов; возвраты показаны отдельно. Чеки — записи платёжного слоя Saby, как в сверке. DAY/NIGHT — по времени платежа. РЦ исключён. Отсутствие записей не равно нулевым продажам. Сравнение сети включает все доступные записи каждой даты; состав точек может отличаться. Полнота загрузки проверяется через /reconcile.')
    # Keep a store card intact and stay below Telegram's message limit.
    chunks = ['']
    for block in blocks:
        if len(chunks[-1]) + len(block) + 2 > 3800:
            chunks.append('')
        chunks[-1] += ('\n\n' if chunks[-1] else '') + block
    return chunks
