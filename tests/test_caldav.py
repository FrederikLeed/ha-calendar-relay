"""Tests for the CalDAV client against mocked iCloud-shaped and standard server responses."""

from __future__ import annotations

import base64
import logging

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.calendar_relay.caldav import (
    CalDavAuthError,
    CalDavClient,
    CalDavConnectionError,
    CalDavError,
    CalDavNotFoundError,
    CalDavRefusedError,
    CalDavStatusError,
    DavCalendar,
    collection_url,
    is_event_href,
    normalize_url,
)

ICLOUD = "https://caldav.icloud.com/"
ICLOUD_PRINCIPAL = "https://caldav.icloud.com/123456789/principal/"
ICLOUD_HOME = "https://p99-caldav.icloud.com/123456789/calendars/"
FAMILY_UUID = "b8e0edb4-1acd-445a-b20e-66a1bf964bb7"
SHARED_HEX = "5f" * 32
EXPECTED_AUTH = "Basic " + base64.b64encode(b"parent@example.com:abcd-efgh-ijkl-mnop").decode()
CALENDAR = "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"


def multistatus(*responses: str) -> str:
    """Wrap response elements in a multistatus document."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
        'xmlns:cs="http://calendarserver.org/ns/" xmlns:ic="http://apple.com/ns/ical/">'
        + "".join(responses)
        + "</d:multistatus>"
    )


def response(href: str, props: str, missing: str = "", status: str | None = "HTTP/1.1 200 OK") -> str:
    """Return one response element, with an optional 404 propstat for missing properties."""
    status_element = f"<d:status>{status}</d:status>" if status else ""
    body = f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop>{status_element}</d:propstat>"
    if missing:
        body += f"<d:propstat><d:prop>{missing}</d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>"
    return body + "</d:response>"


def comps(*names: str) -> str:
    """Return a supported-calendar-component-set property."""
    inner = "".join(f'<c:comp name="{name}"/>' for name in names)
    return f"<c:supported-calendar-component-set>{inner}</c:supported-calendar-component-set>"


def privileges(*names: str) -> str:
    """Return a current-user-privilege-set property."""
    inner = "".join(f"<d:privilege><d:{name}/></d:privilege>" for name in names)
    return f"<d:current-user-privilege-set>{inner}</d:current-user-privilege-set>"


def cup(href: str) -> str:
    """Return a current-user-principal property."""
    return f"<d:current-user-principal><d:href>{href}</d:href></d:current-user-principal>"


def home_set(href: str) -> str:
    """Return a calendar-home-set property."""
    return f"<c:calendar-home-set><d:href>{href}</d:href></c:calendar-home-set>"


def client(hass: HomeAssistant, url: str = ICLOUD) -> CalDavClient:
    """Return a client on the mocked session."""
    return CalDavClient(async_get_clientsession(hass), url, "parent@example.com", "abcd-efgh-ijkl-mnop")


def calls(aioclient_mock: AiohttpClientMocker) -> list[tuple[str, str]]:
    """Return (method, url) of every request."""
    return [(method.upper(), str(url)) for method, url, _data, _headers in aioclient_mock.mock_calls]


def mock_icloud(aioclient_mock: AiohttpClientMocker) -> None:
    """Mock iCloud discovery for an account that owns Family and sees a shared copy of another Family."""
    aioclient_mock.request(
        "PROPFIND",
        ICLOUD,
        status=207,
        # iCloud keys the principal response as /principal/, not the request URI.
        text=multistatus(response("/principal/", cup("/123456789/principal/"), missing="<c:calendar-home-set/>")),
    )
    aioclient_mock.request(
        "PROPFIND",
        ICLOUD_PRINCIPAL,
        status=207,
        text=multistatus(
            response("/123456789/principal/", home_set("https://p99-caldav.icloud.com:443/123456789/calendars/"))
        ),
    )
    aioclient_mock.request(
        "PROPFIND",
        ICLOUD_HOME,
        status=207,
        text=multistatus(
            response("/123456789/calendars/", "<d:resourcetype><d:collection/></d:resourcetype>"),
            response(
                "/123456789/calendars/inbox/", "<d:resourcetype><d:collection/><c:schedule-inbox/></d:resourcetype>"
            ),
            response(
                "/123456789/calendars/outbox/", "<d:resourcetype><d:collection/><c:schedule-outbox/></d:resourcetype>"
            ),
            response(
                "/123456789/calendars/notification/",
                "<d:resourcetype><d:collection/><cs:notification/></d:resourcetype>",
            ),
            response(
                "/123456789/calendars/home/",
                f"{CALENDAR}<d:displayname>Home</d:displayname>",
                missing="<c:supported-calendar-component-set/><d:current-user-privilege-set/>",
            ),
            response(
                f"/123456789/calendars/{FAMILY_UUID}/",
                f"{CALENDAR}<d:displayname>Family</d:displayname>{comps('VEVENT')}{privileges('read', 'write')}"
                '<ic:calendar-color symbolic-color="blue">#00679EFF</ic:calendar-color>',
            ),
            response(
                "/123456789/calendars/tasks/",
                f"{CALENDAR}<d:displayname>Emma reminders</d:displayname>{comps('VTODO')}{privileges('all')}",
            ),
            response(
                f"/123456789/calendars/{SHARED_HEX}/",
                "<d:resourcetype><d:collection/><c:calendar/><cs:shared/></d:resourcetype>"
                f"<d:displayname>Family</d:displayname>{comps('VEVENT', 'VTODO')}{privileges('read')}",
            ),
            response("/123456789/calendars/reallyold/", f"{CALENDAR}<d:displayname></d:displayname>{comps('VEVENT')}"),
        ),
    )


async def test_icloud_discovery(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Principal, then the home on the partition host, then the calendars in it."""
    mock_icloud(aioclient_mock)
    calendars = await client(hass).async_discover()

    assert calls(aioclient_mock) == [
        ("PROPFIND", ICLOUD),
        ("PROPFIND", ICLOUD_PRINCIPAL),
        ("PROPFIND", ICLOUD_HOME),
    ]
    headers = [headers for _method, _url, _data, headers in aioclient_mock.mock_calls]
    assert all(h["Authorization"] == EXPECTED_AUTH for h in headers)
    assert [h["Depth"] for h in headers] == ["0", "0", "1"]
    assert calendars == [
        DavCalendar(f"{ICLOUD_HOME}home/", "Home", None, None),
        DavCalendar(f"{ICLOUD_HOME}{FAMILY_UUID}/", "Family", frozenset({"VEVENT"}), True),
        DavCalendar(f"{ICLOUD_HOME}tasks/", "Emma reminders", frozenset({"VTODO"}), True),
        DavCalendar(f"{ICLOUD_HOME}{SHARED_HEX}/", "Family", frozenset({"VEVENT", "VTODO"}), False),
        DavCalendar(f"{ICLOUD_HOME}reallyold/", "reallyold", frozenset({"VEVENT"}), None),
    ]
    assert [calendar.usable for calendar in calendars] == [True, True, False, False, True]
    assert [calendar.supports_events for calendar in calendars] == [True, True, False, True, True]


