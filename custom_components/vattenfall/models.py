"""Parse only tariff fields; never retain the complete portal response."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

Fuel = Literal["electricity", "gas"]
FUELS: tuple[Fuel, ...] = ("electricity", "gas")


class InvalidTariffData(Exception):
    """The portal returned unsupported, ambiguous or non-current tariff data."""


@dataclass(frozen=True)
class Tariff:
    """A consumption unit price; end is exclusive, matching portal periods."""

    key: str
    fuel: Fuel
    amount: Decimal
    delivery_rate: Decimal
    energy_tax: Decimal
    start: date
    end: date
    unit: str

    def effective_on(self, day: date) -> bool:
        """Return whether this price applies on a Dutch calendar day."""
        return self.start <= day < self.end


def _object(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise InvalidTariffData("Expected a tariff object")
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise InvalidTariffData("Expected a tariff array")
    return value


def _number(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise InvalidTariffData("Expected a numeric tariff")
    try:
        result = Decimal(str(value))
    except InvalidOperation as err:
        raise InvalidTariffData("Invalid numeric tariff") from err
    if not result.is_finite():
        raise InvalidTariffData("Non-finite tariff")
    return result


def _date(value: object) -> date:
    if not isinstance(value, str):
        raise InvalidTariffData("Expected an ISO tariff date")
    try:
        result = date.fromisoformat(value)
    except ValueError as err:
        raise InvalidTariffData("Invalid tariff date") from err
    if result.isoformat() != value:
        raise InvalidTariffData("Expected an ISO tariff date")
    return result


def _key(fuel: Fuel, value: object) -> str:
    labels = (
        {
            "Normaaltarief": "electricity_normal",
            "Daltarief": "electricity_offpeak",
            "Enkeltarief": "electricity_single",
        }
        if fuel == "electricity"
        else {"Enkeltarief": "gas"}
    )
    if not isinstance(value, str) or value not in labels:
        raise InvalidTariffData("Unsupported tariff block")
    return labels[value]


def parse_tariffs(payload: object, today: date) -> tuple[Tariff, ...]:
    """Read VAT-inclusive prices; reject schema drift instead of guessing."""
    root = _object(payload)
    tariff_data = _object(root.get("tariffData"))
    specs = _object(_object(tariff_data.get("includingVat")).get("specs"))
    tariffs: list[Tariff] = []
    for fuel in FUELS:
        expected_unit = "KwHours" if fuel == "electricity" else "M3"
        for raw_contract in _array(specs.get(fuel)):
            contract = _object(raw_contract)
            prices = _array(contract.get("prices"))
            if not prices:
                raise InvalidTariffData("Contract has no tariff prices")
            for raw_price in prices:
                price = _object(raw_price)
                if price.get("unit") != expected_unit:
                    raise InvalidTariffData("Unsupported tariff unit")
                start, end = (
                    _date(price.get("periodStart")),
                    _date(price.get("periodEnd")),
                )
                if end <= start:
                    raise InvalidTariffData("Invalid tariff period")
                tariffs.append(
                    Tariff(
                        key=_key(fuel, price.get("blockDescription")),
                        fuel=fuel,
                        amount=_number(price.get("total")),
                        delivery_rate=_number(price.get("deliveryRate")),
                        energy_tax=_number(price.get("energyTax")),
                        start=start,
                        end=end,
                        unit="EUR/kWh" if fuel == "electricity" else "EUR/m³",
                    )
                )
    if not tariffs:
        raise InvalidTariffData("No consumption tariffs found")
    for key in {tariff.key for tariff in tariffs}:
        periods = sorted((t for t in tariffs if t.key == key), key=lambda t: t.start)
        if any(a.end > b.start for a, b in zip(periods, periods[1:], strict=False)):
            raise InvalidTariffData("Overlapping or tiered tariff periods")
    current = {t.key for t in tariffs if t.effective_on(today)}
    for fuel in {t.fuel for t in tariffs}:
        if not any(t.fuel == fuel and t.effective_on(today) for t in tariffs):
            raise InvalidTariffData("No current tariff for a supplied fuel")
    electricity = {key for key in current if key.startswith("electricity_")}
    if electricity and electricity not in (
        {"electricity_single"},
        {"electricity_normal", "electricity_offpeak"},
    ):
        raise InvalidTariffData("Incomplete or ambiguous electricity tariff blocks")
    return tuple(tariffs)
