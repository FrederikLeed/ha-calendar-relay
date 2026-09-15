"""Constants for Calendar Relay."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Final

DOMAIN: Final = "calendar_relay"
LOGGER = logging.getLogger(__package__)

DEFAULT_URL: Final = "https://caldav.icloud.com/"
APP_PASSWORD_URL: Final = "https://appleid.apple.com"

SUBENTRY_TYPE_RELAY: Final = "relay"

CONF_SOURCE: Final = "source_calendar"
CONF_TARGET: Final = "target_calendar"
CONF_TARGET_NAME: Final = "target_calendar_name"
CONF_TITLE_FILTER: Final = "title_filter"
CONF_REMOVE_FILTER: Final = "remove_filter"
CONF_TITLE_PREFIX: Final = "title_prefix"
CONF_LOOK_AHEAD_DAYS: Final = "look_ahead_days"
CONF_STRUCTURED_LOCATION: Final = "structured_location"
CONF_TRAVEL_TIME: Final = "travel_time"
CONF_TRAVEL_MINUTES: Final = "travel_minutes"
CONF_WAZE_REGION: Final = "waze_region"
CONF_BUFFER_MINUTES: Final = "buffer_minutes"
CONF_LEAVE_REMINDER: Final = "leave_reminder"

DEFAULT_LOOK_AHEAD_DAYS: Final = 60
MIN_LOOK_AHEAD_DAYS: Final = 1
MAX_LOOK_AHEAD_DAYS: Final = 365

TRAVEL_OFF: Final = "off"
TRAVEL_FIXED: Final = "fixed"
TRAVEL_WAZE: Final = "waze"
TRAVEL_MODES: Final = (TRAVEL_OFF, TRAVEL_FIXED, TRAVEL_WAZE)
DEFAULT_TRAVEL_MINUTES: Final = 15
MIN_TRAVEL_MINUTES: Final = 1
MAX_TRAVEL_MINUTES: Final = 480
DEFAULT_BUFFER_MINUTES: Final = 0
MAX_BUFFER_MINUTES: Final = 120
WAZE_REGIONS: Final = ("us", "na", "eu", "il", "au")
DEFAULT_WAZE_REGION: Final = "eu"

WAZE_DOMAIN: Final = "waze_travel_time"
WAZE_SERVICE: Final = "get_travel_times"
# Waze times are rounded up to this many minutes, so small changes do not rewrite events.
TRAVEL_ROUNDING_MINUTES: Final = 5
# A travel time computed earlier is recomputed once with live traffic when the event starts within this window,
# or from this long before its leave time when that is earlier (a long trip), but not after the leave time.
REALTIME_WINDOW: Final = timedelta(hours=3)
REALTIME_BEFORE_LEAVE: Final = timedelta(hours=1)
# A travel time that failed for one event is asked for again after this, doubling while it fails, up to the maximum.
TRAVEL_RETRY_AFTER: Final = timedelta(hours=1)
TRAVEL_RETRY_MAX: Final = timedelta(hours=24)
# A pass makes at most this many Waze calls, and stops asking after this many failures in a row.
WAZE_CALLS_PER_PASS: Final = 10
WAZE_FAILURES_PER_PASS: Final = 3
# Seconds one Waze call may take, and seconds between the Waze calls of all relays (Home Assistant's own Waze
# sensors also wait half a second between calls).
WAZE_CALL_TIMEOUT: Final = 30
WAZE_CALL_PAUSE: Final = 0.5
# Waze answers are shared between events and relays with the same home, destination, region and traffic mode.
WAZE_ANSWER_TTL: Final = timedelta(minutes=15)

SYNC_INTERVAL: Final = timedelta(minutes=15)
SOURCE_CHANGE_COOLDOWN: Final = 30
# A source that suddenly returns no events at all must stay empty this long, on a later pass, before
# relayed future events are deleted. Shorter than SYNC_INTERVAL, so the next interval pass confirms it.
EMPTY_READ_CONFIRM_AFTER: Final = timedelta(minutes=10)

STORAGE_VERSION: Final = 1

ISSUE_TARGET_MISSING: Final = "target_calendar_missing"