async def test_well_known_redirect_and_relative_hrefs(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A server without DAV at the root is found through /.well-known/caldav and a redirect."""
    base = "https://dav.example.com/"
    aioclient_mock.request("PROPFIND", base, status=404)
    aioclient_mock.request(
        "PROPFIND", "https://dav.example.com/.well-known/caldav", status=301, headers={"Location": "/dav/"}
    )
    aioclient_mock.request(
        "PROPFIND",
        "https://dav.example.com/dav/",
        status=207,
        text=multistatus(response("/dav/", cup("principals/emma/"))),
    )
    aioclient_mock.request(
        "PROPFIND",
        "https://dav.example.com/dav/principals/emma/",
        status=207,
        text=multistatus(response("/dav/principals/emma/", home_set("/dav/calendars/emma/"), status=None)),
    )
    aioclient_mock.request(
        "PROPFIND",
        "https://dav.example.com/dav/calendars/emma/",
        status=207,
        text=multistatus(
            response("/dav/calendars/emma/", "<d:resourcetype><d:collection/></d:resourcetype>"),
            response("kids%20club/", f"{CALENDAR}<d:displayname>Kids club</d:displayname>{privileges('bind')}"),
            response("/dav/calendars/emma/broken/", CALENDAR, status="HTTP/1.1 403 Forbidden"),
        ),
    )
    calendars = await client(hass, base).async_discover()
    assert calendars == [
        DavCalendar("https://dav.example.com/dav/calendars/emma/kids%20club/", "Kids club", None, True)
    ]
    assert [url for _method, url in calls(aioclient_mock)] == [
        base,
        "https://dav.example.com/.well-known/caldav",
        "https://dav.example.com/dav/",
        "https://dav.example.com/dav/principals/emma/",
        "https://dav.example.com/dav/calendars/emma/",
    ]


async def test_home_set_on_first_response_skips_principal(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """When the first answer already has the calendar home, discovery goes straight to it."""
    base = "http://127.0.0.1:5232/"
    aioclient_mock.request(
        "PROPFIND", base, status=207, text=multistatus(response("/", cup("/emma/") + home_set("/emma/")))
    )
    aioclient_mock.request(
        "PROPFIND",
        "http://127.0.0.1:5232/emma/",
        status=207,
        text=multistatus(response("/emma/family/", f"{CALENDAR}<d:displayname>Family</d:displayname>")),
    )
    calendars = await client(hass, base).async_discover()
    assert [calendar.url for calendar in calendars] == ["http://127.0.0.1:5232/emma/family/"]
    assert len(aioclient_mock.mock_calls) == 2


async def test_server_url_used_as_home_when_nothing_is_advertised(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A DAV answer without principal or home falls back to listing the configured URL."""
    base = "https://dav.example.com/calendars/emma/"
    aioclient_mock.request("PROPFIND", base, status=207, text=multistatus(response(base, "")))
    aioclient_mock.request("PROPFIND", "https://dav.example.com/.well-known/caldav", status=404)
    calendars = await client(hass, base).async_discover()
    assert calendars == []
    assert calls(aioclient_mock)[-1] == ("PROPFIND", base)


async def test_principal_without_home_is_listed(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """If the principal has no calendar-home-set, the principal itself is listed."""
    base = "https://dav.example.com/"
    aioclient_mock.request("PROPFIND", base, status=207, text=multistatus(response("/", cup("/p/emma/"))))
    aioclient_mock.request("PROPFIND", "https://dav.example.com/p/emma/", status=404)
    with pytest.raises(CalDavStatusError) as err:
        await client(hass, base).async_discover()
    assert err.value.status == 404


@pytest.mark.parametrize(
    ("status", "error"), [(401, CalDavAuthError), (503, CalDavConnectionError), (429, CalDavConnectionError)]
)
async def test_discovery_status_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int, error: type[Exception]
) -> None:
    """401 is an auth error; 429 and 5xx are temporary."""
    aioclient_mock.request("PROPFIND", ICLOUD, status=status)
    with pytest.raises(error):
        await client(hass).async_discover()


async def test_discovery_forbidden_everywhere_is_auth_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """403 on both the server URL and .well-known means the account may not use CalDAV."""
    aioclient_mock.request("PROPFIND", ICLOUD, status=403)
    aioclient_mock.request("PROPFIND", "https://caldav.icloud.com/.well-known/caldav", status=403)
    with pytest.raises(CalDavAuthError):
        await client(hass).async_discover()


@pytest.mark.parametrize("step", ["principal", "home"])
async def test_forbidden_later_in_discovery_is_auth_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, step: str
) -> None:
    """403 on the principal or the home is an auth error."""
    aioclient_mock.request(
        "PROPFIND", ICLOUD, status=207, text=multistatus(response("/", cup("/123456789/principal/")))
    )
    if step == "principal":
        aioclient_mock.request("PROPFIND", ICLOUD_PRINCIPAL, status=403)
    else:
        aioclient_mock.request(
            "PROPFIND", ICLOUD_PRINCIPAL, status=207, text=multistatus(response("/p/", home_set(ICLOUD_HOME)))
        )
        aioclient_mock.request("PROPFIND", ICLOUD_HOME, status=403)
    with pytest.raises(CalDavAuthError):
        await client(hass).async_discover()


