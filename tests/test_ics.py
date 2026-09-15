"""Tests for iCalendar rendering. Places and coordinates are invented."""

from __future__ import annotations

import io
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from dateutil import tz
from freezegun.api import FrozenDateTimeFactory

from custom_components.calendar_relay.ics import (
    PRODID,
    StructuredLocation,
    clean_text,
    content_hash,
    escape_text,
    fold_line,
    format_duration,
    format_geo_uri,
    format_offset,
    quote_param,
    render_event,
    structured_location_line,
    vtimezone_lines,
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
        {"time_zone": "Europe/Copenhagen"},
        {"time_zone": "Asia/Tokyo"},
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


# Copenhagen: summer time from 02:00 on the last Sunday of March to 03:00 on the last Sunday of October, every year.
COPENHAGEN_VTIMEZONE = [
    "BEGIN:VTIMEZONE",
    "TZID:Europe/Copenhagen",
    "BEGIN:DAYLIGHT",
    "DTSTART:19700329T020000",
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
    "TZOFFSETFROM:+0100",
    "TZOFFSETTO:+0200",
    "TZNAME:CEST",
    "END:DAYLIGHT",
    "BEGIN:STANDARD",
    "DTSTART:19701025T030000",
    "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
    "TZOFFSETFROM:+0200",
    "TZOFFSETTO:+0100",
    "TZNAME:CET",
    "END:STANDARD",
    "END:VTIMEZONE",
]


def vtimezone_of(ics: str) -> str:
    """Return the unfolded VTIMEZONE of an event."""
    lines = unfold(ics).split("\r\n")
    return "\r\n".join(lines[lines.index("BEGIN:VTIMEZONE") : lines.index("END:VTIMEZONE") + 1])


def read_back(ics: str, name: str) -> datetime:
    """Read DTSTART or DTEND back as a UTC moment, with the event's own VTIMEZONE read by dateutil.

    dateutil reads VTIMEZONEs for vobject, the parser of Radicale and Home Assistant's CalDAV integration.
    A local time that occurs twice reads as its first occurrence (RFC 5545 3.3.5).
    """
    lines = unfold(ics).split("\r\n")
    event = lines[lines.index("BEGIN:VEVENT") :]
    prop = next(line for line in event if line.startswith((f"{name}:", f"{name};")))
    value = prop.partition(":")[2]
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    zone = tz.tzical(io.StringIO(vtimezone_of(ics))).get()
    return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=zone).astimezone(UTC)


