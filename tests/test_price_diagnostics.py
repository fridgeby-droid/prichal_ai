import asyncio
import json
from unittest.mock import AsyncMock
import httpx

from scripts import diagnose_price_lists as d


def test_values_never_printed():
    data = {"nomenclatures": [{"id": 123, "name": "PRIVATE-NAME", "unit": "PRIVATE-UNIT",
                             "images": ["https://private/?token=SECRET"], "isParent": None}],
            "outcome": {"hasMore": False}, "error": "SECRET", "token": "SECRET"}
    output = json.dumps(d.response_shape(data, "nomenclatures"))
    assert all(value not in output for value in ("PRIVATE", "SECRET", "123", "https"))
    assert '"hasMore": false' in output
    assert d.row_shape(data["nomenclatures"][0])["isParent"]["type"] == "NoneType"


def test_exception_diagnostic_restores_client(monkeypatch, capsys):
    getter = AsyncMock(return_value={"priceLists": {}, "outcome": {"hasMore": False}})
    monkeypatch.setattr(d.service.saby_client, "_get", getter)
    async def broken(*args):
        await d.service.saby_client._get("/retail/nomenclature/price-list", {"page": 0})
        raise d.service.ExportError()
    monkeypatch.setattr(d.service, "get_price", broken)
    assert asyncio.run(d.run("23109", "66")) == 1
    assert d.service.saby_client._get is getter
    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics"][0]["response"]["priceLists"]["type"] == "dict"


def test_http_failure_status_without_secrets(monkeypatch, capsys):
    response = httpx.Response(400, json={"code": 42, "error": "SECRET"},
                             request=httpx.Request("GET", "https://example.org/?token=SECRET"))
    getter = AsyncMock(side_effect=httpx.HTTPStatusError("SECRET", request=response.request,
                                                       response=response))
    monkeypatch.setattr(d.service.saby_client, "_get", getter)
    async def broken(*args):
        return await d.service.saby_client._get("/retail/v2/nomenclature/list",
                                               {"pointId": 23109, "priceListId": 66})
    monkeypatch.setattr(d.service, "get_price", broken)
    assert asyncio.run(d.run("23109", "66")) == 1
    text = capsys.readouterr().out
    assert "SECRET" not in text and "example.org" not in text
    entry = json.loads(text)["diagnostics"][0]
    assert entry["http_status"] == 400
    assert entry["numeric_error_codes"] == {"code": 42}
    assert d.service.saby_client._get is getter
