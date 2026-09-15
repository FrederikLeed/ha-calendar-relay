"""Tests for setting up, unloading and removing accounts and relays."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calendar_relay.caldav import CalDavAuthError, CalDavConnectionError
from custom_components.calendar_relay.const import DOMAIN

from .conftest import CALL_UP, RELAY_ID, FakeCalendar, FakeDav, make_entry, relay_subentry, setup_entry, timed

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

STORE_KEY = f"{DOMAIN}.{RELAY_ID}"


async def test_setup_without_relays(hass: HomeAssistant, fake_dav: FakeDav) -> None:
    """An account without relays loads and keeps its calendar list."""
    entry = make_entry()
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.relays == {}
    assert len(entry.runtime_data.calendars) == 4


async def test_setup_auth_failure_starts_reauth(hass: HomeAssistant, fake_dav: FakeDav) -> None:
    """Rejected credentials at setup start reauthentication."""
    fake_dav.discover_error = CalDavAuthError(401)
    entry = make_entry(relay_subentry())
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_setup_connection_failure_retries(hass: HomeAssistant, fake_dav: FakeDav) -> None:
    """An unreachable server makes setup retry later."""
    fake_dav.discover_error = CalDavConnectionError("PROPFIND failed: ClientConnectorError")
    entry = make_entry(relay_subentry())
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload_stops_the_relays(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, config_entry: MockConfigEntry
) -> None:
    """After unloading, source changes and the interval no longer sync."""
    await setup_entry(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED
    reads = source_calendar.calls
    hass.states.async_set("calendar.kids", "on", {"message": "Match"})
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert source_calendar.calls == reads


async def test_removing_a_relay_keeps_its_events_and_drops_its_state(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Removing a relay deletes its sync state and repair issue but no events."""
    source_calendar.events = [timed(f"{CALL_UP}Home - Away", dt_util.utcnow() + timedelta(days=2))]
    await setup_entry(hass, config_entry)
    assert STORE_KEY in hass_storage
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"target_missing_{RELAY_ID}",
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="target_calendar_missing",
    )

    assert hass.config_entries.async_remove_subentry(config_entry, RELAY_ID)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data.relays == {}
    assert STORE_KEY not in hass_storage
    assert issue_registry.async_get_issue(DOMAIN, f"target_missing_{RELAY_ID}") is None
    assert fake_dav.deletes() == []
    assert len(fake_dav.resources) == 1


async def test_removing_the_account_drops_relay_state(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, hass_storage: dict
) -> None:
    """Removing the account entry deletes the sync state of every relay but no events."""
    source_calendar.events = [timed(f"{CALL_UP}Home - Away", dt_util.utcnow() + timedelta(days=2))]
    entry = make_entry(relay_subentry(), relay_subentry("01RELAYTWO", "Kids → Work"))
    await setup_entry(hass, entry)
    assert STORE_KEY in hass_storage
    assert f"{DOMAIN}.01RELAYTWO" in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert STORE_KEY not in hass_storage
    assert f"{DOMAIN}.01RELAYTWO" not in hass_storage
    assert f"{DOMAIN}.relays_{entry.entry_id}" not in hass_storage
    assert fake_dav.deletes() == []


async def _remove_relay_while_not_loaded(
    hass: HomeAssistant, fake_dav: FakeDav, entry: MockConfigEntry, source_calendar: FakeCalendar
) -> None:
    """Set up the entry with state and a repair issue, put it in setup retry, then remove its relay."""
    source_calendar.events = [timed(f"{CALL_UP}Home - Away", dt_util.utcnow() + timedelta(days=2))]
    await setup_entry(hass, entry)
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"target_missing_{RELAY_ID}",
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="target_calendar_missing",
    )
    fake_dav.discover_error = CalDavConnectionError("PROPFIND returned HTTP 503")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY

    assert hass.config_entries.async_remove_subentry(entry, RELAY_ID)
    await hass.async_block_till_done()


async def test_relay_removed_while_the_account_is_not_loaded(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A relay removed during setup retry loses its state and repair issue at the next setup."""
    index_key = f"{DOMAIN}.relays_{config_entry.entry_id}"
    await _remove_relay_while_not_loaded(hass, fake_dav, config_entry, source_calendar)
    assert STORE_KEY in hass_storage

    fake_dav.discover_error = None
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert config_entry.state is ConfigEntryState.LOADED
    assert STORE_KEY not in hass_storage
    assert issue_registry.async_get_issue(DOMAIN, f"target_missing_{RELAY_ID}") is None
    assert hass_storage[index_key]["data"] == {"relays": []}
    assert fake_dav.deletes() == []


async def test_account_removed_while_not_loaded_drops_state_of_removed_relays(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Removing the account also drops the state of a relay removed while the account was not loaded."""
    await _remove_relay_while_not_loaded(hass, fake_dav, config_entry, source_calendar)

    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()

    assert STORE_KEY not in hass_storage
    assert f"{DOMAIN}.relays_{config_entry.entry_id}" not in hass_storage
    assert issue_registry.async_get_issue(DOMAIN, f"target_missing_{RELAY_ID}") is None
