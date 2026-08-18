"""Button platform for the pvnode integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PvnodeConfigEntry, PvnodeDataUpdateCoordinator
from .entity import PvnodeSiteEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PvnodeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the pvnode buttons."""
    async_add_entities([PvnodeRefreshButton(entry.runtime_data, entry)])


class PvnodeRefreshButton(PvnodeSiteEntity, ButtonEntity):
    """Fetch a forecast now instead of waiting for the next slot.

    Home Assistant's own "reload" does not help here: it goes through the stored
    forecast and deliberately avoids an API call while that is still current. This is
    the escape hatch for the one case where waiting is wrong — a plan change, which
    invalidates the server-side cache and makes a fresh computation available
    immediately.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "refresh"

    def __init__(
        self, coordinator: PvnodeDataUpdateCoordinator, entry: PvnodeConfigEntry
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator, entry, "refresh")

    async def async_press(self) -> None:
        """Request a refresh.

        Goes through the debouncer rather than straight to `async_refresh`, which
        collapses a rapid burst of clicks into two requests instead of one per click —
        it delays rather than drops, so presses further apart than the cooldown each
        cost a request. Every press that lands counts against the monthly quota, even
        when the server answers from its cache.
        """
        await self.coordinator.async_request_refresh()
