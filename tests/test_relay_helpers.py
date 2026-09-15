"""Tests for the title transform, event identity and the travel time helpers. Places are invented."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.calendar import CalendarEvent

from custom_components.calendar_relay.ics import StructuredLocation
from custom_components.calendar_relay.location import Coordinates
from custom_components.calendar_relay.relay import (
    RelayConfig,
    TravelRecord,
    TravelRetry,
    event_key,
    has_ended,
    has_started,
    is_danish,
    leave_text,
    parse_iso,
    place_fingerprint,
    resource_id,
    retry_delay,
    round_travel_minutes,
    structured_location,
    title_matches,
    transform_title,
    travel_destination,
    travel_fingerprint,
)

START = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
POINT = Coordinates(55.1, 10.2, "Example Park")


@pytest.mark.parametrize(
    ("title", "title_filter", "expected"),
    [
        ("⭐ Udtaget: Home - Away", "⭐ Udtaget: ", True),
        ("⭐ UDTAGET: Home - Away", "⭐ udtaget: ", True),
        ("Home - Away", "⭐ Udtaget: ", False),
        ("Træning på Ærø", "ÆRØ", True),
        ("Anything", "", True),
        ("Price (a+b)", "(a+b)", True),
    ],
)
def test_title_matches(title: str, title_filter: str, expected: bool) -> None:
    """The filter is a case-insensitive substring, not a pattern."""
    assert title_matches(title, title_filter) is expected


@pytest.mark.parametrize(
    ("title", "title_filter", "remove", "prefix", "expected"),
    [
        ("⭐ Udtaget: Home - Away", "⭐ Udtaget: ", True, "⚽ Emma: ", "⚽ Emma: Home - Away"),
        ("⭐ Udtaget: Home - Away", "⭐ Udtaget: ", False, "⚽ Emma: ", "⚽ Emma: ⭐ Udtaget: Home - Away"),
        ("udtaget:  - Home - Away", "Udtaget", True, "", "Home - Away"),
        ("Home - Away udtaget", "udtaget", True, "", "Home - Away"),
        ("⭐ Udtaget: ", "⭐ Udtaget: ", True, "", "⭐ Udtaget: "),
        ("Training", "", True, "Emma: ", "Emma: Training"),
        ("Training", "", False, "", "Training"),
    ],
)
def test_transform_title(title: str, title_filter: str, remove: bool, prefix: str, expected: str) -> None:
    """Remove the filter text and leftover leading separators when asked, then add the prefix."""
    assert transform_title(title, title_filter=title_filter, remove_filter=remove, prefix=prefix) == expected


def test_event_key_uses_uid_and_recurrence_id() -> None:
    """With a uid, summary and time do not matter; each recurrence instance is its own key."""
    first = CalendarEvent(
        start=START, end=START + timedelta(hours=1), summary="League", uid="u1", recurrence_id="20260920T100000"
    )
    second = CalendarEvent(
        start=START + timedelta(days=7),
        end=START + timedelta(days=7, hours=1),
        summary="League",
        uid="u1",
        recurrence_id="20260927T100000",
    )
    moved = CalendarEvent(
        start=START + timedelta(hours=2),
        end=START + timedelta(hours=3),
        summary="Renamed",
        uid="u1",
        recurrence_id="20260920T100000",
    )
    single = CalendarEvent(start=START, end=START + timedelta(hours=1), summary="League", uid="u1")
    assert event_key(first) != event_key(second)
    assert event_key(first) == event_key(moved)
    assert event_key(single) not in (event_key(first), event_key(second))


def test_event_key_without_uid_hashes_summary_and_start() -> None:
    """Without a uid the key is a hash of summary and start, independent of the time zone used."""
    local = START.astimezone(ZoneInfo("Europe/Copenhagen"))
    a = CalendarEvent(start=START, end=START + timedelta(hours=1), summary="Match")
    b = CalendarEvent(start=local, end=local + timedelta(hours=2), summary="Match")
    c = CalendarEvent(start=START + timedelta(hours=1), end=START + timedelta(hours=2), summary="Match")
    d = CalendarEvent(start=date(2026, 9, 20), end=date(2026, 9, 21), summary="Match")
    assert event_key(a) == event_key(b)
    assert event_key(a) != event_key(c)
    assert event_key(a) != event_key(d)
    assert re.fullmatch(r"hash:[0-9a-f]{32}", event_key(a))


def test_resource_id_depends_on_relay_and_key() -> None:
    """The id is 32 hex characters and differs between relays for the same event."""
    key = event_key(CalendarEvent(start=START, end=START + timedelta(hours=1), summary="Match", uid="u1"))
    assert re.fullmatch(r"[0-9a-f]{32}", resource_id("relay-a", key))
    assert resource_id("relay-a", key) == resource_id("relay-a", key)
    assert resource_id("relay-a", key) != resource_id("relay-b", key)


def test_started_and_ended() -> None:
    """Timed events compare instants; all-day events compare dates (end exclusive)."""
    now = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
    assert has_started(now, now)
    assert not has_started(now + timedelta(seconds=1), now)
    assert has_started(date(2026, 9, 20), now)
    assert not has_started(date(2026, 9, 21), now)
    assert has_ended(now, now)
    assert not has_ended(now + timedelta(minutes=1), now)
    assert has_ended(date(2026, 9, 20), now)
    assert not has_ended(date(2026, 9, 21), now)


def test_parse_iso_round_trip() -> None:
    """Stored start and end values parse back to dates or UTC datetimes."""
    assert parse_iso("2026-09-20") == date(2026, 9, 20)
    assert parse_iso("2026-09-20T10:00:00+00:00") == START


@pytest.mark.parametrize(
    ("duration", "expected"),
    [(0, 0), (0.1, 5), (5, 5), (5.01, 10), (23.4, 25), (25.0000000001, 25), (25.001, 30), (61, 65), (240.5, 245)],
)
def test_round_travel_minutes(duration: float, expected: int) -> None:
    """Travel times are rounded up to 5 minutes, ignoring float noise."""
    assert round_travel_minutes(duration) == expected


@pytest.mark.parametrize(
    ("language", "expected"),
    [("da", True), ("da-DK", True), ("DA_dk", True), ("en", False), ("en-GB", False), ("nb", False), ("", False)],
)
def test_is_danish(language: str, expected: bool) -> None:
    """Only the primary language subtag counts."""
    assert is_danish(language) is expected
    assert is_danish(None) is False


def test_leave_text_in_both_languages() -> None:
    """Danish gets the Danish line; every other language gets English; the time is 24-hour with leading zeros."""
    leave_at = datetime(2026, 9, 20, 9, 5, 30)
    assert leave_text("da", leave_at, 25) == "Afgang: 09:05 (ca. 25 min. kørsel)"
    assert leave_text("da-DK", leave_at, 120) == "Afgang: 09:05 (ca. 120 min. kørsel)"
    assert leave_text("en", leave_at, 5) == "Leave at 09:05 (about 5 min drive)"
    assert leave_text("de", datetime(2026, 9, 20, 23, 50), 45) == "Leave at 23:50 (about 45 min drive)"


def test_leave_text_on_an_earlier_day() -> None:
    """A leave time on an earlier date than the start says so, so 23:45 is not read as the event's own evening."""
    leave_at = datetime(2026, 9, 19, 23, 45)
    assert leave_text("en", leave_at, 30, 0) == "Leave at 23:45 (about 30 min drive)"
    assert leave_text("en", leave_at, 30, 1) == "Leave at 23:45 the day before (about 30 min drive)"
    assert leave_text("da", leave_at, 30, 1) == "Afgang: 23:45 dagen før (ca. 30 min. kørsel)"
    assert leave_text("en-GB", leave_at, 480, 2) == "Leave at 23:45 2 days before (about 480 min drive)"
    assert leave_text("da-DK", leave_at, 480, 2) == "Afgang: 23:45 2 dage før (ca. 480 min. kørsel)"


