"""Find coordinates in event text: geo: URIs and map links from Apple Maps, Google Maps and OpenStreetMap.

No Home Assistant imports: the relay passes the event's description and location text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

_NUMBER = r"[-+]?\d{1,3}(?:\.\d+)?"
_PAIR = re.compile(rf"\s*({_NUMBER})\s*,\s*({_NUMBER})\s*")
# An Android style query: q=lat,lon(label). The closing parenthesis may be missing.
_PAIR_WITH_LABEL = re.compile(rf"\s*({_NUMBER})\s*,\s*({_NUMBER})\s*(?:\((.*?)\)?)?\s*", re.DOTALL)
_ALTITUDE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_AT_PAIR = re.compile(rf"@({_NUMBER}),({_NUMBER})(?=[,/]|$)")
# The pin of a Google Maps place, in the data segment of its path.
_DATA_PAIR = re.compile(rf"!3d({_NUMBER})!4d({_NUMBER})")
_CANDIDATE = re.compile(r"\bgeo:[^\s<>\"]+|\bhttps?://[^\s<>\"]+", re.IGNORECASE)
# Punctuation after a link that ends a sentence, and brackets that close one opened before the link.
_TRAILING = ".,;:!?'"
_CLOSING = {")": "(", "]": "[", "}": "{"}
_GOOGLE_HOST = re.compile(r"(?:www\.)?google(?:\.[a-z]{2,3}){1,2}")
_GOOGLE_MAPS_HOST = re.compile(r"maps\.google(?:\.[a-z]{2,3}){1,2}")
_OSM_HOSTS = frozenset({"openstreetmap.org", "www.openstreetmap.org", "osm.org", "www.osm.org"})
_APPLE_HOSTS = frozenset({"maps.apple.com"})
# Parameters that hold the place itself, in the order they are tried. Viewport parameters (Apple's
# search center and sll, Google's ll and center) only say where the map looks, so they are left out.
_APPLE_PLACE_PARAMS = ("ll", "coordinate", "destination", "daddr", "q")
_GOOGLE_PLACE_PARAMS = ("query", "q", "destination")


@dataclass(frozen=True, slots=True)
class Coordinates:
    """A point in WGS 84, with the place name the link carried, if any."""

    latitude: float
    longitude: float
    name: str | None = None


def _in_range(latitude: float, longitude: float) -> bool:
    """Return True when latitude is -90 to 90 and longitude -180 to 180."""
    return -90 <= latitude <= 90 and -180 <= longitude <= 180


def _valid(latitude_text: str, longitude_text: str, name: str | None = None) -> Coordinates | None:
    """Return coordinates when both numbers are in range and not both zero.

    0,0 is open sea, and link generators write it when they have no coordinates, so it is not a place.
    """
    latitude = float(latitude_text)
    longitude = float(longitude_text)
    if not _in_range(latitude, longitude) or (latitude == 0 and longitude == 0):
        return None
    name = name.strip() if name else None
    return Coordinates(latitude, longitude, name or None)


def _pair(value: str | None, name: str | None = None) -> Coordinates | None:
    """Return the coordinates of a 'lat,lon' value."""
    if value is None:
        return None
    match = _PAIR.fullmatch(value)
    if match is None:
        return None
    return _valid(match.group(1), match.group(2), name)


def _first(query: dict[str, list[str]], name: str) -> str | None:
    """Return the first value of a query parameter."""
    values = query.get(name)
    return values[0] if values else None


def _from_geo_uri(uri: str) -> Coordinates | None:
    """Parse geo:lat,lon[,alt][;params][?q=...] (RFC 5870, plus the Android q parameter)."""
    body, _, query_text = uri[4:].partition("?")
    path, *params = body.split(";")
    for param in params:
        key, _, value = param.partition("=")
        if key.strip().lower() == "crs" and value.strip().lower() != "wgs84":
            return None
    parts = path.split(",")
    if len(parts) not in (2, 3) or (len(parts) == 3 and not _ALTITUDE.fullmatch(parts[2])):
        return None
    point = _PAIR.fullmatch(f"{parts[0]},{parts[1]}")
    if point is None or not _in_range(float(point.group(1)), float(point.group(2))):
        return None
    label = _first(parse_qs(query_text), "q")
    labelled = _PAIR_WITH_LABEL.fullmatch(label) if label is not None else None
    if labelled is not None:
        # geo:0,0?q=lat,lon(label): the point is a placeholder and the query holds the place.
        return _valid(labelled.group(1), labelled.group(2), labelled.group(3))
    # _valid rejects 0,0, so geo:0,0?q=<address>, a search, gives nothing.
    return _valid(point.group(1), point.group(2), label)


def _from_url(url: str) -> Coordinates | None:
    """Parse an Apple Maps, Google Maps or OpenStreetMap link. Query values are URL-decoded first."""
    try:
        parts = urlsplit(url.replace("&amp;", "&"))
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    query = parse_qs(parts.query)
    if host in _APPLE_HOSTS:
        name = _first(query, "q") or _first(query, "name")
        for param in _APPLE_PLACE_PARAMS:
            point = _pair(_first(query, param), None if param == "q" else name)
            if point is not None:
                return point
        return None
    if _GOOGLE_MAPS_HOST.fullmatch(host) or (_GOOGLE_HOST.fullmatch(host) and parts.path.startswith("/maps")):
        for param in _GOOGLE_PLACE_PARAMS:
            point = _pair(_first(query, param))
            if point is not None:
                return point
        return _from_google_path(unquote(parts.path))
    if host in _OSM_HOSTS:
        latitude = _first(query, "mlat")
        longitude = _first(query, "mlon")
        if latitude is None or longitude is None:
            return None
        return _pair(f"{latitude},{longitude}")
    return None


def _from_google_path(path: str) -> Coordinates | None:
    """Parse the place in the path of a Google Maps link.

    Directions (/maps/dir/<from>/<to>/@<view>) lead to their last waypoint, which only counts when
    it is a coordinate pair. A place (/maps/place/<name>/@<view>/data=...!3d<lat>!4d<lon>) has its
    pin in the data segment. Elsewhere, and for a place without a pin, the @ point is used.
    """
    segments = [segment for segment in path.split("/") if segment]
    if "dir" in segments[:2]:
        after = segments[segments.index("dir") + 1 :]
        waypoints = [segment for segment in after if not segment.startswith(("@", "data="))]
        return _pair(waypoints[-1]) if waypoints else None
    if "place" in segments[:2] and (pins := _DATA_PAIR.findall(path)):
        return _valid(*pins[-1])
    match = _AT_PAIR.search(path)
    return _valid(match.group(1), match.group(2)) if match is not None else None


def _trim(candidate: str) -> str:
    """Remove punctuation after a link: sentence punctuation, and closing brackets the link did not open.

    A place name such as Hall (North) keeps its parenthesis, and a link inside parentheses loses the
    one that closes them. A period is always removed, as linkifiers do, so a name ending in a period
    that is not percent-encoded loses it.
    """
    while (last := candidate[-1]) in _TRAILING or (
        last in _CLOSING and candidate.count(last) > candidate.count(_CLOSING[last])
    ):
        candidate = candidate[:-1]
    return candidate


def find_coordinates(*texts: str | None) -> Coordinates | None:
    """Return the first valid coordinates in texts, searched in the order given and from left to right.

    Links or URIs with values out of range, or that are not map links, are skipped.
    """
    for text in texts:
        if not text:
            continue
        for match in _CANDIDATE.finditer(text):
            candidate = _trim(match.group(0))
            found = _from_geo_uri(candidate) if candidate[:4].lower() == "geo:" else _from_url(candidate)
            if found is not None:
                return found
    return None
