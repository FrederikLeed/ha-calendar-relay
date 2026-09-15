"""Sync now button for each relay."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
    """Add a Sync now button to every relay."""
    for subentry_id, relay in entry.runtime_data.relays.items():
        async_add_entities([RelaySyncButton(relay)], config_subentry_id=subentry_id)


class RelaySyncButton(RelayEntity, ButtonEntity):
    """Run a sync pass for the relay."""

    def __init__(self, relay: Relay) -> None:
        """Initialize the button."""
        super().__init__(relay, "sync_now")

    async def async_press(self) -> None:
        """Sync now."""
        await self.relay.async_sync()
