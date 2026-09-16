# Worked example: football call-ups in the family calendar

This is a complete setup, end to end: what the source calendar holds, what the relay is set to, what it
writes to iCloud, and what the family sees on their phones. Names, addresses and coordinates here are
invented; everything else is what the integration actually produces.

**The goal:** when a child is called up (*udtaget*) for a match or a tournament, an entry appears in the
shared iCloud **Family** calendar with the venue on a map and when to leave. When the match is moved, the
entry moves. When the call-up is withdrawn, the entry disappears.

## What you need

- [KampKlar](https://github.com/FrederikLeed/kampklar-ha) 0.8.0 or newer, set up with the child's DBU
  account. It gives each child a calendar entity and marks call-ups.
- Calendar Relay with an iCloud account entry (Apple Account email plus an app-specific password, see the
  [README](../README.md#icloud)).
- Optional: the **Waze Travel Time** integration, for the leave line and Apple's travel block.

## 1. The source event

KampKlar writes one event per activity on `calendar.emma_kalender`. A call-up looks like this (from
**Developer tools** > **Actions** > `calendar.get_events`):

| Field | Value |
|-------|-------|
| Summary | `⭐ Udtaget: Stævne: Testby Hallen` |
| Start / end | `2026-09-19 10:40` / `2026-09-19 11:50` |
| Location | `Testby Hallen`<br>`Hallevej 1, 1234 Testby` |
| Description | `Type: DBU-Stævne`<br>`Hold: U9 Piger Årgang 2018`<br>`Status: Udtaget`<br>`Tilmeldte: 6`<br>`Kort: https://maps.apple.com/?ll=55.123456,10.654321&q=Testby%20Hallen` |

Two things matter for the relay: the `⭐ Udtaget: ` prefix, which only call-ups get, and the `Kort:` link,
which carries the coordinates the relay needs for the map and the travel time.

## 2. The relay

**Settings** > **Devices & services** > **Calendar Relay** > the iCloud account > **Add relay**:

| Field | Value | Why |
|-------|-------|-----|
| Source calendar | `calendar.emma_kalender` | The child's KampKlar calendar. |
| Target calendar | Family | The shared iCloud calendar everyone in the house sees. |
| Title filter | `⭐ Udtaget: ` | Only call-ups. Training and matches she is not selected for stay out. |
| Remove filter text from title | On | The Family calendar does not need the marker. |
| Title prefix | `⚽ Emma: ` | Whose event it is, at a glance. Keep the trailing space. |
| Look-ahead | 60 days | The season as far as DBU has planned it. |
| Place details for Apple Calendar | On | The map on the iPhone. |
| Travel time | Waze Travel Time | The leave line and the travel block. |
| Waze region | Europe | |
| Arrive early | 15 min | She likes to be there a quarter of an hour before. |
| Leave reminder | Off | On iCloud the alert would only ring on the account the relay uses. |

One relay per child; they can share the same target calendar.

## 3. What the relay writes

The entry the relay PUTs into the Family calendar, rendered by the integration itself:

```text
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//calendar-relay//Calendar Relay for Home Assistant//EN
BEGIN:VTIMEZONE
TZID:Europe/Copenhagen
BEGIN:DAYLIGHT
DTSTART:19700329T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
TZNAME:CEST
END:DAYLIGHT
BEGIN:STANDARD
DTSTART:19701025T030000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
TZNAME:CET
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:6820018@calendar-relay
DTSTAMP:20260916T053000Z
DTSTART;TZID=Europe/Copenhagen:20260919T104000
DTEND;TZID=Europe/Copenhagen:20260919T115000
SUMMARY:⚽ Emma: Stævne: Testby Hallen
DESCRIPTION:Leave at 10:10 (about 15 min drive + 15 min early)\nType: DBU-S
 tævne\nHold: U9 Piger Årgang 2018\nStatus: Udtaget\nTilmeldte: 6\nKort: 
 https://maps.apple.com/?ll=55.123456\,10.654321&q=Testby%20Hallen
LOCATION:Testby Hallen\nHallevej 1\, 1234 Testby
X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-ADDRESS="Hallevej 1, 1234 Testby";X
 -APPLE-RADIUS=71;X-TITLE="Testby Hallen":geo:55.123456,10.654321
X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT30M
END:VEVENT
END:VCALENDAR
```

Reading it back:

- The title lost the filter text and gained the prefix.
- Times are written in Home Assistant's own time zone with a `VTIMEZONE`, so Apple Calendar shows 10:40
  without a second GMT line.
- `LOCATION` is the venue name on the first line and the address on the second, and
  `X-APPLE-STRUCTURED-LOCATION` repeats it with the coordinates, which is what draws the map.
- The description starts with the leave line: 15 minutes of driving plus 15 minutes of arriving early,
  counted back from 10:40.
- `X-APPLE-TRAVEL-DURATION` covers both, so the travel block in Apple Calendar starts at 10:10.

## 4. On the phone

Everyone who can see the Family calendar gets the event with the map and the leave line in the notes. The
travel block and any alert belong to the Apple Account the relay writes with, so they show on that
account's devices; other members can switch on Apple's own **Time to Leave** instead. The
[README](../README.md#what-shows-on-an-iphone) covers what is shared and what is per person.

## 5. When things change

| What happens in KampKlar | What the relay does |
|--------------------------|---------------------|
| The match is moved to another time | Rewrites the entry in place; the travel time is asked again for the new start. |
| The venue changes | New address, new map, new travel time. |
| The call-up is withdrawn | The source title loses `⭐ Udtaget: `, so it no longer matches the filter and the entry is deleted, as long as it has not started yet. |
| The child is called up for another match | A new entry, one per activity. |
| Nothing changed | Nothing is written. The relay only PUTs when the rendered event differs from what it wrote last. |

Traffic before kick-off is picked up too: within three hours of the start the relay asks Waze once more
with live traffic, and rewrites the entry only if the rounded travel time changed.

## A smaller version

The same shape works without KampKlar or Waze. To put one child's school events in the family calendar:

| Field | Value |
|-------|-------|
| Source calendar | `calendar.school_emma` |
| Target calendar | Family |
| Title filter | (empty, relay everything) |
| Title prefix | `🎒 Emma: ` |
| Look-ahead | 30 days |
| Travel time | Off |

That relays every event on the source calendar, prefixed, and keeps it in step.
