"""Read-only Mijn Vattenfall tariffs and energy overview."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PortalCredentials, VattenfallClient
from .const import CONF_ACCESS_TOKEN, CONF_SUBSCRIPTION_KEY, CONF_VF_CONTEXT
from .coordinator import VattenfallCoordinator, VattenfallOverviewCoordinator

type VattenfallConfigEntry = ConfigEntry[VattenfallCoordinator]

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: VattenfallConfigEntry) -> bool:
    """Validate the session and perform the first refresh before creating entities."""
    client = VattenfallClient(
        async_get_clientsession(hass),
        PortalCredentials(
            entry.data[CONF_ACCESS_TOKEN],
            entry.data[CONF_SUBSCRIPTION_KEY],
            entry.data[CONF_VF_CONTEXT],
        ),
    )
    coordinator = VattenfallCoordinator(hass, client, entry)
    await coordinator.async_config_entry_first_refresh()
    coordinator.overview = VattenfallOverviewCoordinator(hass, client, entry)
    await coordinator.overview.async_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: VattenfallConfigEntry) -> bool:
    """Unload entities and their coordinator listeners."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