async def test_not_a_caldav_server(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A web page and a missing .well-known is not a CalDAV server."""
    aioclient_mock.request("PROPFIND", "https://www.example.com/", status=200, text="<html></html>")
    aioclient_mock.request("PROPFIND", "https://www.example.com/.well-known/caldav", status=404)
    with pytest.raises(CalDavError) as err:
        await client(hass, "https://www.example.com/").async_discover()
    assert type(err.value) is CalDavError


async def test_invalid_xml(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A 207 that is not XML fails discovery."""
    aioclient_mock.request("PROPFIND", ICLOUD, status=207, text="this is not xml")
    with pytest.raises(CalDavError, match="invalid XML"):
        await client(hass).async_discover()


@pytest.mark.parametrize("exc", [aiohttp.ClientConnectionError(), TimeoutError()])
async def test_network_errors(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, exc: Exception) -> None:
    """Network failures and timeouts are connection errors without URLs in the message."""
    aioclient_mock.request("PROPFIND", ICLOUD, exc=exc)
    with pytest.raises(CalDavConnectionError) as err:
        await client(hass).async_discover()
    assert "icloud" not in str(err.value)


@pytest.mark.parametrize(
    "location", ["https://evil.example.org/", "http://caldav.icloud.com/", "ftp://caldav.icloud.com/"]
)
async def test_untrusted_redirect_is_not_followed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, location: str
) -> None:
    """Credentials never follow a redirect to another domain or a downgrade to http."""
    aioclient_mock.request("PROPFIND", ICLOUD, status=302, headers={"Location": location})
    with pytest.raises(CalDavError, match="Refusing"):
        await client(hass).async_discover()
    assert len(aioclient_mock.mock_calls) == 1