def test_leave_text_names_arrive_early() -> None:
    """Arrive early is named after the drive when it is not 0, so the line adds up to the leave time."""
    leave_at = datetime(2026, 9, 19, 9, 55)
    assert leave_text("da", leave_at, 50, 0, 15) == "Afgang: 09:55 (ca. 50 min. kørsel + 15 min. før tid)"
    assert leave_text("en", leave_at, 50, 0, 15) == "Leave at 09:55 (about 50 min drive + 15 min early)"
    assert leave_text("da", leave_at, 50, early_minutes=0) == "Afgang: 09:55 (ca. 50 min. kørsel)"
    assert leave_text("en", leave_at, 50, early_minutes=0) == "Leave at 09:55 (about 50 min drive)"
    evening = datetime(2026, 9, 19, 23, 45)
    assert leave_text("da-DK", evening, 30, 1, 5) == "Afgang: 23:45 dagen før (ca. 30 min. kørsel + 5 min. før tid)"
    assert leave_text("en", evening, 30, 1, 5) == "Leave at 23:45 the day before (about 30 min drive + 5 min early)"


@pytest.mark.parametrize(
    ("location", "coordinates", "expected"),
    [
        ("Example Park, Road 1", POINT, StructuredLocation("Example Park, Road 1", 55.1, 10.2)),
        (
            "  Example Park\r\nRoad 1\nSampletown  ",
            POINT,
            StructuredLocation("Example Park", 55.1, 10.2, "Road 1\nSampletown"),
        ),
        (None, POINT, StructuredLocation("Example Park", 55.1, 10.2)),
        ("   ", POINT, StructuredLocation("Example Park", 55.1, 10.2)),
        ("", Coordinates(55.1, 10.2), None),
        ("Example Park", None, None),
    ],
)
def test_structured_location(
    location: str | None, coordinates: Coordinates | None, expected: StructuredLocation | None
) -> None:
    """LOCATION's first line is the title, the other lines the address; the link's name stands in for LOCATION."""
    assert structured_location(location, coordinates) == expected


