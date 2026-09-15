"""Relay engine: mirror matching events from a calendar entity into a CalDAV calendar, one way."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.calendar import DATA_COMPONENT, CalendarEvent
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

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
    CONF_LOOK_AHEAD_DAYS,
    CONF_REMOVE_FILTER,
    CONF_SOURCE,
    CONF_TARGET,
    CONF_TARGET_NAME,
    CONF_TITLE_FILTER,
    CONF_TITLE_PREFIX,
    DEFAULT_LOOK_AHEAD_DAYS,
    DOMAIN,
    EMPTY_READ_CONFIRM_AFTER,
    ISSUE_TARGET_MISSING,
    LOGGER,
    SOURCE_CHANGE_COOLDOWN,
    STORAGE_VERSION,
    SYNC_INTERVAL,
)
from .ics import clean_text, content_hash, render_event

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

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> RelayConfig:
        """Build the settings from subentry data."""
        return cls(
            source=data[CONF_SOURCE],
            target=data[CONF_TARGET],
            target_name=data.get(CONF_TARGET_NAME) or "",
            title_filter=data.get(CONF_TITLE_FILTER) or "",
            remove_filter=bool(data.get(CONF_REMOVE_FILTER, False)),
            title_prefix=data.get(CONF_TITLE_PREFIX) or "",
            look_ahead_days=int(data.get(CONF_LOOK_AHEAD_DAYS, DEFAULT_LOOK_AHEAD_DAYS)),
        )


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
    content_hash: str = ""

    def render(self, dtstamp: datetime | None = None) -> str:
        """Render the event. Without dtstamp the text is stable and used for change detection."""
        return render_event(
            uid=self.uid,
            summary=self.summary,
            start=self.start,
            end=self.end,
            description=self.description,
            location=self.location,
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
        events = data.get("events") if isinstance(data, dict) else None
        if not isinstance(events, dict):
            return
        self._events = {
            key: {name: record[name] for name in RECORD_FIELDS}
            for key, record in events.items()
            if isinstance(record, dict) and all(isinstance(record.get(name), str) for name in RECORD_FIELDS)
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
            try:
                error = await self._async_sync_pass()
            except Exception:
                LOGGER.exception("Relay %s: unexpected error during sync", self.title)
                error = "Unexpected error"
            if error is None:
                self.last_sync = dt_util.utcnow()
                self.last_error = None
                ir.async_delete_issue(self.hass, DOMAIN, issue_id(self.subentry_id))
            else:
                self.last_error = error
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
            await self._async_write_planned(planned, now, failures)
            # An event that could not be planned is not known to be withdrawn, so nothing is removed.
            if complete and self._removals_confirmed(events, now):
                await self._async_remove_unwanted(planned, now, failures)
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
                await self._store.async_save({"events": dict(self._events)})
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
        )
        item.content_hash = content_hash(item.render())
        return key, item

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
