"""Consumption tariff sensors; no fabricated combined day/night price."""

from datetime import date, datetime
from decimal import Decimal

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import VattenfallConfigEntry
from .const import CONTRACT_TIMEZONE, DOMAIN, PORTAL_URL
from .coordinator import VattenfallCoordinator
from .models import Tariff

DESCRIPTIONS = {
    key: SensorEntityDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=unit,
        suggested_display_precision=5,
    )
    for key, unit in (
        ("electricity_normal", "EUR/kWh"),
        ("electricity_offpeak", "EUR/kWh"),
        ("electricity_single", "EUR/kWh"),
        ("gas", "EUR/m³"),
    )
}
LAST_UPDATE_DESCRIPTION = SensorEntityDescription(
    key="last_successful_update",
    translation_key="last_successful_update",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VattenfallConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Discover new tariff blocks after contract changes without a reload."""
    coordinator = entry.runtime_data
    known: set[str] = set()
    async_add_entities(
        [
            VattenfallLastUpdateSensor(
                coordinator, entry.entry_id, LAST_UPDATE_DESCRIPTION
            )
        ]
    )

    @callback
    def discover() -> None:
        if not coordinator.last_update_success or coordinator.data is None:
            return
        today = dt_util.utcnow().astimezone(CONTRACT_TIMEZONE).date()
        keys = {
            tariff.key
            for tariff in coordinator.data.tariffs
            if tariff.effective_on(today)
        } - known
        async_add_entities(
            VattenfallTariffSensor(coordinator, entry.entry_id, DESCRIPTIONS[key])
            for key in sorted(keys)
        )
        known.update(keys)

    entry.async_on_unload(coordinator.async_add_listener(discover))
    discover()


class VattenfallSensor(CoordinatorEntity[VattenfallCoordinator], SensorEntity):
    """Share entry-scoped identity and device metadata."""

    _attr_has_entity_name = True
    _attr_attribution = "Data from the unsupported Mijn Vattenfall portal"

    def __init__(
        self,
        coordinator: VattenfallCoordinator,
        entry_id: str,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Mijn Vattenfall",
            manufacturer="Vattenfall",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=PORTAL_URL,
        )


class VattenfallTariffSensor(VattenfallSensor):
    """Expose current unit prices, never a numeric price after a failed update."""

    def _tariff(self, today: date | None = None) -> Tariff | None:
        if self.coordinator.data is None:
            return None
        day = today or dt_util.utcnow().astimezone(CONTRACT_TIMEZONE).date()
        return next(
            (
                tariff
                for tariff in self.coordinator.data.tariffs
                if tariff.key == self.entity_description.key
                and tariff.effective_on(day)
            ),
            None,
        )

    @property
    def available(self) -> bool:
        return super().available and self._tariff() is not None

    @property
    def native_value(self) -> Decimal | None:
        tariff = self._tariff()
        return (
            tariff.amount if self.coordinator.last_update_success and tariff else None
        )

    @property
    def extra_state_attributes(self) -> dict[str, str | bool | Decimal | None]:
        snapshot = self.coordinator.data
        if snapshot is None:
            return {"stale": True}
        tariff = self._tariff()
        last_known = self._tariff(
            snapshot.fetched_at.astimezone(CONTRACT_TIMEZONE).date()
        )
        metadata = tariff or last_known
        return {
            "stale": not self.available,
            "last_successful_update": snapshot.fetched_at.isoformat(),
            "last_known_rate": last_known.amount if last_known else None,
            "includes_vat": True,
            "includes_energy_tax": True,
            "effective_from": metadata.start.isoformat() if metadata else None,
            "effective_until_exclusive": metadata.end.isoformat() if metadata else None,
            "delivery_rate": metadata.delivery_rate if metadata else None,
            "energy_tax": metadata.energy_tax if metadata else None,
        }


class VattenfallLastUpdateSensor(VattenfallSensor):
    """Keep the last known snapshot readable while tariff entities are unavailable."""

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None

    @property
    def native_value(self) -> datetime | None:
        snapshot = self.coordinator.data
        return snapshot.fetched_at if snapshot else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        snapshot = self.coordinator.data
        if snapshot is None:
            return {"stale": True}
        fetched_day = snapshot.fetched_at.astimezone(CONTRACT_TIMEZONE).date()
        today = dt_util.utcnow().astimezone(CONTRACT_TIMEZONE).date()
        last_known = [t for t in snapshot.tariffs if t.effective_on(fetched_day)]
        return {
            "stale": not self.coordinator.last_update_success
            or any(not tariff.effective_on(today) for tariff in last_known),
            "last_known_tariffs": {
                tariff.key: {
                    "rate": tariff.amount,
                    "unit": tariff.unit,
                    "includes_vat": True,
                    "includes_energy_tax": True,
                    "effective_from": tariff.start.isoformat(),
                    "effective_until_exclusive": tariff.end.isoformat(),
                }
                for tariff in last_known
            },
        }