async def test_redirect_loop(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Redirects stop after a few hops."""
    aioclient_mock.request("PROPFIND", ICLOUD, status=302, headers={"Location": ICLOUD})
    with pytest.raises(CalDavError, match="Too many redirects"):
        await client(hass).async_discover()


@pytest.mark.parametrize(
    ("server", "url", "expected"),
    [
        (ICLOUD, "https://p62-caldav.icloud.com/1/calendars/", True),
        (ICLOUD, "https://caldav.icloud.com:8443/", True),
        (ICLOUD, "https://icloud.com/", False),
        (ICLOUD, "https://caldav.icloud.com.evil.example/", False),
        (ICLOUD, "http://p62-caldav.icloud.com/", False),
        ("http://127.0.0.1:5232/", "http://127.0.0.1:9999/", True),
        ("http://127.0.0.1:5232/", "https://127.0.0.2/", False),
        ("https://10.0.0.1/", "https://1.0.0.1/", False),
        ("https://example.com/", "https://www.example.com/", True),
        ("https://example.com/", "https://cloud.dav.example.com/", True),
        ("https://example.com/", "https://notexample.com/", False),
        ("https://example.com/", "http://www.example.com/", False),
        ("http://dav.example.com/", "http://x.dav.example.com/", False),
        ("http://dav.example.com/", "http://cal.example.com/", False),
        ("https://dav.example.com/", "mailto:someone", False),
    ],
)
def test_may_authenticate(hass: HomeAssistant, server: str, url: str, expected: bool) -> None:
    """Credentials go to the same host, or over https to a sibling under the same parent domain."""
    assert CalDavClient(None, server, "u", "p")._may_authenticate(url) is expected  # type: ignore[arg-type]


async def test_put_creates_event(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """PUT sends UTF-8 iCalendar data into the collection and returns the resource URL."""
    href = f"{ICLOUD_HOME}family/relay-0123abcd.ics"
    aioclient_mock.put(href, status=201)
    ics = "BEGIN:VCALENDAR\r\nSUMMARY:⚽ Emma: Home - Away\r\nEND:VCALENDAR\r\n"
    result = await client(hass).async_put_event(f"{ICLOUD_HOME}family", "relay-0123abcd.ics", ics)
    assert result == href
    _method, _url, data, headers = aioclient_mock.mock_calls[0]
    assert data == ics.encode("utf-8")
    assert headers["Content-Type"] == "text/calendar; charset=utf-8"
    assert headers["Authorization"] == EXPECTED_AUTH


async def test_put_follows_redirect_to_partition_host(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A 307 to the partition host is followed with the body and credentials.

    The URL returned is the one inside the calendar, not the redirected one, so a
    later DELETE of it passes the inside-the-calendar check and is sent.
    """
    first = "https://caldav.icloud.com/123456789/calendars/family/relay-1.ics"
    final = "https://p99-caldav.icloud.com/123456789/calendars/family/relay-1.ics"
    aioclient_mock.put(first, status=307, headers={"Location": final})
    aioclient_mock.put(final, status=204)
    result = await client(hass).async_put_event(
        "https://caldav.icloud.com/123456789/calendars/family/", "relay-1.ics", "X"
    )
    assert result == first
    assert is_event_href("https://caldav.icloud.com/123456789/calendars/family/", result)
    assert [data for _m, _u, data, _h in aioclient_mock.mock_calls] == [b"X", b"X"]


@pytest.mark.parametrize(
    ("status", "body", "error", "condition"),
    [
        (
            403,
            '<d:error xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><c:unique-scheduling-object-resource/>'
            "</d:error>",
            CalDavRefusedError,
            "unique-scheduling-object-resource",
        ),
        (
            409,
            '<d:error xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><c:no-uid-conflict/></d:error>',
            CalDavRefusedError,
            "no-uid-conflict",
        ),
        (403, "", CalDavAuthError, None),
        (404, "", CalDavNotFoundError, None),
        (409, "Conflict", CalDavNotFoundError, None),
        (412, '<d:error xmlns:d="DAV:"/>', CalDavStatusError, None),
        (415, '<d:multistatus xmlns:d="DAV:"/>', CalDavStatusError, None),
    ],
)
async def test_put_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    body: str,
    error: type[CalDavStatusError],
    condition: str | None,
) -> None:
    """A 403 or 409 with a DAV:error reason refuses this event only (CalendarServer answers a UID that exists
    in another calendar with 403, Radicale a UID conflict with 409). A bare 403 is an auth error; 404 and a
    bare 409 mean the calendar is missing."""
    aioclient_mock.put(f"{ICLOUD_HOME}family/relay-1.ics", status=status, text=body)
    with pytest.raises(error) as err:
        await client(hass).async_put_event(f"{ICLOUD_HOME}family/", "relay-1.ics", "X")
    assert type(err.value) is error
    assert err.value.status == status
    assert err.value.condition == condition


