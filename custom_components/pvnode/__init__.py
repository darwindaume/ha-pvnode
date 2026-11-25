"""The pvnode integration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.loader import async_get_integration

from .const import DOMAIN, PLATFORMS
from .coordinator import PvnodeConfigEntry, PvnodeDataUpdateCoordinator
from .entity import site_device_fields


async def async_setup_entry(hass: HomeAssistant, entry: PvnodeConfigEntry) -> bool:
    """Set up pvnode from a config entry."""
    integration = await async_get_integration(hass, DOMAIN)
    coordinator = PvnodeDataUpdateCoordinator(hass, entry, version=integration.version)

    # Only hit the API when the stored forecast is actually stale. Cached responses
    # count against the monthly quota, so restarting Home Assistant must be free.
    if not await coordinator.async_restore_from_store():
        await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(coordinator.async_start_local_refresh())

    # Register the site device before the platforms: roof-surface devices link to it by
    # device id (`via_device_id`), which must already exist when they are created.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **site_device_fields(entry)
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PvnodeConfigEntry) -> bool:
    """Unload a pvnode config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
