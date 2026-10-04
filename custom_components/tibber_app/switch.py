"""Switch platform for the Tibber app integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import TibberConfigEntry
from .const import (
    GIZMO_ELECTRIC_VEHICLE,
    GIZMO_EV_CHARGER,
    VEHICLE_SMART_CHARGING_SUFFIX,
)
from .coordinator import TibberDataUpdateCoordinator, TibberDevice
from .entity import TibberEntity, TibberHomeEntity

# Charger boolean settings: (setting key, translation key).
CHARGER_SWITCHES: tuple[tuple[str, str], ...] = (
    ("isCableLockPermanent", "cable_lock"),
    ("fuseLoadBalancing", "load_balancing"),
)

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TibberConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tibber app switches."""
    coordinator = entry.runtime_data.coordinator
    entities: list[SwitchEntity] = []

    for dev in coordinator.devices_of_type(GIZMO_EV_CHARGER):
        entities += [
            TibberChargerSwitch(coordinator, dev, key, tkey)
            for key, tkey in CHARGER_SWITCHES
        ]
    for dev in coordinator.devices_of_type(GIZMO_ELECTRIC_VEHICLE):
        # Only vehicles that expose the toggle can be switched; which namespace it
        # lives in depends on how the vehicle was added to the account.
        key = coordinator.vehicle_setting_key(dev.id, VEHICLE_SMART_CHARGING_SUFFIX)
        if key is None:
            _LOGGER.debug("No smart-charging setting for vehicle %s", dev.id)
            continue
        entities.append(TibberSmartChargingSwitch(coordinator, dev, key))
    for home_id in coordinator.home_titles:
        entities.append(TibberAwayModeSwitch(coordinator, home_id))
        if home_id not in coordinator.peak_control_denied:
            entities.append(TibberPeakControlSwitch(coordinator, home_id))

    async_add_entities(entities)


class TibberChargerSwitch(TibberEntity, SwitchEntity):
    """A boolean charger setting written via setVehicleChargerSettings."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: TibberDataUpdateCoordinator,
        device: TibberDevice,
        setting_key: str,
        translation_key: str,
    ) -> None:
        super().__init__(coordinator, device, translation_key)
        self._setting_key = setting_key
        self._attr_translation_key = translation_key

    @property
    def is_on(self) -> bool | None:
        node = self.coordinator.data.chargers.get(self._device.id) or {}
        for setting in node.get("userSettings") or []:
            if setting.get("key") == self._setting_key:
                return str(setting.get("value")).lower() in ("true", "1")
        return None

    async def _set(self, value: bool) -> None:
        await self.coordinator.async_set_charger_setting(
            self._device.id, self._device.home_id, self._setting_key, value
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class TibberSmartChargingSwitch(TibberEntity, SwitchEntity):
    """Per-vehicle smart-charging toggle (real read-back via user settings)."""

    _attr_translation_key = "smart_charging"

    def __init__(
        self,
        coordinator: TibberDataUpdateCoordinator,
        device: TibberDevice,
        setting_key: str,
    ) -> None:
        super().__init__(coordinator, device, "smart_charging")
        self._fallback_key = setting_key

    @property
    def _setting_key(self) -> str:
        """The key this vehicle currently exposes, else the one found at setup."""
        return (
            self.coordinator.vehicle_setting_key(
                self._device.id, VEHICLE_SMART_CHARGING_SUFFIX
            )
            or self._fallback_key
        )

    @property
    def is_on(self) -> bool | None:
        node = self.coordinator.data.vehicles.get(self._device.id) or {}
        key = self._setting_key
        for setting in node.get("userSettings") or []:
            if setting.get("key") == key:
                return str(setting.get("value")).lower() in ("true", "1")
        return None

    async def _set(self, value: bool) -> None:
        await self.coordinator.async_set_vehicle_setting(
            self._device.id,
            self._device.home_id,
            self._setting_key,
            value,
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class TibberAwayModeSwitch(TibberHomeEntity, SwitchEntity):
    """Toggle away mode for a home (optimistic; no read-back field)."""

    _attr_translation_key = "away_mode"
    _attr_assumed_state = True

    def __init__(self, coordinator: TibberDataUpdateCoordinator, home_id: str) -> None:
        super().__init__(coordinator, home_id, "away_mode")
        self._is_on = False

    @property
    def is_on(self) -> bool | None:
        return self._is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        now = dt_util.utcnow()
        far = now + timedelta(days=365)
        await self.coordinator.async_set_away_mode(
            self._home_id, True, now.isoformat(), far.isoformat()
        )
        self._is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        now = dt_util.utcnow()
        await self.coordinator.async_set_away_mode(
            self._home_id, False, now.isoformat(), now.isoformat()
        )
        self._is_on = False
        self.async_write_ha_state()


class TibberPeakControlSwitch(TibberHomeEntity, SwitchEntity):
    """Activate/deactivate peak control for a home."""

    _attr_translation_key = "peak_control"

    def __init__(self, coordinator: TibberDataUpdateCoordinator, home_id: str) -> None:
        super().__init__(coordinator, home_id, "peak_control")

    @property
    def _peak(self) -> dict[str, Any]:
        return self._home.get("peakControl") or {}

    @property
    def available(self) -> bool:
        return super().available and self._peak.get("hasRealTimeDevice") is not False

    @property
    def is_on(self) -> bool | None:
        return self._peak.get("isActive")

    async def _set(self, active: bool) -> None:
        limit = self._peak.get("consumptionLimit") or self._peak.get("recommendedValue")
        if limit is None:
            return
        await self.coordinator.async_set_peak_control(self._home_id, active, limit)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