@pytest.mark.parametrize("name", ["event.txt", "../x.ics", "a b.ics", "x@y.ics", ".ics"])
async def test_put_rejects_unsafe_names(hass: HomeAssistant, name: str) -> None:
    """Resource names are plain letters, digits and hyphens."""
    with pytest.raises(ValueError):
        await client(hass).async_put_event(f"{ICLOUD_HOME}family/", name, "X")


@pytest.mark.parametrize(("status", "expected"), [(204, True), (200, True), (404, False), (410, False)])
async def test_delete(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int, expected: bool) -> None:
    """DELETE sends no Depth header; 404 and 410 mean the event was already gone."""
    href = f"{ICLOUD_HOME}family/relay-1.ics"
    aioclient_mock.delete(href, status=status)
    assert await client(hass).async_delete_event(f"{ICLOUD_HOME}family/", href) is expected
    _method, _url, _data, headers = aioclient_mock.mock_calls[0]
    assert "Depth" not in headers


@pytest.mark.parametrize(
    ("status", "error"), [(403, CalDavAuthError), (400, CalDavStatusError), (500, CalDavConnectionError)]
)
async def test_delete_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int, error: type[Exception]
) -> None:
    """Other DELETE answers raise."""
    href = f"{ICLOUD_HOME}family/relay-1.ics"
    aioclient_mock.delete(href, status=status)
    with pytest.raises(error):
        await client(hass).async_delete_event(f"{ICLOUD_HOME}family/", href)


