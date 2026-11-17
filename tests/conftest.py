"""Synthetic contract fixtures; never use downloaded customer responses."""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import ClientSession
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame

TODAY = date(2026, 7, 1)
NOW = datetime(2026, 7, 1, 10, tzinfo=UTC)


@pytest.fixture
def payload():
    """Model the observed portal structure with wholly invented prices."""

    def price(label, unit, total, start="2026-01-01", end="2027-01-01"):
        return {
            "blockDescription": label,
            "unit": unit,
            "periodStart": start,
            "periodEnd": end,
            "total": total,
            "deliveryRate": 0.17,
            "energyTax": 0.11,
        }

    return {
        "contracts": [{"contractId": "synthetic-only"}],
        "tariffData": {
            "includingVat": {
                "specs": {
                    "inputs": {"postalCode": "SYNTHETIC-DO-NOT-RETAIN"},
                    "electricity": [
                        {
                            "prices": [
                                price("Normaaltarief", "KwHours", 0.28),
                                price("Daltarief", "KwHours", 0.24),
                            ]
                        }
                    ],
                    "gas": [{"prices": [price("Enkeltarief", "M3", 1.37)]}],
                }
            },
            "excludingVat": {"specs": {"gas": [{"prices": [{"total": 999}]}]}},
        },
    }


@pytest.fixture
async def hass(tmp_path):
    """Use real HA core/coordinator objects without an external HA pytest plugin."""
    instance = HomeAssistant(str(tmp_path))
    instance.config_entries = ConfigEntries(instance, {})
    frame.async_setup(instance)
    session = MagicMock(spec=ClientSession)
    with (
        patch(
            "custom_components.vattenfall.async_get_clientsession",
            return_value=session,
        ),
        patch(
            "custom_components.vattenfall.config_flow.async_get_clientsession",
            return_value=session,
        ),
    ):
        yield instance
        await instance.async_stop(force=True)
