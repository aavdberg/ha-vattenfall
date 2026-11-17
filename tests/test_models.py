"""Contract parsing, block mapping and effective-date boundaries."""

from copy import deepcopy
from dataclasses import asdict
from datetime import date
from decimal import Decimal

import pytest

from custom_components.vattenfall.models import InvalidTariffData, parse_tariffs

from .conftest import TODAY


def specs(payload):
    return payload["tariffData"]["includingVat"]["specs"]


def test_vat_inclusive_tariffs_are_sanitized(payload):
    tariffs = {tariff.key: tariff for tariff in parse_tariffs(payload, TODAY)}
    assert set(tariffs) == {"electricity_normal", "electricity_offpeak", "gas"}
    assert tariffs["electricity_normal"].amount == Decimal("0.28")
    assert tariffs["electricity_offpeak"].unit == "EUR/kWh"
    assert tariffs["gas"].amount == Decimal("1.37")
    assert tariffs["gas"].unit == "EUR/m³"
    assert tariffs["gas"].delivery_rate == Decimal("0.17")
    assert tariffs["gas"].energy_tax == Decimal("0.11")
    assert "SYNTHETIC" not in repr([asdict(t) for t in tariffs.values()])
    assert "contractId" not in repr(tariffs)


def test_period_boundary_uses_new_price_not_old_or_future(payload):
    specs(payload)["electricity"] = []
    price = specs(payload)["gas"][0]["prices"][0]
    price["periodEnd"] = "2026-07-01"
    new = {
        **price,
        "periodStart": "2026-07-01",
        "periodEnd": "2027-01-01",
        "total": 1.46,
    }
    specs(payload)["gas"][0]["prices"].append(new)
    tariffs = parse_tariffs(payload, TODAY)
    assert [t.amount for t in tariffs if t.effective_on(TODAY)] == [Decimal("1.46")]
    assert [t.amount for t in tariffs if t.effective_on(date(2026, 6, 30))] == [
        Decimal("1.37")
    ]


def test_single_electricity_and_negative_rate(payload):
    specs(payload)["gas"] = []
    prices = specs(payload)["electricity"][0]["prices"]
    prices[:] = [{**prices[0], "blockDescription": "Enkeltarief", "total": -0.02}]
    tariffs = parse_tariffs(payload, TODAY)
    assert len(tariffs) == 1
    assert tariffs[0].key == "electricity_single"
    assert tariffs[0].amount == Decimal("-0.02")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("total", None),
        ("total", "0.28"),
        ("total", True),
        ("total", float("nan")),
        ("total", float("inf")),
        ("deliveryRate", None),
        ("energyTax", "unknown"),
        ("periodStart", "invalid"),
        ("periodStart", "20260701"),
        ("periodStart", "2027-01-01"),
        ("periodEnd", None),
        ("unit", "unexpected"),
        ("blockDescription", "Unknown"),
    ],
)
def test_rejects_malformed_prices(payload, field, value):
    specs(payload)["gas"][0]["prices"][0][field] = value
    with pytest.raises(InvalidTariffData):
        parse_tariffs(payload, TODAY)


@pytest.mark.parametrize("fuel", ["electricity", "gas"])
def test_rejects_overlapping_or_tiered_prices(payload, fuel):
    prices = specs(payload)[fuel][0]["prices"]
    prices.append({**prices[0], "total": 999})
    with pytest.raises(InvalidTariffData, match="Overlapping"):
        parse_tariffs(payload, TODAY)


def test_no_ex_vat_fallback(payload):
    del payload["tariffData"]["includingVat"]
    with pytest.raises(InvalidTariffData):
        parse_tariffs(payload, TODAY)


@pytest.mark.parametrize("fuel", ["electricity", "gas"])
def test_missing_or_empty_prices_are_errors(payload, fuel):
    specs(payload)[fuel][0]["prices"] = []
    with pytest.raises(InvalidTariffData):
        parse_tariffs(payload, TODAY)


@pytest.mark.parametrize("today", [date(2025, 12, 31), date(2027, 1, 1)])
def test_no_current_prices_are_errors(payload, today):
    with pytest.raises(InvalidTariffData, match="current"):
        parse_tariffs(payload, today)


def test_empty_contracts_are_not_zero_prices(payload):
    specs(payload)["electricity"] = []
    specs(payload)["gas"] = []
    with pytest.raises(InvalidTariffData):
        parse_tariffs(payload, TODAY)


def test_partial_dual_rate_contract_is_error(payload):
    specs(payload)["electricity"][0]["prices"].pop()
    with pytest.raises(InvalidTariffData, match="electricity"):
        parse_tariffs(payload, TODAY)


def test_single_to_dual_transition(payload):
    original = deepcopy(specs(payload)["electricity"][0]["prices"])
    old = {
        **original[0],
        "blockDescription": "Enkeltarief",
        "periodEnd": "2026-07-01",
    }
    for price in original:
        price["periodStart"] = "2026-07-01"
    specs(payload)["electricity"][0]["prices"] = [old, *original]
    tariffs = parse_tariffs(payload, TODAY)
    assert {t.key for t in tariffs if t.effective_on(TODAY)} == {
        "electricity_normal",
        "electricity_offpeak",
        "gas",
    }