@pytest.mark.parametrize(
    "href",
    [
        f"{ICLOUD_HOME}family/",
        f"{ICLOUD_HOME}family",
        f"{ICLOUD_HOME}work/relay-1.ics",
        f"{ICLOUD_HOME}family/sub/relay-1.ics",
        f"{ICLOUD_HOME}family/relay-1.txt",
        "https://p98-caldav.icloud.com/123456789/calendars/family/relay-1.ics",
        f"{ICLOUD_HOME}family/relay-1.ics?x=1",
    ],
)
async def test_delete_refuses_anything_but_an_event_in_the_calendar(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, href: str
) -> None:
    """A DELETE never reaches the calendar collection or another calendar."""
    with pytest.raises(ValueError):
        await client(hass).async_delete_event(f"{ICLOUD_HOME}family/", href)
    assert aioclient_mock.mock_calls == []


async def test_delete_follows_a_redirect_to_the_same_resource(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A DELETE redirected to the partition host is sent there."""
    calendar = "https://caldav.icloud.com/123456789/calendars/family/"
    first = f"{calendar}relay-1.ics"
    final = "https://p99-caldav.icloud.com/123456789/calendars/family/relay-1.ics"
    aioclient_mock.delete(first, status=307, headers={"Location": final})
    aioclient_mock.delete(final, status=204)
    assert await client(hass).async_delete_event(calendar, first) is True
    assert calls(aioclient_mock) == [("DELETE", first), ("DELETE", final)]


@pytest.mark.parametrize(
    "location",
    [
        f"{ICLOUD_HOME}family/",
        f"{ICLOUD_HOME}family",
        "../",
        f"{ICLOUD_HOME}family/relay-2.ics",
        f"{ICLOUD_HOME}family/relay-1.ics?x=1",
        f"{ICLOUD_HOME}family/relay-1.ics/",
    ],
)
@pytest.mark.parametrize("method", ["PUT", "DELETE"])
async def test_event_redirect_away_from_the_resource_is_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, method: str, location: str
) -> None:
    """A write or delete is never redirected to the calendar collection or to another resource."""
    calendar = f"{ICLOUD_HOME}family/"
    href = f"{calendar}relay-1.ics"
    aioclient_mock.request(method, href, status=301, headers={"Location": location})
    aioclient_mock.request(method, calendar, status=204)
    with pytest.raises(CalDavError, match="redirect away from the event resource"):
        if method == "PUT":
            await client(hass).async_put_event(calendar, "relay-1.ics", "X")
        else:
            await client(hass).async_delete_event(calendar, href)
    assert calls(aioclient_mock) == [(method, href)]


@pytest.mark.parametrize("status", [403, 409])
async def test_delete_refused_with_a_reason(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int
) -> None:
    """A DELETE refused with a DAV:error precondition fails only that event."""
    href = f"{ICLOUD_HOME}family/relay-1.ics"
    aioclient_mock.delete(href, status=status, text='<d:error xmlns:d="DAV:"><d:need-privileges/></d:error>')
    with pytest.raises(CalDavRefusedError) as err:
        await client(hass).async_delete_event(f"{ICLOUD_HOME}family/", href)
    assert err.value.status == status
    assert err.value.condition == "need-privileges"


async def test_well_known_redirect_to_a_subdomain(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """RFC 6764: example.com may send clients to cloud.example.com, which is trusted over https."""
    base = "https://example.com/"
    dav = "https://cloud.example.com/remote.php/dav/"
    aioclient_mock.request("PROPFIND", base, status=404)
    aioclient_mock.request("PROPFIND", f"{base}.well-known/caldav", status=301, headers={"Location": dav})
    aioclient_mock.request(
        "PROPFIND", dav, status=207, text=multistatus(response("/remote.php/dav/", home_set("calendars/emma/")))
    )
    aioclient_mock.request(
        "PROPFIND",
        f"{dav}calendars/emma/",
        status=207,
        text=multistatus(
            response("/remote.php/dav/calendars/emma/family/", f"{CALENDAR}<d:displayname>Family</d:displayname>")
        ),
    )
    calendars = await client(hass, base).async_discover()
    assert [calendar.url for calendar in calendars] == [f"{dav}calendars/emma/family/"]


async def test_well_known_redirect_to_another_domain_falls_back_to_the_server_url(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, caplog: pytest.LogCaptureFixture
) -> None:
    """A redirect to another domain is not followed, and a DAV answer from the server URL is still used."""
    caplog.set_level(logging.DEBUG, logger="custom_components.calendar_relay.caldav")
    base = "https://example.com/dav/"
    aioclient_mock.request(
        "PROPFIND",
        base,
        status=207,
        text=multistatus(
            response("/dav/", "<d:resourcetype><d:collection/></d:resourcetype>"),
            response("/dav/family/", f"{CALENDAR}<d:displayname>Family</d:displayname>"),
        ),
    )
    aioclient_mock.request(
        "PROPFIND",
        "https://example.com/.well-known/caldav",
        status=301,
        headers={"Location": "https://dav.provider.example.net/"},
    )
    calendars = await client(hass, base).async_discover()
    assert [calendar.url for calendar in calendars] == ["https://example.com/dav/family/"]
    assert calls(aioclient_mock) == [
        ("PROPFIND", base),
        ("PROPFIND", "https://example.com/.well-known/caldav"),
        ("PROPFIND", base),
    ]
    assert "dav.provider.example.net" in caplog.text


async def test_well_known_redirect_to_another_domain_without_a_dav_answer(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Without a DAV answer from the server URL, the refused redirect means this is not a usable CalDAV URL."""
    aioclient_mock.request("PROPFIND", "https://example.com/", status=404)
    aioclient_mock.request(
        "PROPFIND",
        "https://example.com/.well-known/caldav",
        status=301,
        headers={"Location": "https://dav.provider.example.net/"},
    )
    with pytest.raises(CalDavError, match="did not answer as a CalDAV server"):
        await client(hass, "https://example.com/").async_discover()
    assert len(aioclient_mock.mock_calls) == 2


def test_url_helpers() -> None:
    """URLs are normalized for comparison and storage."""
    assert (
        normalize_url(" HTTPS://P99-CalDAV.iCloud.com:443/123/calendars/#frag")
        == "https://p99-caldav.icloud.com/123/calendars/"
    )
    assert normalize_url("http://127.0.0.1:5232") == "http://127.0.0.1:5232/"
    assert normalize_url("http://[::1]:8080/dav") == "http://[::1]:8080/dav"
    assert normalize_url("https://dav.example.com:99999/") == "https://dav.example.com/"
    assert collection_url("https://dav.example.com/a") == "https://dav.example.com/a/"
    assert is_event_href("https://dav.example.com/kids%20club/", "https://DAV.example.com:443/kids club/relay-1.ics")
    assert CalDavClient(None, "HTTPS://CalDAV.iCloud.com", "u", "p").url == ICLOUD  # type: ignore[arg-type]
