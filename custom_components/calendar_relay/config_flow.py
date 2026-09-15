"""Config flow for Calendar Relay: CalDAV accounts, with relays as subentries."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote, urlsplit

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import CalendarRelayConfigEntry, create_client
from .caldav import CalDavAuthError, CalDavError, DavCalendar
from .const import (
    APP_PASSWORD_URL,
    CONF_BUFFER_MINUTES,
    CONF_LEAVE_REMINDER,
    CONF_LOOK_AHEAD_DAYS,
    CONF_REMOVE_FILTER,
    CONF_SOURCE,
    CONF_STRUCTURED_LOCATION,
    CONF_TARGET,
    CONF_TARGET_NAME,
    CONF_TITLE_FILTER,
    CONF_TITLE_PREFIX,
    CONF_TRAVEL_MINUTES,
    CONF_TRAVEL_TIME,
    CONF_WAZE_REGION,
    DEFAULT_BUFFER_MINUTES,
    DEFAULT_LOOK_AHEAD_DAYS,
    DEFAULT_TRAVEL_MINUTES,
    DEFAULT_URL,
    DEFAULT_WAZE_REGION,
    DOMAIN,
    LOGGER,
    MAX_BUFFER_MINUTES,
    MAX_LOOK_AHEAD_DAYS,
    MAX_TRAVEL_MINUTES,
    MIN_LOOK_AHEAD_DAYS,
    MIN_TRAVEL_MINUTES,
    SUBENTRY_TYPE_RELAY,
    TRAVEL_MODES,
    TRAVEL_OFF,
    TRAVEL_WAZE,
    WAZE_DOMAIN,
    WAZE_REGIONS,
    WAZE_SERVICE,
)

_URL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
_USERNAME_SELECTOR = TextSelector(TextSelectorConfig(autocomplete="username"))
_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password"))

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL, default=DEFAULT_URL): _URL_SELECTOR,
        vol.Required(CONF_USERNAME): _USERNAME_SELECTOR,
        vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
    }
)
REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR})
RECONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): _URL_SELECTOR,
        vol.Required(CONF_USERNAME): _USERNAME_SELECTOR,
        vol.Optional(CONF_PASSWORD): _PASSWORD_SELECTOR,
    }
)


def _host(url: str) -> str:
    """Return the lowercased host of a URL."""
    return (urlsplit(url).hostname or "").lower()


def _unique_id(url: str, username: str) -> str:
    """Return the unique id of an account: lowercased server host plus username."""
    return f"{_host(url)}_{username}"


def _entry_title(url: str, username: str) -> str:
    """Return the default title of an account entry."""
    return f"{username} ({_host(url)})"


def _account_data(user_input: Mapping[str, Any]) -> dict[str, str]:
    """Clean up the account fields. A URL without a scheme gets https://."""
    url = str(user_input[CONF_URL]).strip()
    if "://" not in url:
        url = f"https://{url}"
    return {
        CONF_URL: url,
        CONF_USERNAME: str(user_input[CONF_USERNAME]).strip(),
        CONF_PASSWORD: str(user_input[CONF_PASSWORD]),
    }


