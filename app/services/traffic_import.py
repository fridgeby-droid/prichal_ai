"""Offline TrafficBot event validation. No inferred shop, uptime or conversion."""
import csv
import hashlib
import io
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def parse(data, counter_id, point_id, timezone, stream_id):
    if not counter_id.strip() or not stream_id.strip() or not str(point_id).isdecimal() or int(point_id)<=0:
        raise ValueError('Нужны counter_id, положительный point_id и stream_id (серия без сброса ID)')
    zone=ZoneInfo(timezone)
    if len(data)>25*1024*1024:
        raise ValueError('Файл превышает 25 MiB')
    try:
        text=data.decode('utf-8-sig'); encoding='utf-8-sig'
    except UnicodeDecodeError:
        text=data.decode('cp1251'); encoding='cp1251'
    if not text.strip(): raise ValueError('Пустой файл без заголовка')
    delimiter='\t' if '\t' in text.splitlines()[0] else ';' if ';' in text.splitlines()[0] else ','
    reader=csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if reader.fieldnames!=['Дата','Время','Событие','ID']:
        raise ValueError('Ожидаются столбцы Дата, Время, Событие, ID')
    events={}; duplicates=0
    for line,r in enumerate(reader,2):
        if None in r or any(v is None for v in r.values()):
            raise ValueError(f'Некорректная строка {line}')
        direction={'вход':'IN','выход':'OUT'}.get(r['Событие'].strip().lower())
        if not direction or not r['ID'].strip().isdecimal():
            raise ValueError(f'Неизвестное событие или ID, строка {line}')
        stamp=datetime.strptime(r['Дата'].strip()+' '+r['Время'].strip(),'%d.%m.%Y %H:%M:%S')
        aware=stamp.replace(tzinfo=zone)
        # Refuse ambiguous or nonexistent wall times rather than guessing DST fold.
        if aware.utcoffset()!=stamp.replace(tzinfo=zone,fold=1).utcoffset() or aware.astimezone(ZoneInfo('UTC')).astimezone(zone).replace(tzinfo=None)!=stamp:
            raise ValueError(f'Неоднозначное местное время, строка {line}')
        source_id=str(int(r['ID']))
        event=dict(counter_id=counter_id,point_id=str(int(point_id)),stream_id=stream_id,source_event_id=source_id,
                   occurred_at=aware.isoformat(),direction=direction,
                   business_date=(stamp-timedelta(hours=8)).date().isoformat())
        if source_id in events:
            if event!=events[source_id]:
                raise ValueError(f'Конфликт повторного ID, строка {line}; возможен сброс счётчика')
            duplicates+=1
        else: events[source_id]=event
    values=sorted(events.values(),key=lambda e:(e['occurred_at'],int(e['source_event_id'])))
    counts=Counter(e['direction'] for e in values)
    return dict(schema_version=1,status='VALIDATED_NOT_IMPORTED',file_sha256=hashlib.sha256(data).hexdigest(),
        encoding=encoding,delimiter=repr(delimiter),timezone=timezone,events=values,event_count=len(values),
        duplicate_rows=duplicates,entries=counts['IN'],exits=counts['OUT'],
        dates=sorted({e['occurred_at'][:10] for e in values}),coverage_confirmed=False,conversion=None,
        note='Входы/выходы — события, не уникальные люди. Нет данных о непрерывности работы; пустые интервалы не нули. Привязка магазина задана оператором.')
