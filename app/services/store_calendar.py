"""Business dates confirmed by the owner; never inferred from first sale."""
from datetime import date, timedelta

# point_id keys stay stable when a shop is renamed.
OPENINGS = {'33446': date(2026, 8, 18), '37462': date(2026, 9, 24)}


def opening_quality(rows, shops, first, last):
    observed = {(str(r['point_id']), r['business_date']) for r in rows}
    missing, expected, anomalies = [], [], []
    for shop in shops:
        point = str(shop['point_id'])
        opening = OPENINGS.get(point)
        absent, before, unexpected = [], [], []
        for offset in range((last-first).days+1):
            day = first+timedelta(days=offset)
            present = (point, day) in observed
            if opening and day < opening:
                (unexpected if present else before).append(day.isoformat())
            elif not present:
                absent.append(day.isoformat())
        for target, values in ((missing, absent), (expected, before), (anomalies, unexpected)):
            if values:
                record={'point_id':point, 'dates':values}
                if target is not missing:
                    record['opening_date']=opening.isoformat() if opening else None
                target.append(record)
    return {'store_dates_without_records':missing, 'expected_before_opening':expected,
            'records_before_opening':anomalies}


def cohort(shops, current, previous, first, last, before_first, before_last):
    """Require observed records every date; this is not a proof of Saby completeness."""
    included, excluded = [], []
    start = min(first, before_first)
    for shop in shops:
        point = str(shop['point_id'])
        opening = OPENINGS.get(point)
        reasons = []
        if opening and opening > start:
            reasons.append('OPENED_AFTER_PERIOD_START')
        for label, rows, a, b in [('CURRENT',current,first,last),('PREVIOUS',previous,before_first,before_last)]:
            observed = {r['business_date'] for r in rows if str(r['point_id'])==point}
            if any(a+timedelta(days=i) not in observed for i in range((b-a).days+1)):
                reasons.append(label+'_DATES_WITHOUT_RECORDS')
        record = {'point_id':point, 'name':shop['name'],
                  'opening_date':opening.isoformat() if opening else None, 'reasons':reasons}
        (excluded if reasons else included).append(record)
    return included, excluded
