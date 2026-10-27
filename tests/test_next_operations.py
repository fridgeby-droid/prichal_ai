import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from app.db import database as db
from app.services import price_lists as prices
from app import price_lists_api as api
from scripts import price_images,bonuses


def test_read_only_connect_never_prepares_schema(monkeypatch):
    pool=object()
    monkeypatch.setattr(db,'_pool',None)
    monkeypatch.setattr(db.asyncpg,'create_pool',AsyncMock(return_value=pool))
    asyncio.run(db.init_db(prepare_schema=False))
    assert db._pool is pool


def test_timeout_reports_connect_stage(monkeypatch):
    monkeypatch.setattr(bonuses,'init_db',AsyncMock(side_effect=TimeoutError()))
    monkeypatch.setattr(bonuses,'close_db',AsyncMock())
    result=asyncio.run(bonuses.run(SimpleNamespace(action='plan')))
    assert result['stage']=='database_connect' and result['status']=='FAILED'


def test_image_retry_never_fetches_catalog(monkeypatch):
    fetch=AsyncMock(side_effect=[prices.ExportError('SABY_IMAGE_UPSTREAM_ERROR'),(b'png','image/png','sha')])
    monkeypatch.setattr(prices,'_download_image_once',fetch)
    monkeypatch.setattr(prices.asyncio,'sleep',AsyncMock())
    listing=AsyncMock()
    monkeypatch.setattr(prices,'list_prices',listing)
    assert asyncio.run(prices.download_image('reference'))[0]==b'png'
    assert fetch.await_count==2
    listing.assert_not_called()


def test_image_error_contract():
    response=api.image_failed('SABY_IMAGE_UPSTREAM_ERROR',502)
    data=json.loads(response.body)
    assert data['error']['retry_scope']=='image' and data['error']['refetch_price_list'] is False
    assert response.headers['retry-after']=='30'
    assert not json.loads(api.image_failed('SABY_IMAGE_TYPE_INVALID',502).body)['error']['retryable']


def test_manifest_resume_skips_downloaded_files(monkeypatch,tmp_path):
    manifest=tmp_path/'images.json'
    manifest.write_text(json.dumps({'images':{
        'one':{'status':'PENDING','download_path':'/api/v1/core/price-list-images/one'},
        'two':{'status':'PENDING','download_path':'/api/v1/core/price-list-images/two'}}}))
    import hashlib
    image=b'image'
    checksum=hashlib.sha256(image).hexdigest()
    download=AsyncMock(side_effect=[(image,'image/png',checksum),prices.ExportError('SABY_IMAGE_UPSTREAM_ERROR'),
                                    (image,'image/png',checksum)])
    monkeypatch.setattr(price_images,'download_image',download)
    monkeypatch.setattr(price_images.asyncio,'sleep',AsyncMock())
    catalog=AsyncMock()
    monkeypatch.setattr(price_images,'get_price',catalog)
    args=SimpleNamespace(action='download',manifest=str(manifest),output=str(tmp_path/'files'),recheck=False)
    first=asyncio.run(price_images.run(args));second=asyncio.run(price_images.run(args))
    assert first['status']=='PARTIAL_ERRORS' and second['status']=='COMPLETED'
    assert second['skipped']==1 and download.await_count==3
    assert download.await_args_list[-1].args==('two',)
    catalog.assert_not_called()
