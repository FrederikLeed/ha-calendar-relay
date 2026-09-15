"""Relay engine: mirror matching events from a calendar entity into a CalDAV calendar, one way."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components.calendar import DATA_COMPONENT, CalendarEvent
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import ServiceNotFound
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .caldav import (
    CalDavAuthError,
    CalDavClient,
    CalDavConnectionError,
    CalDavError,
    CalDavNotFoundError,
    DavCalendar,
    collection_url,
    is_event_href,
)
from .const import (
    CONF_BUFFER_MINUTES,
    CONF_LEAVE_REMINDER,
    CONF_LOOK_AHEAD_DAYS,
    CONF_REMOVE_FILTER,
    CONF_SOURCE,
    CONF_STRUCTURED_LOCATION,
    CONF_TARGET,
    CONF_TARGET_NAME,
    CONF_TITLE_FILTER,
    CONF_TITLE_PREFIX,
    CONF_TRAVEL_MINUTES,
    CONF_TRAVEL_TIME,
    CONF_WAZE_REGION,
    DEFAULT_BUFFER_MINUTES,
    DEFAULT_LOOK_AHEAD_DAYS,
    DEFAULT_TRAVEL_MINUTES,
    DEFAULT_WAZE_REGION,
    DOMAIN,
    EMPTY_READ_CONFIRM_AFTER,
    ISSUE_TARGET_MISSING,
    LOGGER,
    MAX_TRAVEL_MINUTES,
    REALTIME_BEFORE_LEAVE,
    REALTIME_WINDOW,
    SOURCE_CHANGE_COOLDOWN,
    STORAGE_VERSION,
    SYNC_INTERVAL,
    TRAVEL_FIXED,
    TRAVEL_MODES,
    TRAVEL_OFF,
    TRAVEL_RETRY_AFTER,
    TRAVEL_RETRY_MAX,
    TRAVEL_ROUNDING_MINUTES,
    TRAVEL_WAZE,
    WAZE_ANSWER_TTL,
    WAZE_CALL_PAUSE,
    WAZE_CALL_TIMEOUT,
    WAZE_CALLS_PER_PASS,
    WAZE_DOMAIN,
    WAZE_FAILURES_PER_PASS,
    WAZE_REGIONS,
    WAZE_SERVICE,
)
from .ics import StructuredLocation, clean_text, content_hash, render_event
from .location import Coordinates, find_coordinates

UID_DOMAIN = "calendar-relay"
RECORD_FIELDS = ("href", "hash", "start", "end", "target")

_LEADING_SEPARATORS = re.compile(r"^[\s:;,|-]+")
# Errors that end a pass: nothing else will work either until they are fixed.
_PASS_ENDING_ERRORS = (CalDavAuthError, CalDavNotFoundError, CalDavConnectionError)
# Pass-ending errors that are not about one calendar: bad credentials, or the server cannot be reached.
_ACCOUNT_ERRORS = (CalDavAuthError, CalDavConnectionError)


def storage_key(subentry_id: str) -> str:
    """Return the Store key of a relay."""
    return f"{DOMAIN}.{subentry_id}"


def issue_id(subentry_id: str) -> str:
    """Return the repair issue id for a relay whose target calendar is missing."""
    return f"target_missing_{subentry_id}"


def title_matches(title: str, title_filter: str) -> bool:
    """Return True if title contains title_filter, ignoring case. An empty filter matches everything."""
    if not title_filter:
        return True
    return re.search(re.escape(title_filter), title, re.IGNORECASE) is not None


def transform_title(title: str, *, title_filter: str, remove_filter: bool, prefix: str) -> str:
    """Return the relayed title: optionally without the filter text, then with the prefix."""
    result = title
    if remove_filter and title_filter:
        stripped = re.sub(re.escape(title_filter), "", title, count=1, flags=re.IGNORECASE)
        stripped = _LEADING_SEPARATORS.sub("", stripped).strip()
        if stripped:
            result = stripped
    return f"{prefix}{result}"


def _iso(value: date | datetime) -> str:
    """Serialize a date, or a datetime in UTC."""
    if isinstance(value, datetime):
        return dt_util.as_utc(value).isoformat()
    return value.isoformat()


def parse_iso(value: str) -> date | datetime:
    """Parse a value written by _iso."""
    if len(value) == 10:
        return date.fromisoformat(value)
    return dt_util.as_utc(datetime.fromisoformat(value))


def event_key(event: CalendarEvent) -> str:
    """Return the identity of a source event: uid plus recurrence_id, or a hash of summary and start.

    Text is cleaned first, so a lone surrogate cannot break hashing or saving the state.
    """
    if event.uid:
        parts = [clean_text(event.uid), clean_text(event.recurrence_id or "")]
        return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(json.dumps([clean_text(event.summary), _iso(event.start)], ensure_ascii=False).encode())
    return f"hash:{digest.hexdigest()[:32]}"


def resource_id(subentry_id: str, key: str) -> str:
    """Return the hex id used for the resource name and UID of a relayed event."""
    return hashlib.sha256(f"{subentry_id}{key}".encode()).hexdigest()[:32]


def has_started(start: date | datetime, now: datetime) -> bool:
    """Return True if an event starting at start has started (all-day events use the local date)."""
    if isinstance(start, datetime):
        return start <= now
    return start <= dt_util.as_local(now).date()


def has_ended(end: date | datetime, now: datetime) -> bool:
    """Return True if an event ending at end is over (all-day ends are exclusive local dates)."""
    if isinstance(end, datetime):
        return end <= now
    return end <= dt_util.as_local(now).date()


def is_danish(language: str | None) -> bool:
    """Return True for Danish, with or without a region (da, da-DK)."""
    return re.split(r"[-_]", language or "", maxsplit=1)[0].lower() == "da"


def leave_text(language: str | None, leave_at: datetime, travel_minutes: int, days_before: int = 0) -> str:
    """Return the leave line of the description in Danish or English, with leave_at as local 24-hour time.

    days_before says how many local dates the leave time lies before the start, so a line such as
    23:45 is not read as a time on the event's own day.
    """
    clock = f"{leave_at.hour:02d}:{leave_at.minute:02d}"
    if is_danish(language):
        day = " dagen før" if days_before == 1 else f" {days_before} dage før" if days_before > 1 else ""
        return f"Afgang: {clock}{day} (ca. {travel_minutes} min. kørsel)"
    day = " the day before" if days_before == 1 else f" {days_before} days before" if days_before > 1 else ""
    return f"Leave at {clock}{day} (about {travel_minutes} min drive)"


def round_travel_minutes(duration: float) -> int:
    """Round a travel time in minutes up to a multiple of 5."""
    # Waze adds up seconds and divides by 60; round float noise away first, so 25.0000000001 stays 25.
    return math.ceil(round(duration, 6) / TRAVEL_ROUNDING_MINUTES) * TRAVEL_ROUNDING_MINUTES


def structured_location(location: str | None, coordinates: Coordinates | None) -> StructuredLocation | None:
    """Return Apple's structured location of an event, or None without coordinates or a place name.

    Apple writes LOCATION as the title, a line break and the address, and puts the same title
    and address in the structured location; a map may not show when they differ. So the first
    line of LOCATION is the title and any further lines are the address. Without LOCATION, the
    place name from the map link is the title.
    """
    if coordinates is None:
        return None
    text = clean_text(location or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if text:
        title, _, address = text.partition("\n")
        return StructuredLocation(title, coordinates.latitude, coordinates.longitude, address or None)
    if coordinates.name:
        return StructuredLocation(coordinates.name, coordinates.latitude, coordinates.longitude)
    return None


def travel_destination(location: str | None, coordinates: Coordinates | None) -> str | None:
    """Return what Waze routes to: the coordinates if there are any, otherwise the location text on one line."""
    if coordinates is not None:
        return f"{coordinates.latitude:.6f},{coordinates.longitude:.6f}"
    lines = [line.strip() for line in clean_text(location or "").splitlines()]
    return ", ".join(line for line in lines if line) or None


def travel_fingerprint(origin: str, destination: str, region: str) -> str:
    """Return a hash of what a travel time was computed for, so the sync state holds no address."""
    return hashlib.sha256(json.dumps([origin, destination, region], ensure_ascii=False).encode()).hexdigest()[:32]


def place_fingerprint(destination: str) -> str:
    """Return a hash of the destination alone, to tell a new place from a new home location or region."""
    return hashlib.sha256(json.dumps([destination], ensure_ascii=False).encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class RelayConfig:
    """Settings of one relay, read from its subentry."""

    source: str
    target: str
    target_name: str
    title_filter: str
    remove_filter: bool
    title_prefix: str
    look_ahead_days: int
    structured_location: bool
    travel_time: str
    travel_minutes: int
    waze_region: str
    buffer_minutes: int
    leave_reminder: bool

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> RelayConfig:
        """Build the settings from subentry data. Relays saved before a setting existed get its default."""
        travel_time = data.get(CONF_TRAVEL_TIME, TRAVEL_OFF)
        waze_region = data.get(CONF_WAZE_REGION, DEFAULT_WAZE_REGION)
        return cls(
            source=data[CONF_SOURCE],
            target=data[CONF_TARGET],
            target_name=data.get(CONF_TARGET_NAME) or "",
            title_filter=data.get(CONF_TITLE_FILTER) or "",
            remove_filter=bool(data.get(CONF_REMOVE_FILTER, False)),
            title_prefix=data.get(CONF_TITLE_PREFIX) or "",
            look_ahead_days=int(data.get(CONF_LOOK_AHEAD_DAYS, DEFAULT_LOOK_AHEAD_DAYS)),
            structured_location=bool(data.get(CONF_STRUCTURED_LOCATION, True)),
            travel_time=travel_time if travel_time in TRAVEL_MODES else TRAVEL_OFF,
            travel_minutes=int(data.get(CONF_TRAVEL_MINUTES, DEFAULT_TRAVEL_MINUTES)),
            waze_region=waze_region if waze_region in WAZE_REGIONS else DEFAULT_WAZE_REGION,
            buffer_minutes=int(data.get(CONF_BUFFER_MINUTES, DEFAULT_BUFFER_MINUTES)),
            leave_reminder=bool(data.get(CONF_LEAVE_REMINDER, False)),
        )


def _stored_datetime(value: Any) -> datetime | None:
    """Return a stored timezone-aware ISO datetime in UTC, or None if it is not one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt_util.as_utc(parsed) if parsed.tzinfo is not None else None