@pytest.mark.parametrize(
    ("location", "coordinates", "expected"),
    [
        ("Example Park", Coordinates(55.1234567, -10.2), "55.123457,-10.200000"),
        (" Example Hall \n\n Road 2, 1234 Sampletown ", None, "Example Hall, Road 2, 1234 Sampletown"),
        ("  ", None, None),
        (None, None, None),
    ],
)
def test_travel_destination(location: str | None, coordinates: Coordinates | None, expected: str | None) -> None:
    """Coordinates win over the location text, which is put on one line."""
    assert travel_destination(location, coordinates) == expected


def test_travel_fingerprint() -> None:
    """The fingerprint changes with origin, destination and region, and does not contain them."""
    base = travel_fingerprint("55.000000,11.000000", "Example Hall", "eu")
    assert re.fullmatch(r"[0-9a-f]{32}", base)
    assert base == travel_fingerprint("55.000000,11.000000", "Example Hall", "eu")
    others = {
        travel_fingerprint("55.500000,11.000000", "Example Hall", "eu"),
        travel_fingerprint("55.000000,11.000000", "Example Arena", "eu"),
        travel_fingerprint("55.000000,11.000000", "Example Hall", "us"),
    }
    assert base not in others
    assert len(others) == 3


def test_relay_config_defaults_for_relays_saved_before_the_travel_settings() -> None:
    """Old subentry data needs no migration: the new settings get their defaults, and unknown values fall back."""
    data = {"source_calendar": "calendar.kids", "target_calendar": "https://caldav.example.com/family/"}
    config = RelayConfig.from_data(data)
    assert (
        config.structured_location,
        config.travel_time,
        config.travel_minutes,
        config.waze_region,
        config.buffer_minutes,
        config.leave_reminder,
    ) == (True, "off", 15, "eu", 0, False)
    odd = RelayConfig.from_data({**data, "travel_time": "teleport", "waze_region": "moon", "travel_minutes": 20.0})
    assert (odd.travel_time, odd.waze_region, odd.travel_minutes) == ("off", "eu", 20)


