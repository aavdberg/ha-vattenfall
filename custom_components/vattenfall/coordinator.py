"""Refresh tariff data with explicit stale and reauthentication handling."""

import logging
from dataclasses import dataclass
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import PortalAuthError, PortalConnectionError, VattenfallClient
from .const import CONTRACT_TIMEZONE, DOMAIN, UPDATE_INTERVAL
from .models import InvalidTariffData, OverviewData, Tariff

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TariffSnapshot:
    """Only price data and the last successful retrieval timestamp."""

    tariffs: tuple[Tariff, ...]
    fetched_at: datetime


class VattenfallCoordinator(DataUpdateCoordinator[TariffSnapshot]):
    """Keep the previous snapshot on failure, while marking entities unavailable."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: VattenfallClient,
        config_entry: ConfigEntry | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self._client = client
        self.overview: VattenfallOverviewCoordinator | None = None

    async def _async_update_data(self) -> TariffSnapshot:
        try:
            tariffs = await self._client.async_get_tariffs(
                dt_util.utcnow().astimezone(CONTRACT_TIMEZONE).date()
            )
        except PortalAuthError:
            raise ConfigEntryAuthFailed(
                "Vattenfall authentication expired or rejected; log in again"
            ) from None
        except (PortalConnectionError, InvalidTariffData) as err:
            raise UpdateFailed(str(err)) from None
        return TariffSnapshot(tariffs, dt_util.utcnow())


@dataclass(frozen=True)
class OverviewSnapshot:
    """Non-identifying summary data and its last successful retrieval."""

    overview: OverviewData
    fetched_at: datetime


class VattenfallOverviewCoordinator(DataUpdateCoordinator[OverviewSnapshot]):
    """Keep optional overview failures independent of contract tariff updates."""

    def __init__(
        self, hass: HomeAssistant, client: VattenfallClient, entry: ConfigEntry
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} overview",
            update_interval=UPDATE_INTERVAL,
        )
        self._client = client

    async def _async_update_data(self) -> OverviewSnapshot:
        try:
            overview = await self._client.async_get_overview(
                dt_util.utcnow().astimezone(CONTRACT_TIMEZONE).date()
            )
        except PortalAuthError:
            raise ConfigEntryAuthFailed("Portal overview session rejected") from None
        except (PortalConnectionError, InvalidTariffData) as err:
            raise UpdateFailed(str(err)) from None
        return OverviewSnapshot(overview, dt_util.utcnow())
