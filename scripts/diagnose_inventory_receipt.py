"""Standalone read-only receipt diagnostic, compatible with installed v0.3.54.

Prints stage, numeric error codes, known reason categories and field types.
Never prints upstream free-text messages, headers, URLs or document contents.
Revision 2 normalizes document requests to the documented JSON-RPC UTF-8 media
type even when used with the original v0.3.54 inventory_sources module.
Revision 3 can inspect up to five exact candidates without selecting a winner.
Revision 4 shows compact accounting flags and attachment metadata by default.
"""
import argparse
import asyncio
import json
import re
import hashlib
import logging
from urllib.parse import urlsplit, urljoin
from xml.etree import ElementTree as ET
import httpx
from app.services import inventory_sources as source

MAX_XML_BYTES=8*1024*1024


class ProbeError(ValueError):
    pass


def checked_attachment_url(value):
    if not isinstance(value,str) or len(value)>12000 or any(ord(c)<33 for c in value) or '\\' in value:
        raise ProbeError('ATTACHMENT_URL_INVALID')
    try:
        url=urlsplit(value);host=(url.hostname or '').lower();port=url.port
    except ValueError:
        raise ProbeError('ATTACHMENT_URL_INVALID') from None
    if (url.scheme!='https' or url.username or url.password or url.fragment or port not in (None,443)
        or not any(host==domain or host.endswith('.'+domain) for domain in ('sbis.ru','saby.ru'))):
        raise ProbeError('ATTACHMENT_HOST_NOT_ALLOWED')
    return value


async def download_xml(reference):
    """Bounded read of a fresh Saby attachment link; never forward auth on redirects."""
    url=checked_attachment_url(reference)
    auth_hosts={'online.sbis.ru','online.saby.ru','api.sbis.ru','api.saby.ru'}
    used_auth=False;headers={}
    # Signed URLs and tokens must not reach httpx/httpcore logs.
    loggers=[logging.getLogger(n) for n in ('httpx','httpcore')]
    states=[log.disabled for log in loggers]
    for log in loggers:log.disabled=True
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60,connect=20),follow_redirects=False) as client:
            for attempt in range(6):
                async with client.stream('GET',url,headers=headers) as response:
                    if response.status_code in (301,302,303,307,308):
                        url=checked_attachment_url(urljoin(url,response.headers.get('location','')))
                        headers={}  # Authentication never follows a redirect.
                        continue
                    if response.status_code in (401,403) and not headers and not used_auth and urlsplit(url).hostname in auth_hosts:
                        headers={'X-SBISAccessToken':await source.saby_client._authenticate()}
                        used_auth=True
                        continue
                    if not response.is_success:raise ProbeError('ATTACHMENT_HTTP_'+str(response.status_code))
                    length=response.headers.get('content-length','')
                    if length.isdigit() and int(length)>MAX_XML_BYTES:raise ProbeError('ATTACHMENT_TOO_LARGE')
                    body=bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body)>MAX_XML_BYTES:raise ProbeError('ATTACHMENT_TOO_LARGE')
                    return bytes(body),{'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest(),
                        'authenticated_download':used_auth}
            raise ProbeError('ATTACHMENT_REDIRECT_LIMIT')
    finally:
        for log,state in zip(loggers,states):log.disabled=state


class BoundedXML(ET.TreeBuilder):
    def __init__(self):super().__init__();self.depth=0;self.count=0
    def start(self,tag,attrs):
        self.depth+=1;self.count+=1
        if self.depth>50 or self.count>100000:raise ProbeError('XML_STRUCTURE_LIMIT')
        return super().start(tag,attrs)
    def end(self,tag):
        result=super().end(tag);self.depth-=1;return result
    def doctype(self,*args):raise ProbeError('XML_DTD_FORBIDDEN')


