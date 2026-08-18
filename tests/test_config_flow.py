"""Config flow tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.pvnode.api import (
    PvnodeAuthError,
    PvnodeConnectionError,
    PvnodePlanError,
)
from custom_components.pvnode.const import CONF_SITE_ID, CONF_TIMEZONE, DOMAIN

from .conftest import SITE, SITE_ID, setup_integration


async def _start(hass: HomeAssistant, key: str = "test-key"):
    """Run the first step of the user flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: key}
    )


async def test_single_site_skips_the_picker(hass: HomeAssistant, mock_api) -> None:
    """With exactly one site there is nothing to choose, so the entry is created."""
    result = await _start(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Testanlage"
    assert result["data"][CONF_SITE_ID] == SITE_ID
    # The site's timezone has to be stored: timestamps arrive in UTC, so it is the
    # only way to resolve which day is "today".
    assert result["data"][CONF_TIMEZONE] == "Europe/Berlin"


async def test_multiple_sites_show_the_picker(hass: HomeAssistant, mock_api) -> None:
    """With more than one site the user picks which one to add."""
    second = {**SITE, "id": "site_second", "name": "Zweite Anlage"}
    mock_api.async_list_sites.return_value = [SITE, second]
    from custom_components.pvnode import config_flow

    config_flow.PvnodeApiClient.return_value.async_list_sites = AsyncMock(
        return_value=[SITE, second]
    )

    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "site"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SITE_ID: "site_second"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Zweite Anlage"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (PvnodeAuthError("nope"), "invalid_auth"),
        (PvnodePlanError("no plan"), "no_access"),
        (PvnodeConnectionError("offline"), "cannot_connect"),
    ],
)
async def test_errors_are_shown_on_the_form(
    hass: HomeAssistant, mock_api, error: Exception, reason: str
) -> None:
    """Each failure gets its own message rather than a generic one."""
    from custom_components.pvnode import config_flow

    config_flow.PvnodeApiClient.return_value.async_list_sites = AsyncMock(
        side_effect=error
    )

    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": reason}


async def test_account_without_sites_is_rejected(hass: HomeAssistant, mock_api) -> None:
    """An empty account cannot produce a useful entry."""
    from custom_components.pvnode import config_flow

    config_flow.PvnodeApiClient.return_value.async_list_sites = AsyncMock(
        return_value=[]
    )

    result = await _start(hass)
    assert result["errors"] == {"base": "no_sites"}


async def test_same_site_cannot_be_added_twice(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """The site id is the unique id, so a second attempt aborts."""
    await setup_integration(hass, config_entry)

    result = await _start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_the_key(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Reauth replaces the key without touching the rest of the entry."""
    await setup_integration(hass, config_entry)

    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "fresh-key"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_API_KEY] == "fresh-key"
    assert config_entry.data[CONF_SITE_ID] == SITE_ID


async def test_reauth_rejects_a_key_without_the_site(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """A key for a different account must not silently take over the entry."""
    await setup_integration(hass, config_entry)
    from custom_components.pvnode import config_flow

    config_flow.PvnodeApiClient.return_value.async_list_sites = AsyncMock(
        return_value=[{**SITE, "id": "site_somewhere_else"}]
    )

    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "wrong-account"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "site_not_found"}


async def test_reconfigure_updates_the_key_but_not_the_site(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Repointing an entry at another site would orphan its entities and history."""
    await setup_integration(hass, config_entry)

    result = await config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    # The site must not even be offered — rejecting a field that is shown is worse
    # than not showing it.
    assert CONF_SITE_ID not in result["data_schema"].schema

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "rotated-key"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_API_KEY] == "rotated-key"
    assert config_entry.data[CONF_SITE_ID] == SITE_ID
