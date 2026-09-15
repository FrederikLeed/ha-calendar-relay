"""Shared fixtures and helpers for Calendar Relay tests. All names, accounts and ids are invented."""

from __future__ import annotations

import re
import socket
import threading
import time
from collections.abc import Callable, Generator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import radicale.config
import radicale.server
import voluptuous as vol
from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, setup_test_component_platform
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker, AiohttpClientMockResponse
from yarl import URL

from custom_components.calendar_relay.caldav import DavCalendar, is_event_href
from custom_components.calendar_relay.const import (
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
    DOMAIN,
    SUBENTRY_TYPE_RELAY,
    WAZE_DOMAIN,
    WAZE_SERVICE,
)

ACCOUNT_URL = "https://caldav.example.com/"
USERNAME = "parent@example.com"
PASSWORD = "abcd-efgh-ijkl-mnop"
UNIQUE_ID = f"caldav.example.com_{USERNAME}"
ENTRY_TITLE = f"{USERNAME} (caldav.example.com)"
HOME_URL = "https://p99-caldav.example.com/123456789/calendars/"
FAMILY_URL = f"{HOME_URL}family/"
WORK_URL = f"{HOME_URL}work/"
REMINDERS_URL = f"{HOME_URL}reminders/"
SHARED_URL = f"{HOME_URL}shared/"
SOURCE = "calendar.kids"
RELAY_ID = "01RELAYKIDSFAMILY"
CALL_UP = "⭐ Udtaget: "
PREFIX = "⚽ Emma: "


class FakeCalendar(CalendarEntity):
    """A calendar entity whose events a test sets directly."""

    _attr_name = "Kids"

    def __init__(self) -> None:
        """Start without events."""
        self.events: list[CalendarEvent] = []
        self.error: Exception | None = None
        self.calls = 0

    @property
    def event(self) -> CalendarEvent | None:
        """No current event."""
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Return events starting before end_date. Ended events are left in, the relay must skip them."""
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [event for event in self.events if _as_datetime(event.start) < end_date]


def _as_datetime(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return dt_util.start_of_local_day(value)


class FakeDav:
    """In-memory stand-in for CalDavClient. Calling the instance acts as the class.

    With serve it is a test server instead: it answers the real client's PUT and DELETE
    requests over aioclient_mock, the way iCloud does.
    """

    def __init__(self) -> None:
        """Offer a usable Family and Work calendar plus two that cannot be targets."""
        self.calendars = [
            DavCalendar(FAMILY_URL, "Family", frozenset({"VEVENT"}), True),
            DavCalendar(WORK_URL, "Work", None, None),
            DavCalendar(REMINDERS_URL, "Emma reminders", frozenset({"VTODO"}), True),
            DavCalendar(SHARED_URL, "Shared by a friend", frozenset({"VEVENT"}), False),
        ]
        self.resources: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.client_args: list[tuple[str, str, str]] = []
        self.discover_error: Exception | None = None
        self.put_error: Exception | Callable[[str, str], Exception | None] | None = None
        self.delete_error: Exception | None = None
        # Served over HTTP: hrefs opened on an Apple device, which answer a plain PUT with 412 until they are
        # deleted, hrefs whose other PUTs answer with a status instead of writing, and the body of those answers.
        self.opened: set[str] = set()
        self.failing: dict[str, int] = {}
        self.refusal_body = ""

    def serve(self, aioclient_mock: AiohttpClientMocker, calendar_url: str = FAMILY_URL) -> None:
        """Answer PUT and DELETE of the relay's event resources in calendar_url."""
        resources = re.compile(f"^{re.escape(calendar_url)}relay-[0-9a-f]{{32}}\\.ics$")
        for method in ("PUT", "DELETE"):
            aioclient_mock.request(method, resources, side_effect=self._async_answer)

    async def _async_answer(self, method: str, url: URL, data: bytes | None) -> AiohttpClientMockResponse:
        """Answer one request: 201 or 204 for a PUT, 412 while opened or the failing status, 204 or 404 for a DELETE."""
        href = str(url)
        method = method.upper()
        self.calls.append((method, href))
        if method == "DELETE":
            self.opened.discard(href)
            status = 204 if self.resources.pop(href, None) is not None else 404
            return AiohttpClientMockResponse(method, url, status=status)
        if href in self.opened or href in self.failing:
            status = 412 if href in self.opened else self.failing[href]
            return AiohttpClientMockResponse(method, url, status=status, text=self.refusal_body)
        status = 204 if href in self.resources else 201
        self.resources[href] = (data or b"").decode()
        return AiohttpClientMockResponse(method, url, status=status)

    def __call__(self, session: Any, url: str, username: str, password: str) -> FakeDav:
        """Record the client arguments and return the fake."""
        self.client_args.append((url, username, password))
        return self

    async def async_discover(self) -> list[DavCalendar]:
        """Return the calendars."""
        if self.discover_error is not None:
            raise self.discover_error
        return list(self.calendars)

    async def async_put_event(self, calendar_url: str, name: str, ics: str) -> str:
        """Store the event."""
        href = f"{calendar_url}{name}"
        self.calls.append(("PUT", href))
        error = self.put_error(href, ics) if callable(self.put_error) else self.put_error
        if error is not None:
            raise error
        self.resources[href] = ics
        return href

    async def async_delete_event(self, calendar_url: str, href: str) -> bool:
        """Delete the event. Return False when it was already gone."""
        assert is_event_href(calendar_url, href)
        self.calls.append(("DELETE", href))
        if self.delete_error is not None:
            raise self.delete_error
        return self.resources.pop(href, None) is not None

    def puts(self) -> list[str]:
        """Return the PUT hrefs in order."""
        return [href for method, href in self.calls if method == "PUT"]

    def deletes(self) -> list[str]:
        """Return the DELETE hrefs in order."""
        return [href for method, href in self.calls if method == "DELETE"]


