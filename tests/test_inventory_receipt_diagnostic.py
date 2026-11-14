import asyncio
import json
import httpx
from scripts import diagnose_inventory_receipt as d


def response(code,body):
    return httpx.Response(code,json=body,request=httpx.Request('POST',d.source.DOC_URL))


def test_codes_and_safe_category_no_private_details():
    value=d.error_summary(response(500,{'error':{'code':-32000,'message':'Недостаточно прав',
        'details':'token=super-secret customer@example.com','data':{'code':'403'}}}))
    assert value['reason']=='ACCESS_DENIED'
    assert value['numeric_error_codes']=={'error.code':-32000,'data.code':403}
    assert 'secret' not in str(value) and 'customer@' not in str(value)


def test_non_json_and_rate_limit():
    r=httpx.Response(500,text='<html>secret</html>')
    assert d.error_summary(r)['response_kind']=='non_json'
    result=d.error_summary(response(500,{'error':{'message':'Метод вызывается слишком часто и будет заблокирован на 120 секунд'}}))
    assert result['reason']=='RATE_LIMIT' and result['cooldown_seconds']==120


def test_unknown_error_not_guessed_or_echoed():
    result=d.error_summary(response(500,{'error':{'code':'token-secret','message':'user data'}}))
    assert result['reason']=='UNCLASSIFIED' and result['numeric_error_codes']=={}
    assert 'user data' not in str(result)


def test_stage_captured_and_transport_restored(monkeypatch):
    async def auth():return 'test-token'
    async def request(*args,**kw):return response(500,{'error':{'code':-32602,'message':'Invalid params'}})
    monkeypatch.setattr(d.source.saby_client,'_authenticate',auth)
    monkeypatch.setattr(d.source.saby_client,'_request',request)
    result=asyncio.run(d.diagnose('2026-11-13','УТ-121230'))
    assert result['status']=='FAILED' and result['diagnostics'][0]['stage']=='list_receipts'
    assert result['diagnostics'][0]['reason']=='INVALID_PARAMETERS'
    assert d.source.saby_client._request is request


def test_http_200_rpc_error_diagnostic_retained(monkeypatch):
    async def auth():return 'test-token'
    async def request(*args,**kw):return response(200,{'error':{'code':-32601,'message':'Method not found'}})
    monkeypatch.setattr(d.source.saby_client,'_authenticate',auth)
    monkeypatch.setattr(d.source.saby_client,'_request',request)
    result=asyncio.run(d.diagnose('2026-11-13','УТ-121230'))
    assert result['status']=='FAILED' and result['diagnostics'][0]['reason']=='METHOD_NOT_FOUND'


def test_document_request_wire_bytes_and_charset(monkeypatch):
    sent=[]
    async def auth():return 'secret-token'
    async def request(method,url,**kw):
        sent.append(kw)
        return response(200,{'result':{'Документ':[],'Навигация':{'ЕстьЕще':'Нет'}}})
    monkeypatch.setattr(d.source.saby_client,'_authenticate',auth)
    monkeypatch.setattr(d.source.saby_client,'_request',request)
    result=asyncio.run(d.diagnose('2026-11-13','УТ-121230'))
    assert result['status']=='NOT_FOUND' and result['diagnostic_revision']==5
    wire=httpx.Request('POST',d.source.DOC_URL,**sent[0])
    assert wire.headers['content-type']=='application/json-rpc;charset=utf-8'
    assert int(wire.headers['content-length'])==len(wire.content)
    payload=json.loads(wire.content.decode('utf-8'))
    assert payload['params']['Фильтр']['Маска']=='УТ-121230'
    assert 'secret-token' not in str(result)


def test_revision2_works_with_original_json_transport(monkeypatch):
    seen=[]
    async def old_source(day,number):
        r=await d.source.saby_client._request('POST',d.source.DOC_URL,
            headers={'X-SBISAccessToken':'test'},json={'jsonrpc':'2.0','id':1,
                'method':'СБИС.СписокДокументов','params':{'Фильтр':{'Маска':number}}})
        r.raise_for_status()
        return {'status':'FOUND'}
    async def transport(*args,**kwargs):
        seen.append(kwargs);return response(200,{'result':{}})
    monkeypatch.setattr(d.source,'find_receipt',old_source)
    monkeypatch.setattr(d.source.saby_client,'_request',transport)
    result=asyncio.run(d.diagnose('2026-11-13','УТ-121230'))
    assert result['status']=='FOUND' and 'json' not in seen[0]
    assert json.loads(seen[0]['content'])['params']['Фильтр']['Маска']=='УТ-121230'


def test_parse_code_not_mislabeled_as_proven_encoding_error():
    result=d.error_summary(response(500,{'error':{'code':-32700,'message':'unrecognized source error'}}))
    assert result['rpc_code_category']=='PARSE_ERROR'
    assert result['reason']=='UNCLASSIFIED'


