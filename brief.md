---
project: ha-calendar-relay
repo: https://github.com/FrederikLeed/ha-calendar-relay
updated: 2026-09-15
status: active
---

# Calendar Relay for Home Assistant

Custom integration (domain `calendar_relay`) that pushes events from Home Assistant calendar entities
into a CalDAV calendar (iCloud, Nextcloud, Radicale) and keeps them in step, one way, without churn.
Motivating use: relay KampKlar call-ups (`⭐ Udtaget: ` titles) into the shared iCloud Family calendar
with a per-child prefix, plus the match's place and when to leave.

## Current state

- v0.1.0 released 2026-09-15 and installed through HACS on the owner's production Home Assistant
  (2026.9.1); the account and first relay are set up there by the owner. Public repo, so no personal data.
- v0.2.0 (GitHub issue #1, location details and when to leave) implemented locally on 2026-09-15: place
  details for Apple Calendar, travel time (fixed or Waze Travel Time) with a leave line, and an optional
  leave reminder. Released 2026-09-15 after the test suite and a local hassfest run (no CI). The owner
  confirmed the place details, travel block, leave line and reminder on an iPhone through iCloud, and saw
  a second GMT time on each event, because timed events were written in UTC.
- v0.2.1 released 2026-09-15 (tests and local hassfest; no CI): timed events are written as local times in
  Home Assistant's time zone with a TZID and a VTIMEZONE, and the buffer setting is shown as Arrive early /
  Vær der før tid and named in the leave line. Upgrading rewrites each relayed timed event once, in the
  first sync, without extra Waze calls; all-day events are not rewritten. After review the VTIMEZONE is
  the same in every event (yearly rules), because vobject keeps the first one it reads per TZID.
- v0.2.2 released 2026-09-16 (tests and local hassfest; no CI). Credentials are removed from error
  excerpts before and after escapes are decoded, each in its given and decoded form, so a password
  containing text like %41 or &amp; cannot slip through. Live finding on iCloud on 2026-09-15: an entry the
  relay wrote and the owner then opened on an iPhone can no longer be replaced. A plain PUT (no If-Match or
  If-None-Match) to it returns 412 with no parseable DAV:error; entries no phone opened update fine
  (including added VALARM and X-APPLE-TRAVEL-DURATION), and a DELETE of the opened entry returns 204. So a
  moved match someone had opened stayed at the old time and every pass logged "HTTP 412". Decision: on a
  412 to an event PUT, the client DELETEs the same resource (same resource and redirect guards; 404 or 410
  still continues) and PUTs once more, never looping; the second answer is mapped like any PUT answer.
  The relay sees one successful write (store updated, no extra Waze calls, travel state kept). A PUT to a
  resource deleted by hand gets 201, so that path never deletes. Also: errors from an answer without a
  DAV:error condition quote a safe body excerpt (see Decisions). Review fixes the same day: a write that
  fails after the DELETE clears the stored hash so the next pass rewrites the entry even if the source moved
  back; the excerpt decodes HTML, JSON and percent escapes before removing credentials and is withheld when
  a credential sits inside a longer word; README and this brief say delete and recreate happens on any
  rewrite (source, travel time including live traffic, settings) and how a failed second write is handled.
- No GitHub Actions (owner's choice): lint, tests and the Home Assistant validations are run locally
  before each release. The one CI run before the workflows were removed was green (lint, tests, hassfest,
  HACS validation).
- Tests: 527 passing, 99% coverage (relay, ics, location and config flow at 100%), including regression
  tests for the adversarial review of 0.2.0 (Waze starvation, overflow, churn after a home change, shared
  Waze queue, started events, 0,0, Google links, parameter decoding) and for 0.2.1 (Copenhagen summer,
  winter and DST changes, Tokyo, Sydney, the UTC fallback, the one-time upgrade rewrite, arrive early in
  both languages, TZID and VTIMEZONE round-tripping through Radicale, times read back with dateutil and
  vobject, the same VTIMEZONE for events in any year, on-or-after rules for Jerusalem and Nuuk, Morocco's
  explicit onsets, and events in two years on one Radicale process checked with time-range REPORTs);
  pytest-homeassistant-custom-component 0.13.365 (HA 2026.9.2, Python 3.14), including an end-to-end test
  with core `local_calendar`, a stand-in Waze Travel Time action and a real Radicale 3.8.0 server in a
  thread.
- Verified against a live iCloud account on 2026-09-15 (0.1.0): discovery lands on the account's partition
  host (pNN-caldav.icloud.com), the shared Family calendar is offered as writable, a relayed event was
  created, left alone by a repeat sync, and deleted when the filter stopped matching.

## How it works

- One config entry per CalDAV account (URL, username, password; unique id = host + username).
  Reauth on 401/403, reconfigure for URL/username (password optional there).
- Relays are config subentries (type `relay`): source calendar entity, target calendar URL, title filter,
  remove-filter flag, prefix, look-ahead days, and since 0.2.0 structured location (default on), travel
  time mode (off/fixed/waze), fixed minutes, Waze region, arrive early (key `buffer_minutes`), leave reminder. Subentry changes
  only notify update listeners, so the entry registers one listener that reloads it.
- `caldav.py` is a standalone aiohttp client (no HA imports, ElementTree XML, Basic header built by hand).
  Redirects are followed manually; credentials only go to the same host or, over https, a sibling under
  the same parent domain (caldav.icloud.com to pNN-caldav.icloud.com).
- `relay.py` reads the entity object's `async_get_events` (the service drops uid), plans the wanted
  events, asks Waze where needed, PUTs new or changed ones, DELETEs withdrawn future ones, forgets started
  ones. State per relay in `Store` (`calendar_relay.<subentry_id>`): `events` maps key to href, content
  hash, start, end, target URL; `travel` maps key to Waze minutes, a fingerprint hash of origin,
  destination and region, a place hash of the destination alone, computed_at, a realtime flag and the
  event start it was computed for; `travel_retry` maps key to the failing fingerprint, failures in a row,
  retry_at and the error text.
- `location.py` (no HA imports) finds coordinates in the description, then the location: geo: URIs, Apple
  Maps (`ll`, `coordinate`, `destination`, `daddr`, `q`), Google Maps (`query`, `q`, `destination`,
  `@lat,lon`), OpenStreetMap (`mlat`/`mlon`); the first valid one wins. `ics.py` renders the Apple
  properties with a DQUOTE parameter helper, and timed events in the time zone the relay passes
  (`hass.config.time_zone`) with a yearly-rule VTIMEZONE derived from zoneinfo (no new requirements).
- Resource name `relay-<sha256(subentry_id + key)[:32]>.ics`, UID `<same>@calendar-relay`.
- Triggers: `async_at_started`, source state change (Debouncer 30 s), 15 min interval, Sync now button.

## Decisions and gotchas

- Never delete after a failed or unavailable source read. A read with no events at all (while future
  relayed events exist) must be confirmed by a pass at least 10 minutes later before anything is deleted.
- Triggers (source state change, interval) are only armed after `async_at_started`.
- Target calendar change deletes old copies before writing: CalendarServer rejects one UID in two
  calendars of the same home (403 `unique-scheduling-object-resource`). If the new calendar refuses the
  event, the copy is put back into the old calendar and the move is not retried until the event changes
  or the relay reloads. Old copies that cannot be deleted (untrusted host, other account) are left behind.
- Every PUT and DELETE targets a relay-style `.ics` directly inside the calendar; redirects are only
  followed when they keep the same resource name (a DELETE on a shared iCloud calendar collection would
  unlink it). The stored href is the one inside the target, not the redirected URL.
- Error mapping during sync: 401 and a bare 403 start reauth; 403 or 409 with a DAV:error precondition
  fail that event only (CalendarServer 403, Radicale 409 `no-uid-conflict`); 404, a bare 409, or a bare
  403 from a calendar the account does not list raise the target-missing repair issue. A PUT answered 412
  is replaced by DELETE and one more PUT (0.2.2), on any rewrite of an opened entry: a changed source, a new
  travel time (also the live traffic update shortly before a match) or changed settings, so devices may
  show it as new. If the second PUT fails, or the DELETE gets no usable answer (timeout, 429, 5xx), the
  client sets `resource_deleted` on the error (kept when a foreign 403 becomes a missing calendar) and the
  relay clears the record's hash, keeping href, start, end and target, so the next pass writes the event
  whatever it holds (review finding: with the old hash kept, a match moved back to its first time was never
  written again). The failure itself maps like any PUT: a second 412, 400 or refused event fails alone; a
  bare 403, 404/409, 429/5xx end the pass. An event that is over before a pass succeeds stays missing: the
  source no longer returns it.
- Error text (0.2.2): every error raised from an HTTP answer without a DAV:error condition (PUT, DELETE,
  discovery, 401, 429/5xx) carries `excerpt`, up to 160 characters of the body. HTML character references,
  JSON string escapes and percent-encoding are decoded first; then credentials (username, its
  percent-encoded form, password, Basic token) are replaced at word edges, and a credential inside a longer
  word (letter, digit, underscore or hyphen next to it, e.g. user `calendar` in `valid-calendar-data`)
  withholds the whole excerpt (`EXCERPT_WITHHELD`), since `[redacted]` would show the word. Echoed
  Authorization/Cookie headers, URLs and absolute paths are removed, non-printing characters become spaces
  and whitespace is collapsed; XML namespace URIs are kept.
  `str(err)` ends with `, response body: <excerpt>` and goes to warnings; `err.summary` leaves it out and
  is what `last_error`, the sensor attribute and diagnostics get, because a server may quote event text.
  Tests for the retry run the real client against FakeDav serving HTTP over aioclient_mock (`dav_server`
  fixture); the Radicale e2e test cannot produce iCloud's 412, so it is not faked there.
- Removed relays: a `calendar_relay.relays_<entry_id>` Store lists relay ids with state; setup and
  entry removal drop the state and repair issue of ids no longer present, also when the relay was removed
  while the entry was not loaded.
- Credentials go to the configured host, https subdomains of it (RFC 6764 well-known redirects), or https
  siblings under the same parent (iCloud partitions). A well-known redirect elsewhere is logged and skipped.
- Zero-length events are written without DTEND (RFC 5545). Lone surrogates in source text become U+FFFD.
- Minimum HA 2026.3.0 (local brand folder, Python 3.14 test stack). No `via_device_id`, so relay devices
  hang off the account entry and subentry only.
- HACS validation also checks the repository description, topics and issues; they are set on GitHub.
- Apple properties (undocumented; format from Apple-written samples, Apple's archived Calendar Server and
  decompiled iOS 18.2): `X-APPLE-STRUCTURED-LOCATION;VALUE=URI;[X-ADDRESS="..."];X-APPLE-RADIUS=71;
  X-TITLE="...":geo:lat,lon` right after LOCATION, with title = first LOCATION line and address = the
  rest, as Apple writes it; parameters always quoted, line break as backslash n, DQUOTE as apostrophe.
  No MapKit handle, REFERENCEFRAME, TRAVEL-START (it would publish the home location), ADVISORY-BEHAVIOR
  or TRAVEL-RETURN.
- Calendar Server keeps VALARM and every X- property except X-APPLE-STRUCTURED-LOCATION per user, so on
  a shared iCloud calendar the travel block and the reminder most likely only reach the relay's own Apple
  Account; the leave line in DESCRIPTION is what every member sees. Not yet checked with two Apple IDs.
- `X-APPLE-TRAVEL-DURATION` = travel + arrive early, so Apple's travel block starts at the leave time. The
  VALARM trigger is relative to the start. The leave line names arrive early when it is not 0:
  `Afgang: 09:55 (ca. 50 min. kørsel + 15 min. før tid)`, `Leave at 09:55 (about 50 min drive + 15 min early)`.
- Times (0.2.1): DTSTART/DTEND are local times with `TZID=<hass.config.time_zone>` and one VTIMEZONE
  before the VEVENT; DTSTAMP stays UTC and all-day events stay `VALUE=DATE`. UTC and its tz aliases (UTC,
  UCT, Universal, Zulu, with or without `Etc/`), names zoneinfo does not know, names TZID cannot carry
  unquoted, and times datetime cannot convert (year 9999 edge) keep the 0.2.0 UTC output byte for byte.
  The second occurrence of a repeated local time (fold=1) is written in UTC, since RFC 5545 3.3.5 reads a
  repeated local time as the first.
- VTIMEZONE: the same text for a zone in every event. vobject (the parser in Radicale and in HA's core
  CalDAV integration) registers the first VTIMEZONE it reads for a TZID process-wide and reads every later
  object with it; the first 0.2.1 draft wrote explicit onsets for each event's own years, so one Radicale
  process indexed summer events of other years an hour off (review finding, reproduced with time-range
  REPORTs; pinned by an e2e test with two years on one Radicale). Now yearly RRULE observances like
  Apple's (`RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU`, and `BYDAY=FR;BYMONTHDAY=23,...,29` for on-or-after
  rules, the form in Apple's CalendarServer zoneinfo), DTSTART in 1970 as in Radicale's test data, derived
  from zoneinfo transitions (scanned a day apart, bisected to the second) over 28 years from the current
  year: a full weekday and leap-year cycle, so an on-or-after rule is told apart from an nth weekday. No
  TZif footer parsing (file I/O in the event loop); the `ical` library's footer generator writes Nuuk's and
  Santiago's shifted-day rules a day off. DAYLIGHT when dst() > 0; a zone without transitions gets one
  observance from 1970. Zones that fit no yearly rule (Morocco and Gaza around Ramadan, Egypt's Thursday
  24:00 end, a rule change within the 28 years), or an event year that the rules do not cover (before
  1970 or outside the 28 years with other rules), get explicit onsets for the current year, the next two
  and the event years, shared by the events written in a year. CalendarServer (pycalendar
  `stripStandardTimezones`) drops client VTIMEZONEs for TZIDs it knows, so iCloud only uses the TZID.
- vobject quirk, not fixed: it skips registering a VTIMEZONE that matches UTC over 2000-2020 and reads the
  TZID through pytz without localizing, which Morocco's explicit onsets (+00 standard) hit, so Radicale
  misreads Africa/Casablanca times around Ramadan. Constant +00 zones (Reykjavik) read right that way.
- Churn: the 0.2.1 rendering changes the content hash, so each relayed timed event is rewritten once after
  the upgrade (and once after Home Assistant's time zone changes), never again; stored travel times are
  reused, so there are no extra Waze calls. Zones on explicit onsets are rewritten once more each New
  Year. An event already under way when it is rewritten loses its leave line and alarm, like any changed
  event that has started.
- Waze: only keys that exist in HA 2026.3 are sent (the action schema rejects others, so no `time_delta`
  or `base_coordinates`). The action exists from 2026.8 whenever the integration is loaded, before that
  only while a Waze entry is loaded; the subentry flow checks `has_service`. Asked once per destination
  (non-realtime), once more with realtime within 3 h of the start or from 1 h before the leave time when
  that is earlier, never after the leave time, and again for a moved start (the record stores the start
  it was for); a single realtime call if already within 3 h. Rounded up to 5 min; answers over 8 h are
  failures (a huge value used to overflow the leave-time arithmetic and block removals).
- Waze failures are per event: the event keeps its value for the same place (a new home or region keeps
  it until Waze answers, a new place drops it) and backs off 1 h doubling to 24 h, stored as
  `travel_retry` so restarts do not reset it; other events are still asked, nearest first. A pass stops
  asking after 10 calls, 3 failures in a row, a 30 s timeout, or a missing action. All relays share one
  queue in `hass.data` (one call at a time, 0.5 s pause, like HA's own Waze sensors; the action itself has
  no throttle) and reuse answers for the same fingerprint and traffic mode for 15 min. Travel problems set
  `last_error` but do not hold back `last_sync` or the repair-issue cleanup. Errors only show the exception
  type (no address in diagnostics).
- Travel time only for timed events with a place (coordinates or location text). A started event only
  keeps travel time it was already written with (same hash and calendar), so no past leave line or alarm
  is written. The leave line says "the day before" / "dagen før" when the leave time is on an earlier date.
- Coordinates: 0,0 is rejected (generators write it for a missing place); Google place links use the
  `!3d!4d` pin, directions the last waypoint (never the `@` view centre); a closing bracket after a link is
  only kept when the link opened it. Parameter values drop carets before n, ' or ^ and turn backslash-n
  into /n, so iOS and RFC 6868 readers read X-TITLE like LOCATION.
- Radicale (vobject) cuts unknown property values at the first unescaped comma, so its copy of the geo URI
  keeps only the latitude, and it unquotes parameters that do not need quotes; the e2e test pins that.
- No config entry migration: `RelayConfig.from_data` defaults the new keys, and `buffer_minutes` keeps its
  key in 0.2.1 (only its label changed). Upgrading 0.1.0 to 0.2.0 rewrote nothing without coordinates;
  upgrading to 0.2.1 rewrites timed events once (see Churn).
