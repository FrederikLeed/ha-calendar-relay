"""Last sync sensor for each relay."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CalendarRelayConfigEntry
from .entity import RelayEntity
from .relay import Relay

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CalendarRelayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a Last sync sensor to every relay."""
    for subentry_id, relay in entry.runtime_data.relays.items():
        async_add_entities([RelayLastSyncSensor(relay)], config_subentry_id=subentry_id)


class RelayLastSyncSensor(RelayEntity, SensorEntity):
    """When the relay last completed a sync without errors."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, relay: Relay) -> None:
        """Initialize the sensor."""
        super().__init__(relay, "last_sync")

    @property
    def native_value(self) -> datetime | None:
        """Return the time of the last successful sync."""
        return self.relay.last_sync

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the number of relayed events and the last error."""
        return {"relayed_events": self.relay.relayed_events, "last_error": self.relay.last_error}

    async def async_added_to_hass(self) -> None:
        """Update after every sync pass."""
        await super().async_added_to_hass()
        self.async_on_remove(self.relay.async_add_listener(self.async_write_ha_state))
