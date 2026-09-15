"""Tests for adding and reconfiguring relays."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calendar_relay.caldav import CalDavConnectionError, DavCalendar
from custom_components.calendar_relay.const import (
    CONF_LOOK_AHEAD_DAYS,
    CONF_REMOVE_FILTER,
    CONF_SOURCE,
    CONF_TARGET,
    CONF_TARGET_NAME,
    CONF_TITLE_FILTER,
    CONF_TITLE_PREFIX,
    SUBENTRY_TYPE_RELAY,
)

from .conftest import (
    CALL_UP,
    FAMILY_URL,
    HOME_URL,
    PREFIX,
    RELAY_ID,
    REMINDERS_URL,
    SHARED_URL,
    SOURCE,
    WORK_URL,
    FakeCalendar,
    FakeDav,
    make_entry,
    relay_data,
    relay_subentry,
    setup_entry,
    timed,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def schema_key(result: dict[str, Any], name: str) -> Any:
    """Return a schema key by name."""
    return next(key for key in result["data_schema"].schema if key == name)


def target_options(result: dict[str, Any]) -> list[dict[str, str]]:
    """Return the target calendar options of a relay form."""
    return result["data_schema"].schema[schema_key(result, CONF_TARGET)].config["options"]


async def start_add(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    """Start the Add relay flow."""
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_RELAY), context={"source": SOURCE_USER}
    )


async def test_add_relay(hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav) -> None:
    """Only writable event calendars are offered; the new relay starts syncing."""
    entry = make_entry()
    await setup_entry(hass, entry)
    source_calendar.events = [timed(f"{CALL_UP}Home - Away", dt_util.utcnow() + timedelta(days=2))]

    result = await start_add(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert target_options(result) == [{"value": FAMILY_URL, "label": "Family"}, {"value": WORK_URL, "label": "Work"}]

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_SOURCE: SOURCE,
            CONF_TARGET: FAMILY_URL,
            CONF_TITLE_FILTER: CALL_UP,
            CONF_REMOVE_FILTER: True,
            CONF_TITLE_PREFIX: PREFIX,
            CONF_LOOK_AHEAD_DAYS: 30,
        },
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Kids → Family"
    [subentry] = entry.subentries.values()
    assert subentry.subentry_type == SUBENTRY_TYPE_RELAY
    assert dict(subentry.data) == relay_data(look_ahead_days=30)
    assert set(entry.runtime_data.relays) == {subentry.subentry_id}
    assert len(fake_dav.resources) == 1


async def test_add_relay_with_defaults(hass: HomeAssistant, fake_dav: FakeDav) -> None:
    """Optional fields default to relaying everything for 60 days; an unknown source keeps its id in the title."""
    entry = make_entry()
    await setup_entry(hass, entry)
    result = await start_add(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_SOURCE: "calendar.school", CONF_TARGET: WORK_URL}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "calendar.school → Work"
    assert result["data"] == relay_data(
        source_calendar="calendar.school",
        target_calendar=WORK_URL,
        target_calendar_name="Work",
        title_filter="",
        remove_filter=False,
        title_prefix="",
        look_ahead_days=60,
    )


@pytest.mark.parametrize("days", [0, 366])
async def test_look_ahead_is_limited(hass: HomeAssistant, fake_dav: FakeDav, days: int) -> None:
    """Look-ahead is 1 to 365 days."""
    entry = make_entry()
    await setup_entry(hass, entry)
    result = await start_add(hass, entry)
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_SOURCE: SOURCE, CONF_TARGET: FAMILY_URL, CONF_LOOK_AHEAD_DAYS: days}
        )


async def test_calendars_with_the_same_name_are_told_apart(hass: HomeAssistant, fake_dav: FakeDav) -> None:
    """Two calendars called Family are shown with part of their address."""
    fake_dav.calendars = [
        DavCalendar(f"{HOME_URL}b8e0edb4-1acd-445a-b20e-66a1bf964bb7/", "Family", frozenset({"VEVENT"}), True),
        DavCalendar(f"{HOME_URL}{'5f' * 32}/", "Family", None, True),
    ]
    entry = make_entry()
    await setup_entry(hass, entry)
    result = await start_add(hass, entry)
    assert sorted(option["label"] for option in target_options(result)) == [
        "Family (5f5f5f5f5f5f)",
        "Family (b8e0edb4-1ac)",
    ]


async def test_add_relay_needs_a_loaded_account(hass: HomeAssistant, fake_dav: FakeDav) -> None:
    """Relays can only be added while the account is loaded."""
    entry = make_entry()
    entry.add_to_hass(hass)
    result = await start_add(hass, entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"


async def test_add_relay_needs_a_usable_calendar(hass: HomeAssistant, fake_dav: FakeDav) -> None:
    """Without a writable event calendar there is nothing to relay into."""
    fake_dav.calendars = [calendar for calendar in fake_dav.calendars if calendar.url in (REMINDERS_URL, SHARED_URL)]
    entry = make_entry()
    await setup_entry(hass, entry)
    result = await start_add(hass, entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_calendars"


async def test_calendar_list_is_refreshed(hass: HomeAssistant, fake_dav: FakeDav) -> None:
    """A calendar created after setup is offered; if the refresh fails the list from setup is used."""
    entry = make_entry()
    await setup_entry(hass, entry)
    fake_dav.calendars.append(DavCalendar(f"{HOME_URL}camp/", "Camp", None, True))
    result = await start_add(hass, entry)
    assert [option["label"] for option in target_options(result)] == ["Camp", "Family", "Work"]

    fake_dav.calendars.pop()
    fake_dav.discover_error = CalDavConnectionError("PROPFIND failed: ClientConnectorError")
    result = await start_add(hass, entry)
    assert [option["label"] for option in target_options(result)] == ["Camp", "Family", "Work"]


async def test_reconfigure_relay(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, config_entry: MockConfigEntry
) -> None:
    """The form is prefilled; saving updates the subentry and reloads the relay."""
    await setup_entry(hass, config_entry)
    result = await config_entry.start_subentry_reconfigure_flow(hass, RELAY_ID)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert schema_key(result, CONF_TARGET).description == {"suggested_value": FAMILY_URL}
    assert schema_key(result, CONF_TITLE_PREFIX).description == {"suggested_value": PREFIX}

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_SOURCE: SOURCE,
            CONF_TARGET: WORK_URL,
            CONF_TITLE_FILTER: "Udtaget",
            CONF_REMOVE_FILTER: True,
            CONF_TITLE_PREFIX: "Emma: ",
            CONF_LOOK_AHEAD_DAYS: 90,
        },
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    subentry = config_entry.subentries[RELAY_ID]
    assert subentry.title == "Kids → Work"
    assert dict(subentry.data) == relay_data(
        target_calendar=WORK_URL,
        target_calendar_name="Work",
        title_filter="Udtaget",
        title_prefix="Emma: ",
        look_ahead_days=90,
    )
    relay = config_entry.runtime_data.relays[RELAY_ID]
    assert relay.config.target == WORK_URL
    assert relay.config.look_ahead_days == 90


async def test_reconfigure_keeps_a_target_that_disappeared(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav
) -> None:
    """The current target stays selectable even when discovery no longer lists it."""
    gone = f"{HOME_URL}gone/"
    entry = make_entry(relay_subentry(target_calendar=gone, target_calendar_name="Old camp"))
    await setup_entry(hass, entry)
    fake_dav.calendars = []

    result = await entry.start_subentry_reconfigure_flow(hass, RELAY_ID)
    assert result["type"] is FlowResultType.FORM
    assert target_options(result) == [{"value": gone, "label": "Old camp"}]

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_SOURCE: SOURCE, CONF_TARGET: gone}
    )
    assert result["type"] is FlowResultType.ABORT
    assert entry.subentries[RELAY_ID].data[CONF_TARGET_NAME] == "Old camp"
