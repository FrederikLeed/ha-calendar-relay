"""Tests for the relay device, the Sync now button and the Last sync sensor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calendar_relay.caldav import CalDavConnectionError
from custom_components.calendar_relay.const import DOMAIN

from .conftest import CALL_UP, RELAY_ID, FakeCalendar, FakeDav, setup_entry, timed

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

NOW = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)


def entity_ids(entity_registry: er.EntityRegistry) -> tuple[str, str]:
    """Return the button and sensor entity ids of the relay."""
    button = entity_registry.async_get_entity_id("button", DOMAIN, f"{RELAY_ID}_sync_now")
    sensor = entity_registry.async_get_entity_id("sensor", DOMAIN, f"{RELAY_ID}_last_sync")
    assert button is not None
    assert sensor is not None
    return button, sensor


async def test_relay_device_and_entities(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Each relay has a device on the account entry and its subentry, with a button and a diagnostic sensor."""
    await setup_entry(hass, config_entry)
    device = device_registry.async_get_device_by_identifier((DOMAIN, RELAY_ID), config_entry.entry_id)
    assert device is not None
    assert device.name == "Kids → Family"
    assert device.entry_type is dr.DeviceEntryType.SERVICE
    assert device.config_entry_id == config_entry.entry_id
    assert device.config_subentry_id == RELAY_ID

    button_id, sensor_id = entity_ids(entity_registry)
    for entity_id in (button_id, sensor_id):
        registry_entry = entity_registry.async_get(entity_id)
        assert registry_entry is not None
        assert registry_entry.device_id == device.id
        assert registry_entry.config_entry_id == config_entry.entry_id
        assert registry_entry.config_subentry_id == RELAY_ID
    assert entity_registry.async_get(sensor_id).entity_category is EntityCategory.DIAGNOSTIC
    assert hass.states.get(button_id).attributes["friendly_name"] == "Kids → Family Sync now"
    assert hass.states.get(sensor_id).attributes["friendly_name"] == "Kids → Family Last sync"


async def test_last_sync_sensor(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The sensor shows the last successful sync, the relayed count and the last error."""
    freezer.move_to(NOW)
    source_calendar.events = [timed(f"{CALL_UP}Home - Away", NOW + timedelta(days=2))]
    await setup_entry(hass, config_entry)
    button_id, sensor_id = entity_ids(entity_registry)

    state = hass.states.get(sensor_id)
    assert state.state == "2026-09-15T10:00:00+00:00"
    assert state.attributes["device_class"] == "timestamp"
    assert state.attributes["relayed_events"] == 1
    assert state.attributes["last_error"] is None

    freezer.tick(60)
    fake_dav.put_error = CalDavConnectionError("PUT returned HTTP 503")
    source_calendar.events.append(timed(f"{CALL_UP}Cup", NOW + timedelta(days=3), uid="cup"))
    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)

    state = hass.states.get(sensor_id)
    assert state.state == "2026-09-15T10:00:00+00:00"
    assert state.attributes["relayed_events"] == 1
    assert state.attributes["last_error"] == "PUT returned HTTP 503"


async def test_sync_now_button(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Pressing the button runs a pass right away."""
    await setup_entry(hass, config_entry)
    button_id, _sensor_id = entity_ids(entity_registry)
    source_calendar.events = [timed(f"{CALL_UP}Home - Away", datetime.now(UTC) + timedelta(days=2))]

    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)

    assert len(fake_dav.puts()) == 1
    assert hass.states.get(button_id).state != "unknown"
