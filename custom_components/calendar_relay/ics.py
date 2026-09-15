"""Render relayed events as iCalendar (RFC 5545) text.

No Home Assistant imports: the relay passes plain dates and timezone-aware datetimes.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime

PRODID = "-//calendar-relay//Calendar Relay for Home Assistant//EN"
MAX_LINE_OCTETS = 75


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


def escape_text(value: str) -> str:
    """Escape a TEXT property value: backslash, semicolon, comma and line breaks."""
    value = clean_text(value).replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(char for char in value if _allowed_char(char))
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


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


def render_event(
    *,
    uid: str,
    summary: str,
    start: date | datetime,
    end: date | datetime,
    description: str | None = None,
    location: str | None = None,
    dtstamp: datetime | None = None,
) -> str:
    """Render one VEVENT in a VCALENDAR. Leave out dtstamp to get the text used for change detection.

    DTEND must be later than DTSTART (RFC 5545 3.8.2.2), so an event that ends when
    it starts is written without DTEND: a timed one then takes no time, an all-day
    one takes its day (RFC 5545 3.6.1).
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
    lines.extend(("END:VEVENT", "END:VCALENDAR"))
    return "".join(f"{fold_line(line)}\r\n" for line in lines)


def content_hash(ics: str) -> str:
    """Return a stable hash of rendered event text."""
    return hashlib.sha256(ics.encode("utf-8")).hexdigest()
