"""A small CalDAV client for Calendar Relay.

This module has no Home Assistant imports. It needs an aiohttp ClientSession
(Home Assistant passes its shared session) and speaks just enough WebDAV and
CalDAV to discover calendars and to write and delete single event resources.

Error messages never contain URLs, so they are safe to show in entity
attributes and diagnostics.
"""

from __future__ import annotations

import base64
import ipaddress
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import aiohttp

_LOGGER = logging.getLogger(__name__)

NS_DAV = "DAV:"
NS_CALDAV = "urn:ietf:params:xml:ns:caldav"

DAV_RESPONSE = f"{{{NS_DAV}}}response"
DAV_HREF = f"{{{NS_DAV}}}href"
DAV_PROPSTAT = f"{{{NS_DAV}}}propstat"
DAV_PROP = f"{{{NS_DAV}}}prop"
DAV_STATUS = f"{{{NS_DAV}}}status"
DAV_ERROR = f"{{{NS_DAV}}}error"
DAV_RESOURCETYPE = f"{{{NS_DAV}}}resourcetype"
DAV_DISPLAYNAME = f"{{{NS_DAV}}}displayname"
DAV_CURRENT_USER_PRINCIPAL = f"{{{NS_DAV}}}current-user-principal"
DAV_PRIVILEGE_SET = f"{{{NS_DAV}}}current-user-privilege-set"
DAV_PRIVILEGE = f"{{{NS_DAV}}}privilege"
CALDAV_CALENDAR = f"{{{NS_CALDAV}}}calendar"
CALDAV_HOME_SET = f"{{{NS_CALDAV}}}calendar-home-set"
CALDAV_COMPONENT_SET = f"{{{NS_CALDAV}}}supported-calendar-component-set"
CALDAV_COMP = f"{{{NS_CALDAV}}}comp"

# Creating a resource needs bind (or write, which aggregates it, or all).
WRITE_PRIVILEGES = frozenset({f"{{{NS_DAV}}}all", f"{{{NS_DAV}}}write", f"{{{NS_DAV}}}bind"})

_XML_HEAD = '<?xml version="1.0" encoding="utf-8"?>'
_PROPFIND_OPEN = '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>'
_PROPFIND_CLOSE = "</d:prop></d:propfind>"
PROPFIND_PRINCIPAL = f"{_XML_HEAD}{_PROPFIND_OPEN}<d:current-user-principal/><c:calendar-home-set/>{_PROPFIND_CLOSE}"
PROPFIND_HOME = f"{_XML_HEAD}{_PROPFIND_OPEN}<c:calendar-home-set/>{_PROPFIND_CLOSE}"
PROPFIND_CALENDARS = (
    f"{_XML_HEAD}{_PROPFIND_OPEN}<d:resourcetype/><d:displayname/>"
    f"<c:supported-calendar-component-set/><d:current-user-privilege-set/>{_PROPFIND_CLOSE}"
)

XML_CONTENT_TYPE = "application/xml; charset=utf-8"
ICS_CONTENT_TYPE = "text/calendar; charset=utf-8"
REDIRECT_STATUSES = frozenset({301, 302, 307, 308})
MAX_REDIRECTS = 5
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
RESOURCE_NAME = re.compile(r"[A-Za-z0-9-]+\.ics")
DEFAULT_PORTS = {"http": 80, "https": 443}


class CalDavError(Exception):
    """Base class for CalDAV errors."""


class CalDavConnectionError(CalDavError):
    """The server could not be reached, timed out or answered with a temporary error."""


class CalDavStatusError(CalDavError):
    """The server answered with a status the client cannot use."""

    def __init__(self, status: int, condition: str | None = None) -> None:
        """Store the status and the DAV:error precondition, if any."""
        message = f"HTTP {status}"
        if condition:
            message = f"{message} ({condition})"
        super().__init__(message)
        self.status = status
        self.condition = condition


class CalDavUntrustedHostError(CalDavError):
    """A request or redirect would send the credentials to a host that is not trusted with them."""

    def __init__(self, host: str) -> None:
        """Store the host for logging; the message stays free of it."""
        super().__init__("Refusing to send credentials to a different host")
        self.host = host


class CalDavAuthError(CalDavStatusError):
    """The server rejected the credentials (401) or refused the request without a reason (403)."""


class CalDavNotFoundError(CalDavStatusError):
    """The calendar collection does not exist (404, or 409 without a reason, when writing into it)."""