@dataclass(frozen=True, slots=True)
class TravelRecord:
    """A Waze travel time in the sync state.

    Rounded minutes, a hash of what it was computed for (home, destination, region), a hash of
    the destination alone, when, whether with live traffic, and the event start it was for.
    """

    minutes: int
    fingerprint: str
    place: str
    computed_at: str
    realtime: bool
    start: str

    def as_dict(self) -> dict[str, Any]:
        """Return the record as stored."""
        return {
            "minutes": self.minutes,
            "fingerprint": self.fingerprint,
            "place": self.place,
            "computed_at": self.computed_at,
            "realtime": self.realtime,
            "start": self.start,
        }

    @classmethod
    def from_dict(cls, value: Any) -> TravelRecord | None:
        """Return a stored record, or None if it is not valid. Minutes above the maximum are not valid."""
        if not isinstance(value, dict):
            return None
        minutes = value.get("minutes")
        texts = [value.get(name) for name in ("fingerprint", "place", "computed_at", "start")]
        realtime = value.get("realtime")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 <= minutes <= MAX_TRAVEL_MINUTES:
            return None
        if not all(isinstance(text, str) for text in texts) or not isinstance(realtime, bool):
            return None
        fingerprint, place, computed_at, start = texts
        return cls(minutes, fingerprint, place, computed_at, realtime, start)


