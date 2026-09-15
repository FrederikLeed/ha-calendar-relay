"""Tests for location details, travel time and the leave reminder in relayed events.

Places, coordinates, the home location and the Waze answers are invented.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.calendar import CalendarEvent
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calendar_relay.const import DOMAIN, WAZE_DOMAIN, WAZE_SERVICE
from custom_components.calendar_relay.ics import StructuredLocation, content_hash, render_event, vtimezone_lines
from custom_components.calendar_relay.relay import (
    Relay,
    event_key,
    place_fingerprint,
    resource_id,
    travel_fingerprint,
)

from .conftest import (
    CALL_UP,
    FAMILY_URL,
    PREFIX,
    RELAY_ID,
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

NOW = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
KICKOFF = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)  # 12:00 in Copenhagen
STORE_KEY = f"{DOMAIN}.{RELAY_ID}"
HOME = "55.000000,11.000000"
MAP_LINK = "Kort: https://maps.apple.com/?ll=55.123456,10.654321&q=Example%20Stadium"
VENUE = "Example Stadium, Example Road 1, 1234 Sampletown"
DESTINATION = "55.123456,10.654321"
PLACE = (
    'X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-APPLE-RADIUS=71;X-TITLE="Example Stadium, Example Road 1, 1234 Sampletown"'
    ":geo:55.123456,10.654321"
)
DESCRIPTION = "Kort: https://maps.apple.com/?ll=55.123456\\,10.654321&q=Example%20Stadium"
OLD_KEYS = (
    "source_calendar",
    "target_calendar",
    "target_calendar_name",
    "title_filter",
    "remove_filter",
    "title_prefix",
    "look_ahead_days",
)


@pytest.fixture(autouse=True)
async def _clock_and_home(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await hass.config.async_set_time_zone("Europe/Copenhagen")
    hass.config.latitude = 55.0
    hass.config.longitude = 11.0
    freezer.move_to(NOW)


def match(start: datetime = KICKOFF, **kwargs: Any) -> CalendarEvent:
    """Return a call-up with a map link in the description and the venue as location, unless given."""
    kwargs.setdefault("description", MAP_LINK)
    kwargs.setdefault("location", VENUE)
    return timed(f"{CALL_UP}Home - Away", start, **kwargs)


def lines(fake_dav: FakeDav, name: str = "Home - Away") -> list[str]:
    """Return the unfolded lines of the relayed event whose text contains name."""
    body = next(body for body in fake_dav.resources.values() if name in body)
    return body.replace("\r\n ", "").split("\r\n")


def first_line(fake_dav: FakeDav, prefix: str, name: str = "Home - Away") -> str | None:
    """Return the first unfolded line of an event that starts with prefix."""
    return next((line for line in lines(fake_dav, name) if line.startswith(prefix)), None)


def relay_of(entry: MockConfigEntry) -> Relay:
    return entry.runtime_data.relays[RELAY_ID]


def travel_state(hass_storage: dict) -> dict:
    return hass_storage[STORE_KEY]["data"]["travel"]


def waze_entry(**overrides: Any) -> MockConfigEntry:
    return make_entry(relay_subentry(travel_time="waze", **overrides))


async def test_place_details_are_written_after_location(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, config_entry: MockConfigEntry
) -> None:
    """Coordinates in the description give Apple's structured location; LOCATION and DESCRIPTION stay as they are."""
    source_calendar.events = [match()]
    await setup_entry(hass, config_entry)

    event = lines(fake_dav)
    index = event.index("LOCATION:Example Stadium\\, Example Road 1\\, 1234 Sampletown")
    assert event[index + 1] == PLACE
    assert f"DESCRIPTION:{DESCRIPTION}" in event
    assert not any(line.startswith(("X-APPLE-TRAVEL", "BEGIN:VALARM")) for line in event)
    assert relay_of(config_entry).last_error is None


async def test_place_details_can_be_turned_off(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav
) -> None:
    """Without the setting nothing Apple-specific is written."""
    source_calendar.events = [match()]
    await setup_entry(hass, make_entry(relay_subentry(structured_location=False)))
    assert not any("X-APPLE" in line for line in lines(fake_dav))


@pytest.mark.parametrize(
    ("description", "location", "expected"),
    [
        (
            MAP_LINK,
            "Example Stadium\nExample Road 1, 1234 Sampletown",
            'X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-ADDRESS="Example Road 1, 1234 Sampletown";X-APPLE-RADIUS=71;'
            'X-TITLE="Example Stadium":geo:55.123456,10.654321',
        ),
        (
            MAP_LINK,
            None,
            'X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-APPLE-RADIUS=71;X-TITLE="Example Stadium":geo:55.123456,10.654321',
        ),
        (
            "Pin: geo:55.123456,10.654321",
            'The "Big" Hall; Court 2',
            "X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-APPLE-RADIUS=71;X-TITLE=\"The 'Big' Hall; Court 2\""
            ":geo:55.123456,10.654321",
        ),
        (
            None,
            "Example Hall https://www.openstreetmap.org/?mlat=-33.5&mlon=151.25",
            'X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-APPLE-RADIUS=71;X-TITLE="Example Hall '
            'https://www.openstreetmap.org/?mlat=-33.5&mlon=151.25":geo:-33.500000,151.250000',
        ),
        ("Pin: geo:55.123456,10.654321", None, None),
        ("No map link", VENUE, None),
    ],
)
async def test_structured_location_title_and_address(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    description: str | None,
    location: str | None,
    expected: str | None,
) -> None:
    """LOCATION's first line is the title, the rest the address; the link's name stands in for a missing LOCATION."""
    source_calendar.events = [match(description=description, location=location)]
    await setup_entry(hass, config_entry)
    assert first_line(fake_dav, "X-APPLE-STRUCTURED-LOCATION") == expected


