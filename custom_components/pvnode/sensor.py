"""Sensor platform for the pvnode integration.

Every sensor is a different cut of the same fetched curve — a value at a point in time,
a total over a window, or a maximum and when it occurs. Nothing here performs I/O.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfIrradiance,
    UnitOfPower,
    UnitOfPrecipitationDepth,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from .coordinator import PvnodeConfigEntry, PvnodeDataUpdateCoordinator
from .entity import PvnodeSiteEntity, PvnodeStringEntity
from .forecast import (
    PvnodeData,
    energy_between,
    peak,
    series_entries,
    value_at,
    value_now,
)

# Entities only read already-fetched coordinator data, so updates may run unthrottled.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class PvnodeSensorDescription(SensorEntityDescription):
    """A pvnode sensor and how to derive its value from a refresh."""

    value: Callable[[PvnodeData], StateType | datetime]
    # Key in `values[]` to publish as the `forecast` attribute, if any.
    series_key: str | None = None
    series_name: str = "value"
    attributes: Callable[[PvnodeData], dict[str, Any]] | None = None


@dataclass(frozen=True, kw_only=True)
class PvnodeStringSensorDescription(SensorEntityDescription):
    """A per-roof-surface sensor, derived from that string's own series."""

    value: Callable[[dict[datetime, dict[str, Any]], PvnodeData], StateType | datetime]
    series_key: str | None = None
    series_name: str = "value"


def _as_int(value: float | None) -> int | None:
    """Narrow a parsed number back to an int, for code-like values."""
    return int(value) if value is not None else None


def _peak_power(data: PvnodeData, offset: int) -> float | None:
    """Highest forecast power on the given day."""
    found = peak(data.values, *data.day_bounds(offset))
    return found[1] if found else None


def _peak_time(data: PvnodeData, offset: int) -> datetime | None:
    """When the highest forecast power occurs on the given day."""
    found = peak(data.values, *data.day_bounds(offset))
    return found[0] if found else None


def _hour_energy(data: PvnodeData, hours_ahead: int) -> float | None:
    """Forecast energy for the clock hour `hours_ahead` from now."""
    start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(
        hours=hours_ahead
    )
    return energy_between(data.values, start, start + timedelta(hours=1))


def _remaining_today(data: PvnodeData) -> float | None:
    """Forecast energy still to come today, from now until local midnight."""
    return energy_between(data.values, dt_util.utcnow(), data.day_bounds(0)[1])


def _daily(key: str) -> Callable[[PvnodeData, int], float | None]:
    """Build an accessor for a `daily[]` field on a given day offset."""

    def _get(data: PvnodeData, offset: int) -> float | None:
        day = data.day(offset)
        return day.get(key) if day else None

    return _get


_daily_energy = _daily("pv_energy_kwh")
_daily_energy_clearsky = _daily("pv_energy_kwh_clearsky")


POWER_SENSORS: tuple[PvnodeSensorDescription, ...] = (
    PvnodeSensorDescription(
        key="power_now",
        translation_key="power_now",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda d: value_now(d.values, "pv_power"),
        series_key="pv_power",
        series_name="watts",
    ),
    PvnodeSensorDescription(
        key="power_next_30m",
        translation_key="power_next_30m",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda d: value_at(
            d.values, "pv_power", dt_util.utcnow() + timedelta(minutes=30)
        ),
    ),
    PvnodeSensorDescription(
        key="power_next_1h",
        translation_key="power_next_1h",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda d: value_at(
            d.values, "pv_power", dt_util.utcnow() + timedelta(hours=1)
        ),
    ),
    PvnodeSensorDescription(
        key="peak_power_today",
        translation_key="peak_power_today",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        value=lambda d: _peak_power(d, 0),
    ),
    PvnodeSensorDescription(
        key="peak_power_tomorrow",
        translation_key="peak_power_tomorrow",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        value=lambda d: _peak_power(d, 1),
    ),
    PvnodeSensorDescription(
        key="peak_time_today",
        translation_key="peak_time_today",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda d: _peak_time(d, 0),
    ),
    PvnodeSensorDescription(
        key="peak_time_tomorrow",
        translation_key="peak_time_tomorrow",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda d: _peak_time(d, 1),
    ),
    PvnodeSensorDescription(
        key="power_clearsky_now",
        translation_key="power_clearsky_now",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda d: value_now(d.values, "pv_power_clearsky"),
        series_key="pv_power_clearsky",
        series_name="watts_clearsky",
    ),
)