class CalDavRefusedError(CalDavStatusError):
    """The server refused one event resource with a DAV:error precondition (403 or 409 with a reason).

    Examples are no-uid-conflict, valid-calendar-data and max-resource-size. The
    credentials and the calendar are fine; only this resource was refused.
    """


@dataclass(frozen=True, slots=True)
class DavCalendar:
    """A calendar collection found during discovery."""

    url: str
    name: str
    components: frozenset[str] | None = None
    writable: bool | None = None

    @property
    def supports_events(self) -> bool:
        """Return True if the calendar accepts VEVENT (an absent component set means all)."""
        return self.components is None or "VEVENT" in self.components

    @property
    def usable(self) -> bool:
        """Return True if events can be relayed into this calendar."""
        return self.supports_events and self.writable is not False


def normalize_url(url: str) -> str:
    """Return url with a lowercase scheme and host, no default port and no fragment."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and DEFAULT_PORTS.get(scheme) != port:
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def collection_url(url: str) -> str:
    """Return the normalized URL of a collection, ending with a slash."""
    parts = urlsplit(normalize_url(url))
    path = parts.path if parts.path.endswith("/") else f"{parts.path}/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def is_event_href(calendar_url: str, href: str) -> bool:
    """Return True if href is a relay-style .ics resource directly inside calendar_url.

    Every DELETE is checked with this, so a bad stored href can never hit the
    calendar collection itself (on iCloud that would unlink a shared calendar).
    """
    calendar = urlsplit(collection_url(calendar_url))
    target = urlsplit(normalize_url(href))
    if (target.scheme, target.netloc) != (calendar.scheme, calendar.netloc) or target.query:
        return False
    calendar_path = unquote(calendar.path)
    target_path = unquote(target.path)
    if not target_path.startswith(calendar_path):
        return False
    return RESOURCE_NAME.fullmatch(target_path[len(calendar_path) :]) is not None


def _last_segment(url: str) -> str:
    """Return the last path segment of a URL."""
    return unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])


def _resource_name(url: str) -> str | None:
    """Return the relay-style .ics name a URL ends with, or None (collections, queries, other names)."""
    parts = urlsplit(url)
    if parts.query:
        return None
    name = unquote(parts.path).rsplit("/", 1)[-1]
    return name if RESOURCE_NAME.fullmatch(name) else None


def _status_ok(status: str | None) -> bool:
    """Return True for a propstat status line in the 2xx range (or no status at all)."""
    if not status:
        return True
    parts = status.split()
    return len(parts) > 1 and parts[1].startswith("2")


def _error_condition(body: bytes) -> str | None:
    """Return the local name of the first precondition in a DAV:error body."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    if root.tag != DAV_ERROR:
        return None
    for child in root:
        return child.tag.rsplit("}", 1)[-1]
    return None


@dataclass(slots=True)
class _DavResource:
    """One response element of a multistatus body with its successful properties."""

    url: str
    base_url: str
    props: dict[str, ET.Element] = field(default_factory=dict)

    def hrefs(self, tag: str) -> list[str]:
        """Return the absolute URLs of the DAV:href elements inside a property."""
        element = self.props.get(tag)
        if element is None:
            return []
        return [
            urljoin(self.base_url, href.text.strip())
            for href in element.iter(DAV_HREF)
            if href.text and href.text.strip()
        ]


