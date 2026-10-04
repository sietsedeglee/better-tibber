"""Number platform for the Tibber app integration."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TibberConfigEntry
from .const import GIZMO_ELECTRIC_VEHICLE, GIZMO_EV_CHARGER, VEHICLE_SOC_KEY
from .coordinator import TibberDataUpdateCoordinator, TibberDevice
from .entity import TibberEntity, TibberHomeEntity

# Charger numeric settings: (setting key, translation key, min, max, step).
CHARGER_NUMBERS: tuple[tuple[str, str, float, float, float], ...] = (
    ("maxCurrentCharger", "max_current", 6, 32, 1),
    ("mainFuseSize", "main_fuse", 10, 63, 1),
    ("offlineFallbackCurrent", "offline_fallback_current", 0, 32, 1),
)


MIN_CHARGE_SUFFIX = "smartCharging.minChargeLimit"

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TibberConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tibber app number entities."""
    coordinator = entry.runtime_data.coordinator
    entities: list[NumberEntity] = []

    for dev in coordinator.devices_of_type(GIZMO_ELECTRIC_VEHICLE):
        # A manufacturer-connected vehicle reports its own charge level, and the
        # app hides the manual editor as soon as canReadLevel is true — writing
        # the override for such a vehicle is rejected by the backend. Anything
        # other than an explicit true keeps the entity, so vehicles that don't
        # report the flag at all behave as before.
        if coordinator.vehicle_setting_key(dev.id, MIN_CHARGE_SUFFIX):
            entities.append(TibberVehicleMinimumChargeNumber(coordinator, dev))
        battery = (coordinator.data.vehicles.get(dev.id) or {}).get("battery") or {}
        if battery.get("canReadLevel") is True:
            continue
        entities.append(TibberVehicleSocNumber(coordinator, dev))
    for dev in coordinator.devices_of_type(GIZMO_EV_CHARGER):
        entities += [
            TibberChargerNumber(coordinator, dev, key, tkey, lo, hi, step)
            for key, tkey, lo, hi, step in CHARGER_NUMBERS
        ]
    for home_id in coordinator.home_titles:
        entities.append(TibberPeakLimitNumber(coordinator, home_id))

    async_add_entities(entities)


class TibberVehicleSocNumber(TibberEntity, NumberEntity):
    """Manual state-of-charge override (the repo's original use case).

    Only created for vehicles whose level Tibber cannot read itself.
    """

    _attr_translation_key = "manual_soc"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = NumberDeviceClass.BATTERY
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self, coordinator: TibberDataUpdateCoordinator, device: TibberDevice
    ) -> None:
        super().__init__(coordinator, device, "manual_soc")

    @property
    def native_value(self) -> float | None:
        node = self.coordinator.data.vehicles.get(self._device.id) or {}
        return (node.get("battery") or {}).get("level")

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_vehicle_setting(
            self._device.id, self._device.home_id, VEHICLE_SOC_KEY, int(value)
        )


