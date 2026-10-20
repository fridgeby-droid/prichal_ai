import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from app import price_lists_api as api
from app.services import price_lists as s


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    s._image_cache.clear()
    config = SimpleNamespace(core_price_lists_api_key=SecretStr("p"*40),
                             saby_price_list_point_ids="23109",
                             price_lists_max_pages=2000, business_tz="Asia/Yekaterinburg",
                             price_lists_timeout_seconds=10)
    monkeypatch.setattr(s, "get_settings", lambda: config)
    monkeypatch.setattr(api, "get_settings", lambda: config)
    return config


def page(rows, more=False, key="nomenclatures"):
    return {key: rows, "outcome": {"hasMore": more}}


def product(n=1, **changes):
    return dict(id=n, hierarchicalId=n, isParent=False, name="Товар", unit="кг",
                images=["img?params=abc"], **changes)


def listing():
    return page([{"id": 66, "name": "Поставщик"}], key="priceLists")


def mock_pages(monkeypatch, pages):
    fetch = AsyncMock(side_effect=[listing(), *pages, listing()])
    monkeypatch.setattr(s.saby_client, "_get", fetch)
    return fetch


def test_cursor_folders_and_full_snapshot(monkeypatch):
    fetch = mock_pages(monkeypatch, [
        page([product(1), {"id": 2, "hierarchicalId": 2, "isParent": True}], True),
        page([product(3)])])
    data = asyncio.run(s.get_price("23109", "66"))
    assert data["complete_snapshot"] and data["product_count"] == 2
    assert data["folders_skipped"] == 1
    assert data["products"][0]["id"] == "1"
    assert fetch.call_args_list[2].args[1]["position"] == 2
    assert fetch.call_args_list[1].args[1]["priceListId"] == 66
    assert fetch.call_args_list[1].args[1]["noStopList"] is False
    assert "cost" not in data["products"][0]


def test_over_thousand_cursor(monkeypatch):
    pages = [page([product(n)], more=n < 1002) for n in range(1, 1003)]
    mock_pages(monkeypatch, pages)
    assert asyncio.run(s.get_price("23109", "66"))["product_count"] == 1002


def test_live_nullable_leaves_and_folder_cursor(monkeypatch):
    leaf = product(2)
    leaf.update(isParent=None, images=None)
    pictured = product(3)
    pictured["isParent"] = None
    folder = dict(id=None, name="Раздел", unit=None, images=None,
                  isParent=True, hierarchicalId=4)
    fetch = mock_pages(monkeypatch, [
        page([leaf, folder], True), page([pictured]),
    ])
    result = asyncio.run(s.get_price("23109", "66"))
    assert result["complete_snapshot"] is True
    assert result["product_count"] == 2 and result["folders_skipped"] == 1
    assert result["products"][0]["images"] == []
    assert len(result["products"][1]["images"]) == 1
    assert fetch.call_args_list[2].args[1]["position"] == 4


@pytest.mark.parametrize("change", [
    {"id": None}, {"unit": None}, {"name": ""},
    {"isParent": "false"}, {"isParent": 0},
])
def test_nullable_leaf_still_requires_valid_product(monkeypatch, change):
    row = product()
    row.update(isParent=None)
    row.update(change)
    mock_pages(monkeypatch, [page([row])])
    with pytest.raises(s.ExportError):
        asyncio.run(s.get_price("23109", "66"))


def test_missing_parent_marker_rejected(monkeypatch):
    row = product()
    del row["isParent"]
    mock_pages(monkeypatch, [page([row])])
    with pytest.raises(s.ExportError):
        asyncio.run(s.get_price("23109", "66"))


@pytest.mark.parametrize("bad", [
    {}, {"nomenclatures": []}, page({}, False), page([], True),
    {"nomenclatures": [], "outcome": {"hasMore": "false"}},
    {**page([]), "error": "failed"}, page([{"id": 1}]),
])
def test_bad_or_incomplete_never_empty(monkeypatch, bad):
    mock_pages(monkeypatch, [bad])
    with pytest.raises(s.ExportError):
        asyncio.run(s.get_price("23109", "66"))


