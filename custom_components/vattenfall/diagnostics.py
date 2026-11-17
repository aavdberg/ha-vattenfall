"""Export health metadata without credentials, identifiers or tariff values."""

from homeassistant.core import HomeAssistant

from . import VattenfallConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: VattenfallConfigEntry
) -> dict[str, object]:
    """Return only explicitly permitted coordinator health fields."""
    coordinator = entry.runtime_data
    snapshot = coordinator.data
    result: dict[str, object] = {
        "last_update_success": coordinator.last_update_success,
        "last_successful_update": (
            snapshot.fetched_at.isoformat() if snapshot else None
        ),
        "tariff_kinds": sorted({tariff.key for tariff in snapshot.tariffs})
        if snapshot
        else [],
    }
    if (overview := coordinator.overview) is not None:
        result["overview"] = {
            "last_update_success": overview.last_update_success,
            "last_successful_update": overview.data.fetched_at.isoformat()
            if overview.data
            else None,
            "available_metrics": [
                metric.key for metric in overview.data.overview.metrics
            ]
            if overview.data
            else [],
            "unavailable_sections": list(overview.data.overview.unavailable_sections)
            if overview.data
            else [],
        }
    return result
