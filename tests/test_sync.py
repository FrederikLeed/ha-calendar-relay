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
from custom_components.calendar_relay.relay import Relay, event_key, resource_id, transform_title

from .conftest import (
    CALL_UP,
    FAMILY_URL,
    RELAY_ID,
    SOURCE,
    WORK_URL,
    FakeCalendar,
    FakeDav,
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
    assert "DTSTART:20260920T100000Z\r\nDTEND:20260920T120000Z\r\n" in match_body
    assert "DESCRIPTION:Meet at 9:30\\; bring water\r\n" in match_body
    assert "LOCATION:Pitch 2\r\n" in match_body
    cup_body = next(body for body in fake_dav.resources.values() if "Cup day" in body)
    assert "SUMMARY:⚽ Emma: Cup day\r\n" in cup_body
    assert "DTSTART;VALUE=DATE:20260926\r\nDTEND;VALUE=DATE:20260927\r\n" in cup_body

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
    assert "DTSTART:20260920T130000Z\r\n" in fake_dav.resources[href]
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
    assert all("RRULE" not in body for body in fake_dav.resources.values())

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
    fake_dav.put_error = CalDavAuthError(status)
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
    fake_dav.put_error = CalDavNotFoundError(status)
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
    """Other failures log a warning and the next pass tries again."""
    source_calendar.events = [call_up()]
    fake_dav.put_error = CalDavConnectionError("PUT returned HTTP 503")
    await setup_entry(hass, config_entry)

    assert STORE_KEY not in hass_storage
    assert relay_of(config_entry).last_error == "PUT returned HTTP 503"
    assert "sync failed, retrying on the next pass" in caplog.text

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
    """A failed DELETE keeps the record so the next pass tries again."""
    source_calendar.events = [call_up()]
    await setup_entry(hass, config_entry)
    source_calendar.events = [timed("Training", KICKOFF, uid="training")]

    fake_dav.delete_error = CalDavStatusError(400)
    await relay_of(config_entry).async_sync()
    assert len(stored(hass_storage)) == 1
    assert relay_of(config_entry).last_error == "1 event change(s) failed, first error: HTTP 400"

    fake_dav.delete_error = CalDavConnectionError("DELETE failed: ClientConnectorError")
    await relay_of(config_entry).async_sync()
    assert len(stored(hass_storage)) == 1

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
    """Broken state never crashes setup."""
    hass_storage[STORE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": {"events": {"a": "not a record", "b": {"href": FAMILY_URL}}},
    }
    await setup_entry(hass, config_entry)
    assert relay_of(config_entry).relayed_events == 0
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
) -> None:
    """After the account moved to another user, a 403 from the old user's calendar raises a repair, not reauth."""
    source_calendar.events = [call_up()]
    fake_dav.put_error = CalDavAuthError(403)
    entry = make_entry(relay_subentry(target_calendar=OLD_ACCOUNT_URL))
    await setup_entry(hass, entry)

    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    assert issue_registry.async_get_issue(DOMAIN, f"target_missing_{RELAY_ID}") is not None
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
    fake_dav.put_error = lambda href, ics: CalDavStatusError(415) if href.startswith(WORK_URL) else None
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
