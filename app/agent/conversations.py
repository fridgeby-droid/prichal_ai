"""Bounded local conversation memory, isolated by chat/user/topic. No raw tool results."""
import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime,timezone,timedelta
from app.db.database import pool

TTL=timedelta(days=7)
_locks={}


def key(chat_id,user_id,topic_id=None):
    token=hashlib.sha256(f'{chat_id}:{user_id}:{topic_id or 0}'.encode()).hexdigest()
    return 'assistant:conversation:v1:'+token


@asynccontextmanager
async def serial(session):
    lock,count=_locks.get(session,(asyncio.Lock(),0))
    _locks[session]=(lock,count+1)
    try:
        async with lock:
            yield
    finally:
        _,count=_locks[session]
        if count==1: _locks.pop(session,None)
        else: _locks[session]=(lock,count-1)


def bounded(value):
    updated=datetime.fromisoformat(value['updated_at']) if value.get('updated_at') else None
    if not updated or updated.tzinfo is None or datetime.now(timezone.utc)-updated>TTL:
        return {'messages':[],'scope':{}}
    return {'messages':[{'role':m['role'],'content':str(m['content'])[:6000]}
                        for m in value.get('messages',[])[-12:] if m.get('role') in ('user','assistant')],
            'scope':{k:v for k,v in value.get('scope',{}).items() if k in ('point_id','ambiguous','last_report')}}


async def load(session):
    async with pool().acquire() as conn:
        value=await conn.fetchval('SELECT value FROM app_meta WHERE key=$1',session)
    if not value: return {'messages':[],'scope':{}}
    return bounded(json.loads(value) if isinstance(value,str) else value)


async def save(session,messages,scope):
    value={'updated_at':datetime.now(timezone.utc).isoformat(),'messages':messages[-12:],
           'scope':{k:v for k,v in scope.items() if k in ('point_id','ambiguous','last_report')}}
    value={**bounded(value),'updated_at':value['updated_at']}
    async with pool().acquire() as conn:
        await conn.execute('''INSERT INTO app_meta(key,value,updated_at) VALUES($1,$2::jsonb,now())
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()''',session,json.dumps(value,ensure_ascii=False))
        await conn.execute("DELETE FROM app_meta WHERE key LIKE 'assistant:conversation:v1:%' AND updated_at<now()-interval '7 days'")


async def clear(session):
    async with pool().acquire() as conn:
        await conn.execute('DELETE FROM app_meta WHERE key=$1',session)