class TibberVehicleMinimumChargeNumber(TibberEntity, NumberEntity):
    """Tibber's reserve for spontaneous driving, with app-defined bounds."""

    _attr_translation_key = "minimum_charge_level"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(
        self, coordinator: TibberDataUpdateCoordinator, device: TibberDevice
    ) -> None:
        super().__init__(coordinator, device, "minimum_charge_level")

    @property
    def _setting(self) -> dict[str, Any]:
        key = self.coordinator.vehicle_setting_key(self._device.id, MIN_CHARGE_SUFFIX)
        node = self.coordinator.data.vehicles.get(self._device.id) or {}
        return next(
            (s for s in node.get("userSettings") or [] if s.get("key") == key), {}
        )

    @property
    def _bounds(self) -> tuple[int, int, int] | None:
        inputs = self._setting.get("inputOptions") or {}
        options = inputs.get("rangeOptions") or {}
        lo, hi, step = (options.get(k) for k in ("min", "max", "step"))
        if not all(type(v) is int for v in (lo, hi, step)):
            raw = [s.get("value") for s in inputs.get("selectOptions") or []]
            raw = raw or (inputs.get("pickerOptions") or {}).get("values") or []
            try:
                if any(isinstance(v, bool) or str(int(v)) != str(v) for v in raw):
                    return None
                values = sorted({int(v) for v in raw})
            except (TypeError, ValueError):
                return None
            if len(values) < 2:
                return None
            lo, hi, step = values[0], values[-1], values[1] - values[0]
            if values != list(range(lo, hi + 1, step)):
                return None
        if not 0 <= lo < hi <= 100 or step <= 0 or (hi - lo) % step:
            return None
        return lo, hi, step

    @property
    def capability_attributes(self) -> dict[str, Any] | None:
        if self._bounds is None:
            return {}
        return super().capability_attributes

    @property
    def available(self) -> bool:
        return (
            super().available
            and bool(self._setting)
            and self._setting.get("isReadOnly") is False
            and self._bounds is not None
        )

    @property
    def native_min_value(self) -> float | None:
        return self._bounds[0] if self._bounds else None

    @property
    def native_max_value(self) -> float | None:
        return self._bounds[1] if self._bounds else None

    @property
    def native_step(self) -> float | None:
        return self._bounds[2] if self._bounds else None

    @property
    def native_value(self) -> float | None:
        value = self._setting.get("value")
        if isinstance(value, bool):
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        return numeric if math.isfinite(numeric) else None

    async def async_set_native_value(self, value: float) -> None:
        bounds = self._bounds
        if not self.available or bounds is None:
            raise HomeAssistantError("Minimum charge level settings are unavailable")
        lo, hi, step = bounds
        if (
            not math.isfinite(value)
            or not float(value).is_integer()
            or not lo <= value <= hi
            or (int(value) - lo) % step
        ):
            raise HomeAssistantError(
                "Minimum charge level is outside the allowed range"
            )
        await self.coordinator.async_set_vehicle_setting(
            self._device.id,
            self._device.home_id,
            self._setting["key"],
            int(value),
            require_fresh_readback=True,
        )


class TibberChargerNumber(TibberEntity, NumberEntity):
    """A numeric charger setting written via setVehicleChargerSettings."""

    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: TibberDataUpdateCoordinator,
        device: TibberDevice,
        setting_key: str,
        translation_key: str,
        min_value: float,
        max_value: float,
        step: float,
    ) -> None:
        super().__init__(coordinator, device, translation_key)
        self._setting_key = setting_key
        self._attr_translation_key = translation_key
        self._attr_native_min_value = min_value
        self._attr_native_max_value = max_value
        self._attr_native_step = step

    @property
    def native_value(self) -> float | None:
        node = self.coordinator.data.chargers.get(self._device.id) or {}
        for setting in node.get("userSettings") or []:
            if setting.get("key") == self._setting_key:
                try:
                    return float(setting.get("value"))
                except (TypeError, ValueError):
                    return None
        return None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_charger_setting(
            self._device.id, self._device.home_id, self._setting_key, int(value)
        )


class TibberPeakLimitNumber(TibberHomeEntity, NumberEntity):
    """Peak-control hourly consumption limit (kWh/h)."""

    _attr_translation_key = "peak_limit"
    _attr_native_unit_of_measurement = "kWh"
    _attr_native_step = 0.1
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: TibberDataUpdateCoordinator, home_id: str) -> None:
        super().__init__(coordinator, home_id, "peak_limit")

    @property
    def _peak(self) -> dict[str, Any]:
        return self._home.get("peakControl") or {}

    @property
    def available(self) -> bool:
        return super().available and self._peak.get("hasRealTimeDevice") is not False

    @property
    def native_min_value(self) -> float:
        return self._peak.get("lowerBound") or 0

    @property
    def native_max_value(self) -> float:
        return self._peak.get("upperBound") or 100

    @property
    def native_value(self) -> float | None:
        return self._peak.get("consumptionLimit")

    async def async_set_native_value(self, value: float) -> None:
        is_active = bool(self._peak.get("isActive"))
        await self.coordinator.async_set_peak_control(self._home_id, is_active, value)
