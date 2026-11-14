"""Standalone read-only receipt diagnostic, compatible with installed v0.3.54.

Prints stage, numeric error codes, known reason categories and field types.
Never prints upstream free-text messages, headers, URLs or document contents.
Revision 2 normalizes document requests to the documented JSON-RPC UTF-8 media
type even when used with the original v0.3.54 inventory_sources module.
"""
import argparse
import asyncio
import json
import re
import httpx
from app.services import inventory_sources as source


def error_summary(response):
    result={'http_status':response.status_code}
    try:
        data=response.json()
    except ValueError:
        return {**result,'response_kind':'non_json','reason':'UNCLASSIFIED'}
    result['response_kind']='json'
    result['response_type']=type(data).__name__
    if not isinstance(data,dict): return {**result,'reason':'UNCLASSIFIED'}
    error=data.get('error')
    result['error_present']=error is not None
    result['result_present']='result' in data
    if not isinstance(error,dict): return {**result,'reason':'UNCLASSIFIED'}
    result['error_fields']={key:type(error[key]).__name__ for key in
        ('code','message','details','data','type') if key in error}
    codes={}
    for prefix,entry in [('error',error),('data',error.get('data'))]:
        if not isinstance(entry,dict): continue
        for key in ('code','error_code','errorCode'):
            value=entry.get(key)
            if type(value) is int or isinstance(value,str) and re.fullmatch(r'-?\d{1,10}',value):
                codes[prefix+'.'+key]=int(value)
    result['numeric_error_codes']=codes
    result['rpc_code_category']={-32700:'PARSE_ERROR',-32600:'INVALID_REQUEST',
        -32601:'METHOD_NOT_FOUND',-32602:'INVALID_PARAMETERS',-32603:'INTERNAL_ERROR'}.get(codes.get('error.code'),'OTHER')
    # Inspect bounded text only for known categories. Never echo arbitrary messages.
    text=' '.join(str(error.get(k) or '')[:12000] for k in ('message','details','data')).casefold()
    categories=[
        ('RATE_LIMIT',('слишком часто','too many requests','rate limit')),
        ('ACCESS_DENIED',('недостаточно прав','нет прав','доступ запрещ','access denied','permission denied','forbidden')),
        ('AUTHENTICATION',('не авториз','неавториз','недействительный токен','invalid token','unauthorized','authentication')),
        ('METHOD_NOT_FOUND',('method not found','метод не найден','неизвестный метод')),
        ('INVALID_PARAMETERS',('invalid params','invalid parameter','неверные параметры','некорректные параметры')),
        ('PARSE_ERROR',('parse error','ошибка разбора','ошибка парсинга','не удалось разобрать')),
        ('INTERNAL_SERVER_ERROR',('internal server error','внутренняя ошибка сервера')),
    ]
    result['reason']=next((name for name,markers in categories if any(m in text for m in markers)),'UNCLASSIFIED')
    if result['reason']=='RATE_LIMIT':
        match=re.search(r'(?:на\s+|время блокировки:\s*)(\d{1,6})\s*(?:секунд|\.)',text)
        if match: result['cooldown_seconds']=int(match.group(1))
    return result


async def diagnose(day,number):
    diagnostics=[]
    original=source.saby_client._request
    async def traced(method,url,**kwargs):
        payload={}
        if url==source.DOC_URL:
            payload=kwargs.pop('json',None)
            if payload is None:
                content=kwargs.get('content',b'{}')
                payload=json.loads(content)
            if not isinstance(payload,dict) or payload.get('method') not in ('СБИС.СписокДокументов','СБИС.ПрочитатьДокумент'):
                raise ValueError('READ_ONLY_METHOD_REQUIRED')
            headers=dict(kwargs.get('headers') or {})
            headers={k:v for k,v in headers.items() if k.lower() not in ('content-type','content-length')}
            headers['Content-Type']='application/json-rpc;charset=utf-8'
            kwargs['headers']=headers
            kwargs['content']=json.dumps(payload,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
        response=await original(method,url,**kwargs)
        if url==source.DOC_URL:
            action=payload.get('method')
            stage={'СБИС.СписокДокументов':'list_receipts','СБИС.ПрочитатьДокумент':'read_receipt'}.get(action,'other')
            page=payload.get('params',{}).get('Фильтр',{}).get('Навигация',{}).get('Страница')
            diagnostics.append({'stage':stage,'page':int(page) if str(page).isdigit() else None,
                'transport_profile':'JSON_RPC_UTF8','request_json_valid':True,**error_summary(response)})
        return response
    source.saby_client._request=traced
    try:
        result=await source.find_receipt(day,number)
        return {**result,'diagnostic_revision':2,'diagnostics':diagnostics,'writes_performed':False}
    except Exception as exc:
        result={'status':'FAILED','complete_search':False,'writes_performed':False,
            'diagnostic_revision':2,'error_type':type(exc).__name__,'diagnostics':diagnostics}
        if isinstance(exc,httpx.HTTPStatusError): result['http_status']=exc.response.status_code
        return result
    finally:
        source.saby_client._request=original


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--date',required=True);p.add_argument('--number',required=True)
    args=p.parse_args()
    result=asyncio.run(diagnose(args.date,args.number))
    print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
    if result.get('status')=='FAILED':raise SystemExit(1)


if __name__=='__main__':main()
