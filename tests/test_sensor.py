"""Sensor values, checked against the recorded API response."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.pvnode.api import ForecastResult, RequestLimit

from .conftest import load_forecast, setup_integration, with_variability

TZ = ZoneInfo("Europe/Berlin")


def _entity_id(hass: HomeAssistant, entry_id: str, key: str) -> str | None:
    """Look up an entity by its unique-id key.

    Matched exactly rather than by suffix: the site sensor `<entry>_power_now` and the
    per-string `<entry>_string0_power_now` both end in the same text, and a suffix match
    would return whichever the registry happened to yield first.
    """
    registry = er.async_get(hass)
    wanted = f"{entry_id}_{key}"
    for entity in er.async_entries_for_config_entry(registry, entry_id):
        if entity.unique_id == wanted:
            return entity.entity_id
    return None


def _state(hass: HomeAssistant, entry_id: str, key: str):
    """Return the state object of the entity with that unique-id key."""
    entity_id = _entity_id(hass, entry_id, key)
    assert entity_id, f"no entity with unique id {entry_id}_{key}"
    return hass.states.get(entity_id)


async def test_day_totals_come_from_the_api_not_from_the_curve(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Re-summing the power curve would invent a way to disagree with the portal."""
    payload = load_forecast()
    await setup_integration(hass, config_entry)

    today = _state(hass, config_entry.entry_id, "energy_day0")
    assert float(today.state) == payload["daily"][0]["pv_energy_kwh"]
    assert today.attributes["date"] == payload["daily"][0]["date"]

    tomorrow = _state(hass, config_entry.entry_id, "energy_day1")
    assert float(tomorrow.state) == payload["daily"][1]["pv_energy_kwh"]


async def test_one_day_sensor_per_returned_day(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """The horizon comes from the plan, so nothing local decides the count."""
    payload = load_forecast()
    await setup_integration(hass, config_entry)

    registry = er.async_get(hass)
    days = [
        entity
        for entity in er.async_entries_for_config_entry(registry, config_entry.entry_id)
        if "_energy_day" in entity.unique_id
    ]
    assert len(days) == len(payload["daily"])


async def test_current_power_uses_the_slot_covering_now(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """The value is the last slot at or before now, not the nearest one."""
    payload = load_forecast()
    await setup_integration(hass, config_entry)

    now = dt_util.utcnow()
    past = [
        row
        for row in payload["values"]
        if datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")) <= now
    ]
    expected = max(past, key=lambda row: row["timestamp"])["pv_power"]

    state = _state(hass, config_entry.entry_id, "power_now")
    assert float(state.state) == expected


async def test_peak_is_the_maximum_of_the_local_day(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Day boundaries are the site's, even though timestamps arrive in UTC."""
    payload = load_forecast()
    await setup_integration(hass, config_entry)

    start = datetime.combine(datetime.now(TZ).date(), datetime.min.time(), tzinfo=TZ)
    end = start + timedelta(days=1)
    today = [
        row
        for row in payload["values"]
        if start
        <= datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        < end
    ]
    peak = max(today, key=lambda row: row["pv_power"] or 0)

    assert (
        float(_state(hass, config_entry.entry_id, "peak_power_today").state)
        == (peak["pv_power"])
    )
    assert _state(hass, config_entry.entry_id, "peak_time_today").state == (
        datetime.fromisoformat(peak["timestamp"].replace("Z", "+00:00")).isoformat()
    )


async def test_power_sensor_carries_its_series(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """The chart templates read this attribute, so its shape is part of the contract."""
    payload = load_forecast()
    await setup_integration(hass, config_entry)

    state = _state(hass, config_entry.entry_id, "power_now")
    forecast = state.attributes["forecast"]
    assert len(forecast) == len(payload["values"])
    assert set(forecast[0]) == {"datetime", "watts"}


async def test_one_device_per_roof_surface(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Each string in the payload becomes its own device with its own sensors."""
    payload = load_forecast()
    await setup_integration(hass, config_entry)

    indexes = {row["string_index"] for row in payload["strings"]}
    for index in indexes:
        assert _entity_id(hass, config_entry.entry_id, f"string{index}_power_now"), (
            f"string {index} has no power sensor"
        )


async def test_band_sensors_only_exist_with_the_entitlement(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Without the band there must be no permanently unknown sensors."""
    await setup_integration(hass, config_entry)
    assert _entity_id(hass, config_entry.entry_id, "power_min_now") is None


async def test_band_sensors_appear_when_the_band_does(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """A plan upgrade creates them without a reload."""
    await setup_integration(hass, config_entry)
    assert _entity_id(hass, config_entry.entry_id, "power_min_now") is None

    mock_api.async_get_forecast = AsyncMock(
        return_value=ForecastResult(
            payload=with_variability(load_forecast()), request_limit=RequestLimit()
        )
    )
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert _entity_id(hass, config_entry.entry_id, "power_min_now")
    assert _entity_id(hass, config_entry.entry_id, "string0_power_min_now")


async def test_band_sensors_are_removed_again_on_a_downgrade(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Otherwise a downgrade leaves a row of sensors stuck on unknown."""
    mock_api.async_get_forecast = AsyncMock(
        return_value=ForecastResult(
            payload=with_variability(load_forecast()), request_limit=RequestLimit()
        )
    )
    await setup_integration(hass, config_entry)
    assert _entity_id(hass, config_entry.entry_id, "power_min_now")

    mock_api.async_get_forecast = AsyncMock(
        return_value=ForecastResult(
            payload=load_forecast(), request_limit=RequestLimit()
        )
    )
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert _entity_id(hass, config_entry.entry_id, "power_min_now") is None


async def test_remaining_requests_is_reported(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """The quota headers are the only way a user sees what a plan is spending."""
    await setup_integration(hass, config_entry)

    state = _state(hass, config_entry.entry_id, "requests_remaining")
    assert state.state == "2988"
    assert state.attributes["limit"] == "3000"
    assert state.attributes["unmetered"] is False


async def test_a_longer_horizon_adds_day_sensors_without_a_reload(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Upgrading the plan lengthens the horizon; the extra days should just appear."""
    short = load_forecast()
    short["daily"] = short["daily"][:1]
    mock_api.async_get_forecast = AsyncMock(
        return_value=ForecastResult(payload=short, request_limit=RequestLimit())
    )
    await setup_integration(hass, config_entry)
    assert _entity_id(hass, config_entry.entry_id, "energy_day1") is None

    mock_api.async_get_forecast = AsyncMock(
        return_value=ForecastResult(
            payload=load_forecast(), request_limit=RequestLimit()
        )
    )
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert _entity_id(hass, config_entry.entry_id, "energy_day1")
