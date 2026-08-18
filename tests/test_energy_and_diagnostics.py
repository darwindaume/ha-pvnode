"""Energy platform and diagnostics."""

from __future__ import annotations

from datetime import datetime

from homeassistant.core import HomeAssistant

from custom_components.pvnode.const import SLOT_HOURS
from custom_components.pvnode.diagnostics import async_get_config_entry_diagnostics
from custom_components.pvnode.energy import async_get_solar_forecast

from .conftest import load_forecast, setup_integration


async def test_solar_forecast_matches_the_curve(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """The Energy dashboard reads this dict directly, so the maths has to be right."""
    payload = load_forecast()
    await setup_integration(hass, config_entry)

    forecast = await async_get_solar_forecast(hass, config_entry.entry_id)
    wh_hours = forecast["wh_hours"]

    assert len(wh_hours) == len(payload["values"])

    row = payload["values"][10]
    key = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).isoformat()
    assert wh_hours[key] == row["pv_power"] * SLOT_HOURS


async def test_solar_forecast_ignores_a_foreign_entry(hass: HomeAssistant) -> None:
    """An id from another integration must not raise, just return nothing."""
    assert await async_get_solar_forecast(hass, "does-not-exist") is None


async def test_diagnostics_redacts_the_key(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Diagnostics get pasted into issue trackers."""
    await setup_integration(hass, config_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)

    assert diagnostics["entry"]["api_key"] == "**REDACTED**"
    assert diagnostics["entry"]["site_id"] == "**REDACTED**"


async def test_diagnostics_describe_the_plan_state(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """What the plan allows versus what arrived is the first thing to check on a report."""
    payload = load_forecast()
    await setup_integration(hass, config_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    forecast = diagnostics["forecast"]

    assert forecast["available"] == payload["available"]
    assert forecast["included"] == payload["included"]
    assert forecast["counts"]["values"] == len(payload["values"])
    assert diagnostics["coordinator"]["variability_requested"] is False
    # The sample is there to show which fields arrive, not to dump the whole curve.
    assert len(forecast["values_sample"]) == 3