def test_render_summer_event_in_the_home_time_zone() -> None:
    """A source time in another zone is written as Copenhagen local time with a TZID and one VTIMEZONE."""
    new_york = ZoneInfo("America/New_York")
    ics = render_event(
        uid="u@calendar-relay",
        summary="⚽ Emma: Home - Away",
        start=datetime(2026, 9, 19, 5, 0, tzinfo=new_york),
        end=datetime(2026, 9, 19, 7, 20, tzinfo=new_york),
        dtstamp=STAMP,
        time_zone="Europe/Copenhagen",
    )
    assert unfold(ics).split("\r\n")[:-1] == [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        *COPENHAGEN_VTIMEZONE,
        "BEGIN:VEVENT",
        "UID:u@calendar-relay",
        "DTSTAMP:20260915T120000Z",
        "DTSTART;TZID=Europe/Copenhagen:20260919T110000",
        "DTEND;TZID=Europe/Copenhagen:20260919T132000",
        "SUMMARY:⚽ Emma: Home - Away",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    assert ics.count("BEGIN:VTIMEZONE") == 1
    assert read_back(ics, "DTSTART") == datetime(2026, 9, 19, 9, 0, tzinfo=UTC)
    assert read_back(ics, "DTEND") == datetime(2026, 9, 19, 11, 20, tzinfo=UTC)


def test_render_winter_event_in_the_home_time_zone() -> None:
    """A December event is written in CET, with the same VTIMEZONE as a summer event."""
    ics = render_event(
        uid="u",
        summary="Indoor match",
        start=datetime(2026, 12, 5, 9, 0, tzinfo=UTC),
        end=datetime(2026, 12, 5, 10, 30, tzinfo=UTC),
        time_zone="Europe/Copenhagen",
    )
    lines = unfold(ics).split("\r\n")
    assert lines[3 : 3 + len(COPENHAGEN_VTIMEZONE)] == COPENHAGEN_VTIMEZONE
    assert "DTSTART;TZID=Europe/Copenhagen:20261205T100000" in lines
    assert "DTEND;TZID=Europe/Copenhagen:20261205T113000" in lines
    assert read_back(ics, "DTSTART") == datetime(2026, 12, 5, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("start", "end", "dtstart", "dtend"),
    [
        # Across the October change: 01:30 CEST to 03:30 CET.
        (
            datetime(2026, 10, 24, 23, 30, tzinfo=UTC),
            datetime(2026, 10, 25, 2, 30, tzinfo=UTC),
            "DTSTART;TZID=Europe/Copenhagen:20261025T013000",
            "DTEND;TZID=Europe/Copenhagen:20261025T033000",
        ),
        # Across the March change: 01:30 CET to 03:30 CEST.
        (
            datetime(2026, 3, 29, 0, 30, tzinfo=UTC),
            datetime(2026, 3, 29, 1, 30, tzinfo=UTC),
            "DTSTART;TZID=Europe/Copenhagen:20260329T013000",
            "DTEND;TZID=Europe/Copenhagen:20260329T033000",
        ),
        # 02:30 happens twice in October. The first time reads back right; the second would read as the first.
        (
            datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
            datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
            "DTSTART;TZID=Europe/Copenhagen:20261025T023000",
            "DTEND:20261025T013000Z",
        ),
        # The last minute of summer time, to the moment it ends: 02:00 for the second time, so in UTC.
        (
            datetime(2026, 10, 25, 0, 59, tzinfo=UTC),
            datetime(2026, 10, 25, 1, 0, tzinfo=UTC),
            "DTSTART;TZID=Europe/Copenhagen:20261025T025900",
            "DTEND:20261025T010000Z",
        ),
        # The first minute of winter time after the repeated hour.
        (
            datetime(2026, 10, 25, 2, 0, tzinfo=UTC),
            datetime(2026, 10, 25, 2, 1, tzinfo=UTC),
            "DTSTART;TZID=Europe/Copenhagen:20261025T030000",
            "DTEND;TZID=Europe/Copenhagen:20261025T030100",
        ),
    ],
)
def test_render_event_close_to_a_dst_change(start: datetime, end: datetime, dtstart: str, dtend: str) -> None:
    """Every time written reads back as the moment it was, with the offset in effect at that moment."""
    ics = render_event(uid="u", summary="Night match", start=start, end=end, time_zone="Europe/Copenhagen")
    lines = unfold(ics).split("\r\n")
    assert dtstart in lines
    assert dtend in lines
    assert lines[3 : 3 + len(COPENHAGEN_VTIMEZONE)] == COPENHAGEN_VTIMEZONE
    assert read_back(ics, "DTSTART") == start
    assert read_back(ics, "DTEND") == end


def test_event_only_in_the_repeated_hour_is_written_in_utc_without_vtimezone() -> None:
    """When no time can be written as local time, no TZID is referenced, so no VTIMEZONE is written."""
    start = datetime(2026, 10, 25, 1, 10, tzinfo=UTC)  # 02:10 CET, the second 02:10 of the night
    ics = render_event(
        uid="u", summary="s", start=start, end=start + timedelta(minutes=30), time_zone="Europe/Copenhagen"
    )
    assert "DTSTART:20261025T011000Z\r\nDTEND:20261025T014000Z\r\n" in ics
    assert "VTIMEZONE" not in ics


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (datetime(2026, 12, 5, 9, 0, tzinfo=UTC), datetime(2026, 12, 5, 10, 30, tzinfo=UTC)),
        (datetime(2027, 6, 5, 8, 0, tzinfo=UTC), datetime(2027, 6, 5, 9, 0, tzinfo=UTC)),
        # Over New Year.
        (datetime(2026, 12, 31, 22, 0, tzinfo=UTC), datetime(2027, 1, 1, 1, 0, tzinfo=UTC)),
        # Years ahead, across the October change, and beyond the 28 years the rules are derived from.
        (datetime(2037, 10, 24, 23, 30, tzinfo=UTC), datetime(2037, 10, 25, 2, 30, tzinfo=UTC)),
        (datetime(2080, 7, 1, 10, 0, tzinfo=UTC), datetime(2080, 7, 1, 11, 0, tzinfo=UTC)),
    ],
)
def test_every_event_in_a_zone_gets_the_same_vtimezone(start: datetime, end: datetime) -> None:
    """vobject keeps the first VTIMEZONE it reads for a TZID for every later object, so it cannot depend on dates.

    vobject is the parser of Radicale and of Home Assistant's CalDAV integration. Every event gets the
    yearly rules, and reads back as the moment it was.
    """
    ics = render_event(uid="u", summary="s", start=start, end=end, time_zone="Europe/Copenhagen")
    assert unfold(ics).split("\r\n")[3 : 3 + len(COPENHAGEN_VTIMEZONE)] == COPENHAGEN_VTIMEZONE
    assert read_back(ics, "DTSTART") == start
    assert read_back(ics, "DTEND") == end