ENERGY_SENSORS: tuple[PvnodeSensorDescription, ...] = (
    PvnodeSensorDescription(
        key="energy_remaining_today",
        translation_key="energy_remaining_today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value=_remaining_today,
    ),
    PvnodeSensorDescription(
        key="energy_current_hour",
        translation_key="energy_current_hour",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_display_precision=0,
        value=lambda d: _hour_energy(d, 0),
    ),
    PvnodeSensorDescription(
        key="energy_next_hour",
        translation_key="energy_next_hour",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_display_precision=0,
        value=lambda d: _hour_energy(d, 1),
    ),
    PvnodeSensorDescription(
        key="energy_today_clearsky",
        translation_key="energy_today_clearsky",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value=lambda d: _daily_energy_clearsky(d, 0),
    ),
)

# Site-wide weather, shipped alongside the forecast. Only the two headline values are on
# by default — the rest is available but would otherwise clutter every dashboard.
WEATHER_SENSORS: tuple[PvnodeSensorDescription, ...] = (
    PvnodeSensorDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value=lambda d: value_now(d.values, "temp"),
        series_key="temp",
        series_name="temperature",
    ),
    PvnodeSensorDescription(
        key="weather_code",
        translation_key="weather_code",
        # WMO code — an identifier, not a measurement, so it stays an int with no unit.
        value=lambda d: _as_int(value_now(d.values, "weather_code")),
    ),
    PvnodeSensorDescription(
        key="wind_speed",
        translation_key="wind_speed",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value=lambda d: value_now(d.values, "wind_speed"),
    ),
    PvnodeSensorDescription(
        key="relative_humidity",
        translation_key="relative_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value=lambda d: value_now(d.values, "relative_humidity"),
    ),
    PvnodeSensorDescription(
        key="precipitation",
        translation_key="precipitation",
        device_class=SensorDeviceClass.PRECIPITATION,
        native_unit_of_measurement=UnitOfPrecipitationDepth.MILLIMETERS,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value=lambda d: value_now(d.values, "precipitation_mm"),
    ),
    PvnodeSensorDescription(
        key="snow_water_equivalent",
        translation_key="snow_water_equivalent",
        device_class=SensorDeviceClass.PRECIPITATION,
        native_unit_of_measurement=UnitOfPrecipitationDepth.MILLIMETERS,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value=lambda d: value_now(d.values, "snow_water_equivalent"),
    ),
    PvnodeSensorDescription(
        key="ghi",
        translation_key="ghi",
        device_class=SensorDeviceClass.IRRADIANCE,
        native_unit_of_measurement=UnitOfIrradiance.WATTS_PER_SQUARE_METER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value=lambda d: value_now(d.values, "ghi"),
    ),
    PvnodeSensorDescription(
        key="dhi",
        translation_key="dhi",
        device_class=SensorDeviceClass.IRRADIANCE,
        native_unit_of_measurement=UnitOfIrradiance.WATTS_PER_SQUARE_METER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value=lambda d: value_now(d.values, "dhi"),
    ),
    PvnodeSensorDescription(
        key="bni",
        translation_key="bni",
        device_class=SensorDeviceClass.IRRADIANCE,
        native_unit_of_measurement=UnitOfIrradiance.WATTS_PER_SQUARE_METER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value=lambda d: value_now(d.values, "bni"),
    ),
)

