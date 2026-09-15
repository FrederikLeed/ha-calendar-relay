"""Tests for iCalendar rendering. Places and coordinates are invented."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.calendar_relay.ics import (
    PRODID,
    StructuredLocation,
    clean_text,
    content_hash,
    escape_text,
    fold_line,
    format_duration,
    format_geo_uri,
    quote_param,
    render_event,
    structured_location_line,
)

COPENHAGEN = ZoneInfo("Europe/Copenhagen")
STAMP = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
KICKOFF = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)


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
    for word in ("VALARM", "ORGANIZER", "ATTENDEE", "X-APPLE"):
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
    kwargs = {"uid": "u", "summary": "Home - Away", "start": KICKOFF, "end": KICKOFF}
    first = content_hash(render_event(**kwargs))
    assert first == content_hash(render_event(**kwargs))
    changes = [
        {"summary": "Away - Home"},
        {"location": "Pitch 3"},
        {"structured_location": StructuredLocation("Example Park", 55.1, 10.2)},
        {"travel_minutes": 25},
        {"travel_minutes": 25, "alarm_minutes": 25},
    ]
    hashes = {content_hash(render_event(**{**kwargs, **change})) for change in changes}
    assert first not in hashes
    assert len(hashes) == len(changes)


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
        structured_location=StructuredLocation("Pitch \ud83d", 55.1, 10.2, "Road \udc00"),
    )
    ics.encode("utf-8")
    assert "SUMMARY:Broken �\r\n" in ics
    assert "LOCATION:Pitch �\r\n" in ics
    assert 'X-ADDRESS="Road �";X-APPLE-RADIUS=71;X-TITLE="Pitch �":' in unfold(ics)


def test_mixed_date_and_datetime_is_rejected() -> None:
    """Start and end must have the same type."""
    with pytest.raises(ValueError):
        render_event(uid="u", summary="s", start=date(2026, 9, 20), end=datetime(2026, 9, 20, 11, tzinfo=UTC))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Example Park", '"Example Park"'),
        ("Road 1, 1234 Sampletown; Hall: B", '"Road 1, 1234 Sampletown; Hall: B"'),
        ("Example Park\nRoad 1\r\nSampletown\rDenmark", '"Example Park\\nRoad 1\\nSampletown\\nDenmark"'),
        ('The "Big" Hall', "\"The 'Big' Hall\""),
        ("Bad\x00\x07\x7f\tchars", '"Bad\tchars"'),
        ("Ærø Idrætspark ⚽", '"Ærø Idrætspark ⚽"'),
        ("Back\\slash", '"Back\\slash"'),
        ("", '""'),
        # Sequences readers would decode are neutralised, so the value reads back as one line without DQUOTEs.
        ("Hall A\\nB and C\\ND", '"Hall A/nB and C/ND"'),
        ("Caf^n ^' x ^^ y ^x ^N", '"Cafn \' x ^ y ^x N"'),
        ('Quote^"d', '"Quote\'d"'),
        ("Back\\^n", '"Back/n"'),
        ("Caret^\\n", '"Caret^/n"'),
        ("Line^\nbreak", '"Line^\\nbreak"'),
    ],
)
def test_quote_param(value: str, expected: str) -> None:
    """Values are always quoted; a line break becomes backslash n, DQUOTE an apostrophe, nothing else is escaped."""
    assert quote_param(value) == expected


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(0, "PT0M"), (5, "PT5M"), (45, "PT45M"), (60, "PT1H"), (90, "PT1H30M"), (125, "PT2H5M"), (1440, "PT24H")],
)
def test_format_duration(minutes: int, expected: str) -> None:
    """Durations are written with hours and minutes, the way Apple writes them."""
    assert format_duration(minutes) == expected


def test_negative_duration_is_rejected() -> None:
    """A travel time is never negative."""
    with pytest.raises(ValueError):
        format_duration(-5)


def test_format_geo_uri() -> None:
    """Latitude and longitude are separated by a comma, with 6 decimals and a dot."""
    assert format_geo_uri(55.1234567, -10.2) == "geo:55.123457,-10.200000"
    assert format_geo_uri(-90, 180) == "geo:-90.000000,180.000000"


def test_render_location_details_travel_time_and_alarm() -> None:
    """Structured location follows LOCATION; travel duration and the alarm come before the end of the event."""
    ics = render_event(
        uid="u@calendar-relay",
        summary="⚽ Emma: Home - Away",
        start=KICKOFF,
        end=datetime(2026, 9, 20, 11, 30, tzinfo=UTC),
        description="Bring water",
        location="Example Stadium\nExample Road 1, 1234 Sampletown",
        structured_location=StructuredLocation(
            "Example Stadium", 55.123456, 10.654321, "Example Road 1, 1234 Sampletown"
        ),
        travel_minutes=35,
        alarm_minutes=35,
        dtstamp=STAMP,
    )
    assert unfold(ics).split("\r\n")[:-1] == [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "BEGIN:VEVENT",
        "UID:u@calendar-relay",
        "DTSTAMP:20260915T120000Z",
        "DTSTART:20260920T100000Z",
        "DTEND:20260920T113000Z",
        "SUMMARY:⚽ Emma: Home - Away",
        "DESCRIPTION:Bring water",
        "LOCATION:Example Stadium\\nExample Road 1\\, 1234 Sampletown",
        'X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-ADDRESS="Example Road 1, 1234 Sampletown";X-APPLE-RADIUS=71;'
        'X-TITLE="Example Stadium":geo:55.123456,10.654321',
        "X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT35M",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        "DESCRIPTION:⚽ Emma: Home - Away",
        "TRIGGER:-PT35M",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    for word in ("X-APPLE-TRAVEL-START", "X-APPLE-TRAVEL-ADVISORY-BEHAVIOR", "X-APPLE-MAPKIT-HANDLE", "GEO:"):
        assert word not in ics


def test_structured_location_without_address() -> None:
    """Without an address only the title is written, as Apple Calendar exports a one-line place."""
    line = structured_location_line(StructuredLocation("Example Park, Road 1, 1234 Sampletown", 55.5, -3.25))
    assert line == (
        'X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-APPLE-RADIUS=71;X-TITLE="Example Park, Road 1, 1234 Sampletown"'
        ":geo:55.500000,-3.250000"
    )


def test_structured_location_folds_without_splitting_characters() -> None:
    """A long structured location is folded inside its parameters, never through a multi-byte character."""
    prefix = 'X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-APPLE-RADIUS=71;X-TITLE="'
    assert len(prefix.encode()) == 65
    title = "a" * 9 + "ø Idrætspark ⚽, Øster Allé 12; Hal: B " * 3
    ics = render_event(
        uid="u",
        summary="s",
        start=KICKOFF,
        end=KICKOFF,
        location=title,
        structured_location=StructuredLocation(title, 55.5, 10.25),
    )
    physical = ics.split("\r\n")
    index = next(i for i, line in enumerate(physical) if line.startswith("X-APPLE-STRUCTURED-LOCATION"))
    assert physical[index] == prefix + "a" * 9
    assert physical[index + 1].startswith(" ø")
    continuation = [physical[index]]
    for line in physical[index + 1 :]:
        if not line.startswith(" "):
            break
        continuation.append(line)
    assert len(continuation) >= 3
    assert all(len(line.encode("utf-8")) <= 75 for line in continuation)
    assert "".join([continuation[0], *(line[1:] for line in continuation[1:])]) == (
        f'{prefix}{title}":geo:55.500000,10.250000'
    )