@dataclass(frozen=True, slots=True)
class TravelRetry:
    """A travel time that failed, in the sync state: for what, how often in a row, when to ask again, and why."""

    fingerprint: str
    failures: int
    retry_at: datetime
    error: str

    def as_dict(self) -> dict[str, Any]:
        """Return the retry as stored."""
        return {
            "fingerprint": self.fingerprint,
            "failures": self.failures,
            "retry_at": _iso(self.retry_at),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: Any) -> TravelRetry | None:
        """Return a stored retry, or None if it is not valid."""
        if not isinstance(value, dict):
            return None
        fingerprint = value.get("fingerprint")
        failures = value.get("failures")
        retry_at = _stored_datetime(value.get("retry_at"))
        error = value.get("error")
        if isinstance(failures, bool) or not isinstance(failures, int) or failures < 1:
            return None
        if not isinstance(fingerprint, str) or not isinstance(error, str) or retry_at is None:
            return None
        return cls(fingerprint, failures, retry_at, error)


def retry_delay(failures: int) -> timedelta:
    """Return how long to wait after a travel time failed failures times in a row: 1 hour, doubling up to 24."""
    return min(TRAVEL_RETRY_AFTER * 2 ** min(failures - 1, 10), TRAVEL_RETRY_MAX)


class TravelTimeError(Exception):
    """Waze did not give a travel time. The message is safe to show: it holds no address.

    ends_lookups is True for problems that would hit every other call too, such as a timeout.
    """

    def __init__(self, message: str, *, ends_lookups: bool = False) -> None:
        """Initialize the error."""
        super().__init__(message)
        self.ends_lookups = ends_lookups


@dataclass(slots=True)
class WazeQueue:
    """The Waze calls of all relays: one at a time, with a pause after each, and recent answers to share.

    The waze_travel_time.get_travel_times action has no throttle of its own, unlike the integration's sensors.
    """

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # (fingerprint, realtime) -> (when Waze answered, rounded minutes)
    answers: dict[tuple[str, bool], tuple[datetime, int]] = field(default_factory=dict)


WAZE_QUEUE: HassKey[WazeQueue] = HassKey(f"{DOMAIN}_waze_queue")


@dataclass(slots=True)
class PlannedEvent:
    """An event as it should exist in the target calendar."""

    name: str
    uid: str
    summary: str
    start: date | datetime
    end: date | datetime
    description: str | None
    location: str | None
    structured_location: StructuredLocation | None = None
    # What Waze routes to. Only set for timed events with a place, on relays that use Waze.
    destination: str | None = None
    travel_minutes: int | None = None
    buffer_minutes: int = 0
    leave_reminder: bool = False
    language: str = "en"
    content_hash: str = ""

    def render(self, dtstamp: datetime | None = None) -> str:
        """Render the event. Without dtstamp the text is stable and used for change detection.

        With a travel time, a timed event gets a leave line at the top of its description,
        Apple's travel duration covering travel time plus buffer, and optionally an alarm
        at the leave time. The leave line names the day when it is before the start's local date.
        """
        description = self.description
        leave: int | None = None
        if self.travel_minutes and isinstance(self.start, datetime):
            leave = self.travel_minutes + self.buffer_minutes
            leave_at = dt_util.as_local(self.start - timedelta(minutes=leave))
            days_before = (dt_util.as_local(self.start).date() - leave_at.date()).days
            line = leave_text(self.language, leave_at, self.travel_minutes, days_before)
            description = f"{line}\n{description}" if description else line
        return render_event(
            uid=self.uid,
            summary=self.summary,
            start=self.start,
            end=self.end,
            description=description,
            location=self.location,
            structured_location=self.structured_location,
            travel_minutes=leave,
            alarm_minutes=leave if self.leave_reminder else None,
            dtstamp=dtstamp,
        )


class _SkipPass(Exception):
    """The source could not be read, so the pass must not touch the target."""


