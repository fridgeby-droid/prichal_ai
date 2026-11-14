"""Read-only, documented Saby endpoints. No document execution or attachment downloads."""
import asyncio
from datetime import date, datetime, timezone
from app.services.saby import saby_client, API_BASE, _response_shape
from app.services.sale_tree import numeric
from app.services.price_lists import identifier

DOC_URL = 'https://online.sbis.ru/service/?srv=1'


async def request(path, params=None, method=None):
    allowed = {'/retail/company/list', '/retail/company/warehouses', '/retail/nomenclature/balances'}
    if method not in (None, 'СБИС.СписокДокументов', 'СБИС.ПрочитатьДокумент'):
        raise ValueError('READ_ONLY_METHOD_REQUIRED')
    if method is None and path not in allowed: raise ValueError('READ_ONLY_PATH_REQUIRED')
    token = await saby_client._authenticate()
    headers = {'X-SBISAccessToken': token, 'User-Agent': 'PrichalAI inventory read-only'}
    if method:
        response = await saby_client._request('POST', DOC_URL, headers=headers,
            json={'jsonrpc':'2.0','id':1,'method':method,'params':params})
    else:
        response = await saby_client._request('GET', API_BASE+path, headers=headers, params=params or {})
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and (data.get('error') or data.get('errors') or data.get('success') is False):
        raise ValueError('SABY_SOURCE_ERROR')
    if method:
        if not isinstance(data, dict) or not isinstance(data.get('result'), dict):
            raise ValueError('SABY_RPC_RESPONSE_INVALID')
        data = data['result']
        if data.get('ЧастичныеДанные') == 'Да': raise ValueError('SABY_DOCUMENT_PARTIAL')
    return data


def balances(data, wanted):
    """Fail closed on envelopes/omissions not yet verified on this account."""
    if isinstance(data, dict):
        if data.get('error') or data.get('errors') or data.get('success') is False:
            raise ValueError('SABY_SOURCE_ERROR')
        outcome=data.get('outcome')
        if outcome is not None and (not isinstance(outcome,dict) or outcome.get('hasMore') is not False):
            raise ValueError('STOCK_RESPONSE_INCOMPLETE')
        data=data.get('balances')
    if not isinstance(data,list): raise ValueError('STOCK_RESPONSE_SHAPE_UNCONFIRMED')
    found={}
    for row in data:
        if not isinstance(row,dict): raise ValueError('STOCK_RESPONSE_INVALID')
        pid=identifier(row.get('nomenclature')); amount=numeric(row.get('balance'))
        if amount is None or pid not in wanted or pid in found: raise ValueError('STOCK_RESPONSE_INVALID')
        found[pid]=str(amount)
    if set(found)!=set(wanted): raise ValueError('STOCK_RESPONSE_INCOMPLETE')
    return found


async def fetch_balances(company,warehouse,products):
    company,warehouse=identifier(company),identifier(warehouse)
    wanted=sorted({identifier(p) for p in products})
    if not wanted or len(wanted)>10000: raise ValueError('Укажите 1–10000 ID товаров')
    result={};started=datetime.now(timezone.utc).isoformat()
    for offset in range(0,len(wanted),25):
        batch=wanted[offset:offset+25]
        # Array query parameters are encoded as repeated keys by httpx.
        data=await request('/retail/nomenclature/balances',{'companies':[int(company)],
            'warehouses':[int(warehouse)],'nomenclatures':[int(p) for p in batch]})
        result.update(balances(data,batch))
        if offset+25<len(wanted): await asyncio.sleep(3)
    return {'company_id':company,'warehouse_id':warehouse,'started_at':started,
        'observed_at':datetime.now(timezone.utc).isoformat(),'complete_requested_scope':True,
        'all_warehouse_products':False,'balances':result}


def document_page(data):
    rows=data.get('Документ');nav=data.get('Навигация')
    if (data.get('ЧастичныеДанные')=='Да' or not isinstance(rows,list)
        or not all(isinstance(r,dict) for r in rows) or not isinstance(nav,dict)
        or nav.get('ЕстьЕще') not in ('Да','Нет')): raise ValueError('RECEIPTS_PAGE_INVALID')
    more=nav['ЕстьЕще']=='Да'
    if more and not rows: raise ValueError('RECEIPTS_PAGE_INVALID')
    return rows,more


async def find_receipt(day,number):
    stamp=date.fromisoformat(day).strftime('%d.%m.%Y')
    if not number.strip() or len(number)>100: raise ValueError('Нужен номер документа')
    matched={};seen=set()
    for page in range(100):
        data=await request(None,{'Фильтр':{'Тип':'ДокОтгрВх','ДатаС':stamp,'ДатаПо':stamp,
            'Маска':number,'Навигация':{'РазмерСтраницы':'50','Страница':str(page)}}},'СБИС.СписокДокументов')
        rows,more=document_page(data)
        for row in rows:
            uid=row.get('Идентификатор')
            if not isinstance(uid,str) or not uid or uid in seen: raise ValueError('RECEIPTS_CURSOR_INVALID')
            seen.add(uid)
            if row.get('Номер')==number and row.get('Дата')==stamp and row.get('Удален')!='Да':
                matched[uid]={'id':uid,'number':number,'date':day,'type':row.get('Тип'),
                    'state':row.get('Состояние',{}).get('Название') if isinstance(row.get('Состояние'),dict) else None}
        if not more: break
        await asyncio.sleep(3)
    else: raise ValueError('RECEIPTS_PAGE_LIMIT')
    results=list(matched.values())
    if len(results)==1:
        data=await request(None,{'Документ':{'Идентификатор':results[0]['id']}},'СБИС.ПрочитатьДокумент')
        doc=data.get('Документ',data)
        if not isinstance(doc,dict) or doc.get('Идентификатор')!=results[0]['id']:
            raise ValueError('RECEIPT_ID_MISMATCH')
        if doc.get('ЧастичныеДанные')=='Да': raise ValueError('SABY_DOCUMENT_PARTIAL')
        results[0]['document_shape']=_response_shape(doc)
    return {'status':'FOUND' if results else 'NOT_FOUND','candidates':results,
        'complete_search':True,'posting_confirmed':False,'store_mapping_confirmed':False,
        'note':'Совпадение номера не подтверждает склад и проведение. Вложения и секреты не выводятся.'}
