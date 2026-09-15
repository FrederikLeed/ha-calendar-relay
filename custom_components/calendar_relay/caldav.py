"""A small CalDAV client for Calendar Relay.

This module has no Home Assistant imports. It needs an aiohttp ClientSession
(Home Assistant passes its shared session) and speaks just enough WebDAV and
CalDAV to discover calendars and to write and delete single event resources.

Error messages never contain URLs or credentials. An error raised for an HTTP
answer that names no DAV:error condition ends with a short excerpt of the answer's
body (body_excerpt), which can quote event text the server refused; the error's
summary leaves the excerpt out and is what entity attributes and diagnostics show.
"""

from __future__ import annotations

import base64
import html
import ipaddress
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

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
EXCERPT_LENGTH = 160
# The excerpt of a body in which a credential cannot be removed without showing it.
EXCERPT_WITHHELD = "[withheld, it may contain the credentials]"

# JSON string escapes; an answer may quote the credentials with them.
_JSON_ESCAPE = re.compile(r'\\(?:u([0-9A-Fa-f]{4})|(["\\/bfnrt]))')
_JSON_CHARACTERS = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
# A character that continues a word: [redacted] next to one would show which word was removed.
_WORD_CHARACTER = re.compile(r"[\w-]")
# A header echoed in a body (an error page that shows the request), up to the end of its line.
_ECHOED_HEADER = re.compile(r"\b((?:proxy-)?authorization|(?:set-)?cookie)[ \t]*[:=][^\r\n<]*", re.IGNORECASE)
# Absolute URLs, and absolute paths of two or more segments (iCloud paths carry the account's numeric id).
_ADDRESS = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"']*|(?<![\w.~-])/[^\s<>\"'/]+/[^\s<>\"']*")
# An XML namespace declaration just before a URL: the namespace names the error format, so it is kept.
_NAMESPACE_DECLARATION = re.compile(r"xmlns(?::[\w.-]+)?\s*=\s*[\"']$")


def body_excerpt(body: bytes, secrets: Iterable[str] = ()) -> str | None:
    """Return a short excerpt of a response body that is safe to log, or None for an empty body.

    The secrets (credentials) are replaced with [redacted] both before and after HTML character
    references, JSON string escapes and percent-encoding are decoded, each in the form given and in
    its decoded form: a password such as ab%41cd echoed raw would otherwise decode to abAcd and slip
    through. When a secret is part of a longer word, the whole excerpt is EXCERPT_WITHHELD. Echoed
    Authorization and Cookie headers are then replaced with [redacted], URLs and absolute paths
    become [url] and [path] (XML namespace names are kept), other characters that do not print
    become spaces, whitespace is collapsed, and the result is cut to EXCERPT_LENGTH characters.
    """
    forms = _secret_forms(secrets)
    text = _redact_secrets(body.decode("utf-8", errors="replace"), forms)
    if text is not None:
        text = _redact_secrets(_decode_escapes(text), forms)
    if text is None:
        return EXCERPT_WITHHELD
    text = _ECHOED_HEADER.sub(r"\1: [redacted]", text)
    text = "".join(char if char.isprintable() else " " for char in text)
    text = _ADDRESS.sub(_redact_address, text)
    text = " ".join(text.split())
    if len(text) > EXCERPT_LENGTH:
        text = text[: EXCERPT_LENGTH - 3] + "..."
    return text or None


def _decode_escapes(text: str) -> str:
    """Return text with HTML character references, JSON string escapes and percent-encoding decoded."""
    text = html.unescape(text)
    text = _JSON_ESCAPE.sub(
        lambda match: chr(int(match.group(1), 16)) if match.group(1) else _JSON_CHARACTERS[match.group(2)], text
    )
    text = unquote(text)
    # Joins the surrogate pairs of \u escapes; a lone surrogate becomes U+FFFD.
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _secret_forms(secrets: Iterable[str]) -> list[str]:
    """Return every secret as given and as it reads once its own escapes are decoded."""
    forms: set[str] = set()
    for secret in secrets:
        if secret:
            forms.update((secret, _decode_escapes(secret)))
    return [form for form in forms if form]


