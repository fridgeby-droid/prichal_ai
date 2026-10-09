import pytest

from app.services.saby import _extract_orders


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