# Only created when the plan includes `include=variability`. The band is a min/max
# envelope, not a percentile pair, and the names deliberately avoid suggesting
# otherwise. It is meaningful for roughly the next 48 hours; beyond that both bounds
# collapse onto `pv_power`.
VARIABILITY_SENSORS: tuple[PvnodeSensorDescription, ...] = (
    PvnodeSensorDescription(
        key="power_min_now",
        translation_key="power_min_now",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda d: value_now(d.values, "pv_power_min"),
        series_key="pv_power_min",
        series_name="watts_min",
    ),
    PvnodeSensorDescription(
        key="power_max_now",
        translation_key="power_max_now",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda d: value_now(d.values, "pv_power_max"),
        series_key="pv_power_max",
        series_name="watts_max",
    ),
    PvnodeSensorDescription(
        key="energy_min_today",
        translation_key="energy_min_today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value=lambda d: _daily("pv_energy_kwh_min")(d, 0),
    ),
    PvnodeSensorDescription(
        key="energy_max_today",
        translation_key="energy_max_today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value=lambda d: _daily("pv_energy_kwh_max")(d, 0),
    ),
)

DIAGNOSTIC_SENSORS: tuple[PvnodeSensorDescription, ...] = (
    PvnodeSensorDescription(
        key="last_updated",
        translation_key="last_updated",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: d.computed_at,
    ),
    PvnodeSensorDescription(
        key="next_update",
        translation_key="next_update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: d.next_poll_at,
        # `available` is what the plan permits, `included` what this response carried.
        # Seeing both side by side is what makes a plan change explainable.
        attributes=lambda d: {"included": d.included, "available": d.available},
    ),
)


# Per roof surface. Day totals are derived here rather than read from `daily[]`,
# which pvnode only reports site-wide.
STRING_SENSORS: tuple[PvnodeStringSensorDescription, ...] = (
    PvnodeStringSensorDescription(
        key="power_now",
        translation_key="power_now",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda series, data: value_now(series, "pv_power"),
        series_key="pv_power",
        series_name="watts",
    ),
    PvnodeStringSensorDescription(
        key="energy_today",
        translation_key="energy_today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value=lambda series, data: energy_between(series, *data.day_bounds(0)),
    ),
    PvnodeStringSensorDescription(
        key="energy_tomorrow",
        translation_key="energy_tomorrow",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value=lambda series, data: energy_between(series, *data.day_bounds(1)),
    ),
    PvnodeStringSensorDescription(
        key="gti",
        translation_key="gti",
        device_class=SensorDeviceClass.IRRADIANCE,
        native_unit_of_measurement=UnitOfIrradiance.WATTS_PER_SQUARE_METER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value=lambda series, data: value_now(series, "gti"),
    ),
    PvnodeStringSensorDescription(
        key="gti_shaded",
        translation_key="gti_shaded",
        device_class=SensorDeviceClass.IRRADIANCE,
        native_unit_of_measurement=UnitOfIrradiance.WATTS_PER_SQUARE_METER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value=lambda series, data: value_now(series, "gti_shaded"),
    ),
)

