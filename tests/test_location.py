"""Tests for finding coordinates in event text. All places and coordinates are invented."""

from __future__ import annotations

import pytest

from custom_components.calendar_relay.location import Coordinates, find_coordinates


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # geo: URIs (RFC 5870), with altitude, parameters and the Android q parameter.
        ("geo:55.123456,10.654321", Coordinates(55.123456, 10.654321)),
        ("GEO:-33.5,151.25", Coordinates(-33.5, 151.25)),
        ("geo:55.1,10.2,42.5", Coordinates(55.1, 10.2)),
        ("geo:55.1,10.2;u=35", Coordinates(55.1, 10.2)),
        ("geo:55.1,10.2;crs=WGS84;u=35", Coordinates(55.1, 10.2)),
        ("geo:55.1,10.2?q=Example+Park", Coordinates(55.1, 10.2, "Example Park")),
        ("geo:0,0?q=55.1,10.2(Example%20Park)", Coordinates(55.1, 10.2, "Example Park")),
        ("geo:0,0?q=55.1,10.2", Coordinates(55.1, 10.2)),
        # Apple Maps: legacy links, and the unified URLs of iOS 18.4 and later.
        (
            "Kort: https://maps.apple.com/?ll=55.123456,10.654321&q=Example%20Stadium",
            Coordinates(55.123456, 10.654321, "Example Stadium"),
        ),
        ("https://maps.apple.com/?q=Example+Stadium&ll=55.1%2C10.2", Coordinates(55.1, 10.2, "Example Stadium")),
        ("https://maps.apple.com/?ll=55.1,10.2&q=+", Coordinates(55.1, 10.2)),
        (
            "https://maps.apple.com/place?coordinate=55.1,10.2&name=Example%20Hall",
            Coordinates(55.1, 10.2, "Example Hall"),
        ),
        ("https://maps.apple.com/directions?destination=55.1,10.2&mode=driving", Coordinates(55.1, 10.2)),
        ("https://maps.apple.com/?daddr=55.1,10.2&dirflg=d", Coordinates(55.1, 10.2)),
        ("https://maps.apple.com/?q=55.1,10.2", Coordinates(55.1, 10.2)),
        # Google Maps: query parameters (URL-encoded or not) and @lat,lon in the path.
        ("https://www.google.com/maps/search/?api=1&query=55.1%2C10.2", Coordinates(55.1, 10.2)),
        ("https://www.google.dk/maps?q=55.1,10.2", Coordinates(55.1, 10.2)),
        ("https://maps.google.com/?q=55.1,+10.2", Coordinates(55.1, 10.2)),
        ("https://www.google.com/maps/dir/?api=1&destination=55.1,10.2", Coordinates(55.1, 10.2)),
        ("https://www.google.com/maps/@55.1,10.2,15z", Coordinates(55.1, 10.2)),
        ("https://www.google.co.uk/maps/place/Example+Park/@-33.5,151.25,17z/data=!3m1", Coordinates(-33.5, 151.25)),
        # A place's pin is in the data segment; the @ point is only where the map is centred.
        (
            "https://www.google.com/maps/place/Example+Hall/@55.1,10.2,17z/data=!3m1!4b1!4m6!3m5!1s0x0:0x0"
            "!8m2!3d55.1001!4d10.2002!16s",
            Coordinates(55.1001, 10.2002),
        ),
        # Directions lead to the last waypoint, never to the @ point halfway along the route.
        ("https://www.google.com/maps/dir/55.0,10.0/55.1,10.2/@55.05,10.1,12z", Coordinates(55.1, 10.2)),
        ("https://maps.google.com/maps/dir//55.1,+10.2/@55.05,10.1,12z/data=!4m2!4m1!3e0", Coordinates(55.1, 10.2)),
        # OpenStreetMap: the marker, not the view.
        ("https://www.openstreetmap.org/?mlat=55.1&mlon=10.2#map=17/56.0/11.0", Coordinates(55.1, 10.2)),
        ("https://osm.org/?mlon=10.2&mlat=55.1", Coordinates(55.1, 10.2)),
        # The edges of the valid ranges.
        ("geo:90,180", Coordinates(90, 180)),
        ("geo:-90,-180", Coordinates(-90, -180)),
    ],
)
def test_link_formats(text: str, expected: Coordinates) -> None:
    """Every supported format gives its coordinates and, where the link has one, the place name."""
    assert find_coordinates(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "geo:91,10",
        "geo:55,181",
        "geo:-90.000001,10",
        "geo:55.1;10.2",
        "geo:55.1",
        "geo:55.1,10.2,x",
        "geo:abc,def",
        "geo:55.1,10.2;crs=moon-2011",
        "geo:0,0?q=Example+Road+1",
        # 0,0 is what generators write without coordinates, not a place.
        "geo:0,0",
        "geo:0.0,-0.0;u=10",
        "geo:0,0?q=0,0(Nowhere)",
        "https://maps.apple.com/?ll=0,0&q=Nowhere",
        "https://www.google.com/maps/@0,0,15z",
        "https://www.openstreetmap.org/?mlat=0&mlon=0",
        "geo:91,10?q=55.1,10.2(Example)",
        # Directions to a named place, or without a destination: the @ point is not used.
        "https://www.google.com/maps/dir/55.0,10.0/Example+Stadium/@55.05,10.1,12z",
        "https://www.google.com/maps/dir/@55.05,10.1,12z",
        "https://www.google.com/maps/place/Example+Hall/@55.1,10.2,17z/data=!8m2!3d95.1!4d10.2",
        "https://maps.apple.com/?ll=95.0,10.2&q=Nowhere",
        "https://maps.apple.com/?q=Example+Stadium",
        "https://maps.apple.com/search?query=Stadium&center=55.1,10.2",
        "https://maps.apple.com/?sll=55.1,10.2",
        "https://maps.apple.com.example.com/?ll=55.1,10.2",
        "https://www.google.com/maps?ll=55.1,10.2",
        "https://www.google.com/search?q=55.1,10.2",
        "https://www.google.com/maps/place/Example+Park",
        "https://www.openstreetmap.org/#map=17/55.1/10.2",
        "https://www.openstreetmap.org/?mlat=55.1",
        "https://www.openstreetmap.org/?mlat=55.1&mlon=190",
        "https://example.com/?ll=55.1,10.2",
        "https://[not-a-host/?ll=55.1,10.2",
        "ll=55.1,10.2",
        "Meet at 10:00 at pitch 55,10",
        "",
    ],
)
def test_invalid_or_unsupported_values(text: str) -> None:
    """Out of range numbers, other separators, other sites and viewport-only parameters give nothing."""
    assert find_coordinates(text) is None