def _redact_secrets(text: str, secrets: Iterable[str]) -> str | None:
    """Return text with every secret, in any case, replaced by [redacted].

    Return None when a secret is part of a longer word (a letter, digit, underscore or hyphen
    right before or after it): a username such as calendar would show through valid-[redacted]-data.
    """
    wanted = sorted({secret for secret in secrets if secret}, key=len, reverse=True)
    if wanted:
        pattern = re.compile("|".join(map(re.escape, wanted)), re.IGNORECASE)
        for match in pattern.finditer(text):
            around = text[max(0, match.start() - 1) : match.start()] + text[match.end() : match.end() + 1]
            if _WORD_CHARACTER.search(around):
                return None
        text = pattern.sub("[redacted]", text)
    return text


def _redact_address(match: re.Match[str]) -> str:
    """Return the replacement of a URL or path, or the URL itself when it names an XML namespace."""
    if _NAMESPACE_DECLARATION.search(match.string, max(0, match.start() - 64), match.start()):
        return match.group()
    return "[url]" if "://" in match.group() else "[path]"


class CalDavError(Exception):
    """Base class for CalDAV errors.

    The message may end with an excerpt of the server's answer, which can quote event text.
    summary is the message without it: use it for text that is kept or shown, such as entity
    attributes and diagnostics.
    """

    def __init__(self, message: str, excerpt: str | None = None) -> None:
        """Store the message and the excerpt of the answer, if any."""
        super().__init__(f"{message}, response body: {excerpt}" if excerpt else message)
        self.summary = message
        self.excerpt = excerpt


class CalDavConnectionError(CalDavError):
    """The server could not be reached, timed out or answered with a temporary error."""


class CalDavStatusError(CalDavError):
    """The server answered with a status the client cannot use."""

    def __init__(self, status: int, condition: str | None = None, excerpt: str | None = None) -> None:
        """Store the status, the DAV:error precondition and the excerpt of the answer, if any."""
        message = f"HTTP {status}"
        if condition:
            message = f"{message} ({condition})"
        super().__init__(message, excerpt)
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
        # Removed from excerpts of response bodies, in case a server echoes them.
        self._secrets = (username, quote(username, safe=""), password, token.rstrip("="))

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
                raise CalDavAuthError(status, excerpt=self._excerpt(payload))
            if status == 429 or status >= 500:
                raise CalDavConnectionError(f"{method} returned HTTP {status}", self._excerpt(payload))
            return _Response(status=status, url=url, body=payload)
        raise CalDavError("Too many redirects")

    def _excerpt(self, body: bytes) -> str | None:
        """Return a safe excerpt of a response body, without this client's credentials."""
        return body_excerpt(body, self._secrets)

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
                raise CalDavAuthError(403, excerpt=self._excerpt(response.body))
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
            raise CalDavAuthError(403, excerpt=self._excerpt(response.body))
        if response.status != 207:
            raise CalDavStatusError(response.status, excerpt=self._excerpt(response.body))
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

        One PUT is sent, without If-Match or If-None-Match. iCloud answers it with 412
        and no DAV:error condition for an entry that was opened on an Apple device,
        and for a resource name or UID that was deleted; writing the same name again
        does not help there, so the 412 is raised as CalDavStatusError for the caller,
        which can write the event under another name and UID.
        """
        if RESOURCE_NAME.fullmatch(name) is None:
            raise ValueError("Resource names must be letters, digits and hyphens ending in .ics")
        href = collection_url(calendar_url) + name
        response = await self._request(
            "PUT", href, headers={"Content-Type": ICS_CONTENT_TYPE}, body=ics.encode("utf-8"), resource=True
        )
        if response.status in (200, 201, 204):
            return href
        condition = _error_condition(response.body)
        excerpt = None if condition else self._excerpt(response.body)
        if response.status in (403, 409) and condition:
            raise CalDavRefusedError(response.status, condition)
        if response.status == 403:
            raise CalDavAuthError(response.status, excerpt=excerpt)
        if response.status in (404, 409):
            raise CalDavNotFoundError(response.status, excerpt=excerpt)
        raise CalDavStatusError(response.status, condition, excerpt)

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
        excerpt = None if condition else self._excerpt(response.body)
        if response.status in (403, 409) and condition:
            raise CalDavRefusedError(response.status, condition)
        if response.status == 403:
            raise CalDavAuthError(response.status, excerpt=excerpt)
        raise CalDavStatusError(response.status, condition, excerpt)
