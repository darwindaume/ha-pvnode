"""Config flow for the pvnode integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import (
    PvnodeApiClient,
    PvnodeAuthError,
    PvnodeConnectionError,
    PvnodeError,
    PvnodePlanError,
)
from .const import (
    CONF_SITE_ID,
    CONF_TIMEZONE,
    DEFAULT_BASE_URL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class PvnodeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Two steps: API key, then pick a site."""

    VERSION = 1

    def __init__(self) -> None:
        """Set up the transient state carried between steps."""
        self._api_key: str = ""
        self._sites: list[dict[str, Any]] = []

    async def _async_fetch_sites(self) -> dict[str, str]:
        """Validate the key by listing the account's sites.

        `GET /v2/sites/` is open on every plan and is not counted against the forecast
        request quota, so this is free to call here.
        """
        client = PvnodeApiClient(
            async_get_clientsession(self.hass), self._api_key, DEFAULT_BASE_URL
        )
        try:
            self._sites = await client.async_list_sites()
        except PvnodeAuthError:
            return {"base": "invalid_auth"}
        except PvnodePlanError as err:
            _LOGGER.debug("pvnode plan rejected the sites call: %s", err)
            return {"base": "no_access"}
        except PvnodeConnectionError:
            return {"base": "cannot_connect"}
        except PvnodeError:
            return {"base": "unknown"}
        except Exception:
            _LOGGER.exception("Unexpected error while listing pvnode sites")
            return {"base": "unknown"}

        if not self._sites:
            return {"base": "no_sites"}
        return {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the API key."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._api_key = user_input[CONF_API_KEY].strip()

            errors = await self._async_fetch_sites()
            if not errors:
                return await self.async_step_site()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): str}),
            errors=errors,
        )

    async def async_step_site(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick one of the account's sites."""
        if user_input is not None:
            return await self._async_create(user_input[CONF_SITE_ID])

        if len(self._sites) == 1:
            return await self._async_create(self._sites[0]["id"])

        options = [
            SelectOptionDict(value=site["id"], label=site.get("name") or site["id"])
            for site in self._sites
        ]
        return self.async_show_form(
            step_id="site",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SITE_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.DROPDOWN
                        )
                    )
                }
            ),
        )

    async def _async_create(self, site_id: str) -> ConfigFlowResult:
        """Create the entry for `site_id`."""
        await self.async_set_unique_id(site_id)
        self._abort_if_unique_id_configured()

        site = next((s for s in self._sites if s["id"] == site_id), {})
        title = site.get("name") or f"pvnode {site_id}"

        return self.async_create_entry(
            title=title,
            data={
                CONF_API_KEY: self._api_key,
                CONF_SITE_ID: site_id,
                # Timestamps are fetched in UTC, so the site's own zone is the only way
                # to resolve which `daily` entry is "today".
                CONF_TIMEZONE: site.get("timezone"),
            },
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start reauth after the key was rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a fresh API key and verify it against the same site."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            self._api_key = user_input[CONF_API_KEY].strip()

            errors = await self._async_fetch_sites()
            if not errors:
                site_id = entry.data[CONF_SITE_ID]
                if not any(site["id"] == site_id for site in self._sites):
                    errors["base"] = "site_not_found"
                else:
                    return self.async_update_reload_and_abort(
                        entry, data_updates={CONF_API_KEY: self._api_key}
                    )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): str}),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user swap the API key of an existing entry.

        The site itself is deliberately not offered. The entry's unique id *is* the
        site id, and repointing it would leave every entity, its history and the Energy
        dashboard link attached to an entry that now describes a different plant. A
        second site is a second entry.
        """
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        site_id = entry.data[CONF_SITE_ID]

        if user_input is not None:
            self._api_key = user_input[CONF_API_KEY].strip()

            errors = await self._async_fetch_sites()
            if not errors:
                site = next((s for s in self._sites if s["id"] == site_id), None)
                if site is None:
                    errors["base"] = "site_not_found"
                else:
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={
                            CONF_API_KEY: self._api_key,
                            CONF_TIMEZONE: site.get("timezone"),
                        },
                    )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_API_KEY, default=entry.data.get(CONF_API_KEY, "")
                    ): str,
                }
            ),
            errors=errors,
            description_placeholders={"site": entry.title},
        )
