"""End to end: the core local_calendar integration, Calendar Relay and a real Radicale CalDAV server."""

from __future__ import annotations

import base64
import re
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import pytest
from homeassistant.components.calendar import DATA_COMPONENT
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.calendar_relay.const import (
    CONF_REMOVE_FILTER,
    CONF_SOURCE,
    CONF_TARGET,
    CONF_TITLE_FILTER,
    CONF_TITLE_PREFIX,
    DOMAIN,
    SUBENTRY_TYPE_RELAY,
)

from .conftest import CALL_UP, PREFIX

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

AUTH = {"Authorization": "Basic " + base64.b64encode(b"emma:secret").decode()}
MKCALENDAR = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:set><d:prop>'
    "<d:displayname>{name}</d:displayname>"
    '<c:supported-calendar-component-set><c:comp name="{component}"/></c:supported-calendar-component-set>'
    "</d:prop></d:set></c:mkcalendar>"
)
PROPFIND_ETAG = (
    '<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:"><d:prop><d:getetag/></d:prop></d:propfind>'
)


def utc_stamp(value: Any) -> str:
    """Format a datetime like a UTC DATE-TIME."""
    return dt_util.as_utc(value).strftime("%Y%m%dT%H%M%SZ")


async def server_events(hass: HomeAssistant, calendar_url: str) -> dict[str, str]:
    """Return resource name to unfolded iCalendar text for every event stored on the server."""
    session = async_get_clientsession(hass)
    async with session.request(
        "PROPFIND",
        calendar_url,
        headers={**AUTH, "Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        data=PROPFIND_ETAG,
    ) as response:
        assert response.status == 207
        body = await response.read()
    hrefs = [
        element.text
        for element in ET.fromstring(body).iter("{DAV:}href")
        if element.text and element.text.endswith(".ics")
    ]
    events: dict[str, str] = {}
    for href in hrefs:
        async with session.get(urljoin(calendar_url, href), headers=AUTH) as response:
            assert response.status == 200
            text = await response.text()
        events[href.rsplit("/", 1)[-1]] = text.replace("\r\n ", "").replace("\n ", "")
    return events


def summaries(events: dict[str, str]) -> list[str]:
    """Return the sorted SUMMARY lines of the events."""
    return sorted(line for body in events.values() for line in body.splitlines() if line.startswith("SUMMARY"))


async def test_relay_local_calendar_into_radicale(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, radicale_url: str, tmp_path: Path
) -> None:
    """Create, update in place and delete, as seen on the CalDAV server."""
    await hass.config.async_set_time_zone("Europe/Copenhagen")
    config_dir = tmp_path / "config"
    (config_dir / ".storage").mkdir(parents=True)
    hass.config.config_dir = str(config_dir)

    session = async_get_clientsession(hass)
    for name, component in (("family", "VEVENT"), ("tasks", "VTODO")):
        async with session.request(
            "MKCALENDAR",
            f"{radicale_url}/emma/{name}/",
            headers=AUTH,
            data=MKCALENDAR.format(name=name.title(), component=component),
        ) as response:
            assert response.status == 201
    family_url = f"{radicale_url}/emma/family/"

    # The source: a local calendar with a call-up, a training, an all-day call-up and a recurring call-up.
    source_entry = MockConfigEntry(domain="local_calendar", data={"calendar_name": "Kids"})
    source_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(source_entry.entry_id)
    await hass.async_block_till_done()
    ws = await hass_ws_client(hass)

    async def calendar_command(command: str, payload: dict[str, Any]) -> None:
        await ws.send_json_auto_id({"type": f"calendar/event/{command}", "entity_id": "calendar.kids", **payload})
        result = await ws.receive_json()
        assert result["success"], result

    kickoff = (dt_util.now() + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
    cup_day = (dt_util.now() + timedelta(days=5)).date()
    league = kickoff + timedelta(days=1, hours=8)
    match = {
        "summary": f"{CALL_UP}Home - Away",
        "dtstart": kickoff.isoformat(),
        "dtend": (kickoff + timedelta(hours=2)).isoformat(),
        "description": "Meet at 9:30; bring water, shin pads",
        "location": "Pitch 2",
    }
    await calendar_command("create", {"event": match})
    await calendar_command(
        "create",
        {
            "event": {
                "summary": "Training",
                "dtstart": (kickoff + timedelta(days=1)).isoformat(),
                "dtend": (kickoff + timedelta(days=1, hours=1)).isoformat(),
            }
        },
    )
    await calendar_command(
        "create",
        {
            "event": {
                "summary": f"{CALL_UP}Cup day",
                "dtstart": cup_day.isoformat(),
                "dtend": (cup_day + timedelta(days=1)).isoformat(),
            }
        },
    )
    await calendar_command(
        "create",
        {
            "event": {
                "summary": f"{CALL_UP}League",
                "dtstart": league.isoformat(),
                "dtend": (league + timedelta(hours=1)).isoformat(),
                "rrule": "FREQ=WEEKLY;COUNT=2",
            }
        },
    )

    # The account: the real config flow discovers the Radicale calendars.
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: f"{radicale_url}/", CONF_USERNAME: "emma", CONF_PASSWORD: "secret"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    entry = result["result"]
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert {calendar.url: calendar.usable for calendar in entry.runtime_data.calendars} == {
        family_url: True,
        f"{radicale_url}/emma/tasks/": False,
    }

    # The relay: only the event calendar is offered.
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_RELAY), context={"source": SOURCE_USER}
    )
    options = next(selector for key, selector in result["data_schema"].schema.items() if key == CONF_TARGET)
    assert options.config["options"] == [{"value": family_url, "label": "Family"}]
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_SOURCE: "calendar.kids",
            CONF_TARGET: family_url,
            CONF_TITLE_FILTER: CALL_UP,
            CONF_REMOVE_FILTER: True,
            CONF_TITLE_PREFIX: PREFIX,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    subentry_id = next(iter(entry.subentries))
    registry = er.async_get(hass)
    button_id = registry.async_get_entity_id("button", DOMAIN, f"{subentry_id}_sync_now")
    sensor_id = registry.async_get_entity_id("sensor", DOMAIN, f"{subentry_id}_last_sync")

    # Create: the first sync after adding the relay.
    created = await server_events(hass, family_url)
    assert summaries(created) == [
        "SUMMARY:⚽ Emma: Cup day",
        "SUMMARY:⚽ Emma: Home - Away",
        "SUMMARY:⚽ Emma: League",
        "SUMMARY:⚽ Emma: League",
    ]
    assert all(re.fullmatch(r"relay-[0-9a-f]{32}\.ics", name) for name in created)
    match_name = next(name for name, body in created.items() if "Home - Away" in body)
    match_body = created[match_name]
    assert f"UID:{match_name.removeprefix('relay-').removesuffix('.ics')}@calendar-relay" in match_body
    assert f"DTSTART:{utc_stamp(kickoff)}" in match_body
    assert f"DTEND:{utc_stamp(kickoff + timedelta(hours=2))}" in match_body
    assert "Meet at 9:30\\; bring water\\, shin pads" in match_body
    assert "LOCATION:Pitch 2" in match_body
    cup_body = next(body for body in created.values() if "Cup day" in body)
    assert f"DTSTART;VALUE=DATE:{cup_day.strftime('%Y%m%d')}" in cup_body
    assert f"DTEND;VALUE=DATE:{(cup_day + timedelta(days=1)).strftime('%Y%m%d')}" in cup_body

    # Update in place: the match is rescheduled.
    source = hass.data[DATA_COMPONENT].get_entity("calendar.kids")
    source_events = await source.async_get_events(hass, dt_util.now(), dt_util.now() + timedelta(days=30))
    match_uid = next(event.uid for event in source_events if event.summary == match["summary"])
    new_kickoff = kickoff + timedelta(hours=3)
    rescheduled = {**match, "dtstart": new_kickoff.isoformat(), "dtend": (new_kickoff + timedelta(hours=2)).isoformat()}
    await calendar_command("update", {"uid": match_uid, "event": rescheduled})
    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)

    updated = await server_events(hass, family_url)
    assert set(updated) == set(created)
    assert f"DTSTART:{utc_stamp(new_kickoff)}" in updated[match_name]
    assert f"DTSTART:{utc_stamp(kickoff)}" not in updated[match_name]

    # Delete: the call-up is withdrawn, so the title loses the prefix.
    await calendar_command("update", {"uid": match_uid, "event": {**rescheduled, "summary": "Home - Away"}})
    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)

    remaining = await server_events(hass, family_url)
    assert match_name not in remaining
    assert summaries(remaining) == ["SUMMARY:⚽ Emma: Cup day", "SUMMARY:⚽ Emma: League", "SUMMARY:⚽ Emma: League"]
    state = hass.states.get(sensor_id)
    assert state.attributes["relayed_events"] == 3
    assert state.attributes["last_error"] is None

    # Removing the relay leaves its events in the calendar.
    assert hass.config_entries.async_remove_subentry(entry, subentry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert set(await server_events(hass, family_url)) == set(remaining)