STRING_VARIABILITY_SENSORS: tuple[PvnodeStringSensorDescription, ...] = (
    PvnodeStringSensorDescription(
        key="power_min_now",
        translation_key="power_min_now",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda series, data: value_now(series, "pv_power_min"),
    ),
    PvnodeStringSensorDescription(
        key="power_max_now",
        translation_key="power_max_now",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda series, data: value_now(series, "pv_power_max"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PvnodeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the pvnode sensors."""
    coordinator = entry.runtime_data
    data = coordinator.data

    entities: list[PvnodeSiteEntity | PvnodeStringSensor] = [
        PvnodeSiteSensor(coordinator, entry, description)
        for group in (
            POWER_SENSORS,
            ENERGY_SENSORS,
            WEATHER_SENSORS,
            DIAGNOSTIC_SENSORS,
        )
        for description in group
    ]
    entities.append(PvnodeRequestsRemainingSensor(coordinator, entry))
    async_add_entities(entities)

    known_days = 0

    @callback
    def _sync_days() -> None:
        """Keep one energy sensor per forecast day the API returns.

        The horizon comes from the plan, so an upgrade lengthens it. Creating the extra
        days on sight means the user does not have to reload the integration to see
        them; shrinking is handled in the coordinator, which removes the entities that
        fell outside the horizon.
        """
        nonlocal known_days
        wanted = len(coordinator.data.daily) if coordinator.data else 1
        if wanted <= known_days:
            known_days = wanted
            return
        async_add_entities(
            PvnodeDailyEnergySensor(coordinator, entry, offset)
            for offset in range(known_days, wanted)
        )
        known_days = wanted

    known_strings: set[int] = set()
    band_added = False

    @callback
    def _add_strings(indexes: set[int]) -> None:
        """Create the entities for roof surfaces not seen before."""
        new: list[PvnodeStringSensor] = []
        for index in sorted(indexes - known_strings):
            known_strings.add(index)
            new.extend(
                PvnodeStringSensor(coordinator, entry, index, description)
                for description in STRING_SENSORS
            )
            if band_added:
                new.extend(
                    PvnodeStringSensor(coordinator, entry, index, description)
                    for description in STRING_VARIABILITY_SENSORS
                )
        if new:
            async_add_entities(new)

    @callback
    def _sync_band() -> None:
        """Create or drop the min/max sensors as the plan's entitlement changes.

        Adding on first sight rather than at setup means a plan upgrade shows up without
        a reload; removing means a downgrade does not leave a row of permanently unknown
        sensors behind.
        """
        nonlocal band_added
        has_band = bool(coordinator.data and coordinator.data.has_variability)

        if has_band and not band_added:
            band_added = True
            new: list[PvnodeSiteEntity | PvnodeStringSensor] = [
                PvnodeSiteSensor(coordinator, entry, description)
                for description in VARIABILITY_SENSORS
            ]
            for index in sorted(known_strings):
                new.extend(
                    PvnodeStringSensor(coordinator, entry, index, description)
                    for description in STRING_VARIABILITY_SENSORS
                )
            async_add_entities(new)
        elif not has_band:
            band_added = False
            # Runs on every refresh, not just on the transition: a downgrade that
            # happened while Home Assistant was stopped leaves entities that no
            # transition would ever catch.
            _remove_band_entities(hass, entry)

    _sync_days()
    _add_strings(set(data.strings) if data else set())
    _sync_band()
    entry.async_on_unload(coordinator.add_new_string_listener(_add_strings))
    entry.async_on_unload(coordinator.async_add_listener(_sync_band))
    entry.async_on_unload(coordinator.async_add_listener(_sync_days))


# Suffixes of the unique_ids that only exist while the plan carries the band.
_BAND_KEYS = frozenset(
    description.key
    for description in (*VARIABILITY_SENSORS, *STRING_VARIABILITY_SENSORS)
)


@callback
def _remove_band_entities(hass: HomeAssistant, entry: PvnodeConfigEntry) -> None:
    """Drop any min/max sensors left over from a plan that had the band."""
    registry = er.async_get(hass)
    for entity in list(er.async_entries_for_config_entry(registry, entry.entry_id)):
        if any(entity.unique_id.endswith(f"_{key}") for key in _BAND_KEYS):
            registry.async_remove(entity.entity_id)


class PvnodeSiteSensor(PvnodeSiteEntity, SensorEntity):
    """A site-level sensor described by a `PvnodeSensorDescription`."""

    entity_description: PvnodeSensorDescription
    # The multi-day series exceeds the recorder's 16 KiB per-attribute limit, and
    # historising a forecast-of-the-future is meaningless anyway.
    _unrecorded_attributes = frozenset({"forecast"})

    def __init__(
        self,
        coordinator: PvnodeDataUpdateCoordinator,
        entry: PvnodeConfigEntry,
        description: PvnodeSensorDescription,
    ) -> None:
        """Initialize from the description."""
        super().__init__(coordinator, entry, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType | datetime:
        """Derive the value from the current refresh."""
        if not self.coordinator.data:
            return None
        return self.entity_description.value(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Publish this metric's own time series and any extras it declares."""
        if not (data := self.coordinator.data):
            return None

        attributes: dict[str, Any] = {}
        if key := self.entity_description.series_key:
            attributes["forecast"] = series_entries(
                data.values, key, self.entity_description.series_name
            )
        if extra := self.entity_description.attributes:
            attributes.update(extra(data))
        return attributes or None


class PvnodeDailyEnergySensor(PvnodeSiteEntity, SensorEntity):
    """Forecast energy total for one day.

    Taken straight from `daily[].pv_energy_kwh` rather than re-summing the power curve:
    that is pvnode's own figure, and re-deriving it would only invent a way to disagree
    with the portal.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: PvnodeDataUpdateCoordinator,
        entry: PvnodeConfigEntry,
        day_offset: int,
    ) -> None:
        """Initialize the sensor for the given day offset (0 = today)."""
        super().__init__(coordinator, entry, f"energy_day{day_offset}")
        self._day_offset = day_offset
        if day_offset == 0:
            self._attr_translation_key = "energy_today"
        elif day_offset == 1:
            self._attr_translation_key = "energy_tomorrow"
        else:
            self._attr_translation_key = "energy_day_offset"
            self._attr_translation_placeholders = {"day": str(day_offset)}

    @property
    def native_value(self) -> float | None:
        """Forecast energy for this day."""
        if not self.coordinator.data:
            return None
        return _daily_energy(self.coordinator.data, self._day_offset)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Surface the day's other aggregates alongside the total."""
        if not self.coordinator.data or not (
            day := self.coordinator.data.day(self._day_offset)
        ):
            return None
        return {
            "date": day.get("date"),
            "temp_min": day.get("temp_min"),
            "temp_max": day.get("temp_max"),
            "weather_code": day.get("weather_code"),
            # True when pvnode had fewer than 96 timesteps for the day, so the total
            # covers less than the full day.
            "partial": day.get("partial", False),
        }


class PvnodeStringSensor(PvnodeStringEntity, SensorEntity):
    """A sensor for one roof surface, described by a `PvnodeStringSensorDescription`."""

    entity_description: PvnodeStringSensorDescription
    _unrecorded_attributes = frozenset({"forecast"})

    def __init__(
        self,
        coordinator: PvnodeDataUpdateCoordinator,
        entry: PvnodeConfigEntry,
        string_index: int,
        description: PvnodeStringSensorDescription,
    ) -> None:
        """Initialize from the description."""
        super().__init__(coordinator, entry, string_index, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType | datetime:
        """Derive the value from this string's series."""
        if not (data := self.coordinator.data):
            return None
        return self.entity_description.value(self._series, data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Publish this string's own time series, when the metric has one."""
        key = self.entity_description.series_key
        if not key or not self.coordinator.data:
            return None
        return {
            "forecast": series_entries(
                self._series, key, self.entity_description.series_name
            )
        }


class PvnodeRequestsRemainingSensor(PvnodeSiteEntity, SensorEntity):
    """Requests left in the monthly quota.

    Account-wide, not per site: pvnode counts the quota on the user id, so every
    configured site of the same account reports the same number.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "requests_remaining"

    def __init__(
        self, coordinator: PvnodeDataUpdateCoordinator, entry: PvnodeConfigEntry
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, "requests_remaining")

    @property
    def native_value(self) -> int | None:
        """Remaining requests, or None on an unmetered plan."""
        if not self.coordinator.data:
            return None
        try:
            return int(self.coordinator.data.request_limit.remaining)
        except (TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Limit, usage and reset instant behind the remaining count."""
        if not self.coordinator.data:
            return None
        limits = self.coordinator.data.request_limit
        interval = self.coordinator.update_interval
        return {
            "limit": limits.limit,
            "used": limits.used,
            "reset": limits.reset,
            "unmetered": limits.remaining == "unmetered",
            "poll_interval_seconds": interval.total_seconds() if interval else None,
        }