async def async_remove_relay_data(hass: HomeAssistant, subentry_id: str) -> None:
    """Remove the sync state and repair issue of a relay. Relayed events stay in the calendar."""
    await Store(hass, STORAGE_VERSION, storage_key(subentry_id)).async_remove()
    ir.async_delete_issue(hass, DOMAIN, issue_id(subentry_id))


class Relay:
    """One relay: a source calendar entity, a filter and a target CalDAV calendar."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        subentry: ConfigSubentry,
        client: CalDavClient,
        account_calendars: Callable[[], Iterable[DavCalendar]],
    ) -> None:
        """Initialize the relay. account_calendars returns the calendars last discovered on the account."""
        self.hass = hass
        self.entry = entry
        self.subentry_id = subentry.subentry_id
        self.title = subentry.title
        self.config = RelayConfig.from_data(subentry.data)
        self._client = client
        self._account_calendars = account_calendars
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, storage_key(self.subentry_id), private=True)
        self._events: dict[str, dict[str, str]] = {}
        # key -> Waze travel time. Kept apart from _events, so a write that fails does not lose a paid-for answer.
        self._travel: dict[str, TravelRecord] = {}
        # key -> retry of events whose travel time failed. Stored, so a restart does not ask for all of them at once.
        self._travel_retry: dict[str, TravelRetry] = {}
        self._travel_error: str | None = None
        self._dirty = False
        self._empty_since: datetime | None = None
        # key -> (target, content hash, error) of moves the new calendar refused; not retried until one changes.
        self._failed_moves: dict[str, tuple[str, str, str]] = {}
        self._lock = asyncio.Lock()
        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._debouncer = Debouncer(
            hass,
            LOGGER,
            cooldown=SOURCE_CHANGE_COOLDOWN,
            immediate=False,
            function=self.async_schedule_sync,
        )
        self.last_sync: datetime | None = None
        self.last_error: str | None = None

    @property
    def relayed_events(self) -> int:
        """Return how many relayed events are tracked."""
        return len(self._events)

    @property
    def syncing(self) -> bool:
        """Return True while a pass runs."""
        return self._lock.locked()

    async def async_load(self) -> None:
        """Load the sync state."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return
        events = data.get("events")
        if isinstance(events, dict):
            self._events = {
                key: {name: record[name] for name in RECORD_FIELDS}
                for key, record in events.items()
                if isinstance(record, dict) and all(isinstance(record.get(name), str) for name in RECORD_FIELDS)
            }
        # Travel times are checked on their own, so a bad one never discards the event records.
        travel = data.get("travel")
        if isinstance(travel, dict):
            self._travel = {
                key: record for key, value in travel.items() if (record := TravelRecord.from_dict(value)) is not None
            }
        retries = data.get("travel_retry")
        if isinstance(retries, dict):
            self._travel_retry = {
                key: retry for key, value in retries.items() if (retry := TravelRetry.from_dict(value)) is not None
            }

    @callback
    def async_start(self) -> None:
        """Start the triggers once Home Assistant has started: a first pass, source changes, an interval."""
        self._unsubs.append(async_at_started(self.hass, self._async_at_started))

    @callback
    def async_stop(self) -> None:
        """Stop the triggers."""
        while self._unsubs:
            self._unsubs.pop()()
        self._debouncer.async_shutdown()

    @callback
    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call update_callback after every pass. Returns a function that removes it."""
        self._listeners.append(update_callback)

        @callback
        def _remove() -> None:
            self._listeners.remove(update_callback)

        return _remove

    @callback
    def _async_at_started(self, hass: HomeAssistant) -> None:
        # Source entities often write state while Home Assistant is still starting, before their data is
        # loaded, so the other triggers are only armed now.
        self._unsubs.append(async_track_state_change_event(hass, [self.config.source], self._async_source_changed))
        self._unsubs.append(
            async_track_time_interval(
                hass,
                self._async_interval,
                SYNC_INTERVAL,
                name=f"{DOMAIN} relay {self.subentry_id}",
                cancel_on_shutdown=True,
            )
        )
        self.async_schedule_sync()

    @callback
    def _async_source_changed(self, event: Event[EventStateChangedData]) -> None:
        self._debouncer.async_schedule_call()

    @callback
    def _async_interval(self, now: datetime) -> None:
        self.async_schedule_sync()

    @callback
    def async_schedule_sync(self) -> None:
        """Run a pass in a background task owned by the config entry."""
        self.entry.async_create_background_task(self.hass, self.async_sync(), f"{DOMAIN} sync {self.subentry_id}")

    async def async_sync(self) -> None:
        """Run one sync pass. One pass at a time per relay; never raises."""
        async with self._lock:
            self._travel_error = None
            try:
                error = await self._async_sync_pass()
            except Exception:
                LOGGER.exception("Relay %s: unexpected error during sync", self.title)
                error = "Unexpected error"
            if error is None:
                self.last_sync = dt_util.utcnow()
                ir.async_delete_issue(self.hass, DOMAIN, issue_id(self.subentry_id))
            # A travel time problem is shown, but the events are in step, so it does not hold back last_sync.
            self.last_error = error if error is not None else self._travel_error
            for listener in list(self._listeners):
                listener()

    async def _async_sync_pass(self) -> str | None:
        """Run a pass. Return an error message, or None when everything is in step."""
        try:
            events = await self._async_read_source()
        except _SkipPass as err:
            return str(err)
        now = dt_util.utcnow()
        failures: list[str] = []
        planned, complete = self._plan(events, now, failures)
        self._dirty = False
        try:
            # Travel times are asked for before writing, so a new answer goes out with its event in this pass.
            self._travel_error = await self._async_update_travel(planned, now)
            await self._async_write_planned(planned, now, failures)
            # An event that could not be planned is not known to be withdrawn, so nothing is removed.
            if complete and self._removals_confirmed(events, now):
                await self._async_remove_unwanted(planned, now, failures)
            self._forget_travel(planned)
        except CalDavAuthError as err:
            LOGGER.warning("Relay %s: the server refused the request (%s), starting reauthentication", self.title, err)
            self.entry.async_start_reauth(self.hass)
            return f"Authentication failed ({err})"
        except CalDavNotFoundError as err:
            LOGGER.warning("Relay %s: the target calendar does not exist (%s)", self.title, err)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id(self.subentry_id),
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_TARGET_MISSING,
                translation_placeholders={"relay": self.title, "calendar": self.config.target_name},
            )
            return f"Target calendar not found ({err})"
        except CalDavConnectionError as err:
            LOGGER.warning("Relay %s: sync failed, retrying on the next pass: %s", self.title, err)
            return str(err)
        finally:
            if self._dirty:
                await self._store.async_save(
                    {
                        "events": dict(self._events),
                        "travel": {key: record.as_dict() for key, record in self._travel.items()},
                        "travel_retry": {key: retry.as_dict() for key, retry in self._travel_retry.items()},
                    }
                )
        if failures:
            return f"{len(failures)} event change(s) failed, first error: {failures[0]}"
        return None

    async def _async_read_source(self) -> list[CalendarEvent]:
        """Read the source window. Raise _SkipPass when the source is missing, unavailable or failing."""
        source = self.config.source
        state = self.hass.states.get(source)
        component = self.hass.data.get(DATA_COMPONENT)
        entity = component.get_entity(source) if component is not None else None
        if entity is None or state is None or state.state == STATE_UNAVAILABLE:
            LOGGER.debug("Relay %s: the source calendar is unavailable, skipping this pass", self.title)
            raise _SkipPass("Source calendar unavailable")
        start = dt_util.now()
        end = start + timedelta(days=self.config.look_ahead_days)
        try:
            # The calendar.get_events action drops uid and recurrence_id, so call the entity directly.
            return await entity.async_get_events(self.hass, start, end)
        except Exception as err:
            LOGGER.warning("Relay %s: reading the source calendar failed, skipping this pass: %s", self.title, err)
            raise _SkipPass("Reading the source calendar failed") from err

    def _plan(
        self, events: list[CalendarEvent], now: datetime, failures: list[str]
    ) -> tuple[dict[str, PlannedEvent], bool]:
        """Return the events that should exist in the target by key, and False if an event could not be planned."""
        planned: dict[str, PlannedEvent] = {}
        complete = True
        for event in events:
            try:
                result = self._plan_event(event, now)
            except Exception as err:
                LOGGER.warning("Relay %s: a source event cannot be relayed: %s", self.title, err)
                failures.append(f"A source event cannot be relayed ({type(err).__name__})")
                complete = False
                continue
            if result is not None and result[0] not in planned:
                planned[result[0]] = result[1]
        return planned, complete

    def _plan_event(self, event: CalendarEvent, now: datetime) -> tuple[str, PlannedEvent] | None:
        """Return the key and target form of one source event, or None if it is not relayed."""
        config = self.config
        if not title_matches(event.summary, config.title_filter):
            return None
        start: date | datetime = event.start
        end: date | datetime = event.end
        if isinstance(start, datetime):
            start = dt_util.as_utc(start)
        if isinstance(end, datetime):
            end = dt_util.as_utc(end)
        if has_ended(end, now):
            return None
        key = event_key(event)
        rid = resource_id(self.subentry_id, key)
        coordinates = find_coordinates(event.description, event.location)
        place = structured_location(event.location, coordinates) if config.structured_location else None
        item = PlannedEvent(
            name=f"relay-{rid}.ics",
            uid=f"{rid}@{UID_DOMAIN}",
            summary=transform_title(
                event.summary,
                title_filter=config.title_filter,
                remove_filter=config.remove_filter,
                prefix=config.title_prefix,
            ),
            start=start,
            end=end,
            description=event.description or None,
            location=event.location or None,
            structured_location=place,
            buffer_minutes=config.buffer_minutes,
            leave_reminder=config.leave_reminder,
            language=self.hass.config.language,
        )
        # Travel time only applies to timed events with a place to travel to.
        destination = travel_destination(event.location, coordinates) if isinstance(start, datetime) else None
        if destination is not None and config.travel_time == TRAVEL_FIXED:
            item.travel_minutes = config.travel_minutes
        elif destination is not None and config.travel_time == TRAVEL_WAZE:
            item.destination = destination
            record = self._travel.get(key)
            # A new home location or region keeps the last travel time until Waze answers; a new place does not.
            if record is not None and record.place == place_fingerprint(destination):
                item.travel_minutes = record.minutes
        item.content_hash = content_hash(item.render())
        if item.travel_minutes and has_started(start, now):
            # Nobody leaves for an event that is under way, so a new or changed one gets no leave line or alarm
            # in the past. One written before it started keeps what it has, so the start never rewrites it.
            stored = self._events.get(key)
            if stored is None or stored["target"] != config.target or stored["hash"] != item.content_hash:
                item.travel_minutes = None
                item.content_hash = content_hash(item.render())
        return key, item

    def _waze_origin(self) -> str:
        """Return Home Assistant's home location as coordinates Waze accepts (a decimal point is required)."""
        return f"{self.hass.config.latitude:.6f},{self.hass.config.longitude:.6f}"

    def _travel_fingerprint(self, destination: str) -> str:
        """Return the fingerprint of a travel time from home to destination in the relay's Waze region."""
        return travel_fingerprint(self._waze_origin(), destination, self.config.waze_region)

    async def _async_update_travel(self, planned: dict[str, PlannedEvent], now: datetime) -> str | None:
        """Ask Waze for the travel times that are missing or due a live traffic update. Return a problem, or None.

        Events are asked for nearest first, and only when _travel_lookup says so. A new answer
        only changes the event, and so its hash, when the rounded minutes change. When Waze
        fails for an event, the event keeps its last travel time for the same place and waits
        before it is asked again (retry_delay), and later events are still asked for. A pass
        stops asking after WAZE_CALLS_PER_PASS calls, after WAZE_FAILURES_PER_PASS failures in
        a row, or after a failure every call would hit; the rest waits for a later pass, so
        writes are never held up for long. Never raises.
        """
        if self.config.travel_time != TRAVEL_WAZE:
            if self._travel or self._travel_retry:
                self._travel.clear()
                self._travel_retry.clear()
                self._dirty = True
            return None
        problem: str | None = None
        calls = failures = 0
        checked_available = False
        upcoming = [
            (key, item, item.destination, item.start)
            for key, item in planned.items()
            if item.destination is not None and isinstance(item.start, datetime) and item.start > now
        ]
        for key, item, destination, start in sorted(upcoming, key=lambda entry: entry[3]):
            fingerprint = self._travel_fingerprint(destination)
            realtime = self._travel_lookup(key, start, item.buffer_minutes, fingerprint, now)
            if realtime is None:
                continue
            retry = self._travel_retry.get(key)
            if retry is not None and retry.fingerprint == fingerprint and now < retry.retry_at:
                problem = problem or retry.error
                continue
            if calls >= WAZE_CALLS_PER_PASS or failures >= WAZE_FAILURES_PER_PASS:
                break
            if not checked_available:
                checked_available = True
                if not self.hass.services.has_service(WAZE_DOMAIN, WAZE_SERVICE):
                    LOGGER.debug("Relay %s: the Waze Travel Time action is not available", self.title)
                    return "Travel time: the Waze Travel Time action is not available"
            try:
                minutes, computed_at, called = await self._async_waze_minutes(
                    destination, fingerprint, realtime=realtime
                )
            except TravelTimeError as err:
                calls += 1
                failures += 1
                problem = problem or f"Travel time: {err}"
                self._remember_travel_failure(key, fingerprint, f"Travel time: {err}", now)
                if err.ends_lookups:
                    break
                continue
            calls += called
            failures = 0
            self._travel_retry.pop(key, None)
            self._travel[key] = TravelRecord(
                minutes, fingerprint, place_fingerprint(destination), _iso(computed_at), realtime, _iso(start)
            )
            self._dirty = True
            if item.travel_minutes != minutes:
                item.travel_minutes = minutes
                item.content_hash = content_hash(item.render())
        return problem

    def _travel_lookup(
        self, key: str, start: datetime, buffer_minutes: int, fingerprint: str, now: datetime
    ) -> bool | None:
        """Return None if an event needs no Waze answer now, otherwise whether to ask with live traffic.

        Without an answer for this home, destination and region, Waze is asked, with live traffic
        only when the event starts within REALTIME_WINDOW. An answer without live traffic, or with
        live traffic for another start (the event moved), is renewed once with live traffic from
        REALTIME_WINDOW before the start or REALTIME_BEFORE_LEAVE before the leave time, whichever
        is earlier, so a long trip gets it before its family leaves. After the leave time it would
        only move a time that has passed, so it is skipped.
        """
        record = self._travel.get(key)
        if record is None or record.fingerprint != fingerprint:
            return start - now <= REALTIME_WINDOW
        if record.realtime and record.start == _iso(start):
            return None
        leave_at = start - timedelta(minutes=record.minutes + buffer_minutes)
        if min(start - REALTIME_WINDOW, leave_at - REALTIME_BEFORE_LEAVE) <= now < leave_at:
            return True
        return None

    def _remember_travel_failure(self, key: str, fingerprint: str, message: str, now: datetime) -> None:
        """Record that Waze failed for an event. The wait doubles while the same route keeps failing."""
        previous = self._travel_retry.get(key)
        failures = previous.failures + 1 if previous is not None and previous.fingerprint == fingerprint else 1
        self._travel_retry[key] = TravelRetry(fingerprint, failures, now + retry_delay(failures), message)
        self._dirty = True

    async def _async_waze_minutes(
        self, destination: str, fingerprint: str, *, realtime: bool
    ) -> tuple[int, datetime, bool]:
        """Return a Waze travel time in minutes, when Waze gave it, and whether Waze was called for it.

        The calls of all relays go through one queue, one at a time with a pause after each. An
        answer for the same home, destination, region and traffic mode from the last
        WAZE_ANSWER_TTL is reused. Raise TravelTimeError when there is no usable answer.
        """
        queue = self.hass.data.setdefault(WAZE_QUEUE, WazeQueue())
        async with queue.lock:
            now = dt_util.utcnow()
            queue.answers = {
                wanted: answer for wanted, answer in queue.answers.items() if now - answer[0] < WAZE_ANSWER_TTL
            }
            if (answer := queue.answers.get((fingerprint, realtime))) is not None:
                return answer[1], answer[0], False
            try:
                minutes = await self._async_call_waze(destination, realtime=realtime)
            finally:
                await asyncio.sleep(WAZE_CALL_PAUSE)
            queue.answers[(fingerprint, realtime)] = (now, minutes)
            return minutes, now, True

    async def _async_call_waze(self, destination: str, *, realtime: bool) -> int:
        """Return the Waze travel time from home to destination in minutes, rounded up to 5.

        Only parameters that exist since Home Assistant 2026.3 are sent: the action's schema
        rejects unknown keys. Raise TravelTimeError when there is no usable answer, with
        ends_lookups set for a timeout, a missing action or data the action rejects.
        """
        try:
            async with asyncio.timeout(WAZE_CALL_TIMEOUT):
                response = await self.hass.services.async_call(
                    WAZE_DOMAIN,
                    WAZE_SERVICE,
                    {
                        "origin": self._waze_origin(),
                        "destination": destination,
                        "region": self.config.waze_region,
                        "realtime": realtime,
                    },
                    blocking=True,
                    return_response=True,
                )
        except Exception as err:
            # Waze failures arrive as HomeAssistantError (a place Waze cannot find and a lost connection alike),
            # a missing action as ServiceNotFound and bad data as vol.Invalid, but an action can raise anything,
            # and none of it may end the pass.
            LOGGER.warning("Relay %s: the Waze Travel Time action failed: %s", self.title, str(err) or repr(err))
            raise TravelTimeError(
                f"Waze Travel Time failed ({type(err).__name__})",
                ends_lookups=isinstance(err, TimeoutError | ServiceNotFound | vol.Invalid),
            ) from err
        routes = response.get("routes") if isinstance(response, dict) else None
        route = routes[0] if isinstance(routes, list) and routes else None
        if not isinstance(route, dict):
            LOGGER.warning("Relay %s: Waze Travel Time found no route", self.title)
            raise TravelTimeError("Waze Travel Time found no route")
        duration = route.get("duration")
        if isinstance(duration, bool) or not isinstance(duration, int | float) or not 0 <= duration < math.inf:
            LOGGER.warning("Relay %s: Waze Travel Time returned no usable duration: %r", self.title, duration)
            raise TravelTimeError("Waze Travel Time returned no travel time")
        minutes = round_travel_minutes(duration)
        if minutes > MAX_TRAVEL_MINUTES:
            # Not a drive to a family event; likely a place with the same name elsewhere. It would also make the
            # leave time arithmetic overflow for absurd values.
            LOGGER.warning("Relay %s: Waze Travel Time returned an implausible duration: %r", self.title, duration)
            raise TravelTimeError(f"Waze Travel Time returned more than {MAX_TRAVEL_MINUTES // 60} hours")
        return minutes

    def _forget_travel(self, planned: Mapping[str, PlannedEvent]) -> None:
        """Drop the travel times of events that are neither planned nor tracked, and retries of unplanned events."""
        for key in [key for key in self._travel if key not in planned and key not in self._events]:
            del self._travel[key]
            self._dirty = True
        for key in [key for key in self._travel_retry if key not in planned]:
            del self._travel_retry[key]
            self._dirty = True

    def _removals_confirmed(self, events: list[CalendarEvent], now: datetime) -> bool:
        """Return False while a read without any events waits to be confirmed by a later pass.

        A source that stays available but briefly returns nothing (an upstream API
        answering with an error payload) would otherwise delete every relayed future
        event, and recreate the ones deleted by hand once it recovers. A read that has
        events, none of which match the filter, is acted on at once.
        """
        if events or all(has_started(parse_iso(record["start"]), now) for record in self._events.values()):
            self._empty_since = None
            return True
        if self._empty_since is None:
            self._empty_since = now
        if now - self._empty_since >= EMPTY_READ_CONFIRM_AFTER:
            return True
        LOGGER.info(
            "Relay %s: the source calendar returned no events; relayed events are removed if that is still so "
            "on a pass after %s",
            self.title,
            EMPTY_READ_CONFIRM_AFTER,
        )
        return False

    def _on_account(self, calendar_url: str) -> bool:
        """Return True if calendar_url is among the calendars last discovered on the account."""
        wanted = collection_url(calendar_url)
        return any(collection_url(calendar.url) == wanted for calendar in self._account_calendars())

    def _foreign_refusal(self, err: CalDavError, calendar_url: str) -> CalDavError:
        """Turn a 403 from a calendar the account does not have into a missing calendar.

        That happens when the account was reconfigured to another user: the stored
        target belongs to the old one. New credentials cannot fix it, so it must not
        start reauthentication.
        """
        if isinstance(err, CalDavAuthError) and err.status == 403 and not self._on_account(calendar_url):
            return CalDavNotFoundError(err.status, err.condition)
        return err

    async def _async_put(self, calendar_url: str, item: PlannedEvent, now: datetime) -> str:
        """PUT an event into a calendar and return its href."""
        try:
            return await self._client.async_put_event(calendar_url, item.name, item.render(dtstamp=now))
        except CalDavError as err:
            converted = self._foreign_refusal(err, calendar_url)
            if converted is err:
                raise
            raise converted from err

    async def _async_delete(self, record: Mapping[str, str]) -> bool:
        """Delete a relayed event from the calendar it was written to.

        Return True if a copy was deleted now, False if it was already gone (404 or
        410) or its stored href is not inside its calendar (then nothing is sent).
        """
        calendar_url = record["target"]
        if not is_event_href(calendar_url, record["href"]):
            LOGGER.warning("Relay %s: a stored event is not inside its calendar, forgetting it", self.title)
            return False
        try:
            return await self._client.async_delete_event(calendar_url, record["href"])
        except CalDavError as err:
            converted = self._foreign_refusal(err, calendar_url)
            if converted is err:
                raise
            raise converted from err

    def _remember(self, key: str, item: PlannedEvent, href: str, target: str) -> None:
        """Record where an event was written and what it looked like."""
        self._events[key] = {
            "href": href,
            "hash": item.content_hash,
            "start": _iso(item.start),
            "end": _iso(item.end),
            "target": target,
        }
        self._dirty = True

    async def _async_write_planned(self, planned: dict[str, PlannedEvent], now: datetime, failures: list[str]) -> None:
        """PUT new events and events whose content changed; move events whose target calendar changed."""
        target = self.config.target
        for key, item in planned.items():
            record = self._events.get(key)
            if record is not None and record["target"] != target:
                await self._async_move(key, record, item, now, failures)
                continue
            if record is not None and record["hash"] == item.content_hash:
                continue
            try:
                href = await self._async_put(target, item, now)
            except _PASS_ENDING_ERRORS:
                raise
            except CalDavError as err:
                LOGGER.warning("Relay %s: writing an event failed, retrying on the next pass: %s", self.title, err)
                failures.append(str(err))
                continue
            self._remember(key, item, href, target)

    async def _async_move(
        self, key: str, record: dict[str, str], item: PlannedEvent, now: datetime, failures: list[str]
    ) -> None:
        """Move an event into the new target calendar.

        Servers refuse one UID in two calendars of the same account, so the old copy
        is deleted first. If it cannot be deleted for a reason that will not go away,
        it is left behind and the event is still written. If the new calendar refuses
        the event, the copy is put back into the old calendar and the move is not
        tried again until the event or the target changes, so a refusing calendar
        can never empty the old one.
        """
        target = self.config.target
        refused = self._failed_moves.get(key)
        if refused is not None and refused[:2] == (target, item.content_hash):
            failures.append(refused[2])
            return
        try:
            deleted = await self._async_delete(record)
        except _ACCOUNT_ERRORS:
            raise
        except CalDavError as err:
            LOGGER.warning(
                "Relay %s: the old copy of a moved event could not be deleted and stays in the previous calendar: %s",
                self.title,
                err,
            )
            deleted = False
        del self._events[key]
        self._dirty = True
        try:
            href = await self._async_put(target, item, now)
        except CalDavError as err:
            if deleted:
                await self._async_restore(key, record, item, now)
            if isinstance(err, _ACCOUNT_ERRORS):
                raise
            message = f"Moving an event to the new calendar failed: {err}"
            self._failed_moves[key] = (target, item.content_hash, message)
            if isinstance(err, CalDavNotFoundError):
                raise
            LOGGER.warning("Relay %s: %s", self.title, message)
            failures.append(message)
            return
        self._failed_moves.pop(key, None)
        self._remember(key, item, href, target)

    async def _async_restore(self, key: str, record: Mapping[str, str], item: PlannedEvent, now: datetime) -> None:
        """Put an event back into the calendar it was just deleted from."""
        try:
            href = await self._client.async_put_event(record["target"], item.name, item.render(dtstamp=now))
        except CalDavError as err:
            LOGGER.warning(
                "Relay %s: putting a moved event back into the previous calendar failed: %s", self.title, err
            )
            return
        self._remember(key, item, href, record["target"])

    async def _async_remove_unwanted(
        self, planned: dict[str, PlannedEvent], now: datetime, failures: list[str]
    ) -> None:
        """DELETE withdrawn events that have not started; forget the ones that already started."""
        target = self.config.target
        for key in [key for key in self._events if key not in planned]:
            record = self._events[key]
            if not has_started(parse_iso(record["start"]), now):
                try:
                    await self._async_delete(record)
                except _ACCOUNT_ERRORS:
                    raise
                except CalDavError as err:
                    if record["target"] == target:
                        if isinstance(err, CalDavNotFoundError):
                            raise
                        LOGGER.warning(
                            "Relay %s: deleting an event failed, retrying on the next pass: %s", self.title, err
                        )
                        failures.append(str(err))
                        continue
                    # A copy in a previous target calendar is not retried: that calendar is no longer the relay's.
                    LOGGER.warning(
                        "Relay %s: a withdrawn event stays in the previous target calendar: %s", self.title, err
                    )
            del self._events[key]
            self._dirty = True
