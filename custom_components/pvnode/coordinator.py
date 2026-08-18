"""Data update coordinator for the pvnode integration."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    PvnodeApiClient,
    PvnodeAuthError,
    PvnodeConnectionError,
    PvnodeError,
    PvnodeNotFoundError,
    PvnodePlanError,
    PvnodeQuotaExceeded,
    PvnodeVariabilityUnavailable,
    RequestLimit,
)
from .const import (
    CONF_SITE_ID,
    CONF_TIMEZONE,
    DEFAULT_BASE_URL,
    DOMAIN,
    FALLBACK_UPDATE_INTERVAL,
    ISSUE_QUOTA_EXHAUSTED,
    LOCAL_REFRESH_INTERVAL,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    POLL_GRACE,
    QUOTA_BACKOFF,
    STORAGE_VERSION,
)
from .forecast import PvnodeData, parse_forecast
from .strings_meta import build_string_names

_LOGGER = logging.getLogger(__name__)

type PvnodeConfigEntry = ConfigEntry["PvnodeDataUpdateCoordinator"]

# Matches the unique_id suffix of a day-offset energy sensor, e.g. "..._energy_day5".
_ENERGY_DAY_OFFSET_RE = re.compile(r"_energy_day(\d+)$")


class PvnodeDataUpdateCoordinator(DataUpdateCoordinator[PvnodeData]):
    """Fetch pvnode forecasts for one site, on the cadence the API asks for."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PvnodeConfigEntry,
        version: str | None = None,
    ) -> None:
        """Initialize the coordinator for a config entry."""
        self.entry = entry
        self.site_id: str = entry.data[CONF_SITE_ID]
        self.api = PvnodeApiClient(
            async_get_clientsession(hass),
            entry.data[CONF_API_KEY],
            DEFAULT_BASE_URL,
            user_agent=(f"ha-pvnode/{version or 'unknown'} HomeAssistant/{HA_VERSION}"),
        )
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        # Whether to ask for the min/max band. Driven by `available` in the previous
        # response, which states what the plan allows rather than what was requested —
        # so both an upgrade and a downgrade are picked up on the next refresh without
        # any user action. None on a fresh install, where nothing is known yet.
        self._variability: bool | None = None
        self._string_names: dict[int, dict[str, str]] = {}
        self._string_names_for: set[int] = set()
        self.known_string_indexes: set[int] = set()
        self._new_string_listeners: list[Callable[[set[int]], None]] = []

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=FALLBACK_UPDATE_INTERVAL,
        )

    @property
    def variability_requested(self) -> bool | None:
        """Whether the next fetch will ask for the band; None until it is known."""
        return self._variability

    # -- dynamic strings ---------------------------------------------------

    @callback
    def add_new_string_listener(
        self, listener: Callable[[set[int]], None]
    ) -> CALLBACK_TYPE:
        """Register a callback for roof surfaces that appear after setup."""
        self._new_string_listeners.append(listener)

        def _remove() -> None:
            self._new_string_listeners.remove(listener)

        return _remove

    @callback
    def _async_announce_new_strings(self, data: PvnodeData) -> None:
        """Notify listeners about string indexes seen for the first time."""
        new = set(data.strings) - self.known_string_indexes
        if not new:
            return
        self.known_string_indexes |= new
        for listener in list(self._new_string_listeners):
            listener(new)

    # -- restore / persist -------------------------------------------------

    async def async_restore_from_store(self) -> bool:
        """Seed from the stored payload instead of fetching, when it is still current.

        Cached responses cost quota just like fresh ones, so a Home Assistant restart
        must not trigger an API call while the previous forecast is still the newest
        one the server would hand out.
        """
        stored = await self._store.async_load()
        if not stored or "payload" not in stored:
            return False

        # Tolerate a store written by an older version: unknown or missing quota keys
        # must not keep the restore path from working.
        raw_limits = stored.get("limits") or {}
        known = {f for f in vars(RequestLimit()) if f in raw_limits}
        limits = RequestLimit(**{k: raw_limits[k] for k in known})

        self._string_names = {
            int(index): names
            for index, names in (stored.get("string_names") or {}).items()
        }
        self._string_names_for = set(self._string_names)

        data = self._parse(stored["payload"], limits)
        if data.next_poll_at is None or data.next_poll_at <= dt_util.utcnow():
            return False

        # Safe to take from the store: `available` records the plan's entitlement, not
        # what happened to be requested, so a restored payload carries the real answer.
        self._variability = data.supports_variability
        self.known_string_indexes = set(data.strings)
        self._apply_cadence(data)
        self.async_set_updated_data(data)
        _LOGGER.debug(
            "pvnode %s: restored stored forecast, next poll at %s",
            self.site_id,
            data.next_poll_at,
        )
        return True

    async def _async_persist(
        self, payload: dict[str, Any], limits: RequestLimit
    ) -> None:
        """Write the raw payload to disk so the next restart can reuse it."""
        await self._store.async_save(
            {
                "payload": payload,
                "limits": vars(limits),
                "string_names": {str(k): v for k, v in self._string_names.items()},
            }
        )

    # -- local refresh -----------------------------------------------------

    @callback
    def async_start_local_refresh(self) -> CALLBACK_TYPE:
        """Re-evaluate sensors periodically without contacting the API.

        The forecast is a curve; `power_now` and the running totals move simply because
        time passes. Tying that to the API cadence would mean a free-plan site whose
        current-power sensor updates once a day.
        """

        @callback
        def _tick(_now: datetime) -> None:
            if self.data:
                self.async_update_listeners()

        return async_track_time_interval(self.hass, _tick, LOCAL_REFRESH_INTERVAL)

    # -- cadence -----------------------------------------------------------

    def _apply_cadence(self, data: PvnodeData) -> None:
        """Set the poll interval from `next_poll_at`.

        Clamped only as a guard against an implausible value — the server never
        advertises anything below 15 minutes.
        """
        if data.next_poll_at is None:
            self.update_interval = FALLBACK_UPDATE_INTERVAL
            _LOGGER.debug(
                "pvnode %s: no next_poll_at in response, falling back to %s",
                self.site_id,
                FALLBACK_UPDATE_INTERVAL,
            )
            return

        delay = data.next_poll_at - dt_util.utcnow() + POLL_GRACE
        clamped = max(MIN_UPDATE_INTERVAL, min(delay, MAX_UPDATE_INTERVAL))
        if delay <= MIN_UPDATE_INTERVAL or delay > MAX_UPDATE_INTERVAL:
            # Worth surfacing: either the timestamp is already in the past (clock skew
            # or a stale cached response) or it is implausibly far out.
            _LOGGER.warning(
                "pvnode %s: next_poll_at %s is %s away, using %s instead",
                self.site_id,
                data.next_poll_at,
                delay,
                clamped,
            )
        self.update_interval = clamped

    # -- fetch -------------------------------------------------------------

    def _parse(self, payload: dict[str, Any], limits: RequestLimit) -> PvnodeData:
        """Parse a raw payload with this entry's site metadata."""
        return parse_forecast(
            payload,
            site_timezone=self.entry.data.get(CONF_TIMEZONE),
            request_limit=limits,
            string_names=self._string_names,
        )

    async def _async_fetch(self) -> Any:
        """Fetch the forecast, asking for the band only when the plan allows it.

        The entitlement comes from the previous response's `available` list, so nothing
        has to be guessed and no error path is used for a normal decision. The trade-off
        is that a fresh install learns about the band one refresh late.
        """
        want = bool(self._variability)
        try:
            result = await self.api.async_get_forecast(self.site_id, variability=want)
        except PvnodeVariabilityUnavailable:
            # `available` is by definition one response old. A plan downgrade landing
            # between two refreshes would otherwise fail the whole update, so this stays
            # as a safety net — in normal operation it never fires.
            _LOGGER.debug(
                "pvnode %s: variability withdrawn since the last refresh, retrying without",
                self.site_id,
            )
            self._variability = False
            result = await self.api.async_get_forecast(self.site_id, variability=False)

        available = result.payload.get("available")
        if available is not None:
            self._variability = "variability" in available
        elif self._variability is None:
            # An API that predates `available` gives nothing to decide on. Ask for the
            # band next time and let the 403 answer, rather than silently never
            # offering it.
            _LOGGER.debug(
                "pvnode %s: response has no `available`, falling back to probing",
                self.site_id,
            )
            self._variability = True
        return result

    async def _async_refresh_string_names(self, indexes: set[int]) -> None:
        """Fetch string geometry when the set of roof surfaces changed.

        `GET /v2/sites/{id}` is open on every plan and not counted against the forecast
        quota, so this is cheap — but it is still only worth doing when it can change
        something.
        """
        if indexes and indexes == self._string_names_for:
            return
        try:
            site = await self.api.async_get_site(self.site_id)
        except PvnodeError as err:
            _LOGGER.debug(
                "pvnode %s: could not fetch site metadata: %s", self.site_id, err
            )
            return
        self._string_names = build_string_names(site, self.hass.config.language)
        # Keyed on what was asked for, not on what resolved: a string with unusable
        # geometry keeps its fallback name instead of re-fetching on every refresh.
        self._string_names_for = indexes

    async def _async_update_data(self) -> PvnodeData:
        """Fetch, parse, persist, and reschedule."""
        try:
            result = await self._async_fetch()
        except PvnodeAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (PvnodePlanError, PvnodeNotFoundError) as err:
            raise ConfigEntryError(str(err)) from err
        except PvnodeQuotaExceeded as err:
            self._async_quota_issue(True, err.reset)
            raise UpdateFailed(
                f"pvnode request quota exhausted: {err}",
                retry_after=QUOTA_BACKOFF.total_seconds(),
            ) from err
        except (PvnodeConnectionError, PvnodeError) as err:
            raise UpdateFailed(str(err)) from err

        self._async_quota_issue(False)

        indexes = {
            int(row["string_index"])
            for row in (result.payload.get("strings") or [])
            if row.get("string_index") is not None
        }
        await self._async_refresh_string_names(indexes)

        data = self._parse(result.payload, result.request_limit)
        self._apply_cadence(data)
        await self._async_persist(result.payload, result.request_limit)

        self._async_announce_new_strings(data)
        self._async_remove_stale_strings(set(data.strings))
        self._async_remove_stale_days(len(data.daily))

        _LOGGER.debug(
            "pvnode %s: %d timesteps, %d days, %d strings, variability=%s, "
            "computed_at=%s, next_poll_at=%s, next poll in %s",
            self.site_id,
            len(data.values),
            len(data.daily),
            len(data.strings),
            data.has_variability,
            data.computed_at,
            data.next_poll_at,
            self.update_interval,
        )
        return data

    # -- registry hygiene --------------------------------------------------

    @callback
    def _async_remove_stale_strings(self, valid: set[int]) -> None:
        """Drop devices for roof surfaces pvnode no longer reports.

        Happens when a string is deleted on the portal, or when a downgrade pushes it
        past the plan's `strings_per_site` cap.
        """
        registry = dr.async_get(self.hass)
        valid_ids = {f"{self.entry.entry_id}_string{index}" for index in valid}

        for device in dr.async_entries_for_config_entry(registry, self.entry.entry_id):
            ids = {ident[1] for ident in device.identifiers if ident[0] == DOMAIN}
            # The site device itself is identified by the bare entry id.
            if self.entry.entry_id in ids or not ids:
                continue
            if not ids & valid_ids:
                registry.async_update_device(
                    device.id, remove_config_entry_id=self.entry.entry_id
                )
                self.known_string_indexes.discard(_index_from_identifier(ids))

    @callback
    def _async_remove_stale_days(self, day_count: int) -> None:
        """Drop per-day energy sensors beyond the horizon the plan now returns.

        Without this, downgrading from a 7-day to a 2-day plan would leave five
        permanently unavailable entities behind.
        """
        registry = er.async_get(self.hass)
        for entity in list(
            er.async_entries_for_config_entry(registry, self.entry.entry_id)
        ):
            match = _ENERGY_DAY_OFFSET_RE.search(entity.unique_id)
            if match and int(match.group(1)) >= day_count:
                registry.async_remove(entity.entity_id)

    # -- repair issue ------------------------------------------------------

    @callback
    def _async_quota_issue(self, active: bool, reset: str | None = None) -> None:
        """Raise or clear the repair issue for an exhausted request quota."""
        issue_id = f"{ISSUE_QUOTA_EXHAUSTED}_{self.entry.entry_id}"
        if not active:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return

        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_QUOTA_EXHAUSTED,
            translation_placeholders={
                "site": self.entry.title,
                "reset": reset or "the start of next month",
            },
            learn_more_url="https://pvnode.com",
        )


def _index_from_identifier(identifiers: set[str]) -> int:
    """Pull the string index out of a device identifier, or -1 if absent."""
    for identifier in identifiers:
        if match := re.search(r"_string(\d+)$", identifier):
            return int(match.group(1))
    return -1