def xml_summary(body):
    if not body or len(body)>MAX_XML_BYTES:raise ProbeError('XML_SIZE_INVALID')
    if body.startswith(b'PK'):raise ProbeError('XML_ZIP_UNSUPPORTED')
    try:
        root=ET.fromstring(body,parser=ET.XMLParser(target=BoundedXML()))
    except ProbeError:raise
    except (ET.ParseError,LookupError,ValueError):raise ProbeError('XML_PARSE_ERROR') from None
    def local(name):return name.rsplit('}',1)[-1][:100]
    shape={};warehouse=[];products=[];product_count=0
    numeric_fields={'НомСтр','КолТов','ЦенаТов','СтТовБезНДС','СтТовУчНал','ОКЕИ_Тов'}
    identity_fields={'КодТов','АртикулТов','Номенклатура','Nomenclature','NomenclatureUUID'}
    def walk(node,path=''):
        nonlocal product_count
        tag=local(node.tag);path=path+'/'+tag
        attrs={local(k):v for k,v in node.attrib.items()}
        if path in shape or len(shape)<150:
            row=shape.setdefault(path,{'path':path,'count':0,'attributes':set()})
            row['count']+=1;row['attributes'].update(attrs)
        clue=lambda text:'склад' in text.casefold() or 'warehouse' in text.casefold()
        marked=clue(tag) or any(clue(k) or clue(v) for k,v in attrs.items())
        if marked and len(warehouse)<20:
            # Only fields explicitly labelled as warehouse, not surrounding addresses/contacts.
            values={k:v[:160] for k,v in attrs.items() if clue(k)}
            labels={k:v[:160] for k,v in attrs.items() if clue(v)}
            if clue(tag):values.update({k:v[:160] for k,v in attrs.items() if k in ('Идентификатор','Код','Название','Наименование','id','name')})
            if labels:values.update({k:v[:160] for k,v in attrs.items() if k in ('Значение','Значен','ТекстИнф','value')})
            warehouse.append({'path':path,'fields':values,'labels':labels,
                'text':(node.text or '').strip()[:160] if clue(tag) else None})
        if tag in ('СведТов','Товар','Позиция','Номенклатура'):
            product_count+=1
            if len(products)<5:
                safe={k:v for k,v in attrs.items() if k in numeric_fields and re.fullmatch(r'-?\d{1,20}(?:[.,]\d{1,10})?',v)}
                safe.update({k:v[:160] for k,v in attrs.items() if k in identity_fields or k=='НаимТов'})
                safe.update({k:v[:80] for k,v in attrs.items() if k in ('НаимЕдИзм','НалСт')})
                nested=[]
                for child in node.iter():
                    if child is node:continue
                    child_attrs={local(k):v for k,v in child.attrib.items()}
                    codes={k:v[:160] for k,v in child_attrs.items() if k in identity_fields
                           and re.fullmatch(r'[\w. /:-]{1,160}',v)}
                    label=child_attrs.get('Идентиф','')
                    # Show auxiliary field names to discover semantics; values only for
                    # explicit nomenclature identities, never arbitrary additional data.
                    allowed={'НоменклатураИдентификатор','НоменклатураКод','НоменклатураUUID',
                             'Nomenclature','NomenclatureUUID','КодТов','АртикулТов'}
                    if label in allowed:
                        val=child_attrs.get('Значен','')
                        if re.fullmatch(r'[\w. /:-]{1,160}',val):codes['Значен']=val
                    if (codes or label) and len(nested)<20:
                        nested.append({'tag':local(child.tag),'label':label[:100] or None,'fields':codes})
                products.append({'path':path,'fields':safe,'attribute_names':sorted(attrs),
                                 'nested_identity_candidates':nested})
        for child in node:walk(child,path)
    walk(root)
    return {'root':local(root.tag),'schema_paths':[{**r,'attributes':sorted(r['attributes'])} for r in shape.values()],
        'schema_paths_limit':150,'warehouse_clues':warehouse,'product_node_candidates':product_count,
        'product_examples':products,'stable_saby_product_ids_confirmed':False,'warehouse_mapping_confirmed':False,
        'note':'Структура XML и примеры полей, не импорт движений. Код товара поставщика не считается ID номенклатуры Saby. Отсутствие подсказки склада не доказывает отсутствие реквизита в другом источнике.'}


async def inspect_accounting_xml(doc):
    values=doc.get('ВложениеУчета',[])
    if not isinstance(values,list) or len(values)!=1 or not isinstance(values[0],dict):
        raise ProbeError('ACCOUNTING_ATTACHMENT_NOT_UNIQUE')
    value=values[0]
    if value.get('Удален')!='Нет' or value.get('Зашифрован')!='Нет':raise ProbeError('ACCOUNTING_ATTACHMENT_UNAVAILABLE')
    if value.get('Упакован') not in ('',None,'Нет'):raise ProbeError('XML_ZIP_UNSUPPORTED')
    file=value.get('Файл')
    if not isinstance(file,dict) or not str(file.get('Имя','')).lower().endswith('.xml'):
        raise ProbeError('ACCOUNTING_ATTACHMENT_NOT_XML')
    body,meta=await download_xml(file.get('Ссылка'))
    return {**meta,**xml_summary(body)}


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
    if not isinstance(error,dict):
        return {**result,'reason':'NO_ERROR' if error is None and 'result' in data and response.is_success else 'UNCLASSIFIED'}
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


def structure(value,depth=0):
    if depth>=5:return {'type':type(value).__name__}
    if isinstance(value,dict):
        return {'type':'dict','length':len(value),'fields':{str(k)[:80]:structure(v,depth+1) for k,v in list(value.items())[:60]}}
    if isinstance(value,list):
        return {'type':'list','length':len(value),'examples':[structure(v,depth+1) for v in value[:2]]}
    return {'type':type(value).__name__}


