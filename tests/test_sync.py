"""Tests for the sync engine: what is written, updated, deleted and left alone."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.calendar_relay.caldav import (
    CalDavAuthError,
    CalDavConnectionError,
    CalDavNotFoundError,
    CalDavRefusedError,
    CalDavStatusError,
    CalDavUntrustedHostError,
)
from custom_components.calendar_relay.const import DOMAIN
from custom_components.calendar_relay.ics import content_hash, render_event
from custom_components.calendar_relay.relay import Relay, event_key, resource_id, transform_title

from .conftest import (
    CALL_UP,
    FAMILY_URL,
    PREFIX,
    RELAY_ID,
    SOURCE,
    WORK_URL,
    FakeCalendar,
    FakeDav,
    FakeWaze,
    make_entry,
    relay_data,
    relay_subentry,
    setup_entry,
    timed,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

NOW = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
KICKOFF = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
STORE_KEY = f"{DOMAIN}.{RELAY_ID}"
# A calendar of another account on the same server; the fake account does not list it.
OLD_ACCOUNT_URL = "https://p99-caldav.example.com/987654321/calendars/family/"
VALID_RECORD = {
    "href": f"{FAMILY_URL}relay-{'0' * 32}.ics",
    "hash": "0" * 64,
    "start": "2026-09-30T10:00:00+00:00",
    "end": "2026-09-30T11:00:00+00:00",
    "target": FAMILY_URL,
}


@pytest.fixture(autouse=True)
async def _clock(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await hass.config.async_set_time_zone("Europe/Copenhagen")
    freezer.move_to(NOW)


def relay_of(entry: MockConfigEntry) -> Relay:
    return entry.runtime_data.relays[RELAY_ID]


def stored(hass_storage: dict) -> dict:
    return hass_storage[STORE_KEY]["data"]["events"]


def call_up(start: datetime = KICKOFF, **kwargs) -> object:
    return timed(f"{CALL_UP}Home - Away", start, **kwargs)


async def test_first_sync_relays_matching_events(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """Only call-ups are relayed, with the filter text replaced by the prefix."""
    from homeassistant.components.calendar import CalendarEvent

    source_calendar.events = [
        call_up(description="Meet at 9:30; bring water", location="Pitch 2"),
        timed("Training", KICKOFF + timedelta(days=1), uid="training-1"),
        CalendarEvent(start=date(2026, 9, 26), end=date(2026, 9, 27), summary=f"{CALL_UP}Cup day", uid="cup-1"),
    ]
    await setup_entry(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert len(fake_dav.puts()) == 2
    assert fake_dav.deletes() == []
    for href, body in fake_dav.resources.items():
        name = re.fullmatch(re.escape(FAMILY_URL) + r"relay-([0-9a-f]{32})\.ics", href)
        assert name is not None
        assert f"UID:{name.group(1)}@calendar-relay\r\n" in body
        assert "DTSTAMP:20260915T100000Z\r\n" in body
    match_body = next(body for body in fake_dav.resources.values() if "Home - Away" in body)
    assert "SUMMARY:⚽ Emma: Home - Away\r\n" in match_body
    assert (
        "DTSTART;TZID=Europe/Copenhagen:20260920T120000\r\nDTEND;TZID=Europe/Copenhagen:20260920T140000\r\n"
        in match_body
    )
    assert match_body.count("BEGIN:VTIMEZONE\r\nTZID:Europe/Copenhagen\r\n") == 1
    assert "DESCRIPTION:Meet at 9:30\\; bring water\r\n" in match_body
    assert "LOCATION:Pitch 2\r\n" in match_body
    cup_body = next(body for body in fake_dav.resources.values() if "Cup day" in body)
    assert "SUMMARY:⚽ Emma: Cup day\r\n" in cup_body
    assert "DTSTART;VALUE=DATE:20260926\r\nDTEND;VALUE=DATE:20260927\r\n" in cup_body
    assert "VTIMEZONE" not in cup_body

    records = stored(hass_storage)
    assert len(records) == 2
    assert {record["target"] for record in records.values()} == {FAMILY_URL}
    assert {record["href"] for record in records.values()} == set(fake_dav.resources)
    relay = relay_of(config_entry)
    assert relay.relayed_events == 2
    assert relay.last_error is None
    assert relay.last_sync == NOW


async def test_unchanged_events_are_not_written_again(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, config_entry: MockConfigEntry
) -> None:
    """No churn: a pass with nothing changed makes no requests."""
    source_calendar.events = [call_up()]
    await setup_entry(hass, config_entry)
    fake_dav.calls.clear()
    await relay_of(config_entry).async_sync()
    await relay_of(config_entry).async_sync()
    assert fake_dav.calls == []


async def test_rescheduled_event_is_updated_in_place(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """A changed event is PUT again to the same resource."""
    source_calendar.events = [call_up()]
    await setup_entry(hass, config_entry)
    [href] = fake_dav.puts()
    old_hash = next(iter(stored(hass_storage).values()))["hash"]

    source_calendar.events = [call_up(KICKOFF + timedelta(hours=3))]
    fake_dav.calls.clear()
    await relay_of(config_entry).async_sync()

    assert fake_dav.calls == [("PUT", href)]
    assert "DTSTART;TZID=Europe/Copenhagen:20260920T150000\r\n" in fake_dav.resources[href]
    record = next(iter(stored(hass_storage).values()))
    assert record["hash"] != old_hash
    assert record["start"] == "2026-09-20T13:00:00+00:00"


async def test_withdrawn_call_up_is_deleted(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """When the title loses the filter text, the future event is deleted."""
    source_calendar.events = [call_up()]
    await setup_entry(hass, config_entry)
    [href] = fake_dav.puts()

    source_calendar.events = [timed("Home - Away", KICKOFF)]
    fake_dav.calls.clear()
    await relay_of(config_entry).async_sync()

    assert fake_dav.calls == [("DELETE", href)]
    assert fake_dav.resources == {}
    assert stored(hass_storage) == {}
    assert relay_of(config_entry).relayed_events == 0


async def test_withdrawn_event_that_started_is_forgotten(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A running event that is withdrawn stays in the target and is no longer tracked."""
    source_calendar.events = [call_up(NOW + timedelta(hours=1))]
    await setup_entry(hass, config_entry)
    [href] = fake_dav.puts()

    freezer.move_to(NOW + timedelta(hours=2))
    source_calendar.events = []
    fake_dav.calls.clear()
    await relay_of(config_entry).async_sync()

    assert fake_dav.calls == []
    assert href in fake_dav.resources
    assert stored(hass_storage) == {}