def test_first_valid_match_wins() -> None:
    """Candidates are tried from left to right; an invalid one is skipped."""
    text = "Old: geo:95,10, new: https://maps.apple.com/?ll=55.1,10.2&q=First and geo:56.2,11.3"
    assert find_coordinates(text) == Coordinates(55.1, 10.2, "First")


def test_texts_are_searched_in_order() -> None:
    """The description is searched before the location; missing texts are skipped."""
    assert find_coordinates("geo:55.1,10.2", "geo:56.2,11.3") == Coordinates(55.1, 10.2)
    assert find_coordinates(None, "Example Park geo:56.2,11.3") == Coordinates(56.2, 11.3)
    assert find_coordinates("No link here", "") is None
    assert find_coordinates() is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("(see https://maps.apple.com/?ll=55.1,10.2&q=Example%20Park).", Coordinates(55.1, 10.2, "Example Park")),
        ('<a href="https://maps.apple.com/?q=Example&amp;ll=55.1,10.2">map</a>', Coordinates(55.1, 10.2, "Example")),
        ("Pin: geo:55.1,10.2.", Coordinates(55.1, 10.2)),
        ("Map:\nhttps://www.openstreetmap.org/?mlat=55.1&mlon=10.2\nBring water", Coordinates(55.1, 10.2)),
        # A parenthesis the link opened belongs to it; one that closes text around the link does not.
        ("https://maps.apple.com/?ll=55.1,10.2&q=Hall%20(North)", Coordinates(55.1, 10.2, "Hall (North)")),
        ("(https://maps.apple.com/?ll=55.1,10.2&q=Hall%20(North)).", Coordinates(55.1, 10.2, "Hall (North)")),
        ("[Map](https://maps.apple.com/?ll=55.1,10.2&q=Example)", Coordinates(55.1, 10.2, "Example")),
        ("[https://maps.apple.com/?ll=55.1,10.2&q=Hall%20[B]]", Coordinates(55.1, 10.2, "Hall [B]")),
        ("{geo:0,0?q=55.1,10.2(Example%20Park)}", Coordinates(55.1, 10.2, "Example Park")),
        ("See geo:0,0?q=55.1,10.2(Example%20Park", Coordinates(55.1, 10.2, "Example Park")),
    ],
)
def test_links_inside_text(text: str, expected: Coordinates) -> None:
    """Trailing punctuation, HTML-escaped ampersands and surrounding lines do not get in the way."""
    assert find_coordinates(text) == expected