def test_vtimezone_does_not_depend_on_the_year_it_is_written_in() -> None:
    """The yearly rules are the same whichever year they are derived in and whichever event years they serve."""
    expected = tuple(COPENHAGEN_VTIMEZONE)
    assert vtimezone_lines("Europe/Copenhagen", 2026) == expected
    assert vtimezone_lines("Europe/Copenhagen", 2040, (2026, 2027)) == expected
    assert vtimezone_lines("Europe/Copenhagen", 2026, (2080,)) == expected


@pytest.mark.parametrize(
    ("time_zone", "rules"),
    [
        # Summer time starts on the Friday before the last Sunday of March: the Friday on or after the 23rd.
        (
            "Asia/Jerusalem",
            [
                "DTSTART:19700327T020000",
                "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=FR;BYMONTHDAY=23,24,25,26,27,28,29",
                "DTSTART:19701025T020000",
                "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
            ],
        ),
        # Summer time starts at 23:00 on the Saturday before the last Sunday of March.
        (
            "America/Nuuk",
            [
                "DTSTART:19700328T230000",
                "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=SA;BYMONTHDAY=24,25,26,27,28,29,30",
                "DTSTART:19701025T000000",
                "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
            ],
        ),
    ],
)
def test_rule_on_a_weekday_on_or_after_a_day_of_the_month(time_zone: str, rules: list[str]) -> None:
    """A rule that is not the nth or last weekday of a month is written with BYMONTHDAY, as Apple writes it.

    Times around each change for ten years read back as the moments they were.
    """
    lines = vtimezone_lines(time_zone, 2026)
    assert [line for line in lines if line.startswith(("DTSTART", "RRULE"))] == rules
    zone = ZoneInfo(time_zone)
    changes = 0
    for year in range(2026, 2036):
        for month in (3, 10):
            moment = datetime(year, month, 20, tzinfo=UTC)
            while moment.month == month:
                after = moment + timedelta(hours=1)
                if moment.astimezone(zone).utcoffset() != after.astimezone(zone).utcoffset():
                    changes += 1
                    for minutes in (-90, -30, 0, 30, 60, 90):
                        start = moment + timedelta(minutes=minutes)
                        end = start + timedelta(hours=1)
                        ics = render_event(uid="u", summary="s", start=start, end=end, time_zone=time_zone)
                        assert read_back(ics, "DTSTART") == start
                        assert read_back(ics, "DTEND") == end
                moment = after
    assert changes == 20


def test_zone_without_yearly_rules_gets_onsets_shared_by_the_events_of_a_year(freezer: FrozenDateTimeFactory) -> None:
    """Morocco's clocks go back for Ramadan, which moves every year, so no yearly rule fits.

    Events written in a year get explicit onsets for that year and the next two, so they share one
    VTIMEZONE. An event in a later year adds its own year.
    """
    freezer.move_to(datetime(2026, 9, 15, 10, 0, tzinfo=UTC))
    starts = [
        datetime(2026, 9, 20, 10, 0, tzinfo=UTC),
        datetime(2027, 2, 20, 10, 0, tzinfo=UTC),
        datetime(2027, 3, 14, 5, 0, tzinfo=UTC),
    ]
    bodies = [
        render_event(uid="u", summary="s", start=start, end=start + timedelta(hours=1), time_zone="Africa/Casablanca")
        for start in starts
    ]
    assert len({vtimezone_of(body) for body in bodies}) == 1
    lines = vtimezone_of(bodies[0]).split("\r\n")
    assert not [line for line in lines if line.startswith("RRULE")]
    assert [line for line in lines if line.startswith("DTSTART")] == [
        "DTSTART:20260101T000000",
        "DTSTART:20260215T030000",
        "DTSTART:20260322T020000",
        "DTSTART:20270207T030000",
        "DTSTART:20270314T020000",
        "DTSTART:20280123T030000",
        "DTSTART:20280305T020000",
    ]
    for start, body in zip(starts, bodies, strict=True):
        assert read_back(body, "DTSTART") == start
        assert read_back(body, "DTEND") == start + timedelta(hours=1)
    assert "DTSTART:20310101T000000" in vtimezone_lines("Africa/Casablanca", 2026, (2031,))