async def test_relay_saved_by_version_0_1_gets_the_defaults_of_the_new_settings(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, hass_storage: dict
) -> None:
    """A relay without the new settings gets their defaults, and an all-day event without coordinates keeps its hash.

    Timed events are rewritten once for the home time zone; see test_upgrade_from_0_2_0_rewrites_timed_events_once.
    """
    event = CalendarEvent(
        start=date(2026, 9, 20),
        end=date(2026, 9, 21),
        summary=f"{CALL_UP}Cup day",
        uid="cup",
        description="Bring water",
        location="Pitch 2",
    )
    source_calendar.events = [event]
    key = event_key(event)
    rid = resource_id(RELAY_ID, key)
    old_body = render_event(
        uid=f"{rid}@calendar-relay",
        summary=f"{PREFIX}Cup day",
        start=date(2026, 9, 20),
        end=date(2026, 9, 21),
        description="Bring water",
        location="Pitch 2",
    )
    record = {
        "href": f"{FAMILY_URL}relay-{rid}.ics",
        "hash": content_hash(old_body),
        "start": "2026-09-20",
        "end": "2026-09-21",
        "target": FAMILY_URL,
    }
    hass_storage[STORE_KEY] = {"version": 1, "minor_version": 1, "key": STORE_KEY, "data": {"events": {key: record}}}
    entry = make_entry({**relay_subentry(), "data": {name: relay_data()[name] for name in OLD_KEYS}})
    await setup_entry(hass, entry)

    assert fake_dav.calls == []
    config = relay_of(entry).config
    assert (config.structured_location, config.travel_time, config.buffer_minutes, config.leave_reminder) == (
        True,
        "off",
        0,
        False,
    )
    assert relay_of(entry).last_error is None


async def test_fixed_travel_time_with_arrive_early_and_leave_reminder(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav
) -> None:
    """A leave line tops the description; the travel block and the alarm cover travel time plus arrive early."""
    source_calendar.events = [match()]
    entry = make_entry(relay_subentry(travel_time="fixed", travel_minutes=20, buffer_minutes=10, leave_reminder=True))
    await setup_entry(hass, entry)

    event = lines(fake_dav)
    assert f"DESCRIPTION:Leave at 11:30 (about 20 min drive + 10 min early)\\n{DESCRIPTION}" in event
    index = event.index(PLACE)
    assert event[index + 1 : index + 7] == [
        "X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT30M",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{PREFIX}Home - Away",
        "TRIGGER:-PT30M",
        "END:VALARM",
    ]
    body = "\r\n".join(event)
    for private in ("X-APPLE-TRAVEL-START", "ADVISORY-BEHAVIOR", HOME, "55.0,11.0"):
        assert private not in body


@pytest.mark.parametrize(
    ("language", "arrive_early", "line", "duration"),
    [
        ("en", 0, "Leave at 11:40 (about 20 min drive)", "PT20M"),
        ("en", 15, "Leave at 11:25 (about 20 min drive + 15 min early)", "PT35M"),
        ("da", 0, "Afgang: 11:40 (ca. 20 min. kørsel)", "PT20M"),
        ("da-DK", 15, "Afgang: 11:25 (ca. 20 min. kørsel + 15 min. før tid)", "PT35M"),
    ],
)
async def test_leave_line_in_both_languages_names_arrive_early(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    language: str,
    arrive_early: int,
    line: str,
    duration: str,
) -> None:
    """Danish Home Assistant gets a Danish leave line, which names arrive early unless it is 0.

    The travel block and the reminder cover the drive plus arrive early. A location text without
    coordinates is a place too.
    """
    hass.config.language = language
    source_calendar.events = [match(description="Bring water", location="Example Hall")]
    subentry = relay_subentry(travel_time="fixed", travel_minutes=20, buffer_minutes=arrive_early, leave_reminder=True)
    await setup_entry(hass, make_entry(subentry))

    event = lines(fake_dav)
    assert f"DESCRIPTION:{line}\\nBring water" in event
    assert f"X-APPLE-TRAVEL-DURATION;VALUE=DURATION:{duration}" in event
    assert f"TRIGGER:-{duration}" in event
    assert first_line(fake_dav, "X-APPLE-STRUCTURED-LOCATION") is None


async def test_no_travel_time_without_a_place_or_a_start_time(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav
) -> None:
    """Events without a place, and all-day events, get no leave line, travel block or alarm."""
    source_calendar.events = [
        match(description=None, location=None),
        CalendarEvent(
            start=date(2026, 9, 26),
            end=date(2026, 9, 27),
            summary=f"{CALL_UP}Cup day",
            uid="cup",
            description=MAP_LINK,
            location=VENUE,
        ),
    ]
    await setup_entry(hass, make_entry(relay_subentry(travel_time="fixed", leave_reminder=True)))

    match_event = lines(fake_dav, "Home - Away")
    cup_event = lines(fake_dav, "Cup day")
    for event in (match_event, cup_event):
        assert not any(line.startswith(("X-APPLE-TRAVEL", "BEGIN:VALARM", "DESCRIPTION:Leave")) for line in event)
    assert PLACE in cup_event


async def test_waze_travel_time_on_first_write(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    waze: FakeWaze,
    hass_storage: dict,
) -> None:
    """Waze is asked without live traffic, from home to the coordinates; the answer is rounded up to 5 minutes."""
    source_calendar.events = [match()]
    entry = waze_entry()
    await setup_entry(hass, entry)

    assert len(waze.calls) == 1
    assert {name: waze.calls[0][name] for name in ("origin", "destination", "region", "realtime")} == {
        "origin": HOME,
        "destination": DESTINATION,
        "region": "eu",
        "realtime": False,
    }
    event = lines(fake_dav)
    assert f"DESCRIPTION:Leave at 11:35 (about 25 min drive)\\n{DESCRIPTION}" in event
    assert "X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT25M" in event
    [record] = travel_state(hass_storage).values()
    assert record == {
        "minutes": 25,
        "fingerprint": record["fingerprint"],
        "place": record["place"],
        "computed_at": "2026-09-15T10:00:00+00:00",
        "realtime": False,
        "start": "2026-09-20T10:00:00+00:00",
    }
    assert re.fullmatch(r"[0-9a-f]{32}", record["fingerprint"])
    assert re.fullmatch(r"[0-9a-f]{32}", record["place"])
    assert record["place"] != record["fingerprint"]
    stored = json.dumps(hass_storage[STORE_KEY])
    for private in ("Example", "55.123456", HOME):
        assert private not in stored
    assert relay_of(entry).last_error is None


