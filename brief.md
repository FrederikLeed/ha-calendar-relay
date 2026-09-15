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
with a per-child prefix.

## Current state

- v0.1.0 released 2026-09-15 and installed through HACS on the owner's production Home Assistant
  (2026.9.1); the account and first relay are set up there by the owner. Public repo, so no personal data.
- No GitHub Actions (owner's choice): lint, tests and the Home Assistant validations are run locally
  before each release. The one CI run before the workflows were removed was green (lint, tests, hassfest,
  HACS validation).
- Tests: pytest-homeassistant-custom-component 0.13.365 (HA 2026.9.2, Python 3.14), including an
  end-to-end test with core `local_calendar` and a real Radicale 3.8.0 server in a thread.
- Not yet tried against a live iCloud account. The iCloud discovery shapes come from research and
  recorded responses in other projects. The first live test should record the Family calendar's
  privilege set and whether PUT returns an ETag.

## How it works

- One config entry per CalDAV account (URL, username, password; unique id = host + username).
  Reauth on 401/403, reconfigure for URL/username (password optional there).
- Relays are config subentries (type `relay`): source calendar entity, target calendar URL, title filter,
  remove-filter flag, prefix, look-ahead days. Subentry changes only notify update listeners, so the
  entry registers one listener that reloads it.
- `caldav.py` is a standalone aiohttp client (no HA imports, ElementTree XML, Basic header built by hand).
  Redirects are followed manually; credentials only go to the same host or, over https, a sibling under
  the same parent domain (caldav.icloud.com to pNN-caldav.icloud.com).
- `relay.py` reads the entity object's `async_get_events` (the service drops uid), plans the wanted
  events, PUTs new or changed ones, DELETEs withdrawn future ones, forgets started ones. State per relay in
  `Store` (`calendar_relay.<subentry_id>`): key to href, content hash, start, end, target URL.
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
  403 from a calendar the account does not list raise the target-missing repair issue.
- Removed relays: a `calendar_relay.relays_<entry_id>` Store lists relay ids with state; setup and
  entry removal drop the state and repair issue of ids no longer present, also when the relay was removed
  while the entry was not loaded.
- Credentials go to the configured host, https subdomains of it (RFC 6764 well-known redirects), or https
  siblings under the same parent (iCloud partitions). A well-known redirect elsewhere is logged and skipped.
- Zero-length events are written without DTEND (RFC 5545). Lone surrogates in source text become U+FFFD.
- Minimum HA 2026.3.0 (local brand folder, Python 3.14 test stack). No `via_device_id`, so relay devices
  hang off the account entry and subentry only.
- HACS validation also checks the repository description, topics and issues; they are set on GitHub.
