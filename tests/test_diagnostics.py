"""Tests for diagnostics."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.calendar_relay.caldav import CalDavStatusError

from .conftest import CALL_UP, PASSWORD, USERNAME, FakeCalendar, FakeDav, setup_entry, timed

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def test_diagnostics_contain_counts_and_states_only(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
) -> None:
    """No credentials, URLs, entity ids, titles or names; just counts and states. An excerpt of an answer can quote
    an event, so last_error has the status alone."""
    start = datetime.now(UTC) + timedelta(days=2)
    source_calendar.events = [
        timed(f"{CALL_UP}Home - Away", start),
        timed(f"{CALL_UP}Cup", start + timedelta(days=1), uid="cup"),
    ]
    fake_dav.put_error = lambda href, ics: (
        CalDavStatusError(415, excerpt="Bad SUMMARY:⚽ Emma: Cup") if "Cup" in ics else None
    )
    await setup_entry(hass, config_entry)

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, config_entry)

    assert diagnostics == {
        "entry": {"url": "**REDACTED**", "username": "**REDACTED**", "password": "**REDACTED**"},
        "calendars": {"total": 4, "accept_events": 3, "writable_reported": 2, "read_only_reported": 1, "usable": 2},
        "relays": [
            {
                "relay": "relay_1",
                "source_state": "off",
                "target_calendar_found": True,
                "has_title_filter": True,
                "remove_filter": True,
                "has_title_prefix": True,
                "look_ahead_days": 60,
                "structured_location": True,
                "travel_time": "off",
                "buffer_minutes": 0,
                "leave_reminder": False,
                "waze_available": False,
                "relayed_events": 1,
                "syncing": False,
                "last_sync": None,
                "last_error": "1 event change(s) failed, first error: HTTP 415",
            }
        ],
    }
    dumped = json.dumps(diagnostics, ensure_ascii=False)
    for secret in (PASSWORD, USERNAME, "example.com", "123456789", "calendar.kids", "Kids", "Emma", "Family"):
        assert secret not in dumped
