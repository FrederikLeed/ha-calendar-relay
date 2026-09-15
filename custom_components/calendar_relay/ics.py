"""Render relayed events as iCalendar (RFC 5545) text.

No Home Assistant imports: the relay passes plain dates, timezone-aware datetimes and the name of the time
zone that timed events are written in.
"""

from __future__ import annotations

import hashlib
import re
from calendar import monthrange
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PRODID = "-//calendar-relay//Calendar Relay for Home Assistant//EN"
MAX_LINE_OCTETS = 75
# The geofence radius in metres that Apple's own calendar server writes. It only matters for location-based alarms.
STRUCTURED_LOCATION_RADIUS = 71
# Names of UTC itself in the tz database. Timed events in these zones keep UTC times with a Z suffix.
UTC_ZONE_NAMES = frozenset({"UTC", "Etc/UTC", "UCT", "Etc/UCT", "Universal", "Etc/Universal", "Zulu", "Etc/Zulu"})
# TZID is written without quotes, so only names made of the characters tz database names use are written.
_ZONE_NAME = re.compile(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*")
# Time zone transitions are looked for a day apart, then pinned to the second. No zone changes twice in a day.
_TRANSITION_SCAN_SECONDS = 86400
# Yearly time zone rules are derived from this many years of transitions, and written from 1970.
_RULE_YEARS = 28
_RULE_EPOCH = 1970
# A zone without yearly rules gets explicit onsets for this many years from the current year.
_ONSET_YEARS = 3
_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
# Sequences that readers decode inside parameter values: Apple turns backslash n into a line break, and RFC 6868
# readers turn ^n into a line break, ^' into a DQUOTE and ^^ into a caret.
_DECODED_CARET = re.compile(r"\^(?=[nN'^])")
_DECODED_BACKSLASH = re.compile(r"\\(?=[nN])")


@dataclass(frozen=True, slots=True)
class StructuredLocation:
    """Apple's structured location: a place title, an optional address and a point."""

    title: str
    latitude: float
    longitude: float
    address: str | None = None


def _allowed_char(char: str) -> bool:
    """Return False for control characters that TEXT values may not contain."""
    code = ord(char)
    return char in "\n\t" or (code >= 0x20 and code != 0x7F)


def clean_text(value: str) -> str:
    """Return value as valid Unicode: surrogate pairs joined, lone surrogates replaced with U+FFFD.

    Python strings can hold lone surrogates (json.loads makes one from a truncated
    emoji escape), and those cannot be encoded as UTF-8.
    """
    return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _normalize(value: str) -> str:
    """Return clean text with every line break as LF and without disallowed control characters."""
    value = clean_text(value).replace("\r\n", "\n").replace("\r", "\n")
    return "".join(char for char in value if _allowed_char(char))


def escape_text(value: str) -> str:
    """Escape a TEXT property value: backslash, semicolon, comma and line breaks."""
    value = _normalize(value)
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def quote_param(value: str) -> str:
    """Return a parameter value in DQUOTEs, the way Apple writes place titles and addresses.

    A line break becomes the two characters backslash and n, which Apple reads back as a
    line break. A DQUOTE may not appear in a parameter value (RFC 5545 3.1), so it becomes
    an apostrophe. Commas, semicolons and colons are safe inside the quotes and stay as they are.

    Text that a reader would decode is neutralised first, so the value reads back the way
    LOCATION shows it: a caret before n, an apostrophe or a caret is dropped, and a backslash
    before n becomes a slash. RFC 6868 encoding is not used, because Apple is not known to read it.
    """
    text = _DECODED_CARET.sub("", _normalize(value).replace('"', "'"))
    return '"' + _DECODED_BACKSLASH.sub("/", text).replace("\n", "\\n") + '"'


def fold_line(line: str) -> str:
    """Fold a content line at 75 octets without splitting a UTF-8 character.

    Continuation lines start with a single space, which counts toward their 75 octets.
    """
    lines: list[str] = []
    current = ""
    size = 0
    for char in line:
        octets = len(char.encode("utf-8"))
        if size + octets > MAX_LINE_OCTETS:
            lines.append(current)
            current = " "
            size = 1
        current += char
        size += octets
    lines.append(current)
    return "\r\n".join(lines)


def format_date(value: date) -> str:
    """Format a DATE value."""
    return f"{value.year:04d}{value.month:02d}{value.day:02d}"


def format_local(value: datetime) -> str:
    """Format the date and clock time of a datetime as a DATE-TIME without a zone, ignoring any tzinfo."""
    return f"{format_date(value)}T{value.hour:02d}{value.minute:02d}{value.second:02d}"


def format_utc(value: datetime) -> str:
    """Format a timezone-aware datetime as a UTC DATE-TIME with a Z suffix."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timed events need timezone-aware datetimes")
    return f"{format_local(value.astimezone(UTC))}Z"


def format_offset(value: timedelta) -> str:
    """Format a UTC offset as +HHMM, or +HHMMSS when it has seconds (RFC 5545 3.3.14)."""
    seconds = int(value.total_seconds())
    sign = "-" if seconds < 0 else "+"
    hours, rest = divmod(abs(seconds), 3600)
    minutes, seconds = divmod(rest, 60)
    text = f"{sign}{hours:02d}{minutes:02d}"
    return f"{text}{seconds:02d}" if seconds else text


def format_duration(minutes: int) -> str:
    """Format whole minutes as a positive DURATION such as PT45M, PT1H or PT1H30M."""
    if minutes < 0:
        raise ValueError("Durations are written as positive minutes")
    hours, rest = divmod(minutes, 60)
    text = "PT"
    if hours:
        text += f"{hours}H"
    if rest or not hours:
        text += f"{rest}M"
    return text


def format_geo_uri(latitude: float, longitude: float) -> str:
    """Format a geo URI (RFC 5870): a comma between latitude and longitude, 6 decimals."""
    return f"geo:{latitude:.6f},{longitude:.6f}"


def structured_location_line(location: StructuredLocation) -> str:
    """Return the X-APPLE-STRUCTURED-LOCATION content line, with parameters in the order Apple writes them."""
    params = ["VALUE=URI"]
    if location.address:
        params.append(f"X-ADDRESS={quote_param(location.address)}")
    params.append(f"X-APPLE-RADIUS={STRUCTURED_LOCATION_RADIUS}")
    params.append(f"X-TITLE={quote_param(location.title)}")
    return f"X-APPLE-STRUCTURED-LOCATION;{';'.join(params)}:{format_geo_uri(location.latitude, location.longitude)}"


@lru_cache(maxsize=16)
def event_zone(name: str | None) -> ZoneInfo | None:
    """Return the zone that timed events are written in, or None when they are written in UTC.

    None stands for no name, a name of UTC, a name zoneinfo does not know, and a name that TZID
    cannot carry unquoted. Answers are cached, so an unknown name is only looked up once.
    """
    if not name or name in UTC_ZONE_NAMES or _ZONE_NAME.fullmatch(name) is None:
        return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError, OSError:
        return None


def _zone_state(zone: ZoneInfo, timestamp: int) -> tuple[timedelta, timedelta, str]:
    """Return the UTC offset, the daylight saving offset and the name of a zone at a moment."""
    moment = datetime.fromtimestamp(timestamp, zone)
    return moment.utcoffset() or timedelta(0), moment.dst() or timedelta(0), moment.tzname() or ""


def _transitions(zone: ZoneInfo, start: int, stop: int) -> list[int]:
    """Return the moments after start, up to stop, from which the zone's offset, daylight saving or name changes."""
    found: list[int] = []
    before = start
    state = _zone_state(zone, start)
    while before < stop:
        after = min(before + _TRANSITION_SCAN_SECONDS, stop)
        new_state = _zone_state(zone, after)
        if new_state != state:
            low, high = before, after
            while high - low > 1:
                middle = (low + high) // 2
                if _zone_state(zone, middle) == state:
                    low = middle
                else:
                    high = middle
            found.append(high)
            state = new_state
        before = after
    return found


@dataclass(frozen=True, slots=True)
class _Change:
    """The start of an observance: its onset as a local time in the offset before it, and what it changes to."""

    onset: datetime
    offset_from: timedelta
    offset_to: timedelta
    dst: timedelta
    name: str


def _change(zone: ZoneInfo, moment: int) -> _Change:
    """Return the observance of a zone that starts at a moment."""
    offset_from = _zone_state(zone, moment - 1)[0]
    offset, dst, name = _zone_state(zone, moment)
    onset = datetime.fromtimestamp(moment, UTC).replace(tzinfo=None) + offset_from
    return _Change(onset, offset_from, offset, dst, name)


def _changes(zone: ZoneInfo, start: int, stop: int) -> list[_Change]:
    """Return the observances that start after the moment start, up to stop, in order."""
    return [_change(zone, moment) for moment in _transitions(zone, start, stop)]


def _observance_lines(change: _Change, rrule: str | None = None) -> list[str]:
    """Return the STANDARD or DAYLIGHT observance that begins with change, repeating by rrule if given.

    Its DTSTART is the local time in the offset before the onset (RFC 5545 3.6.5). Daylight saving
    time is an observance with a positive daylight saving offset.
    """
    kind = "DAYLIGHT" if change.dst > timedelta(0) else "STANDARD"
    lines = [f"BEGIN:{kind}", f"DTSTART:{format_local(change.onset)}"]
    if rrule:
        lines.append(rrule)
    lines.extend((f"TZOFFSETFROM:{format_offset(change.offset_from)}", f"TZOFFSETTO:{format_offset(change.offset_to)}"))
    if change.name:
        lines.append(f"TZNAME:{escape_text(change.name)}")
    lines.append(f"END:{kind}")
    return lines


@dataclass(frozen=True, slots=True)
class _YearlyRule:
    """An observance that starts once a year on one weekday in one week of one month, at one local time.

    first_day is the first day of the month the weekday can fall on, or None for the last seven days.
    """

    change: _Change
    first_day: int | None

    def in_year(self, year: int) -> _Change:
        """Return the start of the observance in a year."""
        onset = self.change.onset
        first_day = self.first_day or monthrange(year, onset.month)[1] - 6
        first = datetime.combine(date(year, onset.month, first_day), onset.time())
        return replace(self.change, onset=first + timedelta(days=(onset.weekday() - first.weekday()) % 7))

    def lines(self) -> list[str]:
        """Return the observance with its RRULE, from its first onset in 1970."""
        month, weekday = self.change.onset.month, _WEEKDAYS[self.change.onset.weekday()]
        if self.first_day is None:
            byday = f"BYDAY=-1{weekday}"
        elif self.first_day % 7 == 1:
            byday = f"BYDAY={self.first_day // 7 + 1}{weekday}"
        else:
            days = ",".join(str(day) for day in range(self.first_day, self.first_day + 7))
            byday = f"BYDAY={weekday};BYMONTHDAY={days}"
        return _observance_lines(self.in_year(_RULE_EPOCH), f"RRULE:FREQ=YEARLY;BYMONTH={month};{byday}")


@dataclass(frozen=True, slots=True)
class _Yearly:
    """The yearly rules of a zone: its state when a year begins, and the observances that start each year."""

    start: tuple[timedelta, timedelta, str]
    rules: tuple[_YearlyRule, ...]


def _yearly_rule(changes: list[_Change], first_year: int) -> _YearlyRule | None:
    """Return the yearly rule that the starts of one observance follow, one a year from first_year, or None."""
    onsets = [change.onset for change in changes]
    if [onset.year for onset in onsets] != list(range(first_year, first_year + _RULE_YEARS)):
        return None
    if len({(onset.month, onset.weekday(), onset.time()) for onset in onsets}) > 1:
        return None
    if all(onset.day > monthrange(onset.year, onset.month)[1] - 7 for onset in onsets):
        return _YearlyRule(changes[0], None)
    days = [onset.day for onset in onsets]
    if max(days) - min(days) != 6:
        return None
    return _YearlyRule(changes[0], min(days))


def _year_start(zone: ZoneInfo, year: int) -> int:
    """Return the moment a local year begins in a zone. A year datetime cannot hold raises ValueError."""
    return int(datetime(year, 1, 1, tzinfo=zone).timestamp())


@lru_cache(maxsize=16)
def _yearly(zone: ZoneInfo, first_year: int) -> _Yearly | None:
    """Return the yearly rules a zone follows for 28 years from first_year, or None when it follows none.

    28 years are a full cycle of weekdays and leap years, so every day a rule such as the Sunday on or
    after the 8th can fall on is seen, and that rule is told apart from the second Sunday.
    """
    start = _year_start(zone, first_year)
    observances: dict[tuple[timedelta, timedelta, timedelta, str], list[_Change]] = {}
    for change in _changes(zone, start, _year_start(zone, first_year + _RULE_YEARS)):
        observances.setdefault((change.offset_from, change.offset_to, change.dst, change.name), []).append(change)
    rules = [_yearly_rule(changes, first_year) for changes in observances.values()]
    if None in rules:
        return None
    return _Yearly(_zone_state(zone, start), tuple(rule for rule in rules if rule is not None))


def _follows(zone: ZoneInfo, yearly: _Yearly, year: int) -> bool:
    """Return whether a year from 1970 on begins in the zone's yearly state and changes only as its rules say."""
    start, stop = _year_start(zone, year), _year_start(zone, year + 1)
    expected = sorted((rule.in_year(year) for rule in yearly.rules), key=lambda change: change.onset)
    return year >= _RULE_EPOCH and _zone_state(zone, start) == yearly.start and _changes(zone, start, stop) == expected


def _onset_lines(zone: ZoneInfo, years: list[int]) -> list[str]:
    """Return observances with explicit onsets for the given local years, in order.

    Each year gets the observance in effect when it begins, with the start of the year as its onset,
    unless the year before was covered, then one observance per transition in the year.
    """
    lines: list[str] = []
    covered_until: int | None = None
    for year in years:
        start, stop = _year_start(zone, year), _year_start(zone, year + 1)
        if start != covered_until:
            offset, dst, name = _zone_state(zone, start)
            local_start = datetime.fromtimestamp(start, UTC).replace(tzinfo=None) + offset
            lines.extend(_observance_lines(_Change(local_start, offset, offset, dst, name)))
        for change in _changes(zone, start, stop):
            lines.extend(_observance_lines(change))
        covered_until = stop
    return lines


@lru_cache(maxsize=64)
def vtimezone_lines(name: str, this_year: int, event_years: tuple[int, ...] = ()) -> tuple[str, ...]:
    """Return the VTIMEZONE of a zone, written in the current year this_year, for events in event_years.

    Readers such as vobject, inside Radicale and Home Assistant's CalDAV integration, keep the first
    VTIMEZONE they read for a TZID and use it for every later object with that TZID. So a zone gets the
    same VTIMEZONE in every event, valid for all years: yearly RRULE observances as Apple writes them,
    starting in 1970 as in Radicale's test data, derived from the zone's transitions over 28 years from
    this_year. A zone without transitions gets a single observance.

    A zone whose transitions follow no yearly rule (Morocco's Ramadan changes, a rule change within the
    28 years), or an event year outside them that does not follow the rules, gets explicit onsets for
    this_year, the next two years and the event years instead. Every event written in a year then shares
    those, and they change once a year.

    Raise ValueError for a name that event_zone does not accept, and for years datetime cannot hold.
    """
    zone = event_zone(name)
    if zone is None:
        raise ValueError("No time zone to describe")
    yearly = _yearly(zone, this_year)
    lines = ["BEGIN:VTIMEZONE", f"TZID:{name}"]
    outside = [year for year in event_years if not this_year <= year < this_year + _RULE_YEARS]
    if yearly is None or not all(_follows(zone, yearly, year) for year in outside):
        lines.extend(_onset_lines(zone, sorted({*range(this_year, this_year + _ONSET_YEARS), *event_years})))
    elif yearly.rules:
        for rule in yearly.rules:
            lines.extend(rule.lines())
    else:
        offset, dst, zone_name = yearly.start
        lines.extend(_observance_lines(_Change(datetime(_RULE_EPOCH, 1, 1), offset, offset, dst, zone_name)))
    lines.append("END:VTIMEZONE")
    return tuple(lines)


def _reads_back(local: datetime) -> bool:
    """Return False for the second occurrence of a local time that occurs twice when clocks go back.

    Readers take such a time as its first occurrence (RFC 5545 3.3.5).
    """
    return not local.fold or local.replace(fold=0).utcoffset() == local.utcoffset()


def _timed_lines(start: datetime, end: datetime, time_zone: str | None) -> tuple[list[str], list[str]]:
    """Return the VTIMEZONE lines and the DTSTART and DTEND lines of a timed event.

    The times are local times in time_zone with its TZID, or UTC times when event_zone gives no zone.
    The second occurrence of a local time that occurs twice is written in UTC, and so is an event
    whose times cannot be converted (years at the edge of what datetime holds).
    """
    # format_utc rejects times without a zone. DTEND must be later than DTSTART (RFC 5545 3.8.2.2).
    values = {"DTSTART": start}
    utc_lines = [f"DTSTART:{format_utc(start)}"]
    if end > start:
        values["DTEND"] = end
        utc_lines.append(f"DTEND:{format_utc(end)}")
    zone = event_zone(time_zone)
    if time_zone is None or zone is None:
        return [], utc_lines
    try:
        local = {name: value.astimezone(zone) for name, value in values.items()}
        years = tuple(sorted({value.year for value in local.values()}))
        vtimezone = vtimezone_lines(time_zone, datetime.now(UTC).year, years)
    except OverflowError, ValueError, OSError:
        return [], utc_lines
    lines = [
        f"{name};TZID={time_zone}:{format_local(value)}" if _reads_back(value) else f"{name}:{format_utc(value)}"
        for name, value in local.items()
    ]
    if lines == utc_lines:
        return [], lines
    return list(vtimezone), lines


def render_event(
    *,
    uid: str,
    summary: str,
    start: date | datetime,
    end: date | datetime,
    description: str | None = None,
    location: str | None = None,
    structured_location: StructuredLocation | None = None,
    travel_minutes: int | None = None,
    alarm_minutes: int | None = None,
    dtstamp: datetime | None = None,
    time_zone: str | None = None,
) -> str:
    """Render one VEVENT in a VCALENDAR. Leave out dtstamp to get the text used for change detection.

    DTEND must be later than DTSTART (RFC 5545 3.8.2.2), so an event that ends when
    it starts is written without DTEND: a timed one then takes no time, an all-day
    one takes its day (RFC 5545 3.6.1).

    time_zone is the tz database name that timed events are written in, as local times with
    a TZID and a VTIMEZONE before the VEVENT. Without it, for UTC, or for a name zoneinfo does
    not know, they are written in UTC. All-day events are dates and DTSTAMP is always UTC.

    structured_location adds Apple's X-APPLE-STRUCTURED-LOCATION after LOCATION,
    travel_minutes adds X-APPLE-TRAVEL-DURATION, and alarm_minutes adds a display
    alarm that many minutes before the start.
    """
    if isinstance(start, datetime) != isinstance(end, datetime):
        raise ValueError("start and end must both be dates or both be datetimes")
    vtimezone: list[str] = []
    if isinstance(start, datetime) and isinstance(end, datetime):
        vtimezone, times = _timed_lines(start, end, time_zone)
    else:
        times = [f"DTSTART;VALUE=DATE:{format_date(start)}"]
        if end > start:
            times.append(f"DTEND;VALUE=DATE:{format_date(end)}")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", *vtimezone, "BEGIN:VEVENT", f"UID:{uid}"]
    if dtstamp is not None:
        lines.append(f"DTSTAMP:{format_utc(dtstamp)}")
    lines.extend(times)
    lines.append(f"SUMMARY:{escape_text(summary)}")
    if description:
        lines.append(f"DESCRIPTION:{escape_text(description)}")
    if location:
        lines.append(f"LOCATION:{escape_text(location)}")
    if structured_location is not None:
        lines.append(structured_location_line(structured_location))
    if travel_minutes:
        lines.append(f"X-APPLE-TRAVEL-DURATION;VALUE=DURATION:{format_duration(travel_minutes)}")
    if alarm_minutes is not None:
        lines.extend(
            (
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{escape_text(summary)}",
                f"TRIGGER:-{format_duration(alarm_minutes)}",
                "END:VALARM",
            )
        )
    lines.extend(("END:VEVENT", "END:VCALENDAR"))
    return "".join(f"{fold_line(line)}\r\n" for line in lines)


def content_hash(ics: str) -> str:
    """Return a stable hash of rendered event text."""
    return hashlib.sha256(ics.encode("utf-8")).hexdigest()
