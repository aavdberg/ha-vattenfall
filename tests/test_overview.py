"""Synthetic summary data, period semantics and read-only API scoping."""

from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from custom_components.vattenfall.api import (
    CONSUMPTIONS_URL,
    INSTALLMENT_URL,
    VattenfallClient,
)
from custom_components.vattenfall.models import (
    InvalidTariffData,
    Supply,
    parse_installment,
    parse_month,
    parse_supplies,
    previous_month,
)

from .test_api import CREDENTIALS, mock_session

SYNTHETIC_EAN = "000000000000000001"
SUPPLY = Supply("electricity", SYNTHETIC_EAN, True, True)
START, END = date(2026, 6, 1), date(2026, 6, 30)


@pytest.fixture
def monthly():
    return {
        "product": "E",
        "eanCode": SYNTHETIC_EAN,
        "private": "SYNTHETIC-DO-NOT-RETAIN",
        "intervals": [
            {
                "dateFrom": "2026-05-31T22:00:00Z",
                "dateTo": "2026-06-29T22:00:00Z",
                "errorCode": "NoError",
                "quantity": 32,
                "deliveryQuantity": 42,
                "backDeliveryQuantity": 10,
                "amountInclVat": 15.25,
            }
        ],
    }


def product():
    return {
        "contractStatus": "Active",
        "products": [
            {
                "productCode": "Electricity",
                "eanCode": SYNTHETIC_EAN,
                "contractId": "SYNTHETIC-DO-NOT-RETAIN",
                "calculationType": "Usage",
                "usageProductInformation": {
                    "hasSmartMeter": True,
                    "hasBackdelivery": True,
                },
            },
            {"productCode": "Costs"},
        ],
    }


def test_month_uses_dutch_inclusive_dates_and_import_not_net(monthly):
    values = parse_month(monthly, SUPPLY, START, END)
    assert {metric.key: metric.value for metric in values} == {
        "previous_month_electricity_consumption": Decimal("42"),
        "previous_month_electricity_cost": Decimal("15.25"),
        "previous_month_electricity_export": Decimal("10"),
    }
    assert all(
        metric.period_start == START and metric.period_end == END for metric in values
    )
    assert SYNTHETIC_EAN not in repr(values)
    assert "SYNTHETIC-DO-NOT-RETAIN" not in repr(values)


def test_gas_uses_quantity_and_never_adds_an_export_sensor(monthly):
    monthly["product"] = "G"
    values = parse_month(
        monthly, Supply("gas", SYNTHETIC_EAN, False, False), START, END
    )
    assert values[0].value == 32
    assert len(values) == 2
    assert not values[0].smart_meter


def test_month_handles_dst_boundary(monthly):
    monthly["intervals"][0].update(
        dateFrom="2026-02-28T23:00:00Z", dateTo="2026-03-30T22:00:00Z"
    )
    assert parse_month(monthly, SUPPLY, date(2026, 3, 1), date(2026, 3, 31))
    assert previous_month(date(2027, 1, 2)) == (date(2026, 12, 1), date(2026, 12, 31))


@pytest.mark.parametrize("reason", ["NoData", "future"])
def test_missing_or_failed_month_is_not_a_zero(monthly, reason):
    if reason == "NoData":
        monthly["intervals"][0]["errorCode"] = reason
    else:
        monthly["intervals"][0].update(
            dateFrom="2026-07-01T00:00:00+02:00",
            dateTo="2026-07-31T00:00:00+02:00",
        )
    assert parse_month(monthly, SUPPLY, START, END) == ()


@pytest.mark.parametrize(
    "problem", ["duplicate", "timezone", "negative", "wrong_product"]
)
def test_ambiguous_or_invalid_month_fails_explicitly(monthly, problem):
    if problem == "duplicate":
        monthly["intervals"].append(deepcopy(monthly["intervals"][0]))
    elif problem == "timezone":
        monthly["intervals"][0]["dateFrom"] = "2026-06-01T00:00:00"
    elif problem == "negative":
        monthly["intervals"][0]["deliveryQuantity"] = -1
    else:
        monthly["product"] = "Unknown"
    with pytest.raises(InvalidTariffData):
        parse_month(monthly, SUPPLY, START, END)


def test_supplies_skip_costs_and_do_not_put_identifiers_in_repr():
    status, supplies = parse_supplies(product())
    assert status == "Active"
    assert supplies == (SUPPLY,)
    assert SYNTHETIC_EAN not in repr(supplies)


@pytest.mark.parametrize("problem", ["duplicate", "unsafe_ean", "unknown_product"])
def test_unsupported_supplies_fail_without_echoing_identifiers(problem):
    payload = product()
    if problem == "duplicate":
        payload["products"].append(deepcopy(payload["products"][0]))
    elif problem == "unsafe_ean":
        payload["products"][0]["eanCode"] = "../SYNTHETIC-PRIVATE"
    else:
        payload["products"][0]["productCode"] = "SYNTHETIC-PRIVATE"
    with pytest.raises(InvalidTariffData) as error:
        parse_supplies(payload)
    assert "SYNTHETIC-PRIVATE" not in str(error.value)


def test_installments_require_active_and_successful_portal_data():
    assert parse_installment({"active": False}) == ()
    assert (
        parse_installment({"success": False, "recommendedTermAmount": 0}, advice=True)
        == ()
    )
    (metric,) = parse_installment(
        {"active": True, "termAmountCycle": 1, "termAmount": 135}
    )
    assert metric.key == "installment" and metric.value == 135
    (metric,) = parse_installment(
        {"success": True, "termAmountCycle": 1, "recommendedTermAmount": 147},
        advice=True,
    )
    assert metric.key == "recommended_installment" and metric.value == 147


@pytest.mark.parametrize("amount", [None, True, -1, float("nan"), Decimal("1e1000")])
def test_invalid_installments_are_not_a_successful_zero(amount):
    with pytest.raises(InvalidTariffData):
        parse_installment({"active": True, "termAmountCycle": 1, "termAmount": amount})


async def test_overview_requests_only_fixed_read_services_and_selected_month(monthly):
    payloads = [
        {"active": True, "termAmountCycle": 1, "termAmount": 135},
        {"success": False},
        product(),
        monthly,
    ]
    session, response = mock_session(None)
    response.json.side_effect = payloads
    result = await VattenfallClient(session, CREDENTIALS).async_get_overview(
        date(2026, 7, 1)
    )
    assert len(result.metrics) == 5
    assert result.unavailable_sections == ("installment_advice",)
    calls = session.get.call_args_list
    assert [call.args[0] for call in calls] == [
        INSTALLMENT_URL,
        f"{INSTALLMENT_URL}/advice",
        CONSUMPTIONS_URL,
        f"{CONSUMPTIONS_URL}/products/{SYNTHETIC_EAN}/Month",
    ]
    assert calls[-1].kwargs["params"] == {
        "startDate": "2026-06-01",
        "endDate": "2026-06-30",
    }
    assert all(call.kwargs["allow_redirects"] is False for call in calls)
    assert SYNTHETIC_EAN not in repr(result)


async def test_unsupported_overview_services_are_reported_not_zero(caplog):
    session, response = mock_session(None, status=404)
    result = await VattenfallClient(session, CREDENTIALS).async_get_overview(
        date(2026, 7, 1)
    )
    assert result.metrics == ()
    assert result.unavailable_sections == (
        "installment",
        "installment_advice",
        "consumption",
    )
    response.json.assert_not_called()
    assert "HTTP 404" in caplog.text