def test_empty_and_repeat_and_unit_image_changes(monkeypatch):
    mock_pages(monkeypatch, [page([])])
    empty = asyncio.run(s.get_price("23109", "66"))
    assert empty["complete_snapshot"] and empty["products"] == []
    revisions = []
    for unit, image in [("кг", "abc"), ("кг", "abc"), ("шт", "abc"), ("шт", "def")]:
        row = product()
        row.update(unit=unit, images=["img?params=" + image])
        mock_pages(monkeypatch, [page([row])])
        result = asyncio.run(s.get_price("23109", "66"))
        revisions.append(result["revision"])
        assert result["products"][0]["id"] == "1"
    assert revisions[0] == revisions[1]
    assert len(set(revisions)) == 3


def test_overlap_and_page_limit(monkeypatch, settings):
    mock_pages(monkeypatch, [page([product()], True), page([product()])])
    with pytest.raises(s.ExportError, match="OVERLAP"):
        asyncio.run(s.get_price("23109", "66"))
    settings.price_lists_max_pages = 1
    mock_pages(monkeypatch, [page([product()], True)])
    with pytest.raises(s.ExportError, match="PAGE_LIMIT"):
        asyncio.run(s.get_price("23109", "66"))


def test_price_listing_pagination_and_missing_price(monkeypatch):
    fetch = AsyncMock(side_effect=[page([{"id": 65, "name": "A"}], True, "priceLists"), listing()])
    monkeypatch.setattr(s.saby_client, "_get", fetch)
    data = asyncio.run(s.list_prices("23109"))
    assert len(data["price_lists"]) == 2 and fetch.call_args.args[1]["page"] == 1
    monkeypatch.setattr(s.saby_client, "_get", AsyncMock(return_value=listing()))
    with pytest.raises(s.ExportError, match="NOT_AVAILABLE"):
        asyncio.run(s.get_price("23109", "999"))


def test_removed_price_during_export(monkeypatch):
    monkeypatch.setattr(s.saby_client, "_get", AsyncMock(side_effect=[
        listing(), page([]), page([], key="priceLists")]))
    with pytest.raises(s.ExportError, match="CHANGED"):
        asyncio.run(s.get_price("23109", "66"))


def test_second_page_network_failure_does_not_return_partial(monkeypatch):
    mock_pages(monkeypatch, [page([product()], True), httpx.ReadError("interrupted")])
    result = request("/api/v1/core/price-lists/66?point_id=23109")
    assert result.status_code == 502 and result.json()["complete_snapshot"] is False
    assert "products" not in result.json()


def test_product_duplicate_id_conflict(monkeypatch):
    row = product()
    row.update(hierarchicalId=2, unit="шт")
    mock_pages(monkeypatch, [page([product(), row])])
    with pytest.raises(s.ExportError, match="CONFLICT"):
        asyncio.run(s.get_price("23109", "66"))


def test_empty_scope_and_wrong_point(monkeypatch, settings):
    fetch = AsyncMock()
    monkeypatch.setattr(s.saby_client, "_get", fetch)
    assert request("/api/v1/core/price-lists?point_id=5598").status_code == 403
    settings.saby_price_list_point_ids = ""
    assert request("/api/v1/core/price-lists?point_id=23109").status_code == 503
    fetch.assert_not_awaited()


@pytest.mark.parametrize("locator", ["https://evil/img?a=b", "//evil/img?a=b", "../img?a=b",
                                    "img/../foo?a=b", "img?a=b#f", "img", "img?\na=b"])
def test_rejects_untrusted_image_urls(locator):
    with pytest.raises(s.ExportError):
        s.image_descriptor(locator, "23109")


@pytest.mark.parametrize("locator", [
    "img?params=abc", "/retail/img?params=abc",
    "https://api.sbis.ru/retail/img?params=abc",
])
def test_same_image_endpoint_normalized(locator):
    assert s.image_locator(locator) == "img?params=abc"
    assert s.image_descriptor(locator, "23109") == s.image_descriptor("img?params=abc", "23109")


@pytest.mark.parametrize("locator", [
    "http://api.sbis.ru/retail/img?params=abc",
    "https://api.sbis.ru.evil.test/retail/img?params=abc",
    "https://user@api.sbis.ru/retail/img?params=abc",
    "//api.sbis.ru/retail/img?params=abc",
    "https://api.sbis.ru/retail/../img?params=abc",
    "https://api.sbis.ru/retail/%69mg?params=abc",
    "https://api.sbis.ru:444/retail/img?params=abc",
])
def test_image_canonicalization_does_not_expand_hosts(locator):
    with pytest.raises(s.ExportError):
        s.image_locator(locator)