async def test_unchanged_travel_time_never_rewrites_and_live_traffic_is_asked_once(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    waze: FakeWaze,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Passes make no requests; within 3 hours of the start one live answer that rounds the same changes nothing."""
    source_calendar.events = [match()]
    entry = waze_entry()
    await setup_entry(hass, entry)
    relay = relay_of(entry)
    fake_dav.calls.clear()

    await relay.async_sync()
    freezer.move_to(KICKOFF - timedelta(hours=3, minutes=1))
    await relay.async_sync()
    assert waze.realtime_flags() == [False]
    assert fake_dav.calls == []

    waze.duration = 21.2
    freezer.move_to(KICKOFF - timedelta(hours=2, minutes=30))
    await relay.async_sync()
    assert waze.realtime_flags() == [False, True]
    assert fake_dav.calls == []
    [record] = travel_state(hass_storage).values()
    assert (record["minutes"], record["realtime"], record["computed_at"]) == (25, True, "2026-09-20T07:30:00+00:00")

    for minutes_before in (60, 5):
        freezer.move_to(KICKOFF - timedelta(minutes=minutes_before))
        await relay.async_sync()
    assert waze.realtime_flags() == [False, True]
    assert fake_dav.calls == []
    assert relay.last_error is None


async def test_live_traffic_that_changes_the_rounded_minutes_rewrites_once(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    waze: FakeWaze,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A slower live answer moves the leave time and the travel block, in one write."""
    source_calendar.events = [match()]
    entry = waze_entry()
    await setup_entry(hass, entry)
    [href] = fake_dav.puts()
    fake_dav.calls.clear()

    waze.duration = 31
    freezer.move_to(KICKOFF - timedelta(hours=1))
    await relay_of(entry).async_sync()
    await relay_of(entry).async_sync()

    assert fake_dav.calls == [("PUT", href)]
    assert waze.realtime_flags() == [False, True]
    event = lines(fake_dav)
    assert f"DESCRIPTION:Leave at 11:25 (about 35 min drive)\\n{DESCRIPTION}" in event
    assert "X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT35M" in event


async def test_live_traffic_rewrite_of_an_entry_opened_on_an_apple_device_deletes_it_and_writes_it_again(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    dav_server: FakeDav,
    waze: FakeWaze,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Not only a changed source event: a rewrite for a new travel time an hour before the match also meets iCloud's
    412 on an entry someone opened, so the entry is deleted and written again, as the README's limits say."""
    source_calendar.events = [match()]
    entry = waze_entry()
    await setup_entry(hass, entry)
    [href] = dav_server.puts()
    dav_server.opened.add(href)
    dav_server.calls.clear()

    waze.duration = 31
    freezer.move_to(KICKOFF - timedelta(hours=1))
    await relay_of(entry).async_sync()

    assert dav_server.calls == [("PUT", href), ("DELETE", href), ("PUT", href)]
    assert waze.realtime_flags() == [False, True]
    assert "X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT35M" in lines(dav_server)
    assert relay_of(entry).last_error is None


async def test_event_starting_soon_is_asked_once_with_live_traffic(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, waze: FakeWaze
) -> None:
    """An event first seen within 3 hours of its start needs no second answer."""
    source_calendar.events = [match(NOW + timedelta(hours=2))]
    entry = waze_entry()
    await setup_entry(hass, entry)
    await relay_of(entry).async_sync()
    assert waze.realtime_flags() == [True]


async def test_started_event_is_not_asked_for(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, waze: FakeWaze
) -> None:
    """Nobody leaves for an event that is already running."""
    source_calendar.events = [match(NOW - timedelta(minutes=30))]
    entry = waze_entry()
    await setup_entry(hass, entry)
    assert waze.calls == []
    assert first_line(fake_dav, "X-APPLE-TRAVEL") is None
    assert relay_of(entry).last_error is None


async def test_waze_not_available(hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav) -> None:
    """Without the action the event is written without travel time and the reason shows; once it exists, it is used."""
    source_calendar.events = [match()]
    entry = waze_entry()
    await setup_entry(hass, entry)
    relay = relay_of(entry)

    event = lines(fake_dav)
    assert PLACE in event
    assert not any(line.startswith(("X-APPLE-TRAVEL", "DESCRIPTION:Leave")) for line in event)
    assert relay.last_error == "Travel time: the Waze Travel Time action is not available"
    assert relay.last_sync == NOW

    waze = FakeWaze()
    waze.register(hass)
    fake_dav.calls.clear()
    await relay.async_sync()
    assert len(fake_dav.puts()) == 1
    assert "X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT25M" in lines(fake_dav)
    assert relay.last_error is None


async def test_waze_failure_keeps_the_last_travel_time_and_other_events_are_still_asked(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    waze: FakeWaze,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    hass_storage: dict,
) -> None:
    """A failed event keeps its value and waits an hour; later events are asked in the same pass; writes go on."""
    source_calendar.events = [match()]
    entry = waze_entry()
    await setup_entry(hass, entry)
    relay = relay_of(entry)
    [match_href] = fake_dav.puts()
    fake_dav.calls.clear()

    freezer.move_to(KICKOFF - timedelta(hours=2))
    waze.errors[DESTINATION] = HomeAssistantError("Error on retrieving data: timeout")
    final = timed(
        f"{CALL_UP}Cup final",
        KICKOFF + timedelta(days=1),
        uid="final",
        description="Pin: geo:55.2,10.7",
        location="Example Arena",
    )
    source_calendar.events = [final, match()]
    await relay.async_sync()

    error = "Travel time: Waze Travel Time failed (HomeAssistantError)"
    assert relay.last_error == error
    assert relay.last_sync == KICKOFF - timedelta(hours=2)
    assert "the Waze Travel Time action failed: Error on retrieving data: timeout" in caplog.text
    assert [(call["destination"], call["realtime"]) for call in waze.calls] == [
        (DESTINATION, False),
        (DESTINATION, True),
        ("55.200000,10.700000", False),
    ]
    [final_href] = fake_dav.puts()
    assert final_href != match_href
    assert "X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT25M" in lines(fake_dav, "Home - Away")
    assert "X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT25M" in lines(fake_dav, "Cup final")
    [retry] = hass_storage[STORE_KEY]["data"]["travel_retry"].values()
    assert (retry["failures"], retry["retry_at"], retry["error"]) == (1, "2026-09-20T09:00:00+00:00", error)

    del waze.errors[DESTINATION]
    fake_dav.calls.clear()
    await relay.async_sync()
    assert len(waze.calls) == 3
    assert fake_dav.calls == []
    assert relay.last_error == error

    freezer.tick(timedelta(hours=1))
    await relay.async_sync()
    assert waze.realtime_flags() == [False, True, False, True]
    assert fake_dav.calls == []
    assert relay.last_error is None
    assert hass_storage[STORE_KEY]["data"]["travel_retry"] == {}


@pytest.mark.parametrize(
    ("response", "error"),
    [
        ({"routes": []}, "Travel time: Waze Travel Time found no route"),
        ({"routes": ["Example Road"]}, "Travel time: Waze Travel Time found no route"),
        ({}, "Travel time: Waze Travel Time found no route"),
        ({"routes": [{"duration": None}]}, "Travel time: Waze Travel Time returned no travel time"),
        ({"routes": [{"duration": True}]}, "Travel time: Waze Travel Time returned no travel time"),
        ({"routes": [{"duration": -3.0}]}, "Travel time: Waze Travel Time returned no travel time"),
        ({"routes": [{"duration": float("inf")}]}, "Travel time: Waze Travel Time returned no travel time"),
        ({"routes": [{"duration": float("nan")}]}, "Travel time: Waze Travel Time returned no travel time"),
        ({"routes": [{"duration": 480.5}]}, "Travel time: Waze Travel Time returned more than 8 hours"),
        ({"routes": [{"duration": 2e9}]}, "Travel time: Waze Travel Time returned more than 8 hours"),
    ],
)
async def test_unusable_waze_answer(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    waze: FakeWaze,
    response: dict[str, Any],
    error: str,
) -> None:
    """An answer without a route or a duration is a failure, and the event is written without travel time."""
    waze.response = response
    source_calendar.events = [match()]
    entry = waze_entry()
    await setup_entry(hass, entry)
    assert relay_of(entry).last_error == error
    assert first_line(fake_dav, "X-APPLE-TRAVEL") is None


@pytest.mark.parametrize(
    ("raised", "name"),
    [
        (HomeAssistantError("Error on retrieving data"), "HomeAssistantError"),
        (ServiceNotFound(WAZE_DOMAIN, WAZE_SERVICE), "ServiceNotFound"),
        (vol.Invalid("expected str"), "Invalid"),
        (TimeoutError(), "TimeoutError"),
        (RuntimeError("boom"), "RuntimeError"),
    ],
)
async def test_failing_waze_action(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    waze: FakeWaze,
    raised: Exception,
    name: str,
) -> None:
    """Whatever the action raises, the sync goes on and only the exception type is shown."""
    waze.error = raised
    source_calendar.events = [match()]
    entry = waze_entry()
    await setup_entry(hass, entry)
    assert relay_of(entry).last_error == f"Travel time: Waze Travel Time failed ({name})"
    assert len(fake_dav.resources) == 1


async def test_new_destination_or_home_asks_again(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, waze: FakeWaze
) -> None:
    """A new destination or home location asks Waze again; a moved start time does not."""
    source_calendar.events = [match()]
    entry = waze_entry()
    await setup_entry(hass, entry)
    relay = relay_of(entry)

    waze.durations = {"55.200000,10.700000": 41}
    source_calendar.events = [match(description="Pin: geo:55.2,10.7")]
    await relay.async_sync()
    assert [call["destination"] for call in waze.calls] == [DESTINATION, "55.200000,10.700000"]
    assert "X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT45M" in lines(fake_dav)

    hass.config.latitude = 55.5
    await relay.async_sync()
    assert [call["origin"] for call in waze.calls] == [HOME, HOME, "55.500000,11.000000"]

    source_calendar.events = [match(KICKOFF + timedelta(hours=3), description="Pin: geo:55.2,10.7")]
    await relay.async_sync()
    assert len(waze.calls) == 3
    assert any(line.startswith("DESCRIPTION:Leave at 14:15 (about 45 min drive)") for line in lines(fake_dav))


async def test_location_text_is_the_destination_without_coordinates(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, waze: FakeWaze
) -> None:
    """Without coordinates Waze gets the location on one line, in the relay's region."""
    source_calendar.events = [match(description=None, location="Example Hall\nExample Road 2, 1234 Sampletown")]
    await setup_entry(hass, waze_entry(waze_region="na"))
    assert waze.calls[0]["destination"] == "Example Hall, Example Road 2, 1234 Sampletown"
    assert waze.calls[0]["region"] == "na"
    event = lines(fake_dav)
    assert "DESCRIPTION:Leave at 11:35 (about 25 min drive)" in event
    assert first_line(fake_dav, "X-APPLE-STRUCTURED-LOCATION") is None


async def test_turning_travel_time_off_removes_it(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    waze: FakeWaze,
    hass_storage: dict,
) -> None:
    """Events are rewritten without travel time and the stored travel times are dropped."""
    source_calendar.events = [match()]
    entry = waze_entry()
    await setup_entry(hass, entry)
    [href] = fake_dav.puts()
    fake_dav.calls.clear()

    hass.config_entries.async_update_subentry(entry, entry.subentries[RELAY_ID], data=relay_data())
    await hass.async_block_till_done(wait_background_tasks=True)

    assert fake_dav.calls == [("PUT", href)]
    assert not any(line.startswith(("X-APPLE-TRAVEL", "DESCRIPTION:Leave")) for line in lines(fake_dav))
    assert travel_state(hass_storage) == {}
    assert len(waze.calls) == 1


async def test_travel_times_survive_a_reload(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, waze: FakeWaze
) -> None:
    """After a reload the stored travel time is used: no Waze call, no write."""
    source_calendar.events = [match()]
    entry = waze_entry()
    await setup_entry(hass, entry)
    fake_dav.calls.clear()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert fake_dav.calls == []
    assert len(waze.calls) == 1


@pytest.mark.parametrize(
    "travel",
    [
        {"match": {"minutes": "25", "fingerprint": "f", "computed_at": "x", "realtime": False}, "other": "no record"},
        ["not", "a", "mapping"],
    ],
)
async def test_invalid_stored_travel_times_are_ignored(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    waze: FakeWaze,
    hass_storage: dict,
    travel: Any,
) -> None:
    """Broken travel state never crashes setup; the travel time is simply asked for again."""
    event = match()
    source_calendar.events = [event]
    if isinstance(travel, dict):
        travel = {event_key(event): travel["match"], "other": travel["other"]}
    hass_storage[STORE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": {"events": {}, "travel": travel},
    }
    await setup_entry(hass, waze_entry())
    assert len(waze.calls) == 1
    assert list(travel_state(hass_storage)) == [event_key(event)]


async def test_travel_time_and_retry_of_a_withdrawn_event_are_forgotten(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    waze: FakeWaze,
    hass_storage: dict,
) -> None:
    """A withdrawn event takes its travel time and its stored retry wait with it."""
    later = match(KICKOFF + timedelta(days=1), uid="later", description="Pin: geo:55.2,10.7")
    source_calendar.events = [match(), later]
    waze.errors[DESTINATION] = HomeAssistantError("Error on retrieving data")
    entry = waze_entry()
    await setup_entry(hass, entry)
    relay = relay_of(entry)
    assert len(travel_state(hass_storage)) == 1
    assert len(hass_storage[STORE_KEY]["data"]["travel_retry"]) == 1

    del waze.errors[DESTINATION]
    source_calendar.events = [timed("Home - Away", KICKOFF), timed("Home - Away", KICKOFF, uid="later")]
    await relay.async_sync()
    assert fake_dav.resources == {}
    assert travel_state(hass_storage) == {}
    assert hass_storage[STORE_KEY]["data"]["travel_retry"] == {}
    assert relay.last_error is None

    source_calendar.events = [match()]
    await relay.async_sync()
    assert [call["destination"] for call in waze.calls] == [DESTINATION, "55.200000,10.700000", DESTINATION]
    assert "X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT25M" in lines(fake_dav)


def href_of(event: CalendarEvent) -> str:
    """Return the href a source event is relayed to in the Family calendar."""
    return f"{FAMILY_URL}relay-{resource_id(RELAY_ID, event_key(event))}.ics"


def uid_of(event: CalendarEvent) -> str:
    """Return the UID of the relayed copy of a source event."""
    return f"{resource_id(RELAY_ID, event_key(event))}@calendar-relay"


async def test_upgrade_from_0_2_0_rewrites_timed_events_once(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    waze: FakeWaze,
    hass_storage: dict,
) -> None:
    """Timed events that 0.2.0 wrote in UTC get local times in one write each, and nothing follows.

    The only changes are the times, the VTIMEZONE and the arrive early text. The travel time 0.2.0
    stored is used as it is, so Waze is not asked; an all-day event is not rewritten at all.
    """
    trip = match()
    training = timed(f"{CALL_UP}Training match", KICKOFF + timedelta(days=1), uid="training")
    cup = CalendarEvent(start=date(2026, 9, 26), end=date(2026, 9, 27), summary=f"{CALL_UP}Cup day", uid="cup")
    source_calendar.events = [trip, training, cup]
    # What 0.2.0 wrote: times in UTC, and a leave line without arrive early.
    trip_body = render_event(
        uid=uid_of(trip),
        summary=f"{PREFIX}Home - Away",
        start=KICKOFF,
        end=KICKOFF + timedelta(hours=2),
        description=f"Leave at 11:20 (about 25 min drive)\n{MAP_LINK}",
        location=VENUE,
        structured_location=StructuredLocation(VENUE, 55.123456, 10.654321),
        travel_minutes=40,
        alarm_minutes=40,
    )
    training_start = KICKOFF + timedelta(days=1)
    training_body = render_event(
        uid=uid_of(training),
        summary=f"{PREFIX}Training match",
        start=training_start,
        end=training_start + timedelta(hours=2),
    )
    cup_body = render_event(uid=uid_of(cup), summary=f"{PREFIX}Cup day", start=date(2026, 9, 26), end=date(2026, 9, 27))
    written = [
        (trip, trip_body, "2026-09-20T10:00:00+00:00", "2026-09-20T12:00:00+00:00"),
        (training, training_body, "2026-09-21T10:00:00+00:00", "2026-09-21T12:00:00+00:00"),
        (cup, cup_body, "2026-09-26", "2026-09-27"),
    ]
    hass_storage[STORE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": {
            "events": {
                event_key(event): {
                    "href": href_of(event),
                    "hash": content_hash(body),
                    "start": start,
                    "end": end,
                    "target": FAMILY_URL,
                }
                for event, body, start, end in written
            },
            "travel": {
                event_key(trip): {
                    "minutes": 25,
                    "fingerprint": travel_fingerprint(HOME, DESTINATION, "eu"),
                    "place": place_fingerprint(DESTINATION),
                    "computed_at": "2026-09-15T09:00:00+00:00",
                    "realtime": False,
                    "start": "2026-09-20T10:00:00+00:00",
                }
            },
            "travel_retry": {},
        },
    }
    fake_dav.resources = {href_of(event): body for event, body, _start, _end in written}
    entry = waze_entry(buffer_minutes=15, leave_reminder=True)
    await setup_entry(hass, entry)

    assert sorted(fake_dav.calls) == sorted([("PUT", href_of(trip)), ("PUT", href_of(training))])
    assert waze.calls == []
    vtimezone = vtimezone_lines("Europe/Copenhagen", 2026)

    def upgraded(body: str, changes: dict[str, str]) -> list[str]:
        expected = [changes.get(line, line) for line in body.replace("\r\n ", "").split("\r\n")]
        expected[3:3] = vtimezone
        expected.insert(expected.index("BEGIN:VEVENT") + 2, "DTSTAMP:20260915T100000Z")
        return expected

    assert unfolded(fake_dav, trip) == upgraded(
        trip_body,
        {
            "DTSTART:20260920T100000Z": "DTSTART;TZID=Europe/Copenhagen:20260920T120000",
            "DTEND:20260920T120000Z": "DTEND;TZID=Europe/Copenhagen:20260920T140000",
            f"DESCRIPTION:Leave at 11:20 (about 25 min drive)\\n{DESCRIPTION}": (
                f"DESCRIPTION:Leave at 11:20 (about 25 min drive + 15 min early)\\n{DESCRIPTION}"
            ),
        },
    )
    assert unfolded(fake_dav, training) == upgraded(
        training_body,
        {
            "DTSTART:20260921T100000Z": "DTSTART;TZID=Europe/Copenhagen:20260921T120000",
            "DTEND:20260921T120000Z": "DTEND;TZID=Europe/Copenhagen:20260921T140000",
        },
    )
    assert fake_dav.resources[href_of(cup)] == cup_body

    fake_dav.calls.clear()
    await relay_of(entry).async_sync()
    await relay_of(entry).async_sync()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await relay_of(entry).async_sync()
    assert fake_dav.calls == []
    assert waze.calls == []
    assert relay_of(entry).last_error is None


def unfolded(fake_dav: FakeDav, event: CalendarEvent) -> list[str]:
    """Return the unfolded lines of the relayed copy of a source event."""
    return fake_dav.resources[href_of(event)].replace("\r\n ", "").split("\r\n")


def travel_lines(event_lines: list[str]) -> list[str]:
    """Return the lines that come from travel time: the leave line, the travel block and the alarm."""
    leave = ("DESCRIPTION:Leave", "DESCRIPTION:Afgang", "X-APPLE-TRAVEL", "BEGIN:VALARM")
    return [line for line in event_lines if line.startswith(leave)]


async def test_started_event_gets_no_travel_time_but_keeps_the_one_it_was_written_with(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, freezer: FrozenDateTimeFactory
) -> None:
    """A running event gets no leave line or alarm in the past, and its start never rewrites it."""
    running = timed(
        f"{CALL_UP}Running match", NOW - timedelta(minutes=30), uid="running", description=MAP_LINK, location=VENUE
    )
    source_calendar.events = [running, match()]
    entry = make_entry(relay_subentry(travel_time="fixed", travel_minutes=20, leave_reminder=True))
    await setup_entry(hass, entry)

    assert travel_lines(unfolded(fake_dav, running)) == []
    assert PLACE in unfolded(fake_dav, running)
    assert "TRIGGER:-PT20M" in unfolded(fake_dav, match())
    fake_dav.calls.clear()

    freezer.move_to(KICKOFF + timedelta(minutes=10))
    source_calendar.events = [match()]
    await relay_of(entry).async_sync()
    assert fake_dav.calls == []
    assert "TRIGGER:-PT20M" in unfolded(fake_dav, match())

    source_calendar.events = [match(description="Bring water")]
    await relay_of(entry).async_sync()
    assert fake_dav.calls == [("PUT", href_of(match()))]
    assert travel_lines(unfolded(fake_dav, match())) == []


async def test_events_waze_cannot_route_back_off_and_do_not_starve_later_events(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    waze: FakeWaze,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict,
) -> None:
    """Failing events wait 1, 2, 4, 8 and 16 hours; a later event gets its travel time; a reload keeps the waits."""
    online = [
        timed(f"{CALL_UP}Online {number}", NOW + timedelta(days=20 + number), uid=f"online-{number}", location=place)
        for number, place in enumerate(f"Online meeting {number}" for number in range(4))
    ]
    for number in range(4):
        waze.errors[f"Online meeting {number}"] = HomeAssistantError("Error on retrieving data: no route")
    final = match(NOW + timedelta(days=30), uid="final")
    source_calendar.events = [*online, final]
    entry = waze_entry()
    await setup_entry(hass, entry)
    relay = relay_of(entry)

    assert len(waze.calls) == 3
    freezer.tick(timedelta(minutes=15))
    await relay.async_sync()
    assert [call["destination"] for call in waze.calls[3:]] == ["Online meeting 3", DESTINATION]
    assert "X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT25M" in unfolded(fake_dav, final)

    for _ in range(4 * 24 - 1):
        freezer.tick(timedelta(minutes=15))
        await relay.async_sync()
    failing = [call for call in waze.calls if call["destination"] != DESTINATION]
    assert len(failing) == 4 * 5
    assert len(waze.calls) - len(failing) == 1
    assert relay.last_error == "Travel time: Waze Travel Time failed (HomeAssistantError)"
    retries = hass_storage[STORE_KEY]["data"]["travel_retry"]
    assert sorted(retry["failures"] for retry in retries.values()) == [5, 5, 5, 5]

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(waze.calls) == 4 * 5 + 1
    assert relay_of(entry).last_error == "Travel time: Waze Travel Time failed (HomeAssistantError)"


async def test_a_pass_stops_asking_after_three_failures_in_a_row_or_ten_calls(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    waze: FakeWaze,
    freezer: FrozenDateTimeFactory,
) -> None:
    """While Waze is down a pass makes 3 calls; once it works, 10, nearest first; every event is written at once."""
    events = [
        match(NOW + timedelta(days=1 + number), uid=f"match-{number}", description=f"Pin: geo:55.{10 + number},10.2")
        for number in range(14)
    ]
    source_calendar.events = list(reversed(events))
    waze.error = HomeAssistantError("Connection error: network unreachable")
    entry = waze_entry()
    await setup_entry(hass, entry)
    relay = relay_of(entry)
    assert [call["destination"] for call in waze.calls] == [f"55.{10 + number}0000,10.200000" for number in range(3)]
    assert len(fake_dav.resources) == 14

    def with_travel() -> list[int]:
        return [number for number, event in enumerate(events) if travel_lines(unfolded(fake_dav, event))]

    waze.error = None
    freezer.tick(timedelta(minutes=15))
    await relay.async_sync()
    assert len(waze.calls) == 13
    assert with_travel() == list(range(3, 13))

    freezer.tick(timedelta(minutes=15))
    await relay.async_sync()
    assert with_travel() == list(range(3, 14))

    freezer.tick(timedelta(minutes=30))
    await relay.async_sync()
    assert with_travel() == list(range(14))
    assert len(waze.calls) == 17
    assert relay.last_error is None


async def test_a_waze_timeout_ends_the_lookups_of_the_pass(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, waze: FakeWaze
) -> None:
    """A problem every call would hit is not tried again for the next event in the same pass."""
    later = match(KICKOFF + timedelta(days=1), uid="later", description="Pin: geo:55.2,10.7")
    source_calendar.events = [match(), later]
    waze.errors[DESTINATION] = TimeoutError()
    entry = waze_entry()
    await setup_entry(hass, entry)
    assert [call["destination"] for call in waze.calls] == [DESTINATION]
    assert relay_of(entry).last_error == "Travel time: Waze Travel Time failed (TimeoutError)"
    assert len(fake_dav.resources) == 2


async def test_a_waze_call_that_hangs_is_cut_off(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The call is given a timeout, so a hanging action cannot hold up the writes."""

    class HangingWaze(FakeWaze):
        async def _async_handle(self, call: ServiceCall) -> ServiceResponse:
            self.calls.append(dict(call.data))
            # The event loop's clock follows the frozen clock in these tests, so move it past the timeout.
            freezer.tick(timedelta(seconds=6))
            await asyncio.Event().wait()
            return None

    waze = HangingWaze()
    waze.register(hass)
    source_calendar.events = [match()]
    entry = waze_entry()
    with patch("custom_components.calendar_relay.relay.WAZE_CALL_TIMEOUT", 5):
        await setup_entry(hass, entry)
    assert relay_of(entry).last_error == "Travel time: Waze Travel Time failed (TimeoutError)"
    assert "the Waze Travel Time action failed: TimeoutError()" in caplog.text
    assert len(fake_dav.resources) == 1


async def test_an_absurd_waze_answer_does_not_block_changes_or_removals(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, waze: FakeWaze, hass_storage: dict
) -> None:
    """A huge duration is a failure: the moved place is written without travel time and a withdrawal is deleted."""
    training = timed(f"{CALL_UP}Training", KICKOFF - timedelta(days=2), uid="training", location="Example Hall")
    source_calendar.events = [match(), training]
    entry = waze_entry()
    await setup_entry(hass, entry)
    relay = relay_of(entry)

    waze.duration = 2e9
    moved = match(description="Pin: geo:55.2,10.7")
    source_calendar.events = [moved]
    await relay.async_sync()
    assert fake_dav.deletes() == [href_of(training)]
    assert relay.last_error == "Travel time: Waze Travel Time returned more than 8 hours"
    assert travel_lines(unfolded(fake_dav, moved)) == []
    assert any(line.endswith(":geo:55.200000,10.700000") for line in unfolded(fake_dav, moved))
    assert all(record["minutes"] <= 480 for record in travel_state(hass_storage).values())


async def test_new_home_keeps_travel_times_until_waze_answers_but_a_new_place_does_not(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    waze: FakeWaze,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A failed call after a new home location strips nothing; a new place has no travel time until Waze answers."""
    events = [match(KICKOFF + timedelta(days=number), uid=f"match-{number}") for number in range(3)]
    source_calendar.events = events
    entry = waze_entry()
    await setup_entry(hass, entry)
    relay = relay_of(entry)
    assert len(waze.calls) == 1
    fake_dav.calls.clear()

    hass.config.latitude = 55.01
    waze.error = HomeAssistantError("Error on retrieving data: timeout")
    await relay.async_sync()
    assert fake_dav.calls == []
    assert all("DESCRIPTION:Leave at 11:35 (about 25 min drive)" in "\n".join(unfolded(fake_dav, e)) for e in events)
    assert [call["origin"] for call in waze.calls[1:]] == ["55.010000,11.000000"] * 3

    waze.error = None
    freezer.tick(timedelta(hours=1))
    await relay.async_sync()
    assert len(waze.calls) == 5
    assert fake_dav.calls == []
    assert relay.last_error is None

    waze.errors["55.200000,10.700000"] = HomeAssistantError("Error on retrieving data: no route")
    moved = match(KICKOFF, uid="match-0", description="Pin: geo:55.2,10.7")
    source_calendar.events = [moved, *events[1:]]
    await relay.async_sync()
    assert fake_dav.calls == [("PUT", href_of(moved))]
    assert travel_lines(unfolded(fake_dav, moved)) == []


async def test_moved_event_gets_a_live_update_for_its_new_start(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    waze: FakeWaze,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Live traffic from the old day is kept until the new start is close, then renewed once."""
    start = NOW + timedelta(hours=2)
    waze.duration = 58
    source_calendar.events = [match(start)]
    entry = waze_entry()
    await setup_entry(hass, entry)
    relay = relay_of(entry)
    assert waze.realtime_flags() == [True]

    waze.duration = 20
    new_start = start + timedelta(days=7)
    source_calendar.events = [match(new_start)]
    await relay.async_sync()
    assert waze.realtime_flags() == [True]
    assert first_line(fake_dav, "DESCRIPTION:") == f"DESCRIPTION:Leave at 13:00 (about 60 min drive)\\n{DESCRIPTION}"

    freezer.move_to(new_start - timedelta(hours=2))
    await relay.async_sync()
    freezer.tick(timedelta(minutes=30))
    await relay.async_sync()
    assert waze.realtime_flags() == [True, True]
    assert first_line(fake_dav, "DESCRIPTION:") == f"DESCRIPTION:Leave at 13:40 (about 20 min drive)\\n{DESCRIPTION}"


async def test_live_update_of_a_long_trip_comes_before_its_leave_time(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    waze: FakeWaze,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A 200 minute trip is renewed an hour before its leave time; once the leave time has passed, it is not."""
    waze.duration = 200
    late = match()
    tournament = timed(
        f"{CALL_UP}Tournament", KICKOFF + timedelta(days=1), uid="tournament", description=MAP_LINK, location=VENUE
    )
    source_calendar.events = [late, tournament]
    entry = waze_entry()
    await setup_entry(hass, entry)
    relay = relay_of(entry)
    assert waze.realtime_flags() == [False]
    assert "DESCRIPTION:Leave at 08:40 (about 200 min drive)" in "\n".join(unfolded(fake_dav, tournament))

    waze.duration = 230
    freezer.move_to(KICKOFF - timedelta(hours=1))
    await relay.async_sync()
    assert waze.realtime_flags() == [False]

    moment = KICKOFF + timedelta(days=1, hours=-5)
    for _ in range(20):
        freezer.move_to(moment)
        await relay.async_sync()
        if len(waze.calls) == 2:
            break
        moment += timedelta(minutes=15)
    assert moment == KICKOFF + timedelta(days=1, hours=-4, minutes=-15)
    assert waze.realtime_flags() == [False, True]
    assert "DESCRIPTION:Leave at 08:10 (about 230 min drive)" in "\n".join(unfolded(fake_dav, tournament))


@pytest.mark.parametrize(
    ("language", "start", "minutes", "line"),
    [
        ("en", datetime(2026, 9, 19, 22, 15, tzinfo=UTC), 30, "Leave at 23:45 the day before (about 30 min drive)"),
        ("da", datetime(2026, 9, 19, 22, 15, tzinfo=UTC), 30, "Afgang: 23:45 dagen før (ca. 30 min. kørsel)"),
        ("en", datetime(2026, 9, 19, 22, 45, tzinfo=UTC), 30, "Leave at 00:15 (about 30 min drive)"),
        # 03:30 CET, just after summer time ended at 01:00 UTC; the leave time is 02:30 CEST on the same date.
        ("en", datetime(2026, 10, 25, 2, 30, tzinfo=UTC), 120, "Leave at 02:30 (about 120 min drive)"),
    ],
)
async def test_leave_line_names_the_day_before(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    language: str,
    start: datetime,
    minutes: int,
    line: str,
) -> None:
    """A leave time on the evening before a start just after midnight says so; the same day and DST stay plain."""
    hass.config.language = language
    source_calendar.events = [match(start, description=None, location="Example Hall")]
    await setup_entry(hass, make_entry(relay_subentry(travel_time="fixed", travel_minutes=minutes)))
    assert f"DESCRIPTION:{line}" in lines(fake_dav)


async def test_relays_share_one_waze_queue_and_its_answers(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, freezer: FrozenDateTimeFactory
) -> None:
    """Three relays with six matches at one venue make one call, never two at a time; a stale answer is renewed."""
    in_flight: list[ServiceCall] = []
    peak = 0

    class SlowWaze(FakeWaze):
        async def _async_handle(self, call: ServiceCall) -> ServiceResponse:
            nonlocal peak
            in_flight.append(call)
            peak = max(peak, len(in_flight))
            try:
                for _ in range(20):
                    await asyncio.sleep(0)
                return await super()._async_handle(call)
            finally:
                in_flight.remove(call)

    waze = SlowWaze()
    waze.register(hass)
    source_calendar.events = [match(NOW + timedelta(days=1 + number), uid=f"match-{number}") for number in range(6)]
    entry = make_entry(
        relay_subentry("RELAY1", "A", travel_time="waze"),
        relay_subentry("RELAY2", "B", travel_time="waze", target_calendar=WORK_URL, target_calendar_name="Work"),
        relay_subentry("RELAY3", "C", travel_time="waze", title_prefix="Kid: "),
    )
    await setup_entry(hass, entry)
    assert len(waze.calls) == 1
    assert peak == 1
    assert len(fake_dav.resources) == 18
    assert all("X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT25M" in body for body in fake_dav.resources.values())

    hass.config.longitude = 11.5
    freezer.tick(timedelta(minutes=16))
    await entry.runtime_data.relays["RELAY1"].async_sync()
    await entry.runtime_data.relays["RELAY2"].async_sync()
    assert [call["origin"] for call in waze.calls] == [HOME, "55.000000,11.500000"]