@pytest.mark.parametrize(
    ("time_zone", "this_year", "event_year"),
    [
        # Summer time in New York started on the last Sunday in April until 1986, then on the first.
        ("America/New_York", 1976, 1976),
        # In 2007 it moved to the second Sunday in March.
        ("America/New_York", 1990, 1990),
        # Denmark had no summer time in 1975, so an event then does not follow today's rules.
        ("Europe/Copenhagen", 2026, 1975),
        # Yearly rules are written from 1970, so they do not cover an event before it.
        ("Asia/Tokyo", 2026, 1960),
    ],
)
def test_rules_that_do_not_hold_give_explicit_onsets(time_zone: str, this_year: int, event_year: int) -> None:
    """A rule change within the 28 years, or an event year the rules do not cover, gives explicit onsets."""
    lines = vtimezone_lines(time_zone, this_year, (event_year,))
    assert not [line for line in lines if line.startswith("RRULE")]
    assert next(line for line in lines if line.startswith("DTSTART")) == f"DTSTART:{event_year}0101T000000"
    start = datetime(event_year, 7, 1, 10, 0, tzinfo=UTC)
    ics = render_event(uid="u", summary="s", start=start, end=start + timedelta(hours=1), time_zone=time_zone)
    assert "RRULE" not in ics
    assert read_back(ics, "DTSTART") == start


def test_zone_without_daylight_saving_time_has_one_standard_observance() -> None:
    """Tokyo has had no daylight saving time since 1951."""
    ics = render_event(
        uid="u",
        summary="s",
        start=datetime(2026, 7, 4, 1, 0, tzinfo=UTC),
        end=datetime(2026, 7, 4, 2, 0, tzinfo=UTC),
        time_zone="Asia/Tokyo",
    )
    lines = unfold(ics).split("\r\n")
    assert lines[3:12] == [
        "BEGIN:VTIMEZONE",
        "TZID:Asia/Tokyo",
        "BEGIN:STANDARD",
        "DTSTART:19700101T000000",
        "TZOFFSETFROM:+0900",
        "TZOFFSETTO:+0900",
        "TZNAME:JST",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]
    assert "DTSTART;TZID=Asia/Tokyo:20260704T100000" in lines
    assert read_back(ics, "DTEND") == datetime(2026, 7, 4, 2, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("start", "dtstart"),
    [
        (datetime(2026, 1, 10, 0, 0, tzinfo=UTC), "DTSTART;TZID=Australia/Sydney:20260110T110000"),
        (datetime(2026, 6, 10, 1, 0, tzinfo=UTC), "DTSTART;TZID=Australia/Sydney:20260610T110000"),
        (datetime(2026, 10, 10, 0, 0, tzinfo=UTC), "DTSTART;TZID=Australia/Sydney:20261010T110000"),
    ],
)
def test_southern_hemisphere_zone_has_summer_time_over_new_year(start: datetime, dtstart: str) -> None:
    """Sydney leaves summer time on the first Sunday of April and starts it again on the first Sunday of October."""
    ics = render_event(uid="u", summary="s", start=start, end=start + timedelta(hours=1), time_zone="Australia/Sydney")
    lines = unfold(ics).split("\r\n")
    assert lines[3:20] == [
        "BEGIN:VTIMEZONE",
        "TZID:Australia/Sydney",
        "BEGIN:STANDARD",
        "DTSTART:19700405T030000",
        "RRULE:FREQ=YEARLY;BYMONTH=4;BYDAY=1SU",
        "TZOFFSETFROM:+1100",
        "TZOFFSETTO:+1000",
        "TZNAME:AEST",
        "END:STANDARD",
        "BEGIN:DAYLIGHT",
        "DTSTART:19701004T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=1SU",
        "TZOFFSETFROM:+1000",
        "TZOFFSETTO:+1100",
        "TZNAME:AEDT",
        "END:DAYLIGHT",
        "END:VTIMEZONE",
    ]
    assert dtstart in lines
    assert read_back(ics, "DTSTART") == start
    assert read_back(ics, "DTEND") == start + timedelta(hours=1)


