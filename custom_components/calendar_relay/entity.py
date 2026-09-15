"""Base entity for relay devices."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .relay import Relay


class RelayEntity(Entity):
    """An entity on the device of one relay."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, relay: Relay, key: str) -> None:
        """Initialize the entity."""
        self.relay = relay
        self._attr_translation_key = key
        self._attr_unique_id = f"{relay.subentry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, relay.subentry_id)},
            name=relay.title,
            manufacturer="Calendar Relay",
            model="Relay",
            entry_type=DeviceEntryType.SERVICE,
        )