@pytest.fixture(autouse=True)
def _no_waze_pause() -> Generator[None]:
    """Skip the half second between Waze calls, so tests with many calls stay fast."""
    with patch("custom_components.calendar_relay.relay.WAZE_CALL_PAUSE", 0):
        yield


@pytest.fixture
def fake_dav() -> Generator[FakeDav]:
    """Replace the CalDAV client everywhere the integration creates one."""
    fake = FakeDav()
    with patch("custom_components.calendar_relay.CalDavClient", new=fake):
        yield fake


@pytest.fixture
def dav_server(aioclient_mock: AiohttpClientMocker) -> Generator[FakeDav]:
    """Run the real CalDAV client against FakeDav serving the Family calendar; discovery returns its calendars."""
    fake = FakeDav()
    fake.serve(aioclient_mock)
    with patch("custom_components.calendar_relay.caldav.CalDavClient.async_discover", return_value=fake.calendars):
        yield fake


@pytest.fixture
async def source_calendar(hass: HomeAssistant) -> FakeCalendar:
    """Provide calendar.kids."""
    entity = FakeCalendar()
    setup_test_component_platform(hass, "calendar", [entity])
    assert await async_setup_component(hass, "calendar", {"calendar": {"platform": "test"}})
    await hass.async_block_till_done()
    assert hass.states.get(SOURCE) is not None
    return entity


def relay_data(**overrides: Any) -> dict[str, Any]:
    """Return relay subentry data for the KampKlar example."""
    data: dict[str, Any] = {
        CONF_SOURCE: SOURCE,
        CONF_TARGET: FAMILY_URL,
        CONF_TARGET_NAME: "Family",
        CONF_TITLE_FILTER: CALL_UP,
        CONF_REMOVE_FILTER: True,
        CONF_TITLE_PREFIX: PREFIX,
        CONF_LOOK_AHEAD_DAYS: 60,
        CONF_STRUCTURED_LOCATION: True,
        CONF_TRAVEL_TIME: "off",
        CONF_TRAVEL_MINUTES: 15,
        CONF_WAZE_REGION: "eu",
        CONF_BUFFER_MINUTES: 0,
        CONF_LEAVE_REMINDER: False,
    }
    data.update(overrides)
    return data


