from types import SimpleNamespace
import httpx
from scripts import catalog_categories as cli


def test_http_error_extracts_message_but_not_credentials(monkeypatch):
    monkeypatch.setattr(cli,'get_settings',lambda:SimpleNamespace(
        saby_app_client_id='client-private',saby_app_secret='app-private',saby_secret_key='secret-private'))
    req=httpx.Request('GET','https://api.sbis.ru/retail/v2/nomenclature/list',headers={'X-SBISAccessToken':'header-private'})
    response=httpx.Response(500,request=req,json={'error':{
        'code':123,'message':'Missing pointId; app-private header-private https://hidden/path?key=value',
        'details':'secret=another-password','stack':'SHOULD NOT PRINT','data':{'token':'SHOULD NOT PRINT'}}})
    exc=httpx.HTTPStatusError('SHOULD NOT PRINT',request=req,response=response)
    result=cli.safe_http_error(exc)
    assert result['error']['code']==123
    assert 'Missing pointId' in result['error']['message']
    text=str(result)
    for secret in ('app-private','header-private','hidden/path','another-password','SHOULD NOT PRINT'):
        assert secret not in text


def test_non_json_never_echoes_body():
    req=httpx.Request('GET','https://api.sbis.ru/')
    response=httpx.Response(500,request=req,text='<html>private dump</html>')
    exc=httpx.HTTPStatusError('private',request=req,response=response)
    assert cli.safe_http_error(exc)=={'http_status':500,'response_format':'non_json'}
