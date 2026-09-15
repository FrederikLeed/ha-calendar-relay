"""Calendar Relay: push events from Home Assistant calendars into a CalDAV calendar."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .caldav import CalDavAuthError, CalDavClient, CalDavError, DavCalendar
from .const import DOMAIN, STORAGE_VERSION, SUBENTRY_TYPE_RELAY
from .relay import Relay, async_remove_relay_data

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.SENSOR]


@dataclass
class CalendarRelayData:
    """Runtime data of an account entry."""

    client: CalDavClient
    calendars: list[DavCalendar]
    relays: dict[str, Relay] = field(default_factory=dict)


type CalendarRelayConfigEntry = ConfigEntry[CalendarRelayData]


def create_client(hass: HomeAssistant, data: Mapping[str, Any]) -> CalDavClient:
    """Create a CalDAV client on Home Assistant's shared aiohttp session."""
    return CalDavClient(async_get_clientsession(hass), data[CONF_URL], data[CONF_USERNAME], data[CONF_PASSWORD])


def relay_index_key(entry_id: str) -> str:
    """Return the Store key that lists the relays of an account that may have sync state."""
    return f"{DOMAIN}.relays_{entry_id}"


def _relay_ids(entry: ConfigEntry) -> set[str]:
    """Return the subentry ids of the account's relays."""
    return {
        subentry_id
        for subentry_id, subentry in entry.subentries.items()
        if subentry.subentry_type == SUBENTRY_TYPE_RELAY
    }


async def _async_known_relay_ids(store: Store[dict[str, Any]]) -> set[str]:
    """Load the relay ids recorded for an account."""
    data = await store.async_load()
    relays = data.get("relays") if isinstance(data, dict) else None
    if not isinstance(relays, list):
        return set()
    return {relay for relay in relays if isinstance(relay, str)}


async def _async_remove_stale_relays(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the state and repair issue of every relay that no longer exists, then record the current relays.

    This runs before anything else in setup, so a relay removed while the account
    was not loaded (setup retry, setup error, disabled) is cleaned up too.
    """
    store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, relay_index_key(entry.entry_id))
    known = await _async_known_relay_ids(store)
    current = _relay_ids(entry)
    for subentry_id in known - current:
        await async_remove_relay_data(hass, subentry_id)
    if known != current:
        await store.async_save({"relays": sorted(current)})


async def async_setup_entry(hass: HomeAssistant, entry: CalendarRelayConfigEntry) -> bool:
    """Set up a CalDAV account and its relays."""
    await _async_remove_stale_relays(hass, entry)
    client = create_client(hass, entry.data)
    try:
        calendars = await client.async_discover()
    except CalDavAuthError as err:
        raise ConfigEntryAuthFailed(f"The server rejected the credentials: {err}") from err
    except CalDavError as err:
        raise ConfigEntryNotReady(f"Could not read the calendars: {err}") from err

    data = CalendarRelayData(client=client, calendars=calendars)
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_RELAY:
            continue
        relay = Relay(hass, entry, subentry, client, lambda: data.calendars)
        await relay.async_load()
        data.relays[subentry.subentry_id] = relay
    entry.runtime_data = data

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    for relay in data.relays.values():
        entry.async_on_unload(relay.async_stop)
        relay.async_start()
    # Adding, changing or removing a relay only notifies update listeners, so reload here. The setup that
    # follows drops the state of removed relays.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: CalendarRelayConfigEntry) -> None:
    """Reload after a change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: CalendarRelayConfigEntry) -> bool:
    """Unload an account."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: CalendarRelayConfigEntry) -> None:
    """Remove the sync state of every relay, also of relays removed earlier. Relayed events stay in the calendars."""
    store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, relay_index_key(entry.entry_id))
    for subentry_id in await _async_known_relay_ids(store) | set(entry.subentries):
        await async_remove_relay_data(hass, subentry_id)
    await store.async_remove()
