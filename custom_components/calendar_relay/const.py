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

DEFAULT_LOOK_AHEAD_DAYS: Final = 60
MIN_LOOK_AHEAD_DAYS: Final = 1
MAX_LOOK_AHEAD_DAYS: Final = 365

SYNC_INTERVAL: Final = timedelta(minutes=15)
SOURCE_CHANGE_COOLDOWN: Final = 30
# A source that suddenly returns no events at all must stay empty this long, on a later pass, before
# relayed future events are deleted. Shorter than SYNC_INTERVAL, so the next interval pass confirms it.
EMPTY_READ_CONFIRM_AFTER: Final = timedelta(minutes=10)

STORAGE_VERSION: Final = 1

ISSUE_TARGET_MISSING: Final = "target_calendar_missing"