class FakeWaze:
    """Stand-in for the waze_travel_time.get_travel_times action (pywaze is not installed in the test venv).

    The schema has the keys of the action in Home Assistant 2026.3, the oldest supported
    version, and rejects others, like the real one.
    """

    SCHEMA = vol.Schema(
        {
            vol.Required("origin"): str,
            vol.Required("destination"): str,
            vol.Required("region"): vol.In(["us", "na", "eu", "il", "au"]),
            vol.Optional("realtime", default=False): bool,
            vol.Optional("vehicle_type", default="car"): vol.In(["car", "taxi", "motorcycle"]),
            vol.Optional("units", default="metric"): vol.In(["metric", "imperial"]),
            vol.Optional("avoid_toll_roads", default=False): bool,
            vol.Optional("avoid_subscription_roads", default=False): bool,
            vol.Optional("avoid_ferries", default=False): bool,
            vol.Optional("incl_filter"): [str],
            vol.Optional("excl_filter"): [str],
        }
    )

    def __init__(self) -> None:
        """Answer 23.4 minutes for every destination until a test says otherwise."""
        self.calls: list[dict[str, Any]] = []
        self.duration: float = 23.4
        self.durations: dict[str, float] = {}
        self.response: Any = None
        self.error: Exception | None = None
        # Errors for single destinations, as when Waze cannot find a place.
        self.errors: dict[str, Exception] = {}

    def register(self, hass: HomeAssistant) -> None:
        """Register the action."""
        hass.services.async_register(
            WAZE_DOMAIN, WAZE_SERVICE, self._async_handle, schema=self.SCHEMA, supports_response=SupportsResponse.ONLY
        )

    async def _async_handle(self, call: ServiceCall) -> ServiceResponse:
        self.calls.append(dict(call.data))
        error = self.errors.get(call.data["destination"], self.error)
        if error is not None:
            raise error
        if self.response is not None:
            return self.response
        duration = self.durations.get(call.data["destination"], self.duration)
        return {"routes": [{"duration": duration, "distance": 18.2, "name": "Example Road", "street_names": []}]}

    def realtime_flags(self) -> list[bool]:
        """Return the realtime flag of every call in order."""
        return [call["realtime"] for call in self.calls]


@pytest.fixture
def waze(hass: HomeAssistant) -> FakeWaze:
    """Provide waze_travel_time.get_travel_times with invented answers."""
    fake = FakeWaze()
    fake.register(hass)
    return fake


def relay_subentry(subentry_id: str = RELAY_ID, title: str = "Kids → Family", **overrides: Any) -> dict[str, Any]:
    """Return subentry data for MockConfigEntry."""
    return {
        "data": relay_data(**overrides),
        "subentry_id": subentry_id,
        "subentry_type": SUBENTRY_TYPE_RELAY,
        "title": title,
        "unique_id": None,
    }


def make_entry(*subentries: dict[str, Any], title: str = ENTRY_TITLE) -> MockConfigEntry:
    """Return an account entry with the given relays."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=title,
        unique_id=UNIQUE_ID,
        data={CONF_URL: ACCOUNT_URL, CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
        subentries_data=list(subentries),
    )


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return an account entry with the Kids to Family relay."""
    return make_entry(relay_subentry())


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add and set up an entry, and let the first sync finish."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


def timed(
    summary: str, start: datetime, *, hours: float = 2, uid: str | None = "match-1", **kwargs: Any
) -> CalendarEvent:
    """Return a timed event."""
    return CalendarEvent(start=start, end=start + timedelta(hours=hours), summary=summary, uid=uid, **kwargs)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def radicale_url(tmp_path: Path, socket_enabled: None) -> Generator[str]:
    """Run a real Radicale CalDAV server with the user emma on a free local port."""
    users = tmp_path / "radicale-users"
    users.write_text("emma:secret\n")
    port = _free_port()
    configuration = radicale.config.load([])
    configuration.update(
        {
            "server": {"hosts": f"127.0.0.1:{port}"},
            "auth": {"type": "htpasswd", "htpasswd_filename": str(users), "htpasswd_encryption": "plain"},
            "storage": {"filesystem_folder": str(tmp_path / "radicale-collections")},
            "rights": {"type": "owner_only"},
            "web": {"type": "none"},
            "logging": {"level": "warning"},
        },
        "test",
        privileged=True,
    )
    shutdown_in, shutdown_out = socket.socketpair()
    thread = threading.Thread(
        target=radicale.server.serve, args=(configuration, shutdown_out), name="radicale", daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 10
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    shutdown_in.close()
    thread.join(10)
    shutdown_out.close()
    assert not thread.is_alive()