def test_image_signature_scope_and_key_rotation(settings):
    desc = s.image_descriptor("img?params=abc", "23109")
    token = desc["download_path"].rsplit("/", 1)[1]
    assert s.decode_image(token) == "img?params=abc"
    with pytest.raises(s.ExportError):
        s.decode_image(token + "x")
    settings.saby_price_list_point_ids = "1"
    with pytest.raises(s.ExportError):
        s.decode_image(token)
    settings.saby_price_list_point_ids = "23109"
    settings.core_price_lists_api_key = SecretStr("q"*40)
    with pytest.raises(s.ExportError):
        s.decode_image(token)


def request(path, headers=None):
    app = FastAPI()
    app.include_router(api.router)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            return await client.get(path, headers=headers or {"Authorization": "Bearer " + "p"*40})
    return asyncio.run(run())


def test_key_separation_and_failure_body(monkeypatch, settings):
    fetch = AsyncMock(side_effect=RuntimeError("secret-upstream-details"))
    monkeypatch.setattr(s, "list_prices", fetch)
    assert request("/api/v1/core/price-lists?point_id=23109",
                   {"Authorization": "Bearer " + "directory-key"*4}).status_code == 401
    fetch.assert_not_awaited()
    result = request("/api/v1/core/price-lists?point_id=23109")
    assert result.status_code == 502 and not result.json()["complete_snapshot"]
    assert "secret-upstream" not in result.text
    settings.core_price_lists_api_key = SecretStr("")
    assert request("/api/v1/core/price-lists?point_id=23109").status_code == 503


def test_unchanged_image_304_changed_same_reference_200(monkeypatch):
    fetch = AsyncMock(return_value=(b"image", "image/png", "a"*64))
    monkeypatch.setattr(s, "download_image", fetch)
    headers = {"Authorization": "Bearer " + "p"*40, "If-None-Match": '"' + "a"*64 + '"'}
    result = request("/api/v1/core/price-list-images/test", headers)
    assert result.status_code == 304 and not result.content
    fetch.return_value = (b"changed", "image/png", "b"*64)
    result = request("/api/v1/core/price-list-images/test", headers)
    assert result.status_code == 200 and result.content == b"changed"


@pytest.mark.parametrize("status,mime,body,ok", [
    (200, "image/png", b"\x89PNG\r\n\x1a\ndata", True),
    (302, "image/png", b"", False), (404, "text/html", b"", False),
    (200, "text/html", b"<html>", False), (200, "image/png", b"<html>", False),
    (200, "image/png", b"\x89PNG\r\n\x1a\n" + b"x"*(10*1024*1024), False),
], ids=["ok", "redirect", "missing", "html", "bad-signature", "too-large"])
def test_image_transport_limits(monkeypatch, status, mime, body, ok):
    real_client = httpx.AsyncClient
    def handler(req):
        assert req.url.host == "api.sbis.ru"
        assert req.headers["X-SBISAccessToken"] == "private"
        return httpx.Response(status, headers={"Content-Type": mime}, content=body)
    monkeypatch.setattr(s.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(s.saby_client, "_authenticate", AsyncMock(return_value="private"))
    token = s.image_descriptor("img?params=abc", "23109")["download_path"].rsplit("/", 1)[1]
    if ok:
        assert asyncio.run(s.download_image(token))[0] == body
    else:
        with pytest.raises(s.ExportError):
            asyncio.run(s.download_image(token))


def test_upstream_validator_avoids_second_download(monkeypatch):
    real_client = httpx.AsyncClient
    calls = []
    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(200, headers={"Content-Type": "image/png", "ETag": '"source-v1"'},
                                  content=b"\x89PNG\r\n\x1a\ndata")
        assert req.headers["If-None-Match"] == '"source-v1"'
        return httpx.Response(304)
    monkeypatch.setattr(s.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(s.saby_client, "_authenticate", AsyncMock(return_value="private"))
    token = s.image_descriptor("img?params=abc", "23109")["download_path"].rsplit("/", 1)[1]
    first = asyncio.run(s.download_image(token))
    assert asyncio.run(s.download_image(token)) == first
