# Calendar Relay for Home Assistant

<img src="custom_components/calendar_relay/brand/icon.png" alt="Calendar Relay" width="110" align="right">

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Calendar Relay pushes events from Home Assistant calendar entities into a CalDAV calendar (iCloud,
Nextcloud, Radicale and other CalDAV servers) and keeps them in step. It works one way and without churn:
an event is only written when it is new or has changed, and it is removed when it no longer belongs.
For Apple Calendar it can also add the exact place of an event and when to leave for it.

## Example: football call-ups in the family calendar

The [KampKlar](https://github.com/FrederikLeed/kampklar-ha) integration gives each child a calendar and
marks a match the child is selected for with the title prefix `⭐ Udtaget: `. A parent adds one relay per
child:

| Setting | Value |
|---------|-------|
| Source calendar | Emma's KampKlar calendar |
| Target calendar | Family (the shared iCloud Family calendar) |
| Title filter | `⭐ Udtaget: ` |
| Remove filter text from title | On |
| Title prefix | `⚽ Emma: ` |

The call-up `⭐ Udtaget: Home - Away` now shows in everyone's Family calendar as `⚽ Emma: Home - Away`.
Training sessions and matches Emma is not selected for stay out. When the match is rescheduled, the entry
moves. When the call-up is withdrawn, the source title loses the `⭐ Udtaget: ` prefix, no longer matches
the filter, and the entry disappears from the Family calendar.

From KampKlar 0.7.0 a match carries its address as location and a `Kort: https://maps.apple.com/?ll=...`
link in its description. [Place details](#location-details), on by default, then give the entry a map in
Apple Calendar, and with [travel time](#travel-time) from Waze Travel Time its notes start with
`Leave at 09:35 (about 25 min drive)`. If KampKlar starts events at the meeting time, the leave time is
counted back from the meeting time.

## Installation

### HACS

1. Open HACS, then the three-dot menu > **Custom repositories**.
2. Add `https://github.com/FrederikLeed/ha-calendar-relay` with the category **Integration**.
3. Install **Calendar Relay** and restart Home Assistant.

### Manual

Copy `custom_components/calendar_relay/` into your `config/custom_components/` directory and restart.

Home Assistant 2026.3.0 or newer is required.

## Setup

### 1. Add a CalDAV account

**Settings** > **Devices & services** > **Add integration** > **Calendar Relay**, then enter the server
URL, username and password. The integration signs in and lists your calendars to check the details. Each
account is one entry; add as many as you need.

#### iCloud

iCloud does not accept your normal Apple Account password from other apps. Use an app-specific password:

1. Make sure two-factor authentication is on for your Apple Account.
2. Sign in at [appleid.apple.com](https://appleid.apple.com), open **Sign-In and Security** and choose
   **App-Specific Passwords**.
3. Create a password and give it a name you will recognise, such as "Home Assistant". It looks like
   `abcd-efgh-ijkl-mnop`.
4. In Calendar Relay, keep the server URL `https://caldav.icloud.com/`, enter your Apple Account email
   address as the username and the app-specific password as the password.

Changing your Apple Account password revokes all app-specific passwords. Home Assistant then asks you to
reauthenticate; create a new app-specific password and enter it there.

#### Other servers

Use the address your server documents for CalDAV clients, for example
`https://cloud.example.com/remote.php/dav` for Nextcloud or `http://radicale.local:5232/` for Radicale.
The root of a server that supports `/.well-known/caldav` also works.

### 2. Add a relay

Open the account entry and choose **Add relay**:

| Field | Meaning |
|-------|---------|
| Source calendar | Any calendar entity in Home Assistant. |
| Target calendar | A calendar on the account. Only calendars that accept events, and that you may write to when the server says so, are offered. Reminders lists are left out. |
| Title filter | Only events whose title contains this text are relayed. Case does not matter. Empty relays everything. |
| Remove filter text from title | Removes the filter text from the relayed title, plus separators such as `:` and spaces left in front. |
| Title prefix | Put in front of every relayed title. Include the trailing space you want. |
| Look-ahead | How many days ahead to relay, 1 to 365 (default 60). |
| Place details for Apple Calendar | Writes the exact place for Apple Calendar when the event has coordinates. On by default. See [Location details](#location-details). |
| Travel time | Off (default), Fixed minutes or Waze Travel Time. See [Travel time](#travel-time). |
| Fixed travel time | Minutes of travel for Fixed minutes, 1 to 480 (default 15). |
| Waze region | The Waze server region for Waze Travel Time: Europe (default), United States, North America, Israel or Australia. |
| Extra buffer | Minutes added to the travel time, for example for parking, 0 to 120 (default 0). |
| Leave reminder | An alert at the leave time. Off by default. See [Leave reminder](#leave-reminder) for who hears it. |

Relays created with an earlier version get the defaults above. Nothing is rewritten on upgrade unless an
event has coordinates, which then get their place details once.

A relay can be changed later with **Reconfigure** on the relay. Each relay gets a device with two entities:

- **Sync now** button: runs a sync right away.
- **Last sync** sensor (diagnostic): the time of the last sync that kept the target calendar in step, with
  the attributes `relayed_events` (how many relayed events are tracked) and `last_error` (text, or empty).
  A travel time problem shows in `last_error` without holding back the time, because the events themselves
  are in step.

## Location details

Home Assistant calendar events have no field for coordinates, so the relay looks for them in the event's
description first, then in its location, and uses the first valid one it finds:

| Format | Examples |
|--------|----------|
| geo: URI | `geo:55.123456,10.654321` |
| Apple Maps | `https://maps.apple.com/?ll=55.123456,10.654321&q=Example%20Stadium`, `https://maps.apple.com/place?coordinate=55.123456,10.654321&name=Example%20Stadium`, `https://maps.apple.com/directions?destination=55.123456,10.654321` |
| Google Maps | `https://www.google.com/maps/search/?api=1&query=55.123456%2C10.654321`, `https://www.google.com/maps/@55.123456,10.654321,15z` |
| OpenStreetMap | `https://www.openstreetmap.org/?mlat=55.123456&mlon=10.654321` |

Latitude must be between -90 and 90 and longitude between -180 and 180, and 0,0 is skipped, because link
generators write it when they have no coordinates; the search then goes on to the next link, and Waze uses
the location text. Parameters that only say where a map is centred, such as Apple's search `center`,
Google's `ll` and the OpenStreetMap `#map=` view, are not used, because they are not the place. For the
same reason a Google place link uses the pin in its `data=` part, and a Google directions link uses its
last waypoint when that is a coordinate pair, not the `@` point where the map is centred. Short links such
as `maps.app.goo.gl` carry no coordinates.

With coordinates, the relayed event keeps its plain `LOCATION` and gets Apple's structured location right
after it:

```text
LOCATION:Example Stadium\nExample Road 1\, 1234 Sampletown
X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-ADDRESS="Example Road 1, 1234 Sampletown";X-APPLE-RADIUS=71;X-TITLE="Example Stadium":geo:55.123456,10.654321
```

The first line of the location is the title and any further lines are the address, the way Apple Calendar
writes a place; a one-line location is the title alone. An event without a location uses the place name
from the link (`q` or `name`) as the title, and gets no structured location if the link has no name.

On an iPhone or Mac the event then shows a map of the exact place, and Calendar can base travel time and
Time to Leave on it instead of guessing from the text. Apple's calendar server keeps this property on the
shared event rather than per person, so every member of a shared calendar gets it. Other calendar apps
ignore it; turn the setting off if you do not use Apple Calendar.

Limits:

- Apple has never documented its `X-APPLE-` calendar properties. The format follows what Apple's own
  apps and calendar server write, and it may change.
- Radicale, and other servers built on the vobject library, cut the value off at the comma, so the copy
  stored there only keeps the latitude. Apple devices syncing such a server get no map.
- The map follows the location text: when it changes, the relay writes the new title with it.

## Travel time

Travel time adds when to leave to events that have a start time and a place, meaning coordinates or a
location text. All-day events and events without a place get none. An event that has already started gets
none when it is first relayed or when it changes; an event relayed before it started keeps what it was
written with, so the start never rewrites it.

- **Fixed minutes**: the same number of minutes for every event.
- **Waze Travel Time**: the driving time from Home Assistant's home location (**Settings** > **System** >
  **General**) to the event's coordinates, or to its location text when it has none.

The relayed event then gets:

- A first line in its description, in Home Assistant's language: `Leave at 11:35 (about 25 min drive)`,
  or in Danish `Afgang: 11:35 (ca. 25 min. kørsel)`. The leave time is the start minus the travel time
  and the extra buffer, in Home Assistant's time zone. When it falls on the day before the start, the line
  says so: `Leave at 23:45 the day before (about 30 min drive)`, or `Afgang: 23:45 dagen før (ca. 30 min.
  kørsel)`.
- Apple's `X-APPLE-TRAVEL-DURATION`, covering the travel time plus the buffer, so Apple Calendar draws a
  travel block that starts at the leave time.

The home location is never written into events.

### Setting up Waze Travel Time

1. **Settings** > **Devices & services** > **Add integration** > **Waze Travel Time**, and add an entry.
   Any origin and destination will do; the relay only uses the integration's
   `waze_travel_time.get_travel_times` action. From Home Assistant 2026.8 the action is there whenever the
   integration is loaded; before that, only while a Waze Travel Time entry is loaded.
2. Reconfigure the relay, choose **Waze Travel Time** and the Waze region.

The relay form shows an error when Waze Travel Time is chosen while the action is not available.

When Waze is asked:

- Once for each event, without live traffic, when it is first relayed. The answer is rounded up to
  5 minutes.
- Once more, with live traffic, when the event starts within 3 hours, or from 1 hour before its leave time
  when that is earlier (trips of more than 2 hours), but not once the leave time has passed. An event that
  is already within 3 hours of its start when it is first relayed is asked once, with live traffic. An event
  that moves to another start gets one live update for the new start.
- Again when the event's destination, the home location or the Waze region changes. A new start time alone
  does not ask again without live traffic. After a new home location or region, an event keeps its travel
  time until Waze answers; after a new destination it has none until then.
- An event is only rewritten when its rounded travel time changes.

Waze is asked for the nearest events first, with at most 10 calls in one sync; the rest follow in the next
syncs. The calls of all relays are made one at a time with half a second between them, and each may take
30 seconds. Events and relays with the same destination share an answer for 15 minutes.

If the action is missing or fails, the event keeps its last travel time for that destination (or gets
none), the reason shows in the Last sync sensor's `last_error`, and the rest of the sync goes on, with Waze
still asked for other events. An event whose travel time failed is asked again after 1 hour, then after 2,
4, 8 and 16 hours, and then once a day while it keeps failing; a new destination, home location or region
asks again at once. A sync stops asking Waze after 3 failures in a row, a call that takes too long, or when
the action is missing. An answer of more than 8 hours counts as a failure. Stored travel times and retry
times survive restarts.

Waze Travel Time uses Waze's live map service, which is not an official API and has no published limits.
Keep the look-ahead reasonable when a relay has many events.

### What shows on an iPhone

- **Everyone who can see the calendar** sees the leave line at the top of the event's notes and, with
  place details, the map.
- **The travel block** may only show on devices signed in to the Apple Account the relay uses. Apple's
  calendar server keeps travel time as private data of the account that wrote it, and iCloud users report
  that travel time on a shared calendar does not show for other members. This has not been checked with
  two Apple Accounts.
- **Time to Leave** alerts are worked out by each iPhone from Apple Maps, with live traffic and from where
  that phone is, and only for events whose details show a map. The relay cannot switch them on or time
  them for anyone. A member who wants them needs:
  - **Settings** > **Apps** > **Calendar** > **Default Alert Times** > **Time to Leave** on,
  - Location Services on, with Calendar allowed to use the location while using the app and Precise
    Location on,
  - **Settings** > **Privacy & Security** > **Location Services** > **System Services** >
    **Location-Based Alerts** on.
- The leave line is an estimate from home made in advance. Time to Leave on a phone is live and starts
  from wherever the phone is, so the two will not always agree.
- Apple gives no Time to Leave alert for trips of more than about three hours; the leave line is then the
  only hint.

## Leave reminder

With **Leave reminder** on, every event that has a travel time also gets an alert (`VALARM`) at the leave
time. It is set relative to the start, so it moves with the event.

Who hears it:

- **iCloud**: an alert on a shared calendar belongs to the account that set it, so it only rings on
  devices signed in to the Apple Account the relay uses, not for the other members. They can turn on Time
  to Leave or add their own alert.
- **Radicale and other servers without personal alerts**: every device that syncs the calendar gets it.
- **Nextcloud** removes alerts from calendars shared read-only.

Every rewrite of an event replaces the relay account's own alerts on it, including an alert added by hand
on a device signed in to that account. That was already so before this setting existed, when events were
written without alerts.

## How it works

- **When**: a relay syncs once Home Assistant has started, about 30 seconds after the source calendar's
  state or attributes change, every 15 minutes, and when you press **Sync now**. Only one sync runs per
  relay at a time.
- **What is read**: events that have not ended yet, up to the look-ahead. The relay calls the calendar
  entity directly, because the `calendar.get_events` action leaves out the event's unique id.
- **Identity**: a source event is identified by its uid (plus its recurrence id for a recurring event).
  Each relayed event gets its own resource `relay-<id>.ics` and UID `<id>@calendar-relay`, so a changed
  event is replaced in place.
- **What is written**: the title (filtered and prefixed), start and end, description and location, and
  with the settings above Apple's structured location, the leave line, Apple's travel duration and an
  alert. Timed events are written in UTC, all-day events as dates. No organizer or attendees are written,
  so nobody gets an invitation.
- **Sync state**: what was written where is kept in Home Assistant's storage
  (`.storage/calendar_relay.<relay id>`), so nothing is rewritten after a restart. Waze travel times are
  kept there too: the minutes, when they were computed, whether with live traffic, the event start they
  were computed for, and hashes of what they were computed for instead of the addresses; so is when an
  event whose travel time failed is asked again, and why it failed.
- **Safety**: if the source calendar is missing, unavailable or fails to read, the sync is skipped and
  nothing is deleted. If a source that is available suddenly returns no events at all, relayed events are
  only removed when a sync at least 10 minutes later still sees nothing, so a source that briefly returns
  an empty list does not empty the target calendar. A write or delete only ever targets an `.ics` resource
  inside the calendar it was written to, also when the server redirects the request.
- **Start-up**: nothing syncs until Home Assistant has started, because source calendars often report a
  state before their data is loaded.

## Errors and repairs

- The server answers 401, or 403 without a reason: Home Assistant asks you to reauthenticate the account.
- The target calendar is gone (404, or 409 without a reason, when writing), or it answers 403 and is not a
  calendar of this account (for example after the account was reconfigured to another user): a repair
  issue names the relay. Reconfigure the relay or restore the calendar; the issue disappears after the next
  successful sync.
- The server refuses one event with a reason (403 or 409 with a CalDAV precondition, such as a UID that
  already exists in another calendar): only that event fails, the rest of the sync goes on, and the next
  sync tries it again.
- Waze Travel Time is missing or fails: events keep their last travel time, and `last_error` starts with
  `Travel time:` and says why. See [Travel time](#travel-time).
- Anything else (timeouts, rate limits, server errors): a warning in the log, and the next sync tries again.

## Limits

- One way only. Changes made to a relayed event in the target calendar are kept until the source event
  changes, then overwritten.
- An event you delete by hand from the target calendar is not recreated unless its source event changes.
- A withdrawn event is deleted only if it has not started yet. Running and past events stay in the target
  calendar.
- Removing a relay, or the whole account, leaves its events in the target calendar. To remove them first,
  set the relay's title filter to text that matches nothing, press **Sync now**, then remove the relay.
  Events that have already started stay.
- Changing a relay's target calendar moves its future events: they are deleted from the old calendar and
  written to the new one. If the new calendar refuses an event, it is put back into the old calendar and
  stays there until it changes or Home Assistant restarts. Copies the relay can no longer reach (a server
  address or account that changed) stay in the old calendar.
- Recurring events are relayed as separate single events, one per occurrence inside the look-ahead.
- Source events without a uid are identified by title and start time, so moving or renaming one shows up
  as a delete and a new event. Once the event has started the old copy is kept, so a running event moved
  or renamed then shows up twice until the old copy ends.
- A bare 403 from the account's own calendars is taken as a permission problem and starts
  reauthentication, even when the real cause is something a new password cannot fix.
- iCloud limits CalDAV traffic without publishing the limits. Relays only write changes, but many relays
  with long look-aheads still mean more requests.
- Tested against Radicale in the test suite and against a live iCloud account (create, unchanged resync
  and delete of an event, with the shared Family calendar offered as a target). Place details, travel time
  and the leave reminder are tested against Radicale only, not yet on iCloud or an iPhone.

## Troubleshooting

**Settings** > **Devices & services** > **Calendar Relay** > three-dot menu > **Download diagnostics**
gives a file you can attach to an issue. The integration's part contains counts, states and each relay's
settings (place details, travel time mode, buffer, leave reminder) plus whether the Waze Travel Time action
is available: no username, password, server or calendar addresses, entity ids, titles, places or
coordinates. Home Assistant also adds any open repair issues to the file, and a "target calendar missing"
issue names the relay, so check the file before you share it if a relay is named after a person.

## Development

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt ruff==0.16.7
.venv/bin/python -m pytest --cov=custom_components.calendar_relay --cov-report=term-missing
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

The tests include an end-to-end test that starts a real Radicale server on a free local port and relays
events from Home Assistant's local calendar into it, with a stand-in for the Waze Travel Time action.

## License

MIT