def _without_password(values: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return form values that are safe to suggest again."""
    return {key: value for key, value in (values or {}).items() if key != CONF_PASSWORD}


async def _async_validate(
    hass: HomeAssistant, data: Mapping[str, Any], *, require_calendars: bool = True
) -> dict[str, str]:
    """Discover the calendars with the given credentials and return form errors."""
    try:
        calendars = await create_client(hass, data).async_discover()
    except CalDavAuthError:
        return {"base": "invalid_auth"}
    except CalDavError as err:
        LOGGER.debug("CalDAV discovery failed: %s", err)
        return {"base": "cannot_connect"}
    except Exception:
        LOGGER.exception("Unexpected error while checking the CalDAV account")
        return {"base": "unknown"}
    if require_calendars and not any(calendar.usable for calendar in calendars):
        return {"base": "no_calendars"}
    return {}


class CalendarRelayConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a CalDAV account."""

    VERSION = 1

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        """Relays are subentries of the account."""
        return {SUBENTRY_TYPE_RELAY: RelaySubentryFlow}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the server and the login."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _account_data(user_input)
            await self.async_set_unique_id(_unique_id(data[CONF_URL], data[CONF_USERNAME]))
            self._abort_if_unique_id_configured()
            errors = await _async_validate(self.hass, data)
            if not errors:
                return self.async_create_entry(title=_entry_title(data[CONF_URL], data[CONF_USERNAME]), data=data)
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, _without_password(user_input)),
            errors=errors,
            description_placeholders={"app_password_url": APP_PASSWORD_URL},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauthentication after the server answered 401 or 403."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for a new password."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            errors = await _async_validate(self.hass, data, require_calendars=False)
            if not errors:
                return self._async_update_entry(entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]})
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"username": entry.data[CONF_USERNAME], "app_password_url": APP_PASSWORD_URL},
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the server URL or username (and optionally the password)."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _account_data(
                {**user_input, CONF_PASSWORD: user_input.get(CONF_PASSWORD) or entry.data[CONF_PASSWORD]}
            )
            unique_id = _unique_id(data[CONF_URL], data[CONF_USERNAME])
            if unique_id != entry.unique_id:
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
            errors = await _async_validate(self.hass, data)
            if not errors:
                title = entry.title
                if title == _entry_title(entry.data[CONF_URL], entry.data[CONF_USERNAME]):
                    title = _entry_title(data[CONF_URL], data[CONF_USERNAME])
                return self._async_update_entry(entry, unique_id=unique_id, title=title, data=data)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                RECONFIGURE_SCHEMA, _without_password(user_input if user_input is not None else entry.data)
            ),
            errors=errors,
        )

    @callback
    def _async_update_entry(self, entry: ConfigEntry, **kwargs: Any) -> ConfigFlowResult:
        """Update the entry and finish.

        A loaded entry reloads through its update listener. An entry that failed
        to set up has no listener, so schedule the reload here.
        """
        result = self.async_update_and_abort(entry, **kwargs)
        if entry.state is not ConfigEntryState.LOADED:
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return result


def _url_name(url: str) -> str:
    """Return the last path segment of a calendar URL."""
    return unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])


def _target_options(
    calendars: list[DavCalendar], current_url: str | None, current_name: str | None
) -> list[SelectOptionDict]:
    """Return select options for the target calendar, told apart by URL when names repeat."""
    counts = Counter(calendar.name for calendar in calendars)
    options = [
        SelectOptionDict(
            value=calendar.url,
            label=calendar.name if counts[calendar.name] == 1 else f"{calendar.name} ({_url_name(calendar.url)[:12]})",
        )
        for calendar in sorted(calendars, key=lambda calendar: calendar.name.casefold())
    ]
    if current_url and all(option["value"] != current_url for option in options):
        options.append(SelectOptionDict(value=current_url, label=current_name or _url_name(current_url)))
    return options


def _relay_schema(options: list[SelectOptionDict]) -> vol.Schema:
    """Return the relay form."""
    return vol.Schema(
        {
            vol.Required(CONF_SOURCE): EntitySelector(EntitySelectorConfig(domain="calendar")),
            vol.Required(CONF_TARGET): SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
            ),
            vol.Optional(CONF_TITLE_FILTER): TextSelector(),
            vol.Required(CONF_REMOVE_FILTER, default=False): BooleanSelector(),
            vol.Optional(CONF_TITLE_PREFIX): TextSelector(),
            vol.Required(CONF_LOOK_AHEAD_DAYS, default=DEFAULT_LOOK_AHEAD_DAYS): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_LOOK_AHEAD_DAYS, max=MAX_LOOK_AHEAD_DAYS, step=1, mode=NumberSelectorMode.BOX
                )
            ),
            vol.Required(CONF_STRUCTURED_LOCATION, default=True): BooleanSelector(),
            vol.Required(CONF_TRAVEL_TIME, default=TRAVEL_OFF): SelectSelector(
                SelectSelectorConfig(
                    options=list(TRAVEL_MODES), mode=SelectSelectorMode.DROPDOWN, translation_key=CONF_TRAVEL_TIME
                )
            ),
            vol.Required(CONF_TRAVEL_MINUTES, default=DEFAULT_TRAVEL_MINUTES): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_TRAVEL_MINUTES,
                    max=MAX_TRAVEL_MINUTES,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="min",
                )
            ),
            vol.Required(CONF_WAZE_REGION, default=DEFAULT_WAZE_REGION): SelectSelector(
                SelectSelectorConfig(
                    options=list(WAZE_REGIONS), mode=SelectSelectorMode.DROPDOWN, translation_key=CONF_WAZE_REGION
                )
            ),
            vol.Required(CONF_BUFFER_MINUTES, default=DEFAULT_BUFFER_MINUTES): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=MAX_BUFFER_MINUTES, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="min"
                )
            ),
            vol.Required(CONF_LEAVE_REMINDER, default=False): BooleanSelector(),
        }
    )


class RelaySubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure a relay."""

    _calendars: list[DavCalendar] | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Add a relay."""
        return await self._async_step_relay("user", user_input)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Reconfigure a relay."""
        return await self._async_step_relay("reconfigure", user_input)

    async def _async_step_relay(self, step_id: str, user_input: dict[str, Any] | None) -> SubentryFlowResult:
        """Show and handle the relay form."""
        entry: CalendarRelayConfigEntry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")
        current: Mapping[str, Any] = self._get_reconfigure_subentry().data if step_id == "reconfigure" else {}
        calendars = [calendar for calendar in await self._async_calendars(entry) if calendar.usable]
        if not calendars and not current:
            return self.async_abort(reason="no_calendars")

        errors: dict[str, str] = {}
        if user_input is not None:
            names = {calendar.url: calendar.name for calendar in calendars}
            target = user_input[CONF_TARGET]
            data = {
                CONF_SOURCE: user_input[CONF_SOURCE],
                CONF_TARGET: target,
                CONF_TARGET_NAME: names.get(target) or current.get(CONF_TARGET_NAME) or _url_name(target),
                CONF_TITLE_FILTER: user_input.get(CONF_TITLE_FILTER, ""),
                CONF_REMOVE_FILTER: bool(user_input.get(CONF_REMOVE_FILTER, False)),
                CONF_TITLE_PREFIX: user_input.get(CONF_TITLE_PREFIX, ""),
                CONF_LOOK_AHEAD_DAYS: int(user_input.get(CONF_LOOK_AHEAD_DAYS, DEFAULT_LOOK_AHEAD_DAYS)),
                CONF_STRUCTURED_LOCATION: bool(user_input.get(CONF_STRUCTURED_LOCATION, True)),
                CONF_TRAVEL_TIME: user_input.get(CONF_TRAVEL_TIME, TRAVEL_OFF),
                CONF_TRAVEL_MINUTES: int(user_input.get(CONF_TRAVEL_MINUTES, DEFAULT_TRAVEL_MINUTES)),
                CONF_WAZE_REGION: user_input.get(CONF_WAZE_REGION, DEFAULT_WAZE_REGION),
                CONF_BUFFER_MINUTES: int(user_input.get(CONF_BUFFER_MINUTES, DEFAULT_BUFFER_MINUTES)),
                CONF_LEAVE_REMINDER: bool(user_input.get(CONF_LEAVE_REMINDER, False)),
            }
            # Before Home Assistant 2026.8 the action only exists while a Waze Travel Time entry is loaded.
            if data[CONF_TRAVEL_TIME] == TRAVEL_WAZE and not self.hass.services.has_service(WAZE_DOMAIN, WAZE_SERVICE):
                errors[CONF_TRAVEL_TIME] = "waze_unavailable"
            else:
                title = self._relay_title(data)
                if step_id == "user":
                    return self.async_create_entry(title=title, data=data)
                return self.async_update_and_abort(entry, self._get_reconfigure_subentry(), title=title, data=data)

        schema = _relay_schema(_target_options(calendars, current.get(CONF_TARGET), current.get(CONF_TARGET_NAME)))
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input if user_input is not None else dict(current)
            ),
            errors=errors,
        )

    async def _async_calendars(self, entry: CalendarRelayConfigEntry) -> list[DavCalendar]:
        """Refresh the account's calendars once per flow; fall back to the list from setup."""
        if self._calendars is None:
            runtime = entry.runtime_data
            try:
                runtime.calendars = await runtime.client.async_discover()
            except CalDavError as err:
                LOGGER.debug("Refreshing the calendar list failed, using the list from setup: %s", err)
            self._calendars = runtime.calendars
        return self._calendars

    def _relay_title(self, data: Mapping[str, Any]) -> str:
        """Return a title such as 'Kids → Family'."""
        state = self.hass.states.get(data[CONF_SOURCE])
        source = state.name if state is not None else data[CONF_SOURCE]
        return f"{source} → {data[CONF_TARGET_NAME]}"
