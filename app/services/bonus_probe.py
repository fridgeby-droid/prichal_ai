"""Read-only probe of a documented endpoint; sale Key/externalId mapping unverified."""
import asyncio
import json
from uuid import UUID
from datetime import datetime,timezone
import httpx

from app.db.database import pool
from app.services.saby import saby_client
from app.services.sale_tree import numeric

REFERENCE_CASES=((398,211869,'25529',1,'108'),(398,212324,'25576',-1,'36'))
MONEY_FIELDS=('availableBonusBalance','totalBonusBalance','availableBonusDec',
              'userBonusDec','availableBonusInc','bonusDec','bonusInc')


def candidate_key(raw):
    value=raw.get('Key')
    if not isinstance(value,str): return None
    try:
        parsed=UUID(value)
        return str(parsed) if parsed.int else None
    except (ValueError,AttributeError): return None


def describe(body,expected_action,expected_amount):
    if not isinstance(body,dict): return {'status':'INVALID_RESPONSE'}
    if body.get('success') is False or any(body.get(k) not in (None,False,'',[],{}) for k in ('error','errors','Error','Errors')):
        return {'status':'SOURCE_ERROR','raw_error_hidden':True}
    if body.get('bonusBalanceError') not in (None,''):
        return {'status':'BONUS_BALANCE_ERROR','raw_error_hidden':True}
    action=body.get('bonusAction')
    if type(action) is not int or action not in (-1,1):
        return {'status':'ACTION_MISSING_OR_UNSUPPORTED'}
    amounts={}
    invalid=[]
    for key in MONEY_FIELDS:
        value=numeric(body.get(key))
        if body.get(key) is not None and (value is None or value<0): invalid.append(key)
        amounts[key]=str(value) if value is not None and value>=0 else None
    if invalid: return {'status':'INVALID_AMOUNTS','fields':invalid}
    target='bonusInc' if expected_action==1 else 'bonusDec'
    actual=numeric(body.get(target))
    matches=action==expected_action and actual is not None and actual==numeric(expected_amount)
    deferred=None
    if isinstance(body.get('bonusIncDateTime'),str):
        try: deferred=datetime.fromisoformat(body['bonusIncDateTime'].replace('Z','+00:00')).isoformat()
        except ValueError: pass
    return {'status':'REFERENCE_VALUES_MATCH' if matches else 'REFERENCE_NOT_MATCHED',
            'bonus_action':action,'amounts':amounts,'bonus_inc_datetime':deferred,
            'card_id_present':body.get('cardId') is not None,
            'personal_card_id_present':body.get('personalCardId') is not None,
            'reference_value_matches':matches,
            'mapping_confirmed':False,
            'note':'Совпадение чисел — контрольный результат, не подтверждение полной истории. bonusInc может быть будущим начислением; баланс относится к моменту чтения.'}


async def probe():
    results=[]
    async with pool().acquire() as conn:
        # Only these two already confirmed local sales, not a bulk account scan.
        rows=await conn.fetch('''SELECT point_id,sale_id,business_date,raw_json FROM retail_sales
            WHERE point_id=$1 AND sale_id=ANY($2::BIGINT[]) AND NOT deleted''',398,[211869,212324])
    indexed={int(r['sale_id']):r for r in rows}
    requests=0
    for point,sale,number,action,amount in REFERENCE_CASES:
        item={'point_id':str(point),'sale_id':str(sale),'number':number,
              'expected_action':action,'expected_amount':amount,'mapping':'raw_json.Key -> externalId: hypothesis'}
        row=indexed.get(sale)
        if not row:
            results.append({**item,'status':'LOCAL_SALE_NOT_FOUND'})
            continue
        raw=json.loads(row['raw_json']) if isinstance(row['raw_json'],str) else row['raw_json']
        if not isinstance(raw,dict) or str(raw.get('Number'))!=number or str(row['business_date'])!='2026-10-23':
            results.append({**item,'status':'LOCAL_REFERENCE_MISMATCH'})
            continue
        key=candidate_key(raw)
        if key is None:
            results.append({**item,'status':'NO_UUID_CANDIDATE'})
            continue
        if requests: await asyncio.sleep(3)
        requests+=1
        try:
            body=await saby_client._get(f'/retail/order/{key}/bonus-read',{})
            outcome=describe(body,action,amount)
        except httpx.HTTPStatusError as exc:
            outcome={'status':'HTTP_ERROR','http_status':exc.response.status_code}
        except Exception as exc:
            outcome={'status':'REQUEST_FAILED','error_type':type(exc).__name__}
        results.append({**item,**outcome})
    return {'mode':'read_only_probe','fetched_at':datetime.now(timezone.utc).isoformat(),
            'complete_history':False,'facts_saved':False,'results':results,
            'note':'Только документированный GET bonus-read. UUID взят как кандидат из Key; соответствие не доказано. Нулевые/пустые/ошибочные ответы не означают отсутствие операций. Контакты, карты, UUID и секреты скрыты.'}