@pytest.mark.parametrize(
    "time_zone",
    [None, "", "UTC", "Etc/UTC", "Zulu", "Mars/Olympus_Mons", "Europe", "../zoneinfo/Europe/Copenhagen", "Europe;X"],
)
def test_utc_unknown_or_unusable_zone_keeps_utc_times(time_zone: str | None) -> None:
    """UTC, a zone zoneinfo does not know, and a name TZID cannot carry give the UTC output of 0.2.0."""
    ics = render_event(
        uid="u", summary="s", start=KICKOFF, end=KICKOFF + timedelta(hours=1), dtstamp=STAMP, time_zone=time_zone
    )
    assert ics == render_event(uid="u", summary="s", start=KICKOFF, end=KICKOFF + timedelta(hours=1), dtstamp=STAMP)
    assert "DTSTART:20260920T100000Z\r\nDTEND:20260920T110000Z\r\n" in ics
    assert "TZID" not in ics
    with pytest.raises(ValueError):
        vtimezone_lines(time_zone or "", 2026)


def test_times_the_zone_cannot_hold_keep_utc_times() -> None:
    """Times at the edge of what datetime holds stay in UTC instead of failing the event.

    The last hour of year 9999 would be past it in Copenhagen, and the VTIMEZONE of year 9999 would need
    the start of year 10000.
    """
    for start, dtstart, dtend in (
        (datetime(9999, 12, 31, 23, 0, tzinfo=UTC), "DTSTART:99991231T230000Z", "DTEND:99991231T233000Z"),
        (datetime(9999, 6, 1, 10, 0, tzinfo=UTC), "DTSTART:99990601T100000Z", "DTEND:99990601T103000Z"),
    ):
        ics = render_event(
            uid="u", summary="s", start=start, end=start + timedelta(minutes=30), time_zone="Europe/Copenhagen"
        )
        assert f"{dtstart}\r\n{dtend}\r\n" in ics
        assert "VTIMEZONE" not in ics


def test_long_zone_name_and_text_fold_within_75_octets() -> None:
    """With a long zone name, a zone named by its offset and long Danish text, every line stays within 75 octets."""
    summary = "⚽ Emma: Ærø Idrætsforening - Østerbro Boldklub, 5-mands, bane 2 " * 2
    ics = render_event(
        uid="u",
        summary=summary,
        start=KICKOFF,
        end=KICKOFF + timedelta(hours=1),
        description="Mødetid på Øster Allé " * 6,
        dtstamp=STAMP,
        time_zone="America/Argentina/Buenos_Aires",
    )
    physical = ics.split("\r\n")[:-1]
    assert all(len(line.encode("utf-8")) <= 75 for line in physical)
    assert any(line.startswith(" ") for line in physical)
    lines = unfold(ics).split("\r\n")
    assert "DTSTART;TZID=America/Argentina/Buenos_Aires:20260920T070000" in lines
    assert "TZNAME:-03" in lines
    assert f"SUMMARY:{escape_text(summary)}" in lines
    assert read_back(ics, "DTEND") == KICKOFF + timedelta(hours=1)


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (timedelta(hours=2), "+0200"),
        (timedelta(0), "+0000"),
        (timedelta(hours=-3), "-0300"),
        (timedelta(hours=5, minutes=45), "+0545"),
        (-timedelta(hours=9, minutes=30), "-0930"),
        (-timedelta(minutes=44, seconds=30), "-004430"),
    ],
)
def test_format_offset(offset: timedelta, expected: str) -> None:
    """UTC offsets are +HHMM, with seconds only when there are any."""
    assert format_offset(offset) == expected