def document_summary(doc,expected_id,include_structure=False):
    if not isinstance(doc,dict) or doc.get('Идентификатор')!=expected_id:raise ValueError('RECEIPT_ID_MISMATCH')
    if doc.get('ЧастичныеДанные')=='Да':raise ValueError('SABY_DOCUMENT_PARTIAL')
    def fields(value,names):
        if not isinstance(value,dict):return {}
        return {k:str(value[k])[:200] for k in names if type(value.get(k)) in (str,int)}
    result={'document':fields(doc,('Идентификатор','Номер','Дата','Тип','Удален')),
        'state':fields(doc.get('Состояние'),('Код','Название')),
        'department':fields(doc.get('Подразделение'),('Идентификатор','Название','Структура')),
        'regulation':fields(doc.get('Регламент'),('Идентификатор','Название')),
        'accounting_flags':fields(doc.get('Расширение'),('Проведен','Архивирован','Очищен','ЗакрытОтИзменений')),
        'posting_confirmed':False,'store_mapping_confirmed':False}
    for source_key,target_key in [('ВложениеУчета','accounting_attachments'),('Вложение','attachments')]:
        values=doc.get(source_key,[])
        if not isinstance(values,list) or not all(isinstance(v,dict) for v in values):
            raise ValueError('ATTACHMENTS_SHAPE_INVALID')
        result[target_key]=[]
        result[target_key+'_count']=len(values)
        result[target_key+'_truncated']=len(values)>30
        for value in values[:30]:
            entry=fields(value,('Идентификатор','Тип','Подтип','ВерсияФормата','ПодверсияФормата',
                'Служебный','Удален','Зашифрован','Упакован','КоличествоОшибок'))
            file=value.get('Файл')
            if isinstance(file,dict):
                name=str(file.get('Имя') or '').lower()
                extension=name.rsplit('.',1)[-1] if '.' in name else ''
                entry['file_extension']=extension if extension in ('xml','json','zip','pdf','xls','xlsx','csv','txt') else 'other'
                entry['download_reference_present']=bool(file.get('Ссылка'))
                entry['hash_present']=bool(file.get('Хеш'))
            entry['revision']=fields(value.get('Редакция'),('Номер','ДатаВремя'))
            result[target_key].append(entry)
    revisions=doc.get('Редакция',[])
    if isinstance(revisions,list):
        result['revisions']=[fields(v,('Идентификатор','Актуален','ДатаВремя')) for v in revisions[:10]]
    links=doc.get('ДокументСледствие',[])
    if isinstance(links,list):
        result['related_documents']=[{'relation':fields(v,('ВидСвязи',)),
            'document':fields(v.get('Документ'),('Идентификатор','Номер','Дата','Тип','Подтип'))}
            for v in links[:10] if isinstance(v,dict)]
    if include_structure:result['document_shape']=structure(doc)
    return result


async def diagnose(day,number,read_candidates=False,include_structure=False,inspect_xml=False):
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
        if inspect_xml and len(result.get('candidates',[]))!=1:raise ProbeError('XML_REQUIRES_ONE_EXACT_DOCUMENT')
        if read_candidates or inspect_xml:
            candidates=result.get('candidates',[])
            if len(candidates)>5:raise ValueError('TOO_MANY_CANDIDATES')
            inspected=[]
            for candidate in candidates:
                await asyncio.sleep(3)
                data=await source.request(None,{'Документ':{'Идентификатор':candidate['id']}},'СБИС.ПрочитатьДокумент')
                doc=data.get('Документ',data)
                item=document_summary(doc,candidate['id'],include_structure)
                if inspect_xml:item['accounting_xml']=await inspect_accounting_xml(doc)
                inspected.append(item)
            result['inspected_candidates']=inspected
            result['selected_document_id']=None
        return {**result,'diagnostic_revision':6,'diagnostics':diagnostics,'writes_performed':False}
    except Exception as exc:
        result={'status':'FAILED','complete_search':False,'writes_performed':False,
            'diagnostic_revision':6,'error_type':type(exc).__name__,'diagnostics':diagnostics}
        if isinstance(exc,ProbeError):result['error_code']=str(exc)
        if isinstance(exc,httpx.HTTPStatusError): result['http_status']=exc.response.status_code
        return result
    finally:
        source.saby_client._request=original


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--date',required=True);p.add_argument('--number',required=True)
    p.add_argument('--read-candidates',action='store_true')
    p.add_argument('--structure',action='store_true')
    p.add_argument('--inspect-accounting-xml',action='store_true')
    args=p.parse_args()
    result=asyncio.run(diagnose(args.date,args.number,args.read_candidates,args.structure,args.inspect_accounting_xml))
    print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
    if result.get('status')=='FAILED':raise SystemExit(1)


if __name__=='__main__':main()
