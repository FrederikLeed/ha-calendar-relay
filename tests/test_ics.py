"""Tests for iCalendar rendering."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.calendar_relay.ics import (
    PRODID,
    clean_text,
    content_hash,
    escape_text,
    fold_line,
    render_event,
)

COPENHAGEN = ZoneInfo("Europe/Copenhagen")
STAMP = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def unfold(text: str) -> str:
    """Undo RFC 5545 folding."""
    return text.replace("\r\n ", "")


def test_escape_text() -> None:
    """Backslash, semicolon, comma and every kind of line break are escaped."""
    assert escape_text("a\\b;c,d\ne\r\nf\rg") == "a\\\\b\\;c\\,d\\ne\\nf\\ng"


def test_escape_text_drops_control_characters() -> None:
    """Control characters other than tab cannot appear in TEXT."""
    assert escape_text("a\x00b\x07c\td\x7f") == "abc\td"


def test_short_line_is_not_folded() -> None:
    """A line of exactly 75 octets stays on one line."""
    line = "SUMMARY:" + "x" * 67
    assert len(line.encode()) == 75
    assert fold_line(line) == line


def test_fold_does_not_split_a_two_octet_character() -> None:
    """ø would straddle octet 75, so it moves to the next line."""
    line = "SUMMARY:" + "a" * 66 + "øb"
    assert fold_line(line) == "SUMMARY:" + "a" * 66 + "\r\n øb"


def test_fold_does_not_split_an_emoji() -> None:
    """A 3-octet character that fits exactly stays; a 4-octet one moves."""
    line = "SUMMARY:" + "a" * 64 + "⚽🏆"
    assert fold_line(line) == "SUMMARY:" + "a" * 64 + "⚽\r\n 🏆"


@pytest.mark.parametrize(
    "text",
    [
        "Kamp på Ærø, Øster Åby og Æbeltoft " * 5,
        "⚽🏆⭐ Udtaget: Hjemme - Ude " * 6,
        "👨‍👩‍👧‍👦" * 30,
    ],
)
def test_fold_long_utf8_lines(text: str) -> None:
    """Every folded line is at most 75 octets, valid UTF-8, and as full as possible."""
    line = f"SUMMARY:{text}"
    folded = fold_line(line)
    parts = folded.encode("utf-8").split(b"\r\n")
    assert len(parts) > 1
    for index, part in enumerate(parts):
        assert len(part) <= 75
        decoded = part.decode("utf-8")
        if index:
            assert decoded.startswith(" ")
        if index < len(parts) - 1:
            next_char = parts[index + 1].decode("utf-8")[1]
            assert len(part) + len(next_char.encode("utf-8")) > 75
    assert unfold(folded) == line


def test_render_timed_event_in_utc_across_autumn_dst_change() -> None:
    """Copenhagen leaves summer time during this event; both ends are written in UTC."""
    start = datetime(2026, 10, 25, 1, 30, tzinfo=COPENHAGEN)  # CEST, UTC+2
    end = datetime(2026, 10, 25, 3, 30, tzinfo=COPENHAGEN)  # CET, UTC+1
    ics = render_event(uid="abc@calendar-relay", summary="Night match", start=start, end=end, dtstamp=STAMP)
    assert "DTSTART:20261024T233000Z\r\n" in ics
    assert "DTEND:20261025T023000Z\r\n" in ics
    assert "DTSTAMP:20260915T120000Z\r\n" in ics


def test_render_timed_event_in_utc_across_spring_dst_change() -> None:
    """Copenhagen enters summer time during this event."""
    start = datetime(2026, 3, 29, 1, 30, tzinfo=COPENHAGEN)  # CET, UTC+1
    end = datetime(2026, 3, 29, 3, 30, tzinfo=COPENHAGEN)  # CEST, UTC+2
    ics = render_event(uid="abc@calendar-relay", summary="Early match", start=start, end=end)
    assert "DTSTART:20260329T003000Z\r\n" in ics
    assert "DTEND:20260329T013000Z\r\n" in ics


def test_render_all_day_event() -> None:
    """All-day events use DATE values with an exclusive end."""
    ics = render_event(uid="cup@calendar-relay", summary="Cup day", start=date(2026, 9, 18), end=date(2026, 9, 19))
    assert "DTSTART;VALUE=DATE:20260918\r\n" in ics
    assert "DTEND;VALUE=DATE:20260919\r\n" in ics


def test_render_full_event_structure() -> None:
    """The calendar has the required properties, CRLF endings and nothing that sends invitations or alarms."""
    ics = render_event(
        uid="0123456789abcdef0123456789abcdef@calendar-relay",
        summary="⚽ Emma: Home - Away",
        start=datetime(2026, 9, 20, 10, 0, tzinfo=UTC),
        end=datetime(2026, 9, 20, 11, 30, tzinfo=UTC),
        description="Meet at 9:30; bring water, shin pads\nBus leaves at 8",
        location="Pitch 2, Sports Park",
        dtstamp=STAMP,
    )
    assert ics.endswith("\r\n")
    assert "\n" not in ics.replace("\r\n", "")
    assert unfold(ics).split("\r\n")[:-1] == [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "BEGIN:VEVENT",
        "UID:0123456789abcdef0123456789abcdef@calendar-relay",
        "DTSTAMP:20260915T120000Z",
        "DTSTART:20260920T100000Z",
        "DTEND:20260920T113000Z",
        "SUMMARY:⚽ Emma: Home - Away",
        "DESCRIPTION:Meet at 9:30\\; bring water\\, shin pads\\nBus leaves at 8",
        "LOCATION:Pitch 2\\, Sports Park",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    assert PRODID == "-//calendar-relay//Calendar Relay for Home Assistant//EN"
    for word in ("VALARM", "ORGANIZER", "ATTENDEE"):
        assert word not in ics


def test_empty_description_and_location_are_left_out() -> None:
    """Optional properties only appear when they have a value."""
    ics = render_event(
        uid="u", summary="s", start=date(2026, 9, 18), end=date(2026, 9, 19), description="", location=None
    )
    assert "DESCRIPTION" not in ics
    assert "LOCATION" not in ics
    assert "DTSTAMP" not in ics


def test_content_hash_ignores_nothing_but_dtstamp() -> None:
    """The hash input is rendered without DTSTAMP, so it is stable; any content change alters it."""
    kwargs = {"uid": "u", "summary": "Home - Away", "start": date(2026, 9, 18), "end": date(2026, 9, 19)}
    first = content_hash(render_event(**kwargs))
    assert first == content_hash(render_event(**kwargs))
    assert first != content_hash(render_event(**{**kwargs, "summary": "Away - Home"}))
    assert first != content_hash(render_event(**{**kwargs, "location": "Pitch 3"}))


def test_naive_datetime_is_rejected() -> None:
    """Floating times are never written."""
    with pytest.raises(ValueError):
        render_event(uid="u", summary="s", start=datetime(2026, 9, 20, 10), end=datetime(2026, 9, 20, 11))


def test_event_that_ends_when_it_starts_has_no_dtend() -> None:
    """DTEND must be later than DTSTART, so a zero-length event leaves it out (RFC 5545 3.6.1, 3.8.2.2)."""
    instant = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
    timed = render_event(uid="u", summary="Reminder", start=instant, end=instant)
    assert "DTSTART:20260920T100000Z\r\nSUMMARY:Reminder\r\n" in timed
    assert "DTEND" not in timed
    all_day = render_event(uid="u", summary="Day", start=date(2026, 9, 20), end=date(2026, 9, 20))
    assert "DTSTART;VALUE=DATE:20260920\r\nSUMMARY:Day\r\n" in all_day
    assert "DTEND" not in all_day


def test_lone_surrogates_are_replaced() -> None:
    """A lone surrogate cannot be encoded as UTF-8, so it becomes U+FFFD; a surrogate pair becomes its character."""
    assert clean_text("a😀b\ud83dc") == "a😀b�c"
    assert escape_text("Broken \ud83d, really") == "Broken �\\, really"
    ics = render_event(
        uid="u",
        summary="Broken \ud83d",
        start=date(2026, 9, 20),
        end=date(2026, 9, 21),
        description="\udc00" * 40,
        location="Pitch \ud83d",
    )
    ics.encode("utf-8")
    assert "SUMMARY:Broken �\r\n" in ics
    assert "LOCATION:Pitch �\r\n" in ics


def test_mixed_date_and_datetime_is_rejected() -> None:
    """Start and end must have the same type."""
    with pytest.raises(ValueError):
        render_event(uid="u", summary="s", start=date(2026, 9, 20), end=datetime(2026, 9, 20, 11, tzinfo=UTC))
