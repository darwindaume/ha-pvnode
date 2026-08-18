"""Shared test fixtures for the pvnode integration test suite."""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pvnode.const import (
    CONF_SITE_ID,
    CONF_TIMEZONE,
    DOMAIN,
)

pytest_plugins = "pytest_homeassistant_custom_component"

_REPO_ROOT = Path(__file__).parent.parent
_SOURCE = _REPO_ROOT / "custom_components" / "pvnode"
_FIXTURES = Path(__file__).parent / "fixtures"

SITE_ID = "site_testfixture0000000"


def pytest_configure(config: object) -> None:
    """Make this repo's custom_components/pvnode discoverable by the HA test harness.

    pytest-homeassistant-custom-component always looks for custom integrations
    under its own package's `testing_config/custom_components` directory, so we
    link (or copy, if symlinks aren't available) this repo's integration there
    once before the test session starts.
    """
    import pytest_homeassistant_custom_component.common as ha_common

    target_root = (
        Path(ha_common.__file__).parent / "testing_config" / "custom_components"
    )
    target_root.mkdir(parents=True, exist_ok=True)
    target = target_root / "pvnode"

    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)

    try:
        target.symlink_to(_SOURCE, target_is_directory=True)
    except OSError:
        shutil.copytree(_SOURCE, target)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integration loading for every test in this suite."""


def load_forecast(**overrides: Any) -> dict[str, Any]:
    """Return the recorded forecast response, shifted so day 0 is today.

    The fixture was captured on a real day. Without shifting it, every test that asks
    "what is the forecast for today" would start failing the day after it was recorded.
    """
    payload = json.loads((_FIXTURES / "forecast.json").read_text(encoding="utf-8"))

    tz = ZoneInfo(payload.get("timezone") or "UTC")
    recorded = date.fromisoformat(payload["daily"][0]["date"])
    shift = timedelta(days=(datetime.now(tz).date() - recorded).days)

    def _shift_ts(value: str) -> str:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00")) + shift
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    for day in payload["daily"]:
        day["date"] = (date.fromisoformat(day["date"]) + shift).isoformat()
    for row in payload["values"]:
        row["timestamp"] = _shift_ts(row["timestamp"])
    for row in payload.get("strings") or []:
        row["timestamp"] = _shift_ts(row["timestamp"])
    for key in ("computed_at", "next_poll_at"):
        if payload.get(key):
            payload[key] = _shift_ts(payload[key])

    payload.update(overrides)
    return payload


def with_variability(payload: dict[str, Any]) -> dict[str, Any]:
    """Add the min/max band to a payload, as a plan that allows it would."""
    payload = json.loads(json.dumps(payload))
    payload["included"] = [*payload["included"], "variability"]
    payload["available"] = [*payload["available"], "variability"]
    for row in payload["values"]:
        power = row.get("pv_power") or 0
        row["pv_power_min"] = power * 0.8
        row["pv_power_max"] = power * 1.2
    for row in payload["strings"]:
        power = row.get("pv_power") or 0
        row["pv_power_min"] = power * 0.8
        row["pv_power_max"] = power * 1.2
    for day in payload["daily"]:
        energy = day.get("pv_energy_kwh") or 0
        day["pv_energy_kwh_min"] = energy * 0.8
        day["pv_energy_kwh_max"] = energy * 1.2
    return payload


SITE = {
    "id": SITE_ID,
    "name": "Testanlage",
    "timezone": "Europe/Berlin",
    "latitude": 52.5,
    "longitude": 13.4,
    "status": "active",
    "strings": [
        {"slope": 45.0, "orientation": 180.0, "power_kw": 9.9},
        {"slope": 30.0, "orientation": 90.0, "power_kw": 4.5},
    ],
}


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A configured pvnode entry for the fixture site."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Testanlage",
        unique_id=SITE_ID,
        data={
            CONF_API_KEY: "test-key",
            CONF_SITE_ID: SITE_ID,
            CONF_TIMEZONE: "Europe/Berlin",
        },
    )


@pytest.fixture
def mock_api():
    """Patch the API client so no HTTP happens and calls can be asserted."""
    with (
        patch(
            "custom_components.pvnode.coordinator.PvnodeApiClient", autospec=True
        ) as coordinator_client,
        patch(
            "custom_components.pvnode.config_flow.PvnodeApiClient", autospec=True
        ) as flow_client,
    ):
        from custom_components.pvnode.api import ForecastResult, RequestLimit

        client = coordinator_client.return_value
        client.async_get_forecast = AsyncMock(
            return_value=ForecastResult(
                payload=load_forecast(),
                request_limit=RequestLimit(
                    limit="3000",
                    used=12,
                    remaining="2988",
                    reset="2026-09-01T00:00:00Z",
                ),
            )
        )
        client.async_get_site = AsyncMock(return_value=SITE)
        client.async_list_sites = AsyncMock(return_value=[SITE])

        flow_client.return_value.async_list_sites = AsyncMock(return_value=[SITE])
        flow_client.return_value.async_get_site = AsyncMock(return_value=SITE)

        yield client


async def setup_integration(
    hass: HomeAssistant, entry: MockConfigEntry
) -> MockConfigEntry:
    """Add and set up a config entry, then wait for entities to settle."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
