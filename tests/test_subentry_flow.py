"""Tests for adding and reconfiguring relays."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calendar_relay.caldav import CalDavConnectionError, DavCalendar
from custom_components.calendar_relay.config_flow import _relay_schema
from custom_components.calendar_relay.const import (
    CONF_BUFFER_MINUTES,
    CONF_LEAVE_REMINDER,
    CONF_LOOK_AHEAD_DAYS,
    CONF_REMOVE_FILTER,
    CONF_SOURCE,
    CONF_STRUCTURED_LOCATION,
    CONF_TARGET,
    CONF_TARGET_NAME,
    CONF_TITLE_FILTER,
    CONF_TITLE_PREFIX,
    CONF_TRAVEL_MINUTES,
    CONF_TRAVEL_TIME,
    CONF_WAZE_REGION,
    SUBENTRY_TYPE_RELAY,
    TRAVEL_MODES,
    WAZE_REGIONS,
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
    FakeWaze,
    make_entry,
    relay_data,
    relay_subentry,
    setup_entry,
    timed,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

INTEGRATION_DIR = Path(__file__).parent.parent / "custom_components" / "calendar_relay"
OLD_KEYS = (
    CONF_SOURCE,
    CONF_TARGET,
    CONF_TARGET_NAME,
    CONF_TITLE_FILTER,
    CONF_REMOVE_FILTER,
    CONF_TITLE_PREFIX,
    CONF_LOOK_AHEAD_DAYS,
)
KIDS_TO_FAMILY = {
    CONF_SOURCE: SOURCE,
    CONF_TARGET: FAMILY_URL,
    CONF_TITLE_FILTER: CALL_UP,
    CONF_REMOVE_FILTER: True,
    CONF_TITLE_PREFIX: PREFIX,
}


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


async def test_add_relay_with_location_and_travel_settings(
    hass: HomeAssistant, fake_dav: FakeDav, waze: FakeWaze
) -> None:
    """The new fields have defaults, translated options, and are stored as given."""
    entry = make_entry()
    await setup_entry(hass, entry)
    result = await start_add(hass, entry)
    defaults = {
        CONF_STRUCTURED_LOCATION: True,
        CONF_TRAVEL_TIME: "off",
        CONF_TRAVEL_MINUTES: 15,
        CONF_WAZE_REGION: "eu",
        CONF_BUFFER_MINUTES: 0,
        CONF_LEAVE_REMINDER: False,
    }
    assert {name: schema_key(result, name).default() for name in defaults} == defaults
    schema = result["data_schema"].schema
    assert schema[schema_key(result, CONF_TRAVEL_TIME)].config["options"] == ["off", "fixed", "waze"]
    assert schema[schema_key(result, CONF_TRAVEL_TIME)].config["translation_key"] == "travel_time"
    assert schema[schema_key(result, CONF_WAZE_REGION)].config["options"] == ["us", "na", "eu", "il", "au"]
    assert schema[schema_key(result, CONF_WAZE_REGION)].config["translation_key"] == "waze_region"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            **KIDS_TO_FAMILY,
            CONF_STRUCTURED_LOCATION: False,
            CONF_TRAVEL_TIME: "waze",
            CONF_TRAVEL_MINUTES: 30,
            CONF_WAZE_REGION: "na",
            CONF_BUFFER_MINUTES: 5,
            CONF_LEAVE_REMINDER: True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == relay_data(
        structured_location=False,
        travel_time="waze",
        travel_minutes=30,
        waze_region="na",
        buffer_minutes=5,
        leave_reminder=True,
    )
    assert isinstance(result["data"][CONF_TRAVEL_MINUTES], int)
    assert isinstance(result["data"][CONF_BUFFER_MINUTES], int)


async def test_waze_needs_its_action(hass: HomeAssistant, fake_dav: FakeDav) -> None:
    """Choosing Waze without the action shows an error and keeps what was entered."""
    entry = make_entry()
    await setup_entry(hass, entry)
    result = await start_add(hass, entry)
    user_input = {**KIDS_TO_FAMILY, CONF_TRAVEL_TIME: "waze", CONF_BUFFER_MINUTES: 10}

    result = await hass.config_entries.subentries.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {CONF_TRAVEL_TIME: "waze_unavailable"}
    assert schema_key(result, CONF_TRAVEL_TIME).description == {"suggested_value": "waze"}
    assert schema_key(result, CONF_BUFFER_MINUTES).description == {"suggested_value": 10}
    assert entry.subentries == {}

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**user_input, CONF_TRAVEL_TIME: "fixed"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == relay_data(travel_time="fixed", buffer_minutes=10)


async def test_reconfigure_travel_settings(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, config_entry: MockConfigEntry
) -> None:
    """Reconfigure prefills the new fields, refuses Waze until its action exists, then applies the change."""
    await setup_entry(hass, config_entry)
    result = await config_entry.start_subentry_reconfigure_flow(hass, RELAY_ID)
    assert schema_key(result, CONF_STRUCTURED_LOCATION).description == {"suggested_value": True}
    assert schema_key(result, CONF_TRAVEL_TIME).description == {"suggested_value": "off"}
    assert schema_key(result, CONF_LEAVE_REMINDER).description == {"suggested_value": False}

    user_input = {**KIDS_TO_FAMILY, CONF_TRAVEL_TIME: "waze", CONF_WAZE_REGION: "au", CONF_LEAVE_REMINDER: True}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_TRAVEL_TIME: "waze_unavailable"}

    FakeWaze().register(hass)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], user_input)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert dict(config_entry.subentries[RELAY_ID].data) == relay_data(
        travel_time="waze", waze_region="au", leave_reminder=True
    )
    config = config_entry.runtime_data.relays[RELAY_ID].config
    assert (config.travel_time, config.waze_region, config.leave_reminder) == ("waze", "au", True)


async def test_reconfigure_a_relay_saved_before_the_travel_settings(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav
) -> None:
    """A relay stored without the new keys shows their defaults and gets them written on save."""
    entry = make_entry({**relay_subentry(), "data": {name: relay_data()[name] for name in OLD_KEYS}})
    await setup_entry(hass, entry)

    result = await entry.start_subentry_reconfigure_flow(hass, RELAY_ID)
    assert schema_key(result, CONF_TRAVEL_TIME).description is None
    assert schema_key(result, CONF_TRAVEL_TIME).default() == "off"
    assert schema_key(result, CONF_TITLE_PREFIX).description == {"suggested_value": PREFIX}

    result = await hass.config_entries.subentries.async_configure(result["flow_id"], dict(KIDS_TO_FAMILY))
    assert result["type"] is FlowResultType.ABORT
    assert dict(entry.subentries[RELAY_ID].data) == relay_data()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (CONF_TRAVEL_MINUTES, 0),
        (CONF_TRAVEL_MINUTES, 481),
        (CONF_BUFFER_MINUTES, -1),
        (CONF_BUFFER_MINUTES, 121),
        (CONF_TRAVEL_TIME, "walk"),
        (CONF_WAZE_REGION, "xx"),
    ],
)
async def test_travel_settings_are_limited(hass: HomeAssistant, fake_dav: FakeDav, field: str, value: Any) -> None:
    """Travel minutes are 1 to 480, the buffer 0 to 120, and only known modes and regions are accepted."""
    entry = make_entry()
    await setup_entry(hass, entry)
    result = await start_add(hass, entry)
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_SOURCE: SOURCE, CONF_TARGET: FAMILY_URL, field: value}
        )


def test_translations_cover_the_relay_form() -> None:
    """strings.json is en.json; both languages label and describe every field, the error and every option."""
    english = (INTEGRATION_DIR / "translations" / "en.json").read_bytes()
    assert (INTEGRATION_DIR / "strings.json").read_bytes() == english
    fields = {str(key) for key in _relay_schema([]).schema}
    for language in ("en", "da"):
        strings = json.loads((INTEGRATION_DIR / "translations" / f"{language}.json").read_text(encoding="utf-8"))
        relay = strings["config_subentries"]["relay"]
        for step in ("user", "reconfigure"):
            assert set(relay["step"][step]["data"]) == fields
            assert set(relay["step"][step]["data_description"]) == fields
        assert set(relay["error"]) == {"waze_unavailable"}
        assert set(strings["selector"]["travel_time"]["options"]) == set(TRAVEL_MODES)
        assert set(strings["selector"]["waze_region"]["options"]) == set(WAZE_REGIONS)
