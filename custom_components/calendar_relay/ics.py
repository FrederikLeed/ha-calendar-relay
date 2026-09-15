"""Render relayed events as iCalendar (RFC 5545) text.

No Home Assistant imports: the relay passes plain dates and timezone-aware datetimes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

PRODID = "-//calendar-relay//Calendar Relay for Home Assistant//EN"
MAX_LINE_OCTETS = 75
# The geofence radius in metres that Apple's own calendar server writes. It only matters for location-based alarms.
STRUCTURED_LOCATION_RADIUS = 71
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


def format_utc(value: datetime) -> str:
    """Format a timezone-aware datetime as a UTC DATE-TIME with a Z suffix."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timed events need timezone-aware datetimes")
    value = value.astimezone(UTC)
    return f"{format_date(value)}T{value.hour:02d}{value.minute:02d}{value.second:02d}Z"


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
) -> str:
    """Render one VEVENT in a VCALENDAR. Leave out dtstamp to get the text used for change detection.

    DTEND must be later than DTSTART (RFC 5545 3.8.2.2), so an event that ends when
    it starts is written without DTEND: a timed one then takes no time, an all-day
    one takes its day (RFC 5545 3.6.1).

    structured_location adds Apple's X-APPLE-STRUCTURED-LOCATION after LOCATION,
    travel_minutes adds X-APPLE-TRAVEL-DURATION, and alarm_minutes adds a display
    alarm that many minutes before the start.
    """
    if isinstance(start, datetime) != isinstance(end, datetime):
        raise ValueError("start and end must both be dates or both be datetimes")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "BEGIN:VEVENT", f"UID:{uid}"]
    if dtstamp is not None:
        lines.append(f"DTSTAMP:{format_utc(dtstamp)}")
    if isinstance(start, datetime) and isinstance(end, datetime):
        lines.append(f"DTSTART:{format_utc(start)}")
        if end > start:
            lines.append(f"DTEND:{format_utc(end)}")
    else:
        lines.append(f"DTSTART;VALUE=DATE:{format_date(start)}")
        if end > start:
            lines.append(f"DTEND;VALUE=DATE:{format_date(end)}")
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
