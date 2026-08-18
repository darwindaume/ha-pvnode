"""Coordinator behaviour: poll cadence, persistence, entitlements, errors."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.pvnode.api import (
    ForecastResult,
    PvnodeQuotaExceeded,
    PvnodeVariabilityUnavailable,
    RequestLimit,
)
from custom_components.pvnode.const import (
    DOMAIN,
    ISSUE_QUOTA_EXHAUSTED,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    STORAGE_VERSION,
)

from .conftest import load_forecast, setup_integration, with_variability


def _in(minutes: int) -> str:
    """An RFC 3339 timestamp `minutes` from now."""
    return (dt_util.utcnow() + timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _result(payload: dict, **limit_kwargs) -> ForecastResult:
    """Wrap a payload the way the API client would."""
    return ForecastResult(payload=payload, request_limit=RequestLimit(**limit_kwargs))


async def test_next_poll_at_sets_the_interval(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """The API dictates the cadence; the integration has no interval of its own."""
    mock_api.async_get_forecast.return_value = _result(
        load_forecast(next_poll_at=_in(15))
    )
    await setup_integration(hass, config_entry)

    interval = config_entry.runtime_data.update_interval
    # 15 minutes plus the grace that keeps the poll from racing the slot boundary.
    assert timedelta(minutes=15) < interval <= timedelta(minutes=16)


async def test_absurd_next_poll_at_is_clamped(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """A timestamp in the past must not spin the coordinator into a request loop."""
    mock_api.async_get_forecast.return_value = _result(
        load_forecast(next_poll_at=_in(-600))
    )
    await setup_integration(hass, config_entry)

    assert config_entry.runtime_data.update_interval == MIN_UPDATE_INTERVAL


async def test_far_future_next_poll_at_is_clamped(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """A timestamp weeks out would otherwise freeze the integration."""
    mock_api.async_get_forecast.return_value = _result(
        load_forecast(next_poll_at=_in(60 * 24 * 30))
    )
    await setup_integration(hass, config_entry)

    assert config_entry.runtime_data.update_interval == MAX_UPDATE_INTERVAL


async def test_missing_next_poll_at_falls_back(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """An API that does not send the field must not stop the integration."""
    payload = load_forecast()
    payload.pop("next_poll_at")
    mock_api.async_get_forecast.return_value = _result(payload)

    await setup_integration(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data.update_interval is not None


async def test_a_valid_store_prevents_the_first_request(
    hass: HomeAssistant, mock_api, config_entry, hass_storage
) -> None:
    """Cached responses cost quota, so a restart must not spend one."""
    payload = load_forecast(next_poll_at=_in(30))
    config_entry.add_to_hass(hass)
    hass_storage[f"{DOMAIN}.{config_entry.entry_id}"] = {
        "version": STORAGE_VERSION,
        "key": f"{DOMAIN}.{config_entry.entry_id}",
        "data": {"payload": payload, "limits": {}, "string_names": {}},
    }

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    mock_api.async_get_forecast.assert_not_called()


async def test_an_expired_store_triggers_a_request(
    hass: HomeAssistant, mock_api, config_entry, hass_storage
) -> None:
    """Once the slot has passed the stored forecast is no longer the newest one."""
    payload = load_forecast(next_poll_at=_in(-5))
    config_entry.add_to_hass(hass)
    hass_storage[f"{DOMAIN}.{config_entry.entry_id}"] = {
        "version": STORAGE_VERSION,
        "key": f"{DOMAIN}.{config_entry.entry_id}",
        "data": {"payload": payload, "limits": {}, "string_names": {}},
    }

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    mock_api.async_get_forecast.assert_called()


async def test_variability_is_not_requested_when_the_plan_lacks_it(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """A fresh entry asks conservatively and learns from `available`."""
    await setup_integration(hass, config_entry)

    assert mock_api.async_get_forecast.call_args.kwargs["variability"] is False
    assert config_entry.runtime_data.variability_requested is False


async def test_variability_is_requested_once_available_says_so(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """`available` reports the entitlement, so an upgrade needs no reload."""
    mock_api.async_get_forecast.return_value = _result(
        with_variability(load_forecast(next_poll_at=_in(15)))
    )
    await setup_integration(hass, config_entry)

    coordinator = config_entry.runtime_data
    assert coordinator.variability_requested is True
    assert coordinator.data.has_variability is True

    # The next refresh must actually use it.
    await coordinator.async_refresh()
    assert mock_api.async_get_forecast.call_args.kwargs["variability"] is True


async def test_a_withdrawn_band_is_retried_without_it(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """`available` is one response old, so a downgrade can land mid-flight."""
    with_band = with_variability(load_forecast(next_poll_at=_in(15)))
    without_band = load_forecast(next_poll_at=_in(15))

    mock_api.async_get_forecast = AsyncMock(return_value=_result(with_band))
    await setup_integration(hass, config_entry)
    coordinator = config_entry.runtime_data
    assert coordinator.variability_requested is True

    # The plan loses the band between two refreshes: first call raises, retry succeeds.
    mock_api.async_get_forecast = AsyncMock(
        side_effect=[PvnodeVariabilityUnavailable("gone"), _result(without_band)]
    )
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.variability_requested is False
    assert coordinator.data.has_variability is False


async def test_quota_exhaustion_raises_a_repair_issue(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """A used-up quota is the user's to fix, so it gets a repair rather than a log line."""
    await setup_integration(hass, config_entry)
    coordinator = config_entry.runtime_data

    mock_api.async_get_forecast = AsyncMock(
        side_effect=PvnodeQuotaExceeded("empty", reset="2026-09-01T00:00:00Z")
    )
    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    registry = ir.async_get(hass)
    assert registry.async_get_issue(
        DOMAIN, f"{ISSUE_QUOTA_EXHAUSTED}_{config_entry.entry_id}"
    )


async def test_the_repair_issue_clears_after_a_good_refresh(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """The issue must not outlive the condition that caused it."""
    await setup_integration(hass, config_entry)
    coordinator = config_entry.runtime_data
    issue_id = f"{ISSUE_QUOTA_EXHAUSTED}_{config_entry.entry_id}"

    mock_api.async_get_forecast = AsyncMock(side_effect=PvnodeQuotaExceeded("empty"))
    await coordinator.async_refresh()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id)

    mock_api.async_get_forecast = AsyncMock(return_value=_result(load_forecast()))
    await coordinator.async_refresh()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_site_metadata_is_fetched_only_when_the_strings_change(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Cheap is not free — the site call should not ride along on every refresh."""
    await setup_integration(hass, config_entry)
    assert mock_api.async_get_site.call_count == 1

    await config_entry.runtime_data.async_refresh()
    assert mock_api.async_get_site.call_count == 1
