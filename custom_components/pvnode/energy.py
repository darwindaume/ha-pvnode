"""Energy platform for the pvnode integration.

Implements Home Assistant's solar forecast contract so a pvnode config entry can be
picked as "Forecast production" for a solar panel on the Energy dashboard.

Note that the dashboard attaches this to an *existing* production meter; a forecast
is not itself a source of solar production.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .const import DOMAIN, SLOT_HOURS


async def async_get_solar_forecast(
    hass: HomeAssistant, config_entry_id: str
) -> dict[str, dict[str, float | int]] | None:
    """Return the site-wide forecast as watt-hours per timestamp."""
    entry = hass.config_entries.async_get_entry(config_entry_id)
    if entry is None or entry.domain != DOMAIN:
        return None

    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None or not coordinator.data:
        return None

    # Site level, not per string: `values[].pv_power` is already the sum across the
    # site, and the dashboard wants one series per config entry.
    wh_hours: dict[str, float] = {}
    for timestamp, row in coordinator.data.values.items():
        power = row.get("pv_power")
        if power is None:
            continue
        wh_hours[timestamp.isoformat()] = float(power) * SLOT_HOURS

    return {"wh_hours": wh_hours} if wh_hours else None
