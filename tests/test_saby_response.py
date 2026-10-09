import pytest
import asyncio
from unittest.mock import AsyncMock

from app.services.saby import _extract_orders
from app.services.saby import SabyClient


def parse(data):
    return _extract_orders(data, point_id=42, window_from="2026-07-01 00:00:00",
                           window_to="2026-07-01 23:59:59", page=3)


@pytest.mark.parametrize("data", [{}, {"orders": None}, {"orders": {}},
                                  {"orders": "unexpected"}, {"orders": [None]}])
def test_invalid_format_has_context(data):
    with pytest.raises(ValueError) as caught:
        parse(data)
    message = str(caught.value)
    assert '"point_id": 42' in message
    assert "2026-07-01" in message
    assert '"page": 3' in message
    assert '"response_shape"' in message


def test_empty_array_is_valid():
    assert parse({"orders": []}) == []


def test_terminal_empty_object_is_valid():
    assert parse({"orders": {}, "outcome": {"hasMore": False}}) == []


@pytest.mark.parametrize("data", [
    {"orders": {}, "outcome": {"hasMore": True}},
    {"orders": [], "outcome": {"hasMore": True}},
    {"orders": {}, "outcome": {"hasMore": "false"}},
    {"orders": {}, "outcome": {"hasMore": 0}},
    {"orders": {}, "outcome": {}},
    {"orders": {"1": {"Sale": 1}}, "outcome": {"hasMore": False}},
    {"orders": None, "outcome": {"hasMore": False}},
    {"orders": {}, "outcome": {"hasMore": False}, "error": "denied"},
])
def test_unconfirmed_empty_or_unknown_object_is_rejected(data):
    with pytest.raises(ValueError):
        parse(data)


def test_empty_day_from_live_shape(monkeypatch):
    client = SabyClient()
    request = AsyncMock(return_value={"orders": {}, "outcome": {"hasMore": False}})
    monkeypatch.setattr(client, "_get", request)
    assert asyncio.run(client.orders_for_point_date(37462, "2026-07-01")) == []
    request.assert_awaited_once()


def test_short_page_with_more_is_split_not_truncated(monkeypatch):
    client = SabyClient()
    async def request(path, params):
        if params["fromDateTime"].endswith("00:00:00") and params["toDateTime"].endswith("23:59:59"):
            return {"orders": [{"Sale": 1}], "outcome": {"hasMore": True}}
        if params["fromDateTime"].endswith("00:00:00"):
            return {"orders": [{"Sale": 1}], "outcome": {"hasMore": False}}
        return {"orders": [{"Sale": 2}], "outcome": {"hasMore": False}}
    monkeypatch.setattr(client, "_get", request)
    result = asyncio.run(client.orders_for_point_date(37462, "2026-07-01"))
    assert [row["Sale"] for row in result] == [1, 2]


def test_valid_orders_are_unchanged():
    rows = [{"Sale": 1, "TotalPrice": 100}]
    assert parse({"orders": rows}) is rows


def test_error_payload_is_not_accepted_even_with_orders():
    with pytest.raises(ValueError):
        parse({"orders": [], "error": {"message": "denied"}})


def test_response_values_are_not_logged():
    with pytest.raises(ValueError) as caught:
        parse({"orders": None, "token": "SECRET_TOKEN",
               "error": {"message": "PRIVATE_CUSTOMER_NAME", "code": 123}})
    assert "SECRET_TOKEN" not in str(caught.value)
    assert "PRIVATE_CUSTOMER_NAME" not in str(caught.value)
    assert '"orders": "null"' in str(caught.value)