def _parse_multistatus(body: bytes, base_url: str) -> list[_DavResource]:
    """Parse a 207 body. Relative hrefs are resolved against the URL that answered."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as err:
        raise CalDavError("The server sent invalid XML") from err
    resources: list[_DavResource] = []
    for response in root.iter(DAV_RESPONSE):
        href = (response.findtext(DAV_HREF) or "").strip()
        if not href:
            continue
        resource = _DavResource(url=urljoin(base_url, href), base_url=base_url)
        for propstat in response.findall(DAV_PROPSTAT):
            if not _status_ok(propstat.findtext(DAV_STATUS)):
                continue
            for prop in propstat.findall(DAV_PROP):
                for child in prop:
                    resource.props[child.tag] = child
        resources.append(resource)
    return resources


@dataclass(slots=True)
class _Response:
    """The parts of an HTTP response the client needs."""

    status: int
    url: str
    body: bytes


class CalDavClient:
    """CalDAV client with Basic auth and manual, credential-safe redirects."""

    def __init__(self, session: aiohttp.ClientSession, url: str, username: str, password: str) -> None:
        """Initialize the client. The session is shared and never closed here."""
        self._session = session
        self._url = normalize_url(url)
        # Built by hand: aiohttp 3.14 deprecates BasicAuth, and encode_basic_auth is missing before 3.14.
        token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        self._authorization = f"Basic {token}"

    @property
    def url(self) -> str:
        """Return the normalized server URL."""
        return self._url

    def _may_authenticate(self, url: str) -> bool:
        """Return True if credentials may be sent to url.

        Allowed: the configured host, or over https a subdomain of it (a
        /.well-known/caldav redirect from example.com to cloud.example.com, RFC 6764)
        or a sibling host under the same parent domain (caldav.icloud.com hands out
        pNN-caldav.icloud.com). Never a downgrade from https to http.
        """
        origin = urlsplit(self._url)
        target = urlsplit(url)
        if target.scheme not in DEFAULT_PORTS or not target.hostname:
            return False
        if origin.scheme == "https" and target.scheme != "https":
            return False
        host = target.hostname.lower()
        origin_host = (origin.hostname or "").lower()
        if host == origin_host:
            return True
        if target.scheme != "https":
            return False
        try:
            ipaddress.ip_address(origin_host)
        except ValueError:
            pass
        else:
            return False
        if host.endswith("." + origin_host):
            return True
        labels = origin_host.split(".")
        if len(labels) < 3:
            return False
        return host.endswith("." + ".".join(labels[1:]))

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        resource: bool = False,
    ) -> _Response:
        """Send a request, following redirects by hand so credentials stay on trusted hosts.

        With resource=True (PUT and DELETE of one event) a redirect is only followed
        when it keeps the same .ics resource name, so a write or delete can never be
        sent to a calendar collection or anything else.

        401 raises CalDavAuthError, 429 and 5xx raise CalDavConnectionError, any
        other status is returned to the caller.
        """
        name = _resource_name(url) if resource else None
        for _ in range(MAX_REDIRECTS + 1):
            if not self._may_authenticate(url):
                raise CalDavUntrustedHostError(urlsplit(url).hostname or "")
            request_headers = {"Authorization": self._authorization}
            if headers:
                request_headers.update(headers)
            try:
                async with self._session.request(
                    method,
                    url,
                    headers=request_headers,
                    data=body,
                    allow_redirects=False,
                    timeout=REQUEST_TIMEOUT,
                ) as response:
                    status = response.status
                    location = response.headers.get("Location")
                    payload = await response.read()
            except (aiohttp.ClientError, TimeoutError) as err:
                raise CalDavConnectionError(f"{method} failed: {type(err).__name__}") from err
            if status in REDIRECT_STATUSES and location:
                url = normalize_url(urljoin(url, location))
                if resource and (name is None or _resource_name(url) != name):
                    raise CalDavError("Refusing to follow a redirect away from the event resource")
                continue
            if status == 401:
                raise CalDavAuthError(status)
            if status == 429 or status >= 500:
                raise CalDavConnectionError(f"{method} returned HTTP {status}")
            return _Response(status=status, url=url, body=payload)
        raise CalDavError("Too many redirects")

    async def _async_propfind(self, url: str, body: str, *, depth: int) -> _Response:
        """Send a PROPFIND."""
        return await self._request(
            "PROPFIND",
            url,
            headers={"Depth": str(depth), "Content-Type": XML_CONTENT_TYPE},
            body=body.encode("utf-8"),
        )

    async def async_discover(self) -> list[DavCalendar]:
        """Return every calendar collection in the user's calendar homes."""
        found: dict[str, DavCalendar] = {}
        for home in await self._async_find_homes():
            for calendar in await self._async_list_calendars(home):
                found.setdefault(calendar.url, calendar)
        return list(found.values())

    async def _async_find_homes(self) -> list[str]:
        """Find the calendar home URLs: current-user-principal, then calendar-home-set."""
        principal: str | None = None
        homes: list[str] = []
        answered = False
        refused = False
        well_known = normalize_url(urljoin(self._url, "/.well-known/caldav"))
        for url in (self._url, well_known):
            try:
                response = await self._async_propfind(url, PROPFIND_PRINCIPAL, depth=0)
            except CalDavUntrustedHostError as err:
                if url != well_known:
                    raise
                # RFC 6764 allows a redirect to another host; without trust in it, use what the server URL gave.
                _LOGGER.debug("/.well-known/caldav redirects to %s; enter that host as the server URL", err.host)
                continue
            if response.status != 207:
                refused = refused or response.status == 403
                continue
            answered = True
            # The response href may differ from the request URL (iCloud), so read every response.
            for resource in _parse_multistatus(response.body, response.url):
                homes.extend(resource.hrefs(CALDAV_HOME_SET))
                principal = principal or next(iter(resource.hrefs(DAV_CURRENT_USER_PRINCIPAL)), None)
            if homes or principal:
                break
        if not answered:
            if refused:
                raise CalDavAuthError(403)
            raise CalDavError("The server did not answer as a CalDAV server")
        if not homes and principal:
            response = await self._async_propfind(principal, PROPFIND_HOME, depth=0)
            if response.status == 403:
                raise CalDavAuthError(403)
            if response.status == 207:
                for resource in _parse_multistatus(response.body, response.url):
                    homes.extend(resource.hrefs(CALDAV_HOME_SET))
        if not homes:
            homes = [principal or self._url]
        return list(dict.fromkeys(collection_url(home) for home in homes))

    async def _async_list_calendars(self, home: str) -> list[DavCalendar]:
        """List the calendar collections directly inside a calendar home."""
        response = await self._async_propfind(home, PROPFIND_CALENDARS, depth=1)
        if response.status == 403:
            raise CalDavAuthError(403)
        if response.status != 207:
            raise CalDavStatusError(response.status)
        calendars: list[DavCalendar] = []
        for resource in _parse_multistatus(response.body, response.url):
            resourcetype = resource.props.get(DAV_RESOURCETYPE)
            # Skips the home itself, inbox, outbox, notification and dropbox.
            if resourcetype is None or resourcetype.find(CALDAV_CALENDAR) is None:
                continue
            url = collection_url(resource.url)
            name_element = resource.props.get(DAV_DISPLAYNAME)
            name = (name_element.text or "").strip() if name_element is not None else ""
            component_set = resource.props.get(CALDAV_COMPONENT_SET)
            components = (
                None
                if component_set is None
                else frozenset((comp.get("name") or "").upper() for comp in component_set.iter(CALDAV_COMP))
            )
            privilege_set = resource.props.get(DAV_PRIVILEGE_SET)
            writable = (
                None
                if privilege_set is None
                else any(
                    child.tag in WRITE_PRIVILEGES
                    for privilege in privilege_set.iter(DAV_PRIVILEGE)
                    for child in privilege
                )
            )
            calendars.append(
                DavCalendar(url=url, name=name or _last_segment(url), components=components, writable=writable)
            )
        return calendars

    async def async_put_event(self, calendar_url: str, name: str, ics: str) -> str:
        """Create or replace the resource name inside calendar_url and return its URL.

        The URL returned is the one inside calendar_url, also when the server
        redirected the write (to an iCloud partition host, say): a later DELETE of
        it passes is_event_href and follows the same redirect.
        """
        if RESOURCE_NAME.fullmatch(name) is None:
            raise ValueError("Resource names must be letters, digits and hyphens ending in .ics")
        href = collection_url(calendar_url) + name
        response = await self._request(
            "PUT",
            href,
            headers={"Content-Type": ICS_CONTENT_TYPE},
            body=ics.encode("utf-8"),
            resource=True,
        )
        if response.status in (200, 201, 204):
            return href
        condition = _error_condition(response.body)
        if response.status in (403, 409) and condition:
            raise CalDavRefusedError(response.status, condition)
        if response.status == 403:
            raise CalDavAuthError(response.status)
        if response.status in (404, 409):
            raise CalDavNotFoundError(response.status)
        raise CalDavStatusError(response.status, condition)

    async def async_delete_event(self, calendar_url: str, href: str) -> bool:
        """Delete an event resource. Return False if it was already gone (404 or 410)."""
        if not is_event_href(calendar_url, href):
            raise ValueError("Only .ics resources directly inside the calendar can be deleted")
        # No Depth header: iCloud rejects one on DELETE.
        response = await self._request("DELETE", normalize_url(href), resource=True)
        if response.status in (200, 202, 204):
            return True
        if response.status in (404, 410):
            return False
        condition = _error_condition(response.body)
        if response.status in (403, 409) and condition:
            raise CalDavRefusedError(response.status, condition)
        if response.status == 403:
            raise CalDavAuthError(response.status)
        raise CalDavStatusError(response.status, condition)
