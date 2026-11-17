"""Parse allowlisted portal fields; never retain complete account responses."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Literal

from .const import CONTRACT_TIMEZONE

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
    if not result.is_finite() or not isfinite(float(result)):
        raise InvalidTariffData("Non-finite or out-of-range tariff")
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


@dataclass(frozen=True)
class OverviewMetric:
    """A non-identifying portal value, optionally covering an inclusive period."""

    key: str
    value: Decimal | str
    period_start: date | None = None
    period_end: date | None = None
    smart_meter: bool | None = None


@dataclass(frozen=True, repr=False)
class Supply:
    """An ephemeral identifier used only to request the selected supply's usage."""

    fuel: Fuel
    ean: str
    smart_meter: bool
    feed_in: bool


@dataclass(frozen=True)
class OverviewData:
    """Only public-schema metrics and explicit missing-service indicators."""

    metrics: tuple[OverviewMetric, ...]
    unavailable_sections: tuple[str, ...] = ()


def parse_supplies(payload: object) -> tuple[str, tuple[Supply, ...]]:
    """Extract energy supplies, discarding identifiers after usage retrieval."""
    root = _object(payload)
    status = root.get("contractStatus")
    if not isinstance(status, str) or not re.fullmatch(
        r"[A-Za-z][A-Za-z -]{0,63}", status
    ):
        raise InvalidTariffData("Unsupported portal contract status")
    supplies: list[Supply] = []
    for raw in _array(root.get("products")):
        product = _object(raw)
        code = product.get("productCode")
        if code == "Costs":
            continue
        if code not in ("Electricity", "Gas"):
            raise InvalidTariffData("Unsupported consumption product")
        fuel: Fuel = "electricity" if code == "Electricity" else "gas"
        ean = product.get("eanCode")
        info = _object(product.get("usageProductInformation"))
        smart_meter = info.get("hasSmartMeter")
        feed_in = info.get("hasBackdelivery")
        if (
            not isinstance(ean, str)
            or not re.fullmatch(r"\d{18}", ean)
            or not isinstance(smart_meter, bool)
            or not isinstance(feed_in, bool)
            or product.get("calculationType") != "Usage"
            or any(supply.fuel == fuel for supply in supplies)
        ):
            raise InvalidTariffData("Unsupported or ambiguous consumption supply")
        supplies.append(Supply(fuel, ean, smart_meter, feed_in))
    return status, tuple(supplies)


def parse_installment(
    payload: object, *, advice: bool = False
) -> tuple[OverviewMetric, ...]:
    """Expose monthly installments only when the portal marks them usable."""
    root = _object(payload)
    flag = root.get("success" if advice else "active")
    if not isinstance(flag, bool):
        raise InvalidTariffData("Missing installment availability flag")
    if not flag:
        return ()
    if type(root.get("termAmountCycle")) is not int or root["termAmountCycle"] != 1:
        raise InvalidTariffData("Unsupported installment cycle")
    value = _number(root.get("recommendedTermAmount" if advice else "termAmount"))
    if value < 0:
        raise InvalidTariffData("Negative installment amount")
    return (
        OverviewMetric("recommended_installment" if advice else "installment", value),
    )


def previous_month(today: date) -> tuple[date, date]:
    """Return the previous complete Dutch calendar month, inclusive at both ends."""
    end = today.replace(day=1) - timedelta(days=1)
    return end.replace(day=1), end


def _usage_date(value: object) -> date:
    if not isinstance(value, str):
        raise InvalidTariffData("Missing consumption timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise InvalidTariffData("Invalid consumption timestamp") from None
    if parsed.tzinfo is None:
        raise InvalidTariffData("Consumption timestamps must include a timezone")
    local = parsed.astimezone(CONTRACT_TIMEZONE)
    if local.hour or local.minute or local.second or local.microsecond:
        raise InvalidTariffData("Unexpected monthly consumption boundary time")
    return local.date()


def parse_month(
    payload: object, supply: Supply, start: date, end: date
) -> tuple[OverviewMetric, ...]:
    """Select an exact complete month; never turn missing readings into zero."""
    root = _object(payload)
    if root.get("product") != ("E" if supply.fuel == "electricity" else "G"):
        raise InvalidTariffData("Unexpected monthly consumption product")
    matching = []
    for raw in _array(root.get("intervals")):
        row = _object(raw)
        row_start = _usage_date(row.get("dateFrom"))
        row_end = _usage_date(row.get("dateTo"))
        if row_end < row_start:
            raise InvalidTariffData("Invalid consumption period")
        if row_start == start and row_end == end:
            matching.append(row)
    if len(matching) > 1:
        raise InvalidTariffData("Ambiguous monthly consumption periods")
    if not matching or matching[0].get("errorCode") != "NoError":
        return ()
    row = matching[0]
    quantity = _number(
        row.get("deliveryQuantity" if supply.fuel == "electricity" else "quantity")
    )
    if quantity < 0:
        raise InvalidTariffData("Negative delivered consumption")
    metrics = [
        OverviewMetric(
            f"previous_month_{supply.fuel}_consumption",
            quantity,
            start,
            end,
            supply.smart_meter,
        ),
        OverviewMetric(
            f"previous_month_{supply.fuel}_cost",
            _number(row.get("amountInclVat")),
            start,
            end,
            supply.smart_meter,
        ),
    ]
    if supply.fuel == "electricity" and supply.feed_in:
        exported = _number(row.get("backDeliveryQuantity"))
        if exported < 0:
            raise InvalidTariffData("Negative exported consumption")
        metrics.append(
            OverviewMetric(
                "previous_month_electricity_export",
                exported,
                start,
                end,
                supply.smart_meter,
            )
        )
    return tuple(metrics)
