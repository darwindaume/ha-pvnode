"""Base entities for the pvnode integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import PvnodeConfigEntry, PvnodeDataUpdateCoordinator
from .strings_meta import string_model


class PvnodeSiteEntity(CoordinatorEntity[PvnodeDataUpdateCoordinator]):
    """Base class for entities on the site device.

    Platform-agnostic: subclasses mix in `SensorEntity`, `ButtonEntity` and so on.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PvnodeDataUpdateCoordinator,
        entry: PvnodeConfigEntry,
        key: str,
    ) -> None:
        """Attach the entity to the site device."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            entry_type=DeviceEntryType.SERVICE,
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=MANUFACTURER,
            model="pvnode API v2",
            configuration_url="https://pvnode.com",
        )


class PvnodeStringEntity(CoordinatorEntity[PvnodeDataUpdateCoordinator]):
    """Base class for entities on a per-string (roof surface) device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PvnodeDataUpdateCoordinator,
        entry: PvnodeConfigEntry,
        string_index: int,
        key: str,
    ) -> None:
        """Attach the entity to the device of one roof surface."""
        super().__init__(coordinator)
        self._entry = entry
        self._string_index = string_index
        self._attr_unique_id = f"{entry.entry_id}_string{string_index}_{key}"

        # `PVString` has no name field, so the device is named after what tells two roof
        # surfaces apart: bearing, tilt and size. The site title is part of the name
        # because Home Assistant does not prefix a `via_device` child with its parent —
        # without it, two sites would both produce a "Dachfläche 1". Surfaces identical
        # in all three get a positional marker, decided in `build_string_names` where
        # every string of the site is visible at once.
        names = coordinator.data.string_names if coordinator.data else {}
        placeholders = names.get(string_index)
        if placeholders and "position" in placeholders:
            translation_key = "string_device_numbered"
            translation_placeholders = {"site": entry.title, **placeholders}
        elif placeholders:
            translation_key = "string_device"
            translation_placeholders = {"site": entry.title, **placeholders}
        else:
            translation_key = "string_device_fallback"
            translation_placeholders = {
                "site": entry.title,
                "index": str(string_index + 1),
            }

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_string{string_index}")},
            translation_key=translation_key,
            translation_placeholders=translation_placeholders,
            manufacturer=MANUFACTURER,
            model=string_model(placeholders),
            via_device=(DOMAIN, entry.entry_id),
        )

    @property
    def _series(self) -> dict:
        """This string's time series, or an empty one while unavailable."""
        if not self.coordinator.data:
            return {}
        return self.coordinator.data.strings.get(self._string_index, {})

    @property
    def available(self) -> bool:
        """False once this string stops appearing in the payload."""
        return super().available and bool(self._series)