def test_place_fingerprint() -> None:
    """The place hash only depends on the destination and differs from the full fingerprint."""
    base = place_fingerprint("Example Hall")
    assert re.fullmatch(r"[0-9a-f]{32}", base)
    assert base == place_fingerprint("Example Hall")
    assert base != place_fingerprint("Example Arena")
    assert base != travel_fingerprint("55.000000,11.000000", "Example Hall", "eu")


RECORD = {
    "minutes": 25,
    "fingerprint": "f",
    "place": "p",
    "computed_at": "t",
    "realtime": False,
    "start": "2026-09-20T10:00:00+00:00",
}


def test_travel_record_round_trip() -> None:
    """A stored record reads back as itself, up to the maximum travel time."""
    record = TravelRecord(25, "f" * 32, "p" * 32, "2026-09-15T10:00:00+00:00", False, "2026-09-20T10:00:00+00:00")
    assert TravelRecord.from_dict(record.as_dict()) == record
    assert TravelRecord.from_dict({**RECORD, "minutes": 480}) is not None


@pytest.mark.parametrize(
    "value",
    [
        None,
        "25",
        {**RECORD, "minutes": "25"},
        {**RECORD, "minutes": True},
        {**RECORD, "minutes": -5},
        {**RECORD, "minutes": 485},
        {**RECORD, "minutes": 2_000_000_000},
        {**RECORD, "fingerprint": 1},
        {**RECORD, "place": None},
        {**RECORD, "computed_at": None},
        {**RECORD, "start": 7},
        {**RECORD, "realtime": "yes"},
        {name: value for name, value in RECORD.items() if name != "realtime"},
        {name: value for name, value in RECORD.items() if name not in ("place", "start")},
    ],
)
def test_invalid_travel_records(value: Any) -> None:
    """Anything that is not a complete record with the right types, or has an absurd travel time, is dropped."""
    assert TravelRecord.from_dict(value) is None


def test_travel_retry_round_trip() -> None:
    """A stored retry reads back as itself, in UTC."""
    retry = TravelRetry("f" * 32, 3, datetime(2026, 9, 15, 14, 0, tzinfo=UTC), "Travel time: Waze Travel Time failed")
    assert TravelRetry.from_dict(retry.as_dict()) == retry
    stored = {**retry.as_dict(), "retry_at": "2026-09-15T16:00:00+02:00"}
    assert TravelRetry.from_dict(stored) == retry


RETRY = {"fingerprint": "f", "failures": 1, "retry_at": "2026-09-15T11:00:00+00:00", "error": "Travel time: x"}


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {**RETRY, "failures": 0},
        {**RETRY, "failures": True},
        {**RETRY, "failures": "2"},
        {**RETRY, "fingerprint": None},
        {**RETRY, "error": 5},
        {**RETRY, "retry_at": "soon"},
        {**RETRY, "retry_at": "2026-09-15T11:00:00"},
        {**RETRY, "retry_at": 1757934000},
    ],
)
def test_invalid_travel_retries(value: Any) -> None:
    """A retry with missing or wrong values, or a time without a zone, is dropped."""
    assert TravelRetry.from_dict(value) is None


@pytest.mark.parametrize(
    ("failures", "hours"), [(1, 1), (2, 2), (3, 4), (4, 8), (5, 16), (6, 24), (7, 24), (10_000, 24)]
)
def test_retry_delay_doubles_up_to_a_day(failures: int, hours: int) -> None:
    """The wait doubles with every failure in a row and never exceeds 24 hours, however many failures are stored."""
    assert retry_delay(failures) == timedelta(hours=hours)
