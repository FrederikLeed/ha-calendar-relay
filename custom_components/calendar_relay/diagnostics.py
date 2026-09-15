"""Diagnostics for Calendar Relay.

Only counts and states: no credentials, no URLs (the iCloud URL carries the
account's numeric id), no entity ids or titles (they often contain names).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import CalendarRelayConfigEntry
from .const import WAZE_DOMAIN, WAZE_SERVICE

TO_REDACT = {CONF_URL, CONF_USERNAME, CONF_PASSWORD}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: CalendarRelayConfigEntry) -> dict[str, Any]:
    """Return diagnostics for an account entry."""
    data = entry.runtime_data
    calendar_urls = {calendar.url for calendar in data.calendars}
    relays = []
    for index, relay in enumerate(data.relays.values(), start=1):
        state = hass.states.get(relay.config.source)
        relays.append(
            {
                "relay": f"relay_{index}",
                "source_state": state.state if state is not None else None,
                "target_calendar_found": relay.config.target in calendar_urls,
                "has_title_filter": bool(relay.config.title_filter),
                "remove_filter": relay.config.remove_filter,
                "has_title_prefix": bool(relay.config.title_prefix),
                "look_ahead_days": relay.config.look_ahead_days,
                "structured_location": relay.config.structured_location,
                "travel_time": relay.config.travel_time,
                "buffer_minutes": relay.config.buffer_minutes,
                "leave_reminder": relay.config.leave_reminder,
                "waze_available": hass.services.has_service(WAZE_DOMAIN, WAZE_SERVICE),
                "relayed_events": relay.relayed_events,
                "syncing": relay.syncing,
                "last_sync": relay.last_sync.isoformat() if relay.last_sync else None,
                "last_error": relay.last_error,
            }
        )
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "calendars": {
            "total": len(data.calendars),
            "accept_events": sum(calendar.supports_events for calendar in data.calendars),
            "writable_reported": sum(calendar.writable is True for calendar in data.calendars),
            "read_only_reported": sum(calendar.writable is False for calendar in data.calendars),
            "usable": sum(calendar.usable for calendar in data.calendars),
        },
        "relays": relays,
    }