async def test_ended_events_are_forgotten_not_deleted(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Past events drop out of the window and out of the state, and stay in the calendar."""
    from homeassistant.components.calendar import CalendarEvent

    source_calendar.events = [
        call_up(NOW + timedelta(hours=1)),
        CalendarEvent(start=date(2026, 9, 15), end=date(2026, 9, 16), summary=f"{CALL_UP}Camp", uid="camp"),
    ]
    await setup_entry(hass, config_entry)
    assert len(fake_dav.puts()) == 2

    freezer.move_to(datetime(2026, 9, 16, 0, 30, tzinfo=UTC))  # 02:30 on 16 September in Copenhagen
    fake_dav.calls.clear()
    await relay_of(config_entry).async_sync()

    assert fake_dav.calls == []
    assert len(fake_dav.resources) == 2
    assert stored(hass_storage) == {}


async def test_delete_of_event_removed_by_hand_counts_as_done(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """A 404 on DELETE is success."""
    source_calendar.events = [call_up()]
    await setup_entry(hass, config_entry)
    fake_dav.resources.clear()

    source_calendar.events = [timed("Training", KICKOFF, uid="training")]
    await relay_of(config_entry).async_sync()

    assert len(fake_dav.deletes()) == 1
    assert stored(hass_storage) == {}
    assert relay_of(config_entry).last_error is None


async def test_event_deleted_by_hand_is_not_recreated_until_it_changes(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, config_entry: MockConfigEntry
) -> None:
    """The relay does not fight the user; a changed source event is written again."""
    source_calendar.events = [call_up()]
    await setup_entry(hass, config_entry)
    fake_dav.resources.clear()
    fake_dav.calls.clear()

    await relay_of(config_entry).async_sync()
    assert fake_dav.calls == []

    source_calendar.events = [call_up(location="Pitch 3")]
    await relay_of(config_entry).async_sync()
    assert len(fake_dav.puts()) == 1
    assert len(fake_dav.resources) == 1


async def test_unavailable_source_never_deletes(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """An unavailable source skips the pass entirely."""
    source_calendar.events = [call_up()]
    await setup_entry(hass, config_entry)
    fake_dav.calls.clear()
    reads = source_calendar.calls

    source_calendar.events = []
    source_calendar._attr_available = False
    source_calendar.async_write_ha_state()
    await relay_of(config_entry).async_sync()

    assert fake_dav.calls == []
    assert source_calendar.calls == reads
    assert len(stored(hass_storage)) == 1
    relay = relay_of(config_entry)
    assert relay.last_error == "Source calendar unavailable"
    assert relay.last_sync == NOW


async def test_failing_source_never_deletes(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A source that raises skips the pass entirely."""
    source_calendar.events = [call_up()]
    await setup_entry(hass, config_entry)
    fake_dav.calls.clear()

    source_calendar.error = HomeAssistantError("calendar backend down")
    await relay_of(config_entry).async_sync()

    assert fake_dav.calls == []
    assert len(stored(hass_storage)) == 1
    assert relay_of(config_entry).last_error == "Reading the source calendar failed"
    assert "reading the source calendar failed" in caplog.text


async def test_missing_source_entity(hass: HomeAssistant, fake_dav: FakeDav) -> None:
    """A relay whose source calendar does not exist does nothing."""
    entry = make_entry(relay_subentry(source_calendar="calendar.gone"))
    await setup_entry(hass, entry)
    assert fake_dav.calls == []
    assert relay_of(entry).last_error == "Source calendar unavailable"


async def test_events_without_uid(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """Events without a uid are keyed by summary and start, so a move is a delete plus a create."""
    source_calendar.events = [call_up(uid=None)]
    await setup_entry(hass, config_entry)
    [old_href] = fake_dav.puts()
    assert all(key.startswith("hash:") for key in stored(hass_storage))

    await relay_of(config_entry).async_sync()
    assert len(fake_dav.puts()) == 1

    source_calendar.events = [call_up(KICKOFF + timedelta(days=1), uid=None)]
    fake_dav.calls.clear()
    await relay_of(config_entry).async_sync()

    [new_href] = fake_dav.puts()
    assert new_href != old_href
    assert fake_dav.deletes() == [old_href]
    assert list(fake_dav.resources) == [new_href]


async def test_recurring_instances_are_separate_events(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, config_entry: MockConfigEntry
) -> None:
    """Each instance of a recurring event is relayed on its own; cancelling one removes only that one."""
    instances = [
        timed(
            f"{CALL_UP}League",
            KICKOFF + timedelta(days=7 * week),
            uid="league",
            recurrence_id=f"2026092{week}T120000",
            rrule="FREQ=WEEKLY;COUNT=3",
        )
        for week in range(3)
    ]
    source_calendar.events = list(instances)
    await setup_entry(hass, config_entry)
    hrefs = fake_dav.puts()
    assert len(set(hrefs)) == 3
    # The VTIMEZONE has yearly RRULEs; the events themselves repeat nothing.
    assert all("RRULE" not in body[body.index("BEGIN:VEVENT") :] for body in fake_dav.resources.values())

    source_calendar.events = [instances[0], instances[2]]
    fake_dav.calls.clear()
    await relay_of(config_entry).async_sync()
    assert fake_dav.calls == [("DELETE", hrefs[1])]


async def test_look_ahead_limits_the_window(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav
) -> None:
    """Events beyond the look-ahead are not read."""
    source_calendar.events = [call_up(NOW + timedelta(days=3)), call_up(NOW + timedelta(days=10), uid="later")]
    await setup_entry(hass, make_entry(relay_subentry(look_ahead_days=7)))
    assert len(fake_dav.puts()) == 1


async def test_empty_filter_relays_everything(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav
) -> None:
    """Without a filter or prefix the titles are copied as they are."""
    source_calendar.events = [call_up(), timed("Training", KICKOFF, uid="training")]
    await setup_entry(hass, make_entry(relay_subentry(title_filter="", title_prefix="", remove_filter=False)))
    summaries = sorted(
        line for body in fake_dav.resources.values() for line in body.split("\r\n") if line.startswith("SUMMARY")
    )
    assert summaries == ["SUMMARY:Training", f"SUMMARY:{CALL_UP}Home - Away"]


async def test_changing_the_target_moves_future_events(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """The old copy is deleted before the event is written into the new calendar."""
    source_calendar.events = [call_up()]
    await setup_entry(hass, config_entry)
    [old_href] = fake_dav.puts()
    fake_dav.calls.clear()

    hass.config_entries.async_update_subentry(
        config_entry,
        config_entry.subentries[RELAY_ID],
        data=relay_data(target_calendar=WORK_URL, target_calendar_name="Work"),
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    new_href = old_href.replace(FAMILY_URL, WORK_URL)
    assert fake_dav.calls == [("DELETE", old_href), ("PUT", new_href)]
    assert list(fake_dav.resources) == [new_href]
    assert next(iter(stored(hass_storage).values()))["target"] == WORK_URL


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_error_starts_reauth(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, config_entry: MockConfigEntry, status: int
) -> None:
    """401 and 403 while syncing start the reauth flow for the account."""
    source_calendar.events = [call_up()]
    fake_dav.put_error = CalDavAuthError(status, excerpt="Unauthorized")
    await setup_entry(hass, config_entry)

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]
    assert flows[0]["context"]["entry_id"] == config_entry.entry_id
    assert relay_of(config_entry).last_error == f"Authentication failed (HTTP {status})"
    assert config_entry.state is ConfigEntryState.LOADED


@pytest.mark.parametrize("status", [404, 409])
async def test_missing_target_creates_repair_issue_until_next_success(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
    status: int,
) -> None:
    """A missing target calendar raises a repair issue that goes away after a successful sync."""
    source_calendar.events = [call_up()]
    fake_dav.put_error = CalDavNotFoundError(status, excerpt="Not Found")
    await setup_entry(hass, config_entry)

    issue = issue_registry.async_get_issue(DOMAIN, f"target_missing_{RELAY_ID}")
    assert issue is not None
    assert issue.translation_key == "target_calendar_missing"
    assert issue.translation_placeholders == {"relay": "Kids → Family", "calendar": "Family"}
    assert issue.severity is ir.IssueSeverity.ERROR
    assert relay_of(config_entry).last_error == f"Target calendar not found (HTTP {status})"

    fake_dav.put_error = None
    await relay_of(config_entry).async_sync()
    assert issue_registry.async_get_issue(DOMAIN, f"target_missing_{RELAY_ID}") is None


async def test_connection_error_is_retried_on_the_next_pass(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Other failures log a warning with the server's answer and the next pass tries again."""
    source_calendar.events = [call_up()]
    fake_dav.put_error = CalDavConnectionError("PUT returned HTTP 503", "Service Unavailable")
    await setup_entry(hass, config_entry)

    assert STORE_KEY not in hass_storage
    assert relay_of(config_entry).last_error == "PUT returned HTTP 503"
    assert "sync failed, retrying on the next pass: PUT returned HTTP 503, response body: Service Unavailable" in (
        caplog.text
    )

    fake_dav.put_error = None
    await relay_of(config_entry).async_sync()
    assert len(fake_dav.resources) == 1
    assert relay_of(config_entry).last_error is None


async def test_rejected_event_does_not_block_the_others(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, config_entry: MockConfigEntry
) -> None:
    """An event the server refuses is retried later while the rest are written."""
    source_calendar.events = [call_up(), timed(f"{CALL_UP}Cup", KICKOFF + timedelta(days=2), uid="cup")]
    fake_dav.put_error = lambda href, ics: CalDavStatusError(412) if "Home - Away" in ics else None
    await setup_entry(hass, config_entry)

    assert len(fake_dav.resources) == 1
    assert relay_of(config_entry).last_error == "1 event change(s) failed, first error: HTTP 412"
    assert relay_of(config_entry).relayed_events == 1


async def test_rejected_delete_is_retried(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """A failed DELETE keeps the record so the next pass tries again. A refused DELETE (a status, or credentials that
    fail) deleted nothing, so the record keeps its hash; one without a usable answer may have been carried out, so its
    hash is cleared and the event is written again if it comes back."""
    source_calendar.events = [call_up()]
    await setup_entry(hass, config_entry)
    record = dict(stored(hass_storage)[event_key(call_up())])
    source_calendar.events = [timed("Training", KICKOFF, uid="training")]

    fake_dav.delete_error = CalDavStatusError(400, excerpt="Bad Request")
    await relay_of(config_entry).async_sync()
    assert stored(hass_storage) == {event_key(call_up()): record}
    assert relay_of(config_entry).last_error == "1 event change(s) failed, first error: HTTP 400"

    fake_dav.delete_error = CalDavAuthError(401)
    await relay_of(config_entry).async_sync()
    assert stored(hass_storage) == {event_key(call_up()): record}
    assert relay_of(config_entry).last_error == "Authentication failed (HTTP 401)"

    fake_dav.delete_error = CalDavConnectionError("DELETE failed: ClientConnectorError")
    await relay_of(config_entry).async_sync()
    assert stored(hass_storage) == {event_key(call_up()): {**record, "hash": ""}}

    fake_dav.delete_error = None
    await relay_of(config_entry).async_sync()
    assert stored(hass_storage) == {}
    assert fake_dav.resources == {}


async def test_sync_never_raises(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unexpected exception is logged and recorded, not raised."""
    await setup_entry(hass, config_entry)
    relay = relay_of(config_entry)
    with patch.object(relay, "_plan", side_effect=RuntimeError("boom")):
        await relay.async_sync()
    assert relay.last_error == "Unexpected error"
    assert "unexpected error during sync" in caplog.text


async def test_one_sync_at_a_time(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, config_entry: MockConfigEntry
) -> None:
    """A second pass waits for the first; it then finds nothing to do."""
    await setup_entry(hass, config_entry)
    relay = relay_of(config_entry)
    source_calendar.events = [call_up()]
    reads = source_calendar.calls

    gate = asyncio.Event()
    active = 0
    peak = 0
    original_put = fake_dav.async_put_event

    async def slow_put(calendar_url: str, name: str, ics: str) -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await gate.wait()
        active -= 1
        return await original_put(calendar_url, name, ics)

    fake_dav.async_put_event = slow_put  # type: ignore[method-assign]
    first = hass.async_create_task(relay.async_sync())
    second = hass.async_create_task(relay.async_sync())
    for _ in range(5):
        await asyncio.sleep(0)
    assert relay.syncing
    assert source_calendar.calls == reads + 1

    gate.set()
    await asyncio.gather(first, second)
    assert peak == 1
    assert source_calendar.calls == reads + 2
    assert len(fake_dav.puts()) == 1
    assert not relay.syncing


async def test_state_survives_a_reload(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, config_entry: MockConfigEntry
) -> None:
    """After a reload the stored state prevents rewriting."""
    source_calendar.events = [call_up()]
    await setup_entry(hass, config_entry)
    fake_dav.calls.clear()

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert fake_dav.calls == []
    assert relay_of(config_entry).relayed_events == 1


async def test_invalid_stored_records_are_ignored(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """Broken state never crashes setup; only valid records load, with or without a token and pending removals."""
    hass_storage[STORE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": {
            "events": {
                "a": "not a record",
                "b": {"href": FAMILY_URL},
                "c": {**VALID_RECORD, "token": "../x"},
                "d": {**VALID_RECORD, "pending": FAMILY_URL},
                "e": {**VALID_RECORD, "token": "0a1b2c3d", "pending": [VALID_RECORD["href"]]},
            }
        },
    }
    await setup_entry(hass, config_entry)
    assert relay_of(config_entry).relayed_events == 1
    assert fake_dav.calls == []


async def test_stored_href_outside_its_calendar_is_never_deleted(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A record pointing at the calendar collection itself is forgotten without a DELETE."""
    source_calendar.events = [timed("Training", KICKOFF, uid="training")]
    hass_storage[STORE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": {
            "events": {
                '["gone",""]': {
                    "href": FAMILY_URL,
                    "hash": "0" * 64,
                    "start": "2026-09-30T10:00:00+00:00",
                    "end": "2026-09-30T11:00:00+00:00",
                    "target": FAMILY_URL,
                }
            }
        },
    }
    await setup_entry(hass, config_entry)
    assert fake_dav.calls == []
    assert stored(hass_storage) == {}
    assert "not inside its calendar" in caplog.text


async def test_source_change_is_debounced(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, config_entry: MockConfigEntry
) -> None:
    """A change of the source state or attributes syncs about 30 seconds later, once."""
    await setup_entry(hass, config_entry)
    reads = source_calendar.calls
    source_calendar.events = [call_up()]

    hass.states.async_set("calendar.kids", "off", {"message": "Home - Away"})
    await hass.async_block_till_done()
    hass.states.async_set("calendar.kids", "off", {"message": "Home - Away", "location": "Pitch 2"})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert source_calendar.calls == reads

    async_fire_time_changed(hass, NOW + timedelta(seconds=31))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert source_calendar.calls == reads + 1
    assert len(fake_dav.puts()) == 1


async def test_interval_sync(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, config_entry: MockConfigEntry
) -> None:
    """Every 15 minutes a pass runs even without a state change."""
    await setup_entry(hass, config_entry)
    source_calendar.events = [call_up()]
    async_fire_time_changed(hass, NOW + timedelta(minutes=15, seconds=1))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(fake_dav.puts()) == 1


async def test_first_sync_waits_until_home_assistant_has_started(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, config_entry: MockConfigEntry
) -> None:
    """While Home Assistant is starting nothing is synced."""
    source_calendar.events = [call_up()]
    hass.set_state(CoreState.starting)
    await setup_entry(hass, config_entry)
    assert fake_dav.puts() == []

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(fake_dav.puts()) == 1


async def test_triggers_wait_until_home_assistant_has_started(
    hass: HomeAssistant, source_calendar: FakeCalendar, fake_dav: FakeDav, config_entry: MockConfigEntry
) -> None:
    """Source changes and the interval do not sync while Home Assistant is starting; afterwards they do."""
    hass.set_state(CoreState.starting)
    await setup_entry(hass, config_entry)
    source_calendar.events = [call_up()]
    hass.states.async_set(SOURCE, "off", {"message": "Home - Away"})
    async_fire_time_changed(hass, NOW + timedelta(seconds=31))
    await hass.async_block_till_done(wait_background_tasks=True)
    async_fire_time_changed(hass, NOW + timedelta(minutes=15, seconds=1))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert source_calendar.calls == 0
    assert fake_dav.calls == []

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert source_calendar.calls == 1

    source_calendar.events = [call_up(location="Pitch 2")]
    hass.states.async_set(SOURCE, "off", {"message": "Home - Away", "location": "Pitch 2"})
    async_fire_time_changed(hass, NOW + timedelta(seconds=31))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert source_calendar.calls == 2
    async_fire_time_changed(hass, NOW + timedelta(minutes=15, seconds=1))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert source_calendar.calls == 3
    assert len(fake_dav.puts()) == 2


async def test_redirected_write_is_later_deleted_through_the_same_redirect(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict,
) -> None:
    """A PUT redirected to another partition host is recorded inside the target, so a withdrawal still deletes it."""
    event = call_up()
    source_calendar.events = [event]
    requested = f"{FAMILY_URL}relay-{resource_id(RELAY_ID, event_key(event))}.ics"
    redirected = requested.replace("p99-caldav", "p98-caldav")
    for method, status in (("PUT", 201), ("DELETE", 204)):
        aioclient_mock.request(method, requested, status=307, headers={"Location": redirected})
        aioclient_mock.request(method, redirected, status=status)
    with patch("custom_components.calendar_relay.caldav.CalDavClient.async_discover", return_value=FakeDav().calendars):
        await setup_entry(hass, config_entry)
    assert stored(hass_storage)[event_key(event)]["href"] == requested

    source_calendar.events = [timed("Home - Away", KICKOFF)]
    await relay_of(config_entry).async_sync()

    assert [(method.upper(), str(url)) for method, url, _data, _headers in aioclient_mock.mock_calls] == [
        ("PUT", requested),
        ("PUT", redirected),
        ("DELETE", requested),
        ("DELETE", redirected),
    ]
    assert stored(hass_storage) == {}
    assert relay_of(config_entry).last_error is None


@pytest.mark.parametrize("deleted_on_device", [False, True])
async def test_update_of_an_opened_entry_moves_to_a_fresh_identity_and_deletes_the_old(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    dav_server: FakeDav,
    waze: FakeWaze,
    hass_storage: dict,
    deleted_on_device: bool,
) -> None:
    """iCloud answers a PUT to an entry someone opened on an iPhone, or deleted there, with 412 for good, so a moved
    match is written under a fresh resource name and UID, and then the old entry is deleted (already gone is fine).

    That is one write: the store holds the new href, token and start, the event keeps its travel time and alert, Waze
    is not asked again and its state is kept, and the next pass has nothing to do. Later updates go to the new entry.
    """
    event = call_up(location="Pitch 2")
    source_calendar.events = [event]
    entry = make_entry(relay_subentry(travel_time="waze", leave_reminder=True))
    await setup_entry(hass, entry)
    [old_href] = dav_server.puts()
    travel = hass_storage[STORE_KEY]["data"]["travel"]
    rid = resource_id(RELAY_ID, event_key(event))
    assert len(waze.calls) == 1

    if deleted_on_device:
        dav_server.delete_on_device(old_href)
    else:
        dav_server.open_on_device(old_href)
    source_calendar.events = [call_up(KICKOFF + timedelta(hours=3), location="Pitch 2")]
    dav_server.calls.clear()
    relay = relay_of(entry)
    await relay.async_sync()

    record = stored(hass_storage)[event_key(event)]
    token = record["token"]
    assert re.fullmatch(r"[0-9a-f]{8}", token)
    new_href = f"{FAMILY_URL}relay-{rid}-{token}.ics"
    assert dav_server.calls == [("PUT", old_href), ("PUT", new_href), ("DELETE", old_href)]
    assert list(dav_server.resources) == [new_href]
    body = dav_server.resources[new_href]
    assert f"UID:{rid}-{token}@calendar-relay\r\n" in body
    assert "DTSTART;TZID=Europe/Copenhagen:20260920T150000\r\n" in body
    assert "X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT25M\r\n" in body
    assert "BEGIN:VALARM\r\n" in body
    assert record == {
        "href": new_href,
        "hash": record["hash"],
        "start": "2026-09-20T13:00:00+00:00",
        "end": "2026-09-20T15:00:00+00:00",
        "target": FAMILY_URL,
        "token": token,
    }
    assert hass_storage[STORE_KEY]["data"]["travel"] == travel
    assert len(waze.calls) == 1
    assert relay.last_error is None

    dav_server.calls.clear()
    await relay.async_sync()
    assert dav_server.calls == []

    source_calendar.events = [call_up(KICKOFF + timedelta(hours=4), location="Pitch 2")]
    await relay.async_sync()
    assert dav_server.calls == [("PUT", new_href)]
    assert "DTSTART;TZID=Europe/Copenhagen:20260920T160000\r\n" in dav_server.resources[new_href]
    assert stored(hass_storage)[event_key(event)]["token"] == token
    assert len(waze.calls) == 1


@pytest.mark.parametrize("blocked", ["name and UID", "name", "UID"])
async def test_call_up_reinstated_after_its_entry_was_deleted_gets_a_fresh_identity(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    dav_server: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
    blocked: str,
) -> None:
    """After a withdrawn call-up's entry is deleted, iCloud refuses its resource name or UID (which one is not known).
    A reinstated call-up has no record, so it is created under its base name, and after the 412 under a fresh name and
    UID, without deleting anything. Withdrawn again, its entry is deleted by the stored href, whatever its token."""
    event = call_up()
    source_calendar.events = [event]
    await setup_entry(hass, config_entry)
    [base_href] = dav_server.puts()
    rid = resource_id(RELAY_ID, event_key(event))
    relay = relay_of(config_entry)

    source_calendar.events = [timed("Home - Away", KICKOFF)]
    await relay.async_sync()
    assert dav_server.deletes() == [base_href]
    assert stored(hass_storage) == {}
    if blocked == "name":
        dav_server.blocked.remove(f"{rid}@calendar-relay")
    elif blocked == "UID":
        dav_server.blocked.remove(base_href)

    source_calendar.events = [event]
    dav_server.calls.clear()
    await relay.async_sync()

    record = stored(hass_storage)[event_key(event)]
    new_href = f"{FAMILY_URL}relay-{rid}-{record['token']}.ics"
    assert dav_server.calls == [("PUT", base_href), ("PUT", new_href)]
    assert list(dav_server.resources) == [new_href]
    assert f"UID:{rid}-{record['token']}@calendar-relay\r\n" in dav_server.resources[new_href]
    assert (record["href"], "pending" in record) == (new_href, False)
    assert relay.last_error is None

    dav_server.calls.clear()
    await relay.async_sync()
    assert dav_server.calls == []

    source_calendar.events = [timed("Home - Away", KICKOFF)]
    await relay.async_sync()
    assert dav_server.calls == [("DELETE", new_href)]
    assert dav_server.resources == {}
    assert stored(hass_storage) == {}


@pytest.mark.parametrize(
    ("status", "last_error"),
    [(400, "1 event change(s) failed, first error: HTTP 400"), (503, "DELETE returned HTTP 503")],
)
async def test_old_entry_that_cannot_be_deleted_is_deleted_on_a_later_pass(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    dav_server: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
    status: int,
    last_error: str,
) -> None:
    """When the old entry cannot be deleted after the event was written under a fresh identity, the record keeps its
    href as a pending removal and later passes delete it, never the new entry; an update meanwhile goes to the new
    entry. Old entries are deleted after every write, so the other events are written first; a refused DELETE fails
    alone, a server error ends the pass like any other."""
    a, b = call_up(uid="a"), call_up(KICKOFF + timedelta(days=1), uid="b")
    source_calendar.events = [a, b]
    await setup_entry(hass, config_entry)
    a_old, b_href = dav_server.puts()
    dav_server.open_on_device(a_old)
    dav_server.delete_failing[a_old] = status
    moved_b = call_up(KICKOFF + timedelta(days=1), uid="b", location="Pitch 2")
    source_calendar.events = [call_up(KICKOFF + timedelta(hours=3), uid="a"), moved_b]
    dav_server.calls.clear()
    relay = relay_of(config_entry)
    await relay.async_sync()

    record = stored(hass_storage)[event_key(a)]
    a_new = record["href"]
    assert a_new == f"{FAMILY_URL}relay-{resource_id(RELAY_ID, event_key(a))}-{record['token']}.ics"
    assert dav_server.calls == [("PUT", a_old), ("PUT", a_new), ("PUT", b_href), ("DELETE", a_old)]
    assert record["pending"] == [a_old]
    assert set(dav_server.resources) == {a_old, a_new, b_href}
    assert relay.last_error == last_error

    dav_server.calls.clear()
    await relay.async_sync()
    assert dav_server.calls == [("DELETE", a_old)]
    assert stored(hass_storage)[event_key(a)] == record
    assert relay.last_error == last_error

    dav_server.delete_failing.clear()
    source_calendar.events = [call_up(KICKOFF + timedelta(hours=4), uid="a"), moved_b]
    dav_server.calls.clear()
    await relay.async_sync()
    assert dav_server.calls == [("PUT", a_new), ("DELETE", a_old)]
    assert set(dav_server.resources) == {a_new, b_href}
    assert "DTSTART;TZID=Europe/Copenhagen:20260920T160000\r\n" in dav_server.resources[a_new]
    assert "LOCATION:Pitch 2\r\n" in dav_server.resources[b_href]
    updated = stored(hass_storage)[event_key(a)]
    assert updated == {
        "href": a_new,
        "hash": updated["hash"],
        "start": "2026-09-20T14:00:00+00:00",
        "end": "2026-09-20T16:00:00+00:00",
        "target": FAMILY_URL,
        "token": record["token"],
    }
    assert relay.last_error is None

    dav_server.calls.clear()
    await relay.async_sync()
    assert dav_server.calls == []


async def test_old_entry_is_not_deleted_once_its_event_is_over(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    dav_server: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Pending removals are only retried while the event is relayed: an event that is over is forgotten, like the
    entries of every past event."""
    event = call_up()
    source_calendar.events = [event]
    await setup_entry(hass, config_entry)
    [old_href] = dav_server.puts()
    dav_server.open_on_device(old_href)
    dav_server.delete_failing[old_href] = 400
    source_calendar.events = [call_up(location="Pitch 2")]
    relay = relay_of(config_entry)
    await relay.async_sync()
    new_href = stored(hass_storage)[event_key(event)]["href"]
    assert stored(hass_storage)[event_key(event)]["pending"] == [old_href]

    freezer.move_to(KICKOFF + timedelta(hours=3))
    dav_server.calls.clear()
    await relay.async_sync()
    assert dav_server.calls == []
    assert stored(hass_storage) == {}
    assert set(dav_server.resources) == {old_href, new_href}


@pytest.mark.parametrize(
    ("status", "written", "error", "pass_goes_on"),
    [
        (412, False, "1 event change(s) failed, first error: HTTP 412", True),
        (400, False, "1 event change(s) failed, first error: HTTP 400", True),
        (503, False, "PUT returned HTTP 503", False),
        (503, True, "PUT returned HTTP 503", False),
        (403, False, "Authentication failed (HTTP 403)", False),
        (404, False, "Target calendar not found (HTTP 404)", False),
    ],
)
async def test_event_whose_fresh_identity_fails_is_written_on_the_next_pass(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    dav_server: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
    issue_registry: ir.IssueRegistry,
    caplog: pytest.LogCaptureFixture,
    status: int,
    written: bool,
    error: str,
    pass_goes_on: bool,
) -> None:
    """When the fresh identity cannot be written either, nothing that was there goes missing: the old entry stays, and
    the failure is handled like any failed write (a refused event fails alone with an excerpt in the warning, a server
    error, a bare 403 and a 404 end the pass). After a refusal the record keeps its href with the hash cleared, and the
    next pass writes the event under another fresh identity, also after the match moved back to the time the entry
    had. A fresh PUT without a usable answer may have been carried out, so the record moves to that name with the old
    entry to be deleted, and the next pass writes that same name again."""
    a, b = call_up(uid="a"), call_up(KICKOFF + timedelta(days=1), uid="b")
    source_calendar.events = [a, b]
    await setup_entry(hass, config_entry)
    a_old, b_href = dav_server.puts()
    a_record = dict(stored(hass_storage)[event_key(a)])
    old_body = dav_server.resources[a_old]
    moved_b = call_up(KICKOFF + timedelta(days=1), uid="b", location="Pitch 2")

    dav_server.open_on_device(a_old)
    dav_server.create_failure = (status, written)
    dav_server.refusal_body = "<html>\n  <body>Refused\tagain</body>\n</html>"
    source_calendar.events = [call_up(KICKOFF + timedelta(hours=3), uid="a"), moved_b]
    dav_server.calls.clear()
    relay = relay_of(config_entry)
    await relay.async_sync()

    first_fresh = dav_server.calls[1][1]
    assert first_fresh not in (a_old, b_href)
    assert dav_server.calls == [("PUT", a_old), ("PUT", first_fresh)] + [("PUT", b_href)] * pass_goes_on
    assert dav_server.resources[a_old] == old_body
    assert (first_fresh in dav_server.resources) is written
    if status == 503:
        expected = {
            **a_record,
            "href": first_fresh,
            "hash": "",
            "start": "2026-09-20T13:00:00+00:00",
            "end": "2026-09-20T15:00:00+00:00",
            "token": first_fresh.removesuffix(".ics").rsplit("-", 1)[1],
            "pending": [a_old],
        }
    else:
        expected = {**a_record, "hash": ""}
    assert stored(hass_storage)[event_key(a)] == expected
    assert relay.last_error == error
    if pass_goes_on:
        assert (
            f"writing an event failed, retrying on the next pass: HTTP {status}, "
            "response body: <html> <body>Refused again</body> </html>"
        ) in caplog.text
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == (["reauth"] if status == 403 else [])
    assert (issue_registry.async_get_issue(DOMAIN, f"target_missing_{RELAY_ID}") is not None) is (status == 404)

    dav_server.create_failure = None
    source_calendar.events = [a, moved_b]
    dav_server.calls.clear()
    await relay.async_sync()

    record = stored(hass_storage)[event_key(a)]
    a_new = record["href"]
    assert a_new != a_old
    assert (a_new == first_fresh) is (status == 503)
    assert dav_server.calls == (
        [("PUT", a_old)] * (status != 503)
        + [("PUT", a_new)]
        + [("PUT", b_href)] * (not pass_goes_on)
        + [("DELETE", a_old)]
    )
    assert set(dav_server.resources) == {a_new, b_href}
    assert "DTSTART;TZID=Europe/Copenhagen:20260920T120000\r\n" in dav_server.resources[a_new]
    assert "LOCATION:Pitch 2\r\n" in dav_server.resources[b_href]
    assert record == {**a_record, "href": a_new, "token": record["token"]}
    assert relay.last_error is None
    assert issue_registry.async_get_issue(DOMAIN, f"target_missing_{RELAY_ID}") is None

    dav_server.calls.clear()
    await relay.async_sync()
    assert dav_server.calls == []


async def test_new_event_whose_fresh_entry_got_no_answer_is_not_left_twice(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    dav_server: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """A reinstated call-up whose fresh entry was written although its PUT got a server error keeps a record on that
    fresh name, so the next pass writes the same name again and no second copy is left."""
    event = call_up()
    source_calendar.events = [event]
    await setup_entry(hass, config_entry)
    [base_href] = dav_server.puts()
    relay = relay_of(config_entry)
    source_calendar.events = [timed("Home - Away", KICKOFF)]
    await relay.async_sync()

    dav_server.create_failure = (503, True)
    source_calendar.events = [event]
    dav_server.calls.clear()
    await relay.async_sync()

    first_fresh = dav_server.calls[1][1]
    assert dav_server.calls == [("PUT", base_href), ("PUT", first_fresh)]
    assert list(dav_server.resources) == [first_fresh]
    assert stored(hass_storage)[event_key(event)] == {
        "href": first_fresh,
        "hash": "",
        "start": "2026-09-20T10:00:00+00:00",
        "end": "2026-09-20T12:00:00+00:00",
        "target": FAMILY_URL,
        "token": first_fresh.removesuffix(".ics").rsplit("-", 1)[1],
    }
    assert relay.relayed_events == 1
    assert relay.last_error == "PUT returned HTTP 503"

    dav_server.create_failure = None
    dav_server.calls.clear()
    await relay.async_sync()

    record = stored(hass_storage)[event_key(event)]
    assert dav_server.calls == [("PUT", first_fresh)]
    assert list(dav_server.resources) == [first_fresh]
    assert (record["href"], record["hash"] != "", "pending" in record) == (first_fresh, True, False)
    assert relay.last_error is None

    dav_server.calls.clear()
    await relay.async_sync()
    assert dav_server.calls == []


async def test_fresh_identity_without_an_answer_is_written_again_under_the_same_name(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    dav_server: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """While the server gives no usable answer to creates, the record stays on the fresh name its PUT may have written,
    so every later pass writes that same name again instead of drawing another token, and the entries still to be
    deleted do not grow. Once the server answers, that name is written and the old entry deleted."""
    event = call_up()
    key = event_key(event)
    source_calendar.events = [event]
    await setup_entry(hass, config_entry)
    [base_href] = dav_server.puts()
    relay = relay_of(config_entry)
    dav_server.open_on_device(base_href)
    dav_server.create_failure = (503, False)
    source_calendar.events = [call_up(KICKOFF + timedelta(hours=1))]
    dav_server.calls.clear()
    await relay.async_sync()

    record = stored(hass_storage)[key]
    fresh_href = record["href"]
    assert fresh_href == f"{FAMILY_URL}relay-{resource_id(RELAY_ID, key)}-{record['token']}.ics"
    assert dav_server.calls == [("PUT", base_href), ("PUT", fresh_href)]
    assert (record["hash"], record["pending"]) == ("", [base_href])
    for _ in range(3):
        dav_server.calls.clear()
        await relay.async_sync()
        assert dav_server.calls == [("PUT", fresh_href)]
        assert stored(hass_storage)[key] == record
        assert relay.last_error == "PUT returned HTTP 503"

    dav_server.create_failure = None
    dav_server.calls.clear()
    await relay.async_sync()
    assert dav_server.calls == [("PUT", fresh_href), ("DELETE", base_href)]
    assert list(dav_server.resources) == [fresh_href]
    written = stored(hass_storage)[key]
    assert (written["href"], written["token"], written["hash"] != "", "pending" in written) == (
        fresh_href,
        record["token"],
        True,
        False,
    )
    assert relay.last_error is None


async def test_pass_cancelled_during_a_fresh_put_leaves_no_second_entry(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    dav_server: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """The record moves to the fresh name before its PUT is sent, so a pass cancelled while the PUT is under way (Home
    Assistant stopping or the entry reloading) still saves it. The next pass writes that same name and deletes the old
    entry, and a withdrawal then deletes the one entry left."""
    event = call_up()
    key = event_key(event)
    source_calendar.events = [event]
    await setup_entry(hass, config_entry)
    [base_href] = dav_server.puts()
    relay = relay_of(config_entry)
    dav_server.open_on_device(base_href)
    dav_server.cancel_fresh_puts = 1
    source_calendar.events = [call_up(KICKOFF + timedelta(hours=1))]
    dav_server.calls.clear()
    with pytest.raises(asyncio.CancelledError):
        await relay.async_sync()

    record = stored(hass_storage)[key]
    fresh_href = record["href"]
    assert dav_server.calls == [("PUT", base_href), ("PUT", fresh_href)]
    assert (record["hash"], record["pending"]) == ("", [base_href])
    assert set(dav_server.resources) == {base_href, fresh_href}

    dav_server.calls.clear()
    await relay.async_sync()
    assert dav_server.calls == [("PUT", fresh_href), ("DELETE", base_href)]
    assert list(dav_server.resources) == [fresh_href]
    assert relay.last_error is None

    source_calendar.events = [timed("Home - Away", KICKOFF + timedelta(hours=1))]
    dav_server.calls.clear()
    await relay.async_sync()
    assert dav_server.calls == [("DELETE", fresh_href)]
    assert dav_server.resources == {}
    assert stored(hass_storage) == {}


async def test_old_entry_whose_delete_gets_a_server_error_holds_back_no_other_event(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    dav_server: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """Old entries are deleted after every write and removal of a pass, so a server error that keeps coming for one of
    them ends each pass only after new call-ups are written and withdrawn ones are deleted."""
    a, b = call_up(uid="a"), call_up(KICKOFF + timedelta(days=1), uid="b")
    c = call_up(KICKOFF + timedelta(days=2), uid="c")
    source_calendar.events = [a, b]
    await setup_entry(hass, config_entry)
    a_old, b_href = dav_server.puts()
    relay = relay_of(config_entry)
    dav_server.open_on_device(a_old)
    dav_server.delete_failing[a_old] = 503
    source_calendar.events = [call_up(KICKOFF + timedelta(hours=3), uid="a"), c]
    dav_server.calls.clear()
    await relay.async_sync()

    a_new = stored(hass_storage)[event_key(a)]["href"]
    c_href = f"{FAMILY_URL}relay-{resource_id(RELAY_ID, event_key(c))}.ics"
    assert dav_server.calls == [("PUT", a_old), ("PUT", a_new), ("PUT", c_href), ("DELETE", b_href), ("DELETE", a_old)]
    assert set(dav_server.resources) == {a_old, a_new, c_href}
    assert relay.last_error == "DELETE returned HTTP 503"

    dav_server.calls.clear()
    await relay.async_sync()
    assert dav_server.calls == [("DELETE", a_old)]
    assert relay.last_error == "DELETE returned HTTP 503"

    dav_server.delete_failing.clear()
    dav_server.calls.clear()
    await relay.async_sync()
    assert dav_server.calls == [("DELETE", a_old)]
    assert set(dav_server.resources) == {a_new, c_href}
    assert "pending" not in stored(hass_storage)[event_key(a)]
    assert relay.last_error is None


async def test_creates_and_updates_without_a_412_keep_their_identity(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    dav_server: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """Without a 412 a change is one PUT to the same name and UID, and never deletes or adds a token. On a server that
    lets a deleted name be used again, an entry deleted by hand stays deleted until its source changes, and is then
    created under its own name."""
    source_calendar.events = [call_up(uid="a"), call_up(KICKOFF + timedelta(days=1), uid="b")]
    await setup_entry(hass, config_entry)
    a_href, b_href = dav_server.puts()
    del dav_server.resources[b_href]
    dav_server.calls.clear()
    relay = relay_of(config_entry)

    source_calendar.events = [
        call_up(KICKOFF + timedelta(hours=3), uid="a"),
        call_up(KICKOFF + timedelta(days=1), uid="b"),
    ]
    await relay.async_sync()
    assert dav_server.calls == [("PUT", a_href)]
    assert "DTSTART;TZID=Europe/Copenhagen:20260920T150000\r\n" in dav_server.resources[a_href]
    assert b_href not in dav_server.resources

    source_calendar.events[1] = call_up(KICKOFF + timedelta(days=1), uid="b", location="Pitch 2")
    dav_server.calls.clear()
    await relay.async_sync()
    assert dav_server.calls == [("PUT", b_href)]
    assert "LOCATION:Pitch 2\r\n" in dav_server.resources[b_href]
    for key, href in ((event_key(call_up(uid="a")), a_href), (event_key(call_up(uid="b")), b_href)):
        assert stored(hass_storage)[key]["href"] == href
        assert set(stored(hass_storage)[key]) == {"href", "hash", "start", "end", "target"}
        assert f"UID:{resource_id(RELAY_ID, key)}@calendar-relay\r\n" in dav_server.resources[href]
    assert relay.last_error is None


@pytest.mark.parametrize("pending_status", [204, 400])
async def test_withdrawn_event_deletes_its_entry_and_its_pending_removals(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    dav_server: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
    caplog: pytest.LogCaptureFixture,
    pending_status: int,
) -> None:
    """A withdrawn event whose record has a token and pending removals has all of them deleted: the pending ones, then
    its entry by the stored href. A stored href that is not one of the event's resources, or not inside its calendar
    or the target calendar, is forgotten without a request. If one DELETE fails, the others are still sent and the
    record waits for the next pass with what is left, with its hash cleared because its entry is gone."""
    event = call_up()
    key = event_key(event)
    rid = resource_id(RELAY_ID, key)
    base_href = f"{FAMILY_URL}relay-{rid}.ics"
    other_href = f"{FAMILY_URL}relay-{rid}-99999999.ics"
    token_href = f"{FAMILY_URL}relay-{rid}-0a1b2c3d.ics"
    not_ours = f"{FAMILY_URL}relay-{resource_id(RELAY_ID, 'another event')}.ics"
    elsewhere = f"{WORK_URL}relay-{rid}-12345678.ics"
    record = {
        "href": token_href,
        "hash": "0" * 64,
        "start": "2026-09-20T10:00:00+00:00",
        "end": "2026-09-20T12:00:00+00:00",
        "target": FAMILY_URL,
        "token": "0a1b2c3d",
        "pending": [base_href, not_ours, elsewhere, other_href],
    }
    hass_storage[STORE_KEY] = {"version": 1, "minor_version": 1, "key": STORE_KEY, "data": {"events": {key: record}}}
    dav_server.resources = {
        href: f"BEGIN:VEVENT\r\nUID:{uid}\r\nEND:VEVENT\r\n"
        for href, uid in (
            (base_href, f"{rid}@calendar-relay"),
            (token_href, f"{rid}-0a1b2c3d@calendar-relay"),
            (not_ours, "another-event@example.com"),
        )
    }
    dav_server.delete_failing = {base_href: pending_status} if pending_status != 204 else {}
    source_calendar.events = [timed("Home - Away", KICKOFF)]
    await setup_entry(hass, config_entry)

    assert dav_server.calls == [("DELETE", base_href), ("DELETE", other_href), ("DELETE", token_href)]
    assert "a stored earlier entry is not one of its event's, forgetting it" in caplog.text
    relay = relay_of(config_entry)
    if pending_status == 204:
        assert stored(hass_storage) == {}
        assert list(dav_server.resources) == [not_ours]
        assert relay.last_error is None
        return
    assert stored(hass_storage) == {key: {**record, "hash": "", "pending": [base_href]}}
    assert set(dav_server.resources) == {base_href, not_ours}
    assert relay.last_error == "1 event change(s) failed, first error: HTTP 400"

    dav_server.delete_failing.clear()
    dav_server.calls.clear()
    await relay.async_sync()
    assert dav_server.calls == [("DELETE", base_href), ("DELETE", token_href)]
    assert stored(hass_storage) == {}
    assert list(dav_server.resources) == [not_ours]


async def test_withdrawn_event_waits_while_its_pending_removal_cannot_be_reached(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """A connection error while deleting a pending removal ends the pass before the entry is deleted."""
    event = call_up()
    key = event_key(event)
    rid = resource_id(RELAY_ID, key)
    record = {
        "href": f"{FAMILY_URL}relay-{rid}-0a1b2c3d.ics",
        "hash": "0" * 64,
        "start": "2026-09-20T10:00:00+00:00",
        "end": "2026-09-20T12:00:00+00:00",
        "target": FAMILY_URL,
        "token": "0a1b2c3d",
        "pending": [f"{FAMILY_URL}relay-{rid}.ics"],
    }
    hass_storage[STORE_KEY] = {"version": 1, "minor_version": 1, "key": STORE_KEY, "data": {"events": {key: record}}}
    fake_dav.delete_error = CalDavConnectionError("DELETE failed: ClientConnectorError")
    source_calendar.events = [timed("Home - Away", KICKOFF)]
    await setup_entry(hass, config_entry)

    assert fake_dav.calls == [("DELETE", f"{FAMILY_URL}relay-{rid}.ics")]
    assert stored(hass_storage) == {key: record}
    assert relay_of(config_entry).relayed_events == 1
    assert relay_of(config_entry).last_error == "DELETE failed: ClientConnectorError"


@pytest.mark.parametrize("old_delete_works", [True, False])
async def test_reinstated_call_up_is_written_again_after_a_withdrawal_that_left_an_old_entry(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    dav_server: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
    old_delete_works: bool,
) -> None:
    """A withdrawal that deletes the entry but not an old entry still to be deleted keeps the record for a retry with
    its hash cleared, since its entry is gone. Reinstated unchanged, the call-up is written again (under a fresh
    identity, as iCloud blocks the deleted name) and the old entries are deleted once it is, so the calendar never ends
    without the event; an old entry that still cannot be deleted shows next to it until it can."""
    event = call_up()
    key = event_key(event)
    moved = call_up(KICKOFF + timedelta(hours=1))
    source_calendar.events = [event]
    await setup_entry(hass, config_entry)
    [base_href] = dav_server.puts()
    relay = relay_of(config_entry)
    dav_server.open_on_device(base_href)
    dav_server.delete_failing[base_href] = 400
    source_calendar.events = [moved]
    await relay.async_sync()
    first = stored(hass_storage)[key]
    assert first["pending"] == [base_href]

    source_calendar.events = [timed("Home - Away", KICKOFF + timedelta(hours=1))]
    dav_server.calls.clear()
    await relay.async_sync()
    assert dav_server.calls == [("DELETE", base_href), ("DELETE", first["href"])]
    assert stored(hass_storage)[key] == {**first, "hash": ""}
    assert set(dav_server.resources) == {base_href}
    assert relay.last_error == "1 event change(s) failed, first error: HTTP 400"

    if old_delete_works:
        dav_server.delete_failing.clear()
    source_calendar.events = [moved]
    dav_server.calls.clear()
    await relay.async_sync()

    record = stored(hass_storage)[key]
    new_href = record["href"]
    assert new_href not in (base_href, first["href"])
    assert dav_server.calls == [
        ("PUT", first["href"]),
        ("PUT", new_href),
        ("DELETE", base_href),
        ("DELETE", first["href"]),
    ]
    assert "DTSTART;TZID=Europe/Copenhagen:20260920T130000\r\n" in dav_server.resources[new_href]
    assert record["hash"] == first["hash"]
    if old_delete_works:
        assert list(dav_server.resources) == [new_href]
        assert "pending" not in record
        assert relay.last_error is None
    else:
        assert set(dav_server.resources) == {base_href, new_href}
        assert record["pending"] == [base_href]
        assert relay.last_error == "1 event change(s) failed, first error: HTTP 400"


async def test_reinstated_call_up_is_written_again_after_a_withdrawal_without_an_answer(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    dav_server: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """A withdrawal DELETE that gets no usable answer may have been carried out, so the record kept for a retry has its
    hash cleared, and the call-up reinstated unchanged is written again, here under a fresh identity because the
    server did delete the entry."""
    event = call_up()
    key = event_key(event)
    source_calendar.events = [event]
    await setup_entry(hass, config_entry)
    [base_href] = dav_server.puts()
    record = dict(stored(hass_storage)[key])
    relay = relay_of(config_entry)

    # The server carries out the DELETE, and then answers it with a server error.
    dav_server.delete_on_device(base_href)
    dav_server.delete_failing[base_href] = 503
    source_calendar.events = [timed("Home - Away", KICKOFF)]
    await relay.async_sync()
    assert stored(hass_storage)[key] == {**record, "hash": ""}
    assert relay.last_error == "DELETE returned HTTP 503"

    dav_server.delete_failing.clear()
    source_calendar.events = [event]
    dav_server.calls.clear()
    await relay.async_sync()
    written = stored(hass_storage)[key]
    assert dav_server.calls == [("PUT", base_href), ("PUT", written["href"]), ("DELETE", base_href)]
    assert list(dav_server.resources) == [written["href"]]
    assert written == {**record, "href": written["href"], "token": written["token"]}
    assert relay.last_error is None


async def test_records_stored_by_0_2_2_load_and_keep_their_names(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    dav_server: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """Records without a token or pending removals load as they are: an unchanged event is left alone, a changed one
    is written in place under its name and UID, and one whose hash 0.2.2 cleared is written again. None gets a token,
    and the records keep their 0.2.2 shape."""
    events = [
        call_up(uid="a"),
        call_up(KICKOFF + timedelta(days=1), uid="b", location="Pitch 2"),
        call_up(KICKOFF + timedelta(days=2), uid="c"),
    ]
    records = {}
    for index, event in enumerate(events):
        key = event_key(event)
        rid = resource_id(RELAY_ID, key)
        body = render_event(
            uid=f"{rid}@calendar-relay",
            summary=f"{PREFIX}Home - Away",
            start=event.start,
            end=event.end,
            time_zone="Europe/Copenhagen",
        )
        href = f"{FAMILY_URL}relay-{rid}.ics"
        dav_server.resources[href] = body
        records[key] = {
            "href": href,
            "hash": [content_hash(body), content_hash(body), ""][index],
            "start": event.start.isoformat(),
            "end": event.end.isoformat(),
            "target": FAMILY_URL,
        }
    hass_storage[STORE_KEY] = {"version": 1, "minor_version": 1, "key": STORE_KEY, "data": {"events": records}}
    source_calendar.events = events
    await setup_entry(hass, config_entry)

    a_href, b_href, c_href = (record["href"] for record in records.values())
    assert dav_server.calls == [("PUT", b_href), ("PUT", c_href)]
    assert set(dav_server.resources) == {a_href, b_href, c_href}
    assert "LOCATION:Pitch 2\r\n" in dav_server.resources[b_href]
    after = stored(hass_storage)
    assert after[event_key(events[0])] == records[event_key(events[0])]
    for event in events:
        key = event_key(event)
        assert set(after[key]) == {"href", "hash", "start", "end", "target"}
        assert after[key]["href"] == records[key]["href"]
        assert f"UID:{resource_id(RELAY_ID, key)}@calendar-relay\r\n" in dav_server.resources[records[key]["href"]]
    assert after[event_key(events[1])]["hash"] not in ("", records[event_key(events[1])]["hash"])
    assert after[event_key(events[2])]["hash"] != ""
    assert relay_of(config_entry).last_error is None

    dav_server.calls.clear()
    await relay_of(config_entry).async_sync()
    assert dav_server.calls == []


async def test_text_with_lone_surrogates_is_relayed(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """A truncated emoji escape in one source event does not stop the relay; it becomes a replacement character."""
    source_calendar.events = [
        call_up(uid="good"),
        timed(f"{CALL_UP}Broken \ud83d", KICKOFF + timedelta(days=1), uid="bad\udc00", description="x\ud83d"),
        timed(f"{CALL_UP}No uid \ud83d", KICKOFF + timedelta(days=2), uid=None),
    ]
    await setup_entry(hass, config_entry)

    assert len(fake_dav.resources) == 3
    assert any("SUMMARY:⚽ Emma: Broken �\r\n" in body for body in fake_dav.resources.values())
    assert len(stored(hass_storage)) == 3
    assert relay_of(config_entry).last_error is None


async def test_event_that_cannot_be_planned_blocks_removals_only(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """Other events are still written, but nothing is deleted while one source event could not be planned."""
    source_calendar.events = [call_up(uid="a"), call_up(KICKOFF + timedelta(days=1), uid="b")]
    await setup_entry(hass, config_entry)
    [a_href, _b_href] = fake_dav.puts()
    fake_dav.calls.clear()

    source_calendar.events = [
        timed("Home - Away", KICKOFF, uid="a"),
        call_up(KICKOFF + timedelta(days=1), uid="b", location="Pitch 2"),
        timed(f"{CALL_UP}Cup", KICKOFF + timedelta(days=2), uid="cup"),
    ]

    def failing_transform(title: str, **kwargs: object) -> str:
        if "Cup" in title:
            raise ValueError("cannot relay this one")
        return transform_title(title, **kwargs)  # type: ignore[arg-type]

    with patch("custom_components.calendar_relay.relay.transform_title", side_effect=failing_transform):
        await relay_of(config_entry).async_sync()

    assert fake_dav.deletes() == []
    assert len(fake_dav.puts()) == 1
    assert a_href in fake_dav.resources
    assert len(stored(hass_storage)) == 2
    assert relay_of(config_entry).last_error == (
        "1 event change(s) failed, first error: A source event cannot be relayed (ValueError)"
    )


@pytest.mark.parametrize("status", [403, 409])
async def test_event_refused_with_a_reason_does_not_end_the_pass(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
    status: int,
) -> None:
    """A precondition refusal fails only that event: later writes and deletes still run, without reauth or repair."""
    source_calendar.events = [call_up(uid="a"), call_up(KICKOFF + timedelta(days=1), uid="b")]
    await setup_entry(hass, config_entry)
    [a_href, _b_href] = fake_dav.puts()

    source_calendar.events = [
        call_up(KICKOFF + timedelta(days=1), uid="b", location="Pitch 9"),
        timed(f"{CALL_UP}Cup", KICKOFF + timedelta(days=2), uid="cup"),
        timed("Home - Away", KICKOFF, uid="a"),
    ]
    fake_dav.put_error = lambda href, ics: CalDavRefusedError(status, "no-uid-conflict") if "Pitch 9" in ics else None
    fake_dav.calls.clear()
    for _ in range(2):
        await relay_of(config_entry).async_sync()

    assert fake_dav.deletes() == [a_href]
    assert a_href not in fake_dav.resources
    assert any("Cup" in body for body in fake_dav.resources.values())
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    assert issue_registry.async_get_issue(DOMAIN, f"target_missing_{RELAY_ID}") is None
    assert relay_of(config_entry).last_error == (
        f"1 event change(s) failed, first error: HTTP {status} (no-uid-conflict)"
    )


async def test_forbidden_target_that_is_not_on_the_account_is_a_repair_issue(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    issue_registry: ir.IssueRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After the account moved to another user, a 403 from the old user's calendar raises a repair, not reauth."""
    source_calendar.events = [call_up()]
    fake_dav.put_error = CalDavAuthError(403, excerpt="Forbidden")
    entry = make_entry(relay_subentry(target_calendar=OLD_ACCOUNT_URL))
    await setup_entry(hass, entry)

    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    assert issue_registry.async_get_issue(DOMAIN, f"target_missing_{RELAY_ID}") is not None
    assert relay_of(entry).last_error == "Target calendar not found (HTTP 403)"
    assert "the target calendar does not exist (HTTP 403, response body: Forbidden)" in caplog.text


async def test_forbidden_fresh_identity_in_a_target_that_is_not_on_the_account_keeps_the_record(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    issue_registry: ir.IssueRegistry,
    hass_storage: dict,
) -> None:
    """A 403 on the fresh identity from a calendar the account does not list is a missing calendar, and the record keeps
    its href with the hash cleared, so the event is written once the calendar can be reached."""
    source_calendar.events = [call_up()]
    entry = make_entry(relay_subentry(target_calendar=OLD_ACCOUNT_URL))
    await setup_entry(hass, entry)
    [href] = fake_dav.puts()
    record = dict(stored(hass_storage)[event_key(call_up())])

    fake_dav.put_error = lambda put_href, ics: CalDavStatusError(412) if put_href == href else CalDavAuthError(403)
    source_calendar.events = [call_up(KICKOFF + timedelta(hours=3))]
    fake_dav.calls.clear()
    await relay_of(entry).async_sync()

    assert len(fake_dav.puts()) == 2
    assert fake_dav.puts()[0] == href
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    assert issue_registry.async_get_issue(DOMAIN, f"target_missing_{RELAY_ID}") is not None
    assert stored(hass_storage)[event_key(call_up())] == {**record, "hash": ""}
    assert relay_of(entry).last_error == "Target calendar not found (HTTP 403)"


async def test_empty_read_waits_for_confirmation_before_deleting(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A source that briefly returns no events at all does not wipe the calendar or undo deletions made by hand."""
    call_ups = [call_up(uid="a"), call_up(KICKOFF + timedelta(days=1), uid="b")]
    source_calendar.events = list(call_ups)
    await setup_entry(hass, config_entry)
    hrefs = fake_dav.puts()
    del fake_dav.resources[hrefs[0]]
    fake_dav.calls.clear()
    relay = relay_of(config_entry)

    source_calendar.events = []
    await relay.async_sync()
    freezer.tick(timedelta(minutes=9))
    await relay.async_sync()
    assert fake_dav.calls == []
    assert len(stored(hass_storage)) == 2
    assert relay.last_error is None

    source_calendar.events = list(call_ups)
    await relay.async_sync()
    assert fake_dav.calls == []
    assert hrefs[0] not in fake_dav.resources

    source_calendar.events = []
    await relay.async_sync()
    assert fake_dav.calls == []
    freezer.tick(timedelta(minutes=10))
    await relay.async_sync()
    assert fake_dav.deletes() == hrefs
    assert fake_dav.resources == {}
    assert stored(hass_storage) == {}


@pytest.mark.parametrize(
    ("old_target", "delete_error"),
    [
        (FAMILY_URL, CalDavUntrustedHostError("cloud.old-example.org")),
        ("https://p99-caldav.example.com/987654321/calendars/family/", CalDavAuthError(403)),
    ],
)
async def test_target_change_writes_the_new_calendar_when_old_copies_cannot_be_deleted(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    hass_storage: dict,
    old_target: str,
    delete_error: Exception,
) -> None:
    """Old copies on a host no longer trusted, or in a calendar of another account, are left behind."""
    source_calendar.events = [
        call_up(uid="a"),
        call_up(KICKOFF + timedelta(days=1), uid="b"),
        call_up(KICKOFF + timedelta(days=2), uid="c"),
    ]
    entry = make_entry(relay_subentry(target_calendar=old_target))
    await setup_entry(hass, entry)
    old_hrefs = fake_dav.puts()
    fake_dav.delete_error = delete_error
    fake_dav.calls.clear()

    source_calendar.events = source_calendar.events[:2]
    hass.config_entries.async_update_subentry(
        entry, entry.subentries[RELAY_ID], data=relay_data(target_calendar=WORK_URL, target_calendar_name="Work")
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    new_hrefs = [href.replace(old_target, WORK_URL) for href in old_hrefs[:2]]
    assert fake_dav.puts() == new_hrefs
    assert fake_dav.deletes() == old_hrefs
    assert set(fake_dav.resources) == {*old_hrefs, *new_hrefs}
    assert {record["target"] for record in stored(hass_storage).values()} == {WORK_URL}
    assert len(stored(hass_storage)) == 2
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    assert relay_of(entry).last_error is None


async def test_new_calendar_refusing_events_keeps_them_in_the_old_calendar(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """A refused move puts the copy back and is not tried again until the event changes, so nothing churns."""
    source_calendar.events = [call_up(uid="a"), call_up(KICKOFF + timedelta(days=1), uid="b")]
    await setup_entry(hass, config_entry)
    old_hrefs = fake_dav.puts()
    new_hrefs = [href.replace(FAMILY_URL, WORK_URL) for href in old_hrefs]
    fake_dav.put_error = lambda href, ics: (
        CalDavStatusError(415, excerpt="Unsupported") if href.startswith(WORK_URL) else None
    )
    fake_dav.calls.clear()

    hass.config_entries.async_update_subentry(
        config_entry,
        config_entry.subentries[RELAY_ID],
        data=relay_data(target_calendar=WORK_URL, target_calendar_name="Work"),
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert fake_dav.calls == [
        ("DELETE", old_hrefs[0]),
        ("PUT", new_hrefs[0]),
        ("PUT", old_hrefs[0]),
        ("DELETE", old_hrefs[1]),
        ("PUT", new_hrefs[1]),
        ("PUT", old_hrefs[1]),
    ]
    assert sorted(fake_dav.resources) == sorted(old_hrefs)
    assert {record["target"] for record in stored(hass_storage).values()} == {FAMILY_URL}
    error = "Moving an event to the new calendar failed: HTTP 415"
    assert relay_of(config_entry).last_error == f"2 event change(s) failed, first error: {error}"

    fake_dav.calls.clear()
    await relay_of(config_entry).async_sync()
    assert fake_dav.calls == []
    assert relay_of(config_entry).last_error == f"2 event change(s) failed, first error: {error}"

    source_calendar.events = [call_up(uid="a", location="Pitch 2"), call_up(KICKOFF + timedelta(days=1), uid="b")]
    fake_dav.put_error = None
    await relay_of(config_entry).async_sync()
    assert fake_dav.calls == [("DELETE", old_hrefs[0]), ("PUT", new_hrefs[0])]
    assert relay_of(config_entry).last_error == f"1 event change(s) failed, first error: {error}"


@pytest.mark.parametrize(
    ("error", "last_error"),
    [
        (CalDavNotFoundError(404), "Target calendar not found (HTTP 404)"),
        (CalDavConnectionError("PUT returned HTTP 503"), "PUT returned HTTP 503"),
    ],
)
async def test_move_ended_by_the_new_calendar_puts_the_copy_back(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
    error: Exception,
    last_error: str,
) -> None:
    """A missing or unreachable new calendar ends the pass, with the moved copy back in the old calendar."""
    source_calendar.events = [call_up()]
    await setup_entry(hass, config_entry)
    [old_href] = fake_dav.puts()
    fake_dav.put_error = lambda href, ics: error if href.startswith(WORK_URL) else None

    hass.config_entries.async_update_subentry(
        config_entry,
        config_entry.subentries[RELAY_ID],
        data=relay_data(target_calendar=WORK_URL, target_calendar_name="Work"),
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert list(fake_dav.resources) == [old_href]
    assert [record["target"] for record in stored(hass_storage).values()] == [FAMILY_URL]
    assert relay_of(config_entry).last_error == last_error


@pytest.mark.parametrize("scenario", ["same name", "fresh name", "put-back refused"])
async def test_move_whose_new_entry_got_no_answer_leaves_one_copy(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
    scenario: str,
) -> None:
    """A PUT to the new calendar that gets no usable answer may still have written the entry there. The copy put back
    into the old calendar keeps that entry to be deleted once the event is written to the new calendar, never the entry
    just written when the names match; when the put-back is refused, the record follows the entry in the new calendar.
    Either way the new calendar ends with one copy, and a withdrawal leaves none."""
    event = call_up()
    key = event_key(event)
    rid = resource_id(RELAY_ID, key)
    source_calendar.events = [event]
    await setup_entry(hass, config_entry)
    [family_base] = fake_dav.puts()
    work_base = family_base.replace(FAMILY_URL, WORK_URL)

    def put_error(href: str, ics: str) -> Exception | None:
        if href == work_base and scenario != "same name":
            return CalDavStatusError(412)
        if href.startswith(WORK_URL):
            fake_dav.resources[href] = ics
            return CalDavConnectionError("PUT returned HTTP 503")
        if href == family_base and scenario != "same name":
            return CalDavStatusError(415 if scenario == "put-back refused" else 412)
        return None

    fake_dav.put_error = put_error
    fake_dav.calls.clear()
    hass.config_entries.async_update_subentry(
        config_entry,
        config_entry.subentries[RELAY_ID],
        data=relay_data(target_calendar=WORK_URL, target_calendar_name="Work"),
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    relay = relay_of(config_entry)
    record = stored(hass_storage)[key]
    assert relay.last_error == "PUT returned HTTP 503"
    if scenario == "same name":
        assert fake_dav.calls == [("DELETE", family_base), ("PUT", work_base), ("PUT", family_base)]
        assert (record["href"], record["target"], record["pending"]) == (family_base, FAMILY_URL, [work_base])
        final_href, next_calls = work_base, [("DELETE", family_base), ("PUT", work_base)]
    else:
        work_fresh = fake_dav.calls[2][1]
        assert work_fresh.startswith(f"{WORK_URL}relay-{rid}-")
        assert fake_dav.calls[:4] == [
            ("DELETE", family_base),
            ("PUT", work_base),
            ("PUT", work_fresh),
            ("PUT", family_base),
        ]
        if scenario == "fresh name":
            assert fake_dav.calls[4:] == [("PUT", record["href"])]
            assert (record["target"], record["pending"]) == (FAMILY_URL, [work_fresh])
            final_href = record["href"].replace(FAMILY_URL, WORK_URL)
            next_calls = [("DELETE", record["href"]), ("PUT", final_href), ("DELETE", work_fresh)]
        else:
            assert fake_dav.calls[4:] == []
            assert (record["href"], record["target"], record["hash"]) == (work_fresh, WORK_URL, "")
            assert "pending" not in record
            final_href, next_calls = work_fresh, [("PUT", work_fresh)]

    fake_dav.put_error = None
    fake_dav.calls.clear()
    await relay.async_sync()
    assert fake_dav.calls == next_calls
    assert list(fake_dav.resources) == [final_href]
    moved = stored(hass_storage)[key]
    assert (moved["href"], moved["target"], moved["hash"] != "", "pending" in moved) == (
        final_href,
        WORK_URL,
        True,
        False,
    )
    assert relay.last_error is None

    source_calendar.events = [timed("Home - Away", KICKOFF)]
    fake_dav.calls.clear()
    await relay.async_sync()
    assert fake_dav.calls == [("DELETE", final_href)]
    assert fake_dav.resources == {}
    assert stored(hass_storage) == {}


async def test_move_that_cannot_put_the_copy_back_is_written_later(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """If neither calendar takes the event, it is written to the new calendar once that works."""
    source_calendar.events = [call_up()]
    await setup_entry(hass, config_entry)
    [old_href] = fake_dav.puts()
    fake_dav.put_error = CalDavStatusError(400)

    hass.config_entries.async_update_subentry(
        config_entry,
        config_entry.subentries[RELAY_ID],
        data=relay_data(target_calendar=WORK_URL, target_calendar_name="Work"),
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert fake_dav.resources == {}
    assert stored(hass_storage) == {}

    fake_dav.put_error = None
    await relay_of(config_entry).async_sync()
    assert list(fake_dav.resources) == [old_href.replace(FAMILY_URL, WORK_URL)]
    assert relay_of(config_entry).last_error is None


async def test_event_without_uid_moved_after_it_started_is_written_again(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Without a uid a move is a new event, and the copy that already started stays (a documented limit)."""
    source_calendar.events = [call_up(NOW + timedelta(hours=1), uid=None)]
    await setup_entry(hass, config_entry)
    [old_href] = fake_dav.puts()

    freezer.move_to(NOW + timedelta(hours=1, minutes=5))
    source_calendar.events = [call_up(NOW + timedelta(hours=1, minutes=30), uid=None)]
    fake_dav.calls.clear()
    await relay_of(config_entry).async_sync()

    [new_href] = fake_dav.puts()
    assert fake_dav.deletes() == []
    assert sorted(fake_dav.resources) == sorted([old_href, new_href])
    assert [record["href"] for record in stored(hass_storage).values()] == [new_href]


async def test_move_waits_while_the_old_calendar_cannot_be_reached(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """A connection error while deleting the old copy ends the pass and keeps the event where it is."""
    source_calendar.events = [call_up()]
    await setup_entry(hass, config_entry)
    [old_href] = fake_dav.puts()
    fake_dav.delete_error = CalDavConnectionError("DELETE failed: ClientConnectorError")
    fake_dav.calls.clear()

    hass.config_entries.async_update_subentry(
        config_entry,
        config_entry.subentries[RELAY_ID],
        data=relay_data(target_calendar=WORK_URL, target_calendar_name="Work"),
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert fake_dav.calls == [("DELETE", old_href)]
    assert [record["target"] for record in stored(hass_storage).values()] == [FAMILY_URL]
    assert relay_of(config_entry).last_error == "DELETE failed: ClientConnectorError"


async def test_forbidden_delete_in_a_target_that_is_not_on_the_account_is_a_repair_issue(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    hass_storage: dict,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A withdrawal answered with 403 by another account's calendar raises the repair issue and keeps the record."""
    source_calendar.events = [call_up()]
    entry = make_entry(relay_subentry(target_calendar=OLD_ACCOUNT_URL))
    await setup_entry(hass, entry)
    fake_dav.delete_error = CalDavAuthError(403)

    source_calendar.events = [timed("Home - Away", KICKOFF)]
    await relay_of(entry).async_sync()

    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    assert issue_registry.async_get_issue(DOMAIN, f"target_missing_{RELAY_ID}") is not None
    assert len(stored(hass_storage)) == 1
    assert relay_of(entry).last_error == "Target calendar not found (HTTP 403)"


@pytest.mark.parametrize(
    "delete_error",
    [
        None,
        CalDavUntrustedHostError("cloud.old-example.org"),
        CalDavConnectionError("DELETE failed: ClientConnectorError"),
    ],
)
async def test_moved_event_keeps_its_token_and_deletes_its_pending_removals(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    hass_storage: dict,
    delete_error: Exception | None,
) -> None:
    """A move deletes the old calendar's pending removals, then the old copy, and writes the new calendar under the same
    token. Entries that cannot be deleted for a reason that will not go away stay behind; an unreachable old calendar
    ends the pass before anything else is deleted or written."""
    event = call_up()
    key = event_key(event)
    rid = resource_id(RELAY_ID, key)
    old_href = f"{FAMILY_URL}relay-{rid}-0a1b2c3d.ics"
    pending_href = f"{FAMILY_URL}relay-{rid}.ics"
    record = {
        "href": old_href,
        "hash": "0" * 64,
        "start": "2026-09-20T10:00:00+00:00",
        "end": "2026-09-20T12:00:00+00:00",
        "target": FAMILY_URL,
        "token": "0a1b2c3d",
        "pending": [pending_href],
    }
    hass_storage[STORE_KEY] = {"version": 1, "minor_version": 1, "key": STORE_KEY, "data": {"events": {key: record}}}
    fake_dav.resources = {old_href: "BEGIN:VCALENDAR", pending_href: "BEGIN:VCALENDAR"}
    fake_dav.delete_error = delete_error
    source_calendar.events = [event]
    entry = make_entry(relay_subentry(target_calendar=WORK_URL, target_calendar_name="Work"))
    await setup_entry(hass, entry)

    new_href = f"{WORK_URL}relay-{rid}-0a1b2c3d.ics"
    if isinstance(delete_error, CalDavConnectionError):
        assert fake_dav.calls == [("DELETE", pending_href)]
        assert stored(hass_storage) == {key: record}
        assert relay_of(entry).last_error == "DELETE failed: ClientConnectorError"
        return
    assert fake_dav.calls == [("DELETE", pending_href), ("DELETE", old_href), ("PUT", new_href)]
    assert f"UID:{rid}-0a1b2c3d@calendar-relay\r\n" in fake_dav.resources[new_href]
    left_behind = {old_href, pending_href} if delete_error else set()
    assert set(fake_dav.resources) == {new_href, *left_behind}
    moved = stored(hass_storage)[key]
    assert set(moved) == {"href", "hash", "start", "end", "target", "token"}
    assert (moved["href"], moved["target"], moved["token"]) == (new_href, WORK_URL, "0a1b2c3d")
    assert relay_of(entry).last_error is None


@pytest.mark.parametrize("refused_by", ["new calendar", "old calendar"])
async def test_move_takes_a_fresh_identity_after_a_412(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
    refused_by: str,
) -> None:
    """Moves meet iCloud's 412 too: a new calendar that refuses the event's name gets the event under a fresh identity,
    and a copy put back into the old calendar, where its name was just deleted, is put back under a fresh identity."""
    event = call_up()
    source_calendar.events = [event]
    await setup_entry(hass, config_entry)
    [old_href] = fake_dav.puts()
    work_href = old_href.replace(FAMILY_URL, WORK_URL)
    blocked = work_href if refused_by == "new calendar" else old_href

    def put_error(href: str, ics: str) -> Exception | None:
        if href == blocked:
            return CalDavStatusError(412)
        if refused_by == "old calendar" and href.startswith(WORK_URL):
            return CalDavStatusError(415)
        return None

    fake_dav.put_error = put_error
    fake_dav.calls.clear()
    hass.config_entries.async_update_subentry(
        config_entry,
        config_entry.subentries[RELAY_ID],
        data=relay_data(target_calendar=WORK_URL, target_calendar_name="Work"),
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    record = stored(hass_storage)[event_key(event)]
    rid = resource_id(RELAY_ID, event_key(event))
    calendar = WORK_URL if refused_by == "new calendar" else FAMILY_URL
    fresh_href = f"{calendar}relay-{rid}-{record['token']}.ics"
    if refused_by == "new calendar":
        assert fake_dav.calls == [("DELETE", old_href), ("PUT", work_href), ("PUT", fresh_href)]
        assert relay_of(config_entry).last_error is None
    else:
        assert fake_dav.calls == [("DELETE", old_href), ("PUT", work_href), ("PUT", old_href), ("PUT", fresh_href)]
        assert relay_of(config_entry).last_error == (
            "1 event change(s) failed, first error: Moving an event to the new calendar failed: HTTP 415"
        )
    assert list(fake_dav.resources) == [fresh_href]
    assert f"UID:{rid}-{record['token']}@calendar-relay\r\n" in fake_dav.resources[fresh_href]
    assert (record["href"], record["target"], "pending" in record) == (fresh_href, calendar, False)


async def test_put_back_into_the_old_calendar_is_recorded_before_it_is_sent(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """A put-back whose PUT gets no usable answer may have been written, so its name is recorded before it is sent.
    Without that, the entry it may have left in the old calendar would belong to no record."""
    event = call_up()
    source_calendar.events = [event]
    await setup_entry(hass, config_entry)
    [old_href] = fake_dav.puts()
    work_href = old_href.replace(FAMILY_URL, WORK_URL)

    def put_error(href: str, ics: str) -> Exception | None:
        if href == work_href:
            return CalDavStatusError(412)
        if href.startswith(WORK_URL):
            return CalDavStatusError(415)
        # The put-back into the old calendar gets no usable answer, so it may have been written.
        return CalDavConnectionError("PUT failed: ServerTimeoutError")

    fake_dav.put_error = put_error
    fake_dav.calls.clear()
    hass.config_entries.async_update_subentry(
        config_entry,
        config_entry.subentries[RELAY_ID],
        data=relay_data(target_calendar=WORK_URL, target_calendar_name="Work"),
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    record = stored(hass_storage)[event_key(event)]
    assert (record["href"], record["target"], record["hash"]) == (old_href, FAMILY_URL, "")


async def test_an_earlier_entry_is_kept_while_the_event_is_not_written(
    hass: HomeAssistant,
    source_calendar: FakeCalendar,
    fake_dav: FakeDav,
    config_entry: MockConfigEntry,
    hass_storage: dict,
) -> None:
    """An event whose own write failed keeps its earlier entry: deleting it first would leave the calendar without the
    event until a later pass writes it."""
    event = call_up()
    key = event_key(event)
    rid = resource_id(RELAY_ID, key)
    earlier_href = f"{FAMILY_URL}relay-{rid}.ics"
    record = {
        "href": f"{FAMILY_URL}relay-{rid}-0a1b2c3d.ics",
        "hash": "",
        "start": "2026-09-20T10:00:00+00:00",
        "end": "2026-09-20T12:00:00+00:00",
        "target": FAMILY_URL,
        "token": "0a1b2c3d",
        "pending": [earlier_href],
    }
    hass_storage[STORE_KEY] = {"version": 1, "minor_version": 1, "key": STORE_KEY, "data": {"events": {key: record}}}
    fake_dav.resources = {earlier_href: "BEGIN:VCALENDAR"}
    fake_dav.put_error = CalDavStatusError(400)
    source_calendar.events = [event]
    await setup_entry(hass, config_entry)

    assert ("DELETE", earlier_href) not in fake_dav.calls
    assert earlier_href in fake_dav.resources
    assert stored(hass_storage)[key]["pending"] == [earlier_href]
