"""Diagnostics for the pvnode integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant

from .coordinator import PvnodeConfigEntry

TO_REDACT = {CONF_API_KEY, "site_id", "latitude", "longitude"}

# The forecast is hundreds of timesteps long; a diagnostics download should be readable.
_SAMPLE = 3


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PvnodeConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data

    diagnostics: dict[str, Any] = {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "variability_requested": coordinator.variability_requested,
            "known_string_indexes": sorted(coordinator.known_string_indexes),
        },
    }

    if not data:
        diagnostics["forecast"] = None
        return diagnostics

    limits = data.request_limit
    diagnostics["forecast"] = {
        "computed_at": data.computed_at.isoformat() if data.computed_at else None,
        "next_poll_at": (
            data.next_poll_at.isoformat() if data.next_poll_at else None
        ),
        "included": data.included,
        "available": data.available,
        "site_timezone": data.site_timezone,
        "string_names": data.string_names,
        "counts": {
            "values": len(data.values),
            "daily": len(data.daily),
            "strings": {index: len(rows) for index, rows in data.strings.items()},
        },
        "daily": data.daily,
        # A few rows are enough to tell which fields the plan actually delivers.
        "values_sample": [
            {"timestamp": ts.isoformat(), **row}
            for ts, row in sorted(data.values.items())[:_SAMPLE]
        ],
        "request_limit": {
            "limit": limits.limit,
            "used": limits.used,
            "remaining": limits.remaining,
            "reset": limits.reset,
        },
    }
    return diagnostics