def test_two_documents_read_without_selecting_by_state(monkeypatch):
    calls=[]
    async def find(day,number):return {'status':'FOUND','candidates':[{'id':'a'},{'id':'b'}]}
    async def read(path,params,method):
        uid=params['Документ']['Идентификатор'];calls.append(uid)
        return {'Идентификатор':uid,'Состояние':{'Название':'Выполнение завершено успешно'},
            'Подразделение':{'Идентификатор':'store','Название':'Батумская'},
            'Контрагент':{'ИНН':'private-tax-id'},'Вложение':[{'Файл':{'Ссылка':'https://private'}}]}
    async def sleep(n):pass
    monkeypatch.setattr(d.source,'find_receipt',find)
    monkeypatch.setattr(d.source,'request',read)
    monkeypatch.setattr(d.asyncio,'sleep',sleep)
    result=asyncio.run(d.diagnose('2026-11-13','УТ-12130',True))
    assert calls==['a','b'] and result['selected_document_id'] is None
    assert len(result['inspected_candidates'])==2
    assert all(not r['posting_confirmed'] for r in result['inspected_candidates'])
    assert 'private-tax-id' not in str(result) and 'https://private' not in str(result)
    assert result['inspected_candidates'][0]['department']['Название']=='Батумская'


def test_partial_or_wrong_document_not_accepted():
    import pytest
    for doc in ({'Идентификатор':'wrong'},{'Идентификатор':'a','ЧастичныеДанные':'Да'}):
        with pytest.raises(ValueError):d.document_summary(doc,'a')


def test_success_is_not_an_unclassified_error():
    assert d.error_summary(response(200,{'result':{}}))['reason']=='NO_ERROR'


def test_accounting_metadata_without_links_or_private_filename():
    doc={'Идентификатор':'a','Расширение':{'Проведен':'Да','secret':'token'},
        'ВложениеУчета':[{'Идентификатор':'attachment','Тип':'Учёт','Удален':'Нет',
            'Файл':{'Имя':'personal-tax-id.xml','Ссылка':'https://secret','Хеш':'private-hash'}}]}
    result=d.document_summary(doc,'a')
    assert result['accounting_flags']=={'Проведен':'Да'}
    assert result['accounting_attachments'][0]['file_extension']=='xml'
    assert result['accounting_attachments'][0]['download_reference_present'] is True
    assert result['posting_confirmed'] is False
    assert 'document_shape' not in result
    for private in ('token','personal-tax-id','https://secret','private-hash'):assert private not in str(result)


def test_xml_structure_and_product_fields_do_not_expose_contacts():
    raw='<Файл><Документ><Продавец ИНН="private" Телефон="secret"/><Склад Код="42" Название="Камская 2/1"/><ТаблСчФакт><СведТов НомСтр="1" НаимТов="Товар" КолТов="2" КодТов="supplier123"/></ТаблСчФакт></Документ></Файл>'
    result=d.xml_summary(raw.encode())
    assert result['product_node_candidates']==1 and result['stable_saby_product_ids_confirmed'] is False
    assert result['warehouse_clues'][0]['fields']['Название']=='Камская 2/1'
    assert 'private' not in str(result) and 'secret' not in str(result)


def test_xml_cp1251_and_utf16():
    for encoding in ('windows-1251','utf-16'):
        body=('<?xml version="1.0" encoding="'+encoding+'"?><Файл><СведТов КолТов="1"/></Файл>').encode(encoding)
        assert d.xml_summary(body)['product_node_candidates']==1


def test_dtd_rejected_even_utf16():
    import pytest
    for encoding in ('utf-8','utf-16'):
        raw=('<?xml version="1.0" encoding="'+encoding+'"?><!DOCTYPE x [<!ENTITY y "secret">]><x>&y;</x>').encode(encoding)
        with pytest.raises(d.ProbeError,match='DTD_FORBIDDEN'):d.xml_summary(raw)


def test_attachment_hosts_reject_ssrf_and_credentials():
    import pytest
    for url in ('http://online.sbis.ru/x','https://127.0.0.1/x','https://sbis.ru.attacker.com/x',
        'https://user:pass@online.sbis.ru/x','https://online.sbis.ru:8443/x','https://evil.com/x'):
        with pytest.raises(d.ProbeError):d.checked_attachment_url(url)
    assert d.checked_attachment_url('https://online.sbis.ru/file?token=secret').startswith('https://')


def test_xml_depth_and_archive_limits():
    import pytest
    for body,code in [(b'PK123','ZIP_UNSUPPORTED'),(b'<a>'*52+b'</a>'*52,'STRUCTURE_LIMIT')]:
        with pytest.raises(d.ProbeError,match=code):d.xml_summary(body)


def test_download_redirect_does_not_forward_token(monkeypatch):
    seen=[]
    def handler(req):
        seen.append((req.url.host,req.headers.get('X-SBISAccessToken')))
        if len(seen)==1:return httpx.Response(401)
        if len(seen)==2:return httpx.Response(302,headers={'Location':'https://files.sbis.ru/file'})
        return httpx.Response(200,content=b'<root/>')
    real=httpx.AsyncClient
    monkeypatch.setattr(d.httpx,'AsyncClient',lambda **kw:real(**kw,transport=httpx.MockTransport(handler)))
    async def auth():return 'private-token'
    monkeypatch.setattr(d.source.saby_client,'_authenticate',auth)
    body,meta=asyncio.run(d.download_xml('https://online.sbis.ru/file'))
    assert seen==[('online.sbis.ru',None),('online.sbis.ru','private-token'),('files.sbis.ru',None)]
    assert body==b'<root/>' and meta['authenticated_download'] is True


def test_ambiguous_document_never_downloads_xml(monkeypatch):
    async def find(*args):return {'candidates':[{'id':'a'},{'id':'b'}]}
    monkeypatch.setattr(d.source,'find_receipt',find)
    result=asyncio.run(d.diagnose('2026-11-13','14537',inspect_xml=True))
    assert result['error_code']=='XML_REQUIRES_ONE_EXACT_DOCUMENT'
