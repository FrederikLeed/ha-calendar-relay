"""Tests for the title transform and event identity."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.calendar import CalendarEvent

from custom_components.calendar_relay.relay import (
    event_key,
    has_ended,
    has_started,
    parse_iso,
    resource_id,
    title_matches,
    transform_title,
)

START = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)


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
