"""Tests for the account config flow, reauthentication and reconfiguration."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calendar_relay.caldav import CalDavAuthError, CalDavConnectionError, CalDavError
from custom_components.calendar_relay.const import DOMAIN

from .conftest import (
    ACCOUNT_URL,
    ENTRY_TITLE,
    PASSWORD,
    REMINDERS_URL,
    SHARED_URL,
    UNIQUE_ID,
    USERNAME,
    FakeDav,
    make_entry,
    setup_entry,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

USER_INPUT = {CONF_URL: ACCOUNT_URL, CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
NEW_PASSWORD = "wxyz-wxyz-wxyz-wxyz"


def schema_keys(result: dict[str, Any]) -> dict[str, Any]:
    """Return the form's schema keys by name."""
    return {str(key): key for key in result["data_schema"].schema}


async def test_user_flow_creates_account(hass: HomeAssistant, fake_dav: FakeDav) -> None:
    """The form defaults to iCloud and explains app-specific passwords."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}
    assert result["description_placeholders"] == {"app_password_url": "https://appleid.apple.com"}
    assert schema_keys(result)[CONF_URL].default() == "https://caldav.icloud.com/"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "https://CalDAV.example.com/", CONF_USERNAME: f" {USERNAME} ", CONF_PASSWORD: PASSWORD},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == ENTRY_TITLE
    assert result["data"] == {CONF_URL: "https://CalDAV.example.com/", CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    assert result["result"].unique_id == UNIQUE_ID
    assert result["result"].state is ConfigEntryState.LOADED
    assert fake_dav.client_args[0] == ("https://CalDAV.example.com/", USERNAME, PASSWORD)


async def test_user_flow_adds_https_to_a_bare_host(hass: HomeAssistant, fake_dav: FakeDav) -> None:
    """A server entered without a scheme gets https://."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_URL: "caldav.example.com"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_URL] == "https://caldav.example.com"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (CalDavAuthError(401), "invalid_auth"),
        (CalDavAuthError(403), "invalid_auth"),
        (CalDavConnectionError("PROPFIND returned HTTP 503"), "cannot_connect"),
        (CalDavError("The server did not answer as a CalDAV server"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_errors_then_recovers(
    hass: HomeAssistant, fake_dav: FakeDav, error: Exception, reason: str
) -> None:
    """Errors show on the form, which keeps the server and username but not the password."""
    fake_dav.discover_error = error
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": reason}
    keys = schema_keys(result)
    assert keys[CONF_USERNAME].description == {"suggested_value": USERNAME}
    assert (keys[CONF_PASSWORD].description or {}).get("suggested_value") is None

    fake_dav.discover_error = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_needs_a_writable_event_calendar(hass: HomeAssistant, fake_dav: FakeDav) -> None:
    """Reminders lists and read-only calendars do not count."""
    fake_dav.calendars = [calendar for calendar in fake_dav.calendars if calendar.url in (REMINDERS_URL, SHARED_URL)]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_calendars"}


async def test_user_flow_already_configured(hass: HomeAssistant, fake_dav: FakeDav) -> None:
    """The same user on the same host is one account."""
    make_entry().add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_URL: "https://CALDAV.example.com/other/path"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert fake_dav.client_args == []


async def test_reauth_updates_password_and_reloads(hass: HomeAssistant, fake_dav: FakeDav) -> None:
    """A new password is checked, stored, and the loaded entry reloads with it."""
    entry = make_entry()
    await setup_entry(hass, entry)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["username"] == USERNAME

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: NEW_PASSWORD})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == NEW_PASSWORD
    assert entry.state is ConfigEntryState.LOADED
    assert fake_dav.client_args == [
        (ACCOUNT_URL, USERNAME, PASSWORD),
        (ACCOUNT_URL, USERNAME, NEW_PASSWORD),
        (ACCOUNT_URL, USERNAME, NEW_PASSWORD),
    ]


async def test_reauth_wrong_password(hass: HomeAssistant, fake_dav: FakeDav) -> None:
    """A rejected password keeps the form open and the old password stored."""
    entry = make_entry()
    await setup_entry(hass, entry)
    result = await entry.start_reauth_flow(hass)
    fake_dav.discover_error = CalDavAuthError(401)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: NEW_PASSWORD})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data[CONF_PASSWORD] == PASSWORD


async def test_reauth_reloads_an_entry_that_failed_setup(hass: HomeAssistant, fake_dav: FakeDav) -> None:
    """An entry that never loaded has no update listener, so the flow reloads it."""
    fake_dav.discover_error = CalDavAuthError(401)
    entry = make_entry()
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    [flow] = hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    fake_dav.discover_error = None
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {CONF_PASSWORD: NEW_PASSWORD})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.state is ConfigEntryState.LOADED


async def test_reconfigure_changes_url_and_username(hass: HomeAssistant, fake_dav: FakeDav) -> None:
    """Server and username change; the password is kept when left empty; the default title follows."""
    entry = make_entry()
    await setup_entry(hass, entry)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    keys = schema_keys(result)
    assert keys[CONF_URL].description == {"suggested_value": ACCOUNT_URL}
    assert keys[CONF_USERNAME].description == {"suggested_value": USERNAME}
    assert (keys[CONF_PASSWORD].description or {}).get("suggested_value") is None

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "https://dav.example.org/", CONF_USERNAME: "parent@example.org"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {
        CONF_URL: "https://dav.example.org/",
        CONF_USERNAME: "parent@example.org",
        CONF_PASSWORD: PASSWORD,
    }
    assert entry.unique_id == "dav.example.org_parent@example.org"
    assert entry.title == "parent@example.org (dav.example.org)"
    assert entry.state is ConfigEntryState.LOADED
    assert fake_dav.client_args[-1] == ("https://dav.example.org/", "parent@example.org", PASSWORD)


async def test_reconfigure_with_new_password_keeps_a_custom_title(hass: HomeAssistant, fake_dav: FakeDav) -> None:
    """A title the user chose is not overwritten."""
    entry = make_entry(title="Family iCloud")
    await setup_entry(hass, entry)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_PASSWORD: NEW_PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.title == "Family iCloud"
    assert entry.unique_id == UNIQUE_ID
    assert entry.data[CONF_PASSWORD] == NEW_PASSWORD


async def test_reconfigure_into_an_existing_account(hass: HomeAssistant, fake_dav: FakeDav) -> None:
    """An account cannot be reconfigured into another configured account."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="dav.example.org_parent@example.org",
        data={CONF_URL: "https://dav.example.org/", CONF_USERNAME: "parent@example.org", CONF_PASSWORD: "x"},
    ).add_to_hass(hass)
    entry = make_entry()
    await setup_entry(hass, entry)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "https://dav.example.org/", CONF_USERNAME: "parent@example.org"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.unique_id == UNIQUE_ID


async def test_reconfigure_error(hass: HomeAssistant, fake_dav: FakeDav) -> None:
    """Errors show on the form with the entered values suggested."""
    entry = make_entry()
    await setup_entry(hass, entry)
    result = await entry.start_reconfigure_flow(hass)
    fake_dav.discover_error = CalDavConnectionError("PROPFIND failed: ClientConnectorError")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "https://dav.example.org/", CONF_USERNAME: USERNAME}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert schema_keys(result)[CONF_URL].description == {"suggested_value": "https://dav.example.org/"}
    assert entry.data[CONF_URL] == ACCOUNT_URL
