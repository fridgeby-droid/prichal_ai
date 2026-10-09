from decimal import Decimal

from app.services.payroll import PayrollService


service = PayrollService()


def test_current_seller_tiers():
    tiers = service.parse_tiers(
        "1.00=0.03;1.25=0.05"
    )

    assert service.select_kpi_percent(
        Decimal("0.99"),
        tiers,
    ) == Decimal("0")

    assert service.select_kpi_percent(
        Decimal("1.00"),
        tiers,
    ) == Decimal("0.03")

    assert service.select_kpi_percent(
        Decimal("1.2499"),
        tiers,
    ) == Decimal("0.03")

    assert service.select_kpi_percent(
        Decimal("1.25"),
        tiers,
    ) == Decimal("0.05")

    assert service.select_kpi_percent(
        Decimal("2.00"),
        tiers,
    ) == Decimal("0.05")


def test_weekday_parsing():
    assert service.parse_weekday("ALL") is None
    assert service.parse_weekday("все") is None

    assert service.parse_weekday("ПН") == 0
    assert service.parse_weekday("ПТ") == 4
    assert service.parse_weekday("ВС") == 6

    assert service.parse_weekday("4") == 4


def test_new_policy_tiers_can_change():
    tiers = service.parse_tiers(
        "1.00=0.04;1.25=0.06"
    )

    assert service.select_kpi_percent(
        Decimal("1.10"),
        tiers,
    ) == Decimal("0.04")

    assert service.select_kpi_percent(
        Decimal("1.30"),
        tiers,
    ) == Decimal("0.06")
