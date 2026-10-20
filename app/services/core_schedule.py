from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictStr, AwareDatetime, model_validator
from app.config import get_settings
from app.db.database import pool

settings = get_settings()
logger = logging.getLogger(__name__)
ID = Annotated[StrictStr, Field(pattern=r'^[1-9][0-9]{0,18}$')]


class Assignment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    core_assignment_id: ID
    core_employee_id: ID
    employee_name: str = Field(min_length=1, max_length=256)
    core_store_id: ID
    store_name: str = Field(min_length=1, max_length=256)
    saby_point_id: ID | None
    position: str = Field(min_length=1, max_length=64)
    work_date: date
    shift_type: Literal['DAY','NIGHT']
    slot: int = Field(ge=1, le=100, strict=True)


class Snapshot(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal[1]
    source: Literal['prichal_core']
    authority: Literal['actual_schedule']
    generated_at: AwareDatetime
    date_from: date
    date_to: date
    complete_snapshot: Literal[True]
    count: int = Field(ge=0, le=10000, strict=True)
    shifts: list[Assignment] = Field(max_length=10000)

    @model_validator(mode='before')
    @classmethod
    def require_full_snapshot(cls, data):
        if not isinstance(data, dict) or data.get('complete_snapshot') is not True or type(data.get('schema_version')) is not int:
            raise ValueError('Explicit versioned full snapshot required')
        return data

    @model_validator(mode='after')
    def check_snapshot(self):
        if not 0 <= (self.date_to-self.date_from).days <= 92:
            raise ValueError('Invalid snapshot range')
        if self.count != len(self.shifts):
            raise ValueError('Incomplete snapshot count')
        ids = set()
        for shift in self.shifts:
            if shift.core_assignment_id in ids or not self.date_from <= shift.work_date <= self.date_to:
                raise ValueError('Duplicate or out-of-range assignment')
            if shift.saby_point_id and (shift.saby_point_id=='23109' or int(shift.saby_point_id)>9223372036854775807):
                raise ValueError('Excluded or invalid Saby store')
            ids.add(shift.core_assignment_id)
        return self


def configured_source():
    url = settings.core_base_url.strip().rstrip('/')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
        raise ValueError('Задайте CORE_BASE_URL как HTTPS-адрес Core без пути, логина и параметров.')
    key = settings.core_directory_api_key.get_secret_value()
    if len(key) < 32:
        raise ValueError('CORE_DIRECTORY_API_KEY должен совпадать с Core и содержать не меньше 32 символов.')
    return url, key


def default_range():
    today = datetime.now(ZoneInfo(settings.business_tz)).date()
    return today-timedelta(days=31), today+timedelta(days=31)


async def fetch_snapshot(url, key, start, end):
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        response = await client.get(url+'/api/v1/ai/schedule', params={'date_from':str(start),'date_to':str(end)}, headers={'Authorization':'Bearer '+key})
    if response.status_code != 200:
        raise ValueError(f'Core вернул HTTP {response.status_code}; прежний график сохранён.')
    data = Snapshot.model_validate(response.json())
    if data.date_from != start or data.date_to != end:
        raise ValueError('Core вернул другой период; прежний график сохранён.')
    return data


UPSERT = """
INSERT INTO core_schedule_assignments
(source_url,core_assignment_id,core_employee_id,employee_name,core_store_id,store_name,
 saby_point_id,position,work_date,shift_type,slot,cancelled,snapshot_at)
VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,FALSE,$12)
ON CONFLICT(source_url,core_assignment_id) DO UPDATE SET
 core_employee_id=EXCLUDED.core_employee_id,employee_name=EXCLUDED.employee_name,
 core_store_id=EXCLUDED.core_store_id,store_name=EXCLUDED.store_name,saby_point_id=EXCLUDED.saby_point_id,
 position=EXCLUDED.position,work_date=EXCLUDED.work_date,shift_type=EXCLUDED.shift_type,
 slot=EXCLUDED.slot,cancelled=FALSE,snapshot_at=EXCLUDED.snapshot_at,synced_at=NOW()
"""


async def apply_snapshot(conn, url, data):
    # Caller owns a transaction and the schedule lock. Mark only the covered range.
    await conn.execute("""UPDATE core_schedule_assignments SET cancelled=TRUE,snapshot_at=$4,synced_at=NOW()
        WHERE source_url=$1 AND work_date BETWEEN $2 AND $3 AND cancelled=FALSE""",url,data.date_from,data.date_to,data.generated_at)
    if data.shifts:
        await conn.executemany(UPSERT,[(url,s.core_assignment_id,s.core_employee_id,s.employee_name,s.core_store_id,s.store_name,
            int(s.saby_point_id) if s.saby_point_id else None,s.position,s.work_date,s.shift_type,s.slot,data.generated_at) for s in data.shifts])
    missing = sorted({s.core_store_id for s in data.shifts if s.saby_point_id is None})
    status = dict(source_url=url,date_from=str(data.date_from),date_to=str(data.date_to),assignments=data.count,
                  unmapped_core_store_ids=missing,snapshot_at=data.generated_at.isoformat())
    await conn.execute("""INSERT INTO app_meta(key,value,updated_at) VALUES('core_schedule:last_success',$1::jsonb,NOW())
        ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=NOW()""",json.dumps(status))
    return status


async def sync_schedule(start=None,end=None):
    if start is None and end is None:
        start,end=default_range()
    if start is None or end is None or not 0 <= (end-start).days <= 92:
        raise ValueError('Укажите период от 1 до 93 дней.')
    url,key=configured_source()
    async with pool().acquire() as conn:
        async with conn.transaction():
            # Serialize fetch + commit across CLI, bot and automatic worker.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext('core_schedule'))")
            data=await fetch_snapshot(url,key,start,end)
            return await apply_snapshot(conn,url,data)


async def read_schedule(start,end):
    url,_=configured_source()
    if not 0 <= (end-start).days <= 92:
        raise ValueError('Укажите период от 1 до 93 дней.')
    async with pool().acquire() as conn:
        rows=await conn.fetch("""SELECT core_assignment_id,employee_name,store_name,saby_point_id,position,work_date,shift_type,slot
          FROM core_schedule_assignments WHERE source_url=$1 AND work_date BETWEEN $2 AND $3 AND cancelled=FALSE
          ORDER BY work_date,store_name,shift_type,slot,core_assignment_id""",url,start,end)
        success=await conn.fetchrow("SELECT value,updated_at FROM app_meta WHERE key='core_schedule:last_success' AND value->>'source_url'=$1",url)
    return {'rows':[dict(r) for r in rows], 'last_success':dict(success) if success else None}


async def schedule_loop():
    while True:
        try:
            if settings.core_schedule_sync_enabled:
                result=await sync_schedule()
                logger.info('Core schedule sync completed: %s',result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Core schedule sync failed; previous snapshot retained')
        await asyncio.sleep(max(5,settings.core_schedule_sync_interval_minutes)*60)
