"""The Better Tibber integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import TibberAppClient
from .const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_TOKEN,
    DOMAIN,
    GIZMO_BATTERY,
    GIZMO_REAL_TIME_METER,
    MANUFACTURER,
)
from .coordinator import TibberDataUpdateCoordinator
from .live import LiveMeterManager
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
    Platform.WEATHER,
]


@dataclass
class TibberRuntimeData:
    """Objects shared with the platforms via ``entry.runtime_data``."""

    coordinator: TibberDataUpdateCoordinator
    live: LiveMeterManager


type TibberConfigEntry = ConfigEntry[TibberRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration-level actions once, independent of entries."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TibberConfigEntry) -> bool:
    """Set up Tibber app from a config entry."""
    session = async_get_clientsession(hass)
    client = TibberAppClient(
        session,
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        token=entry.data.get(CONF_TOKEN),
    )

    coordinator = TibberDataUpdateCoordinator(hass, entry, client)
    await coordinator.async_discover()
    await coordinator.async_config_entry_first_refresh()

    # Register one HA device per home so per-device entities can use via_device.
    device_registry = dr.async_get(hass)
    for home_id, title in coordinator.home_titles.items():
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, home_id)},
            manufacturer=MANUFACTURER,
            name=title,
            model="Home",
        )

    live = LiveMeterManager(coordinator)
    live.start(
        [d.id for d in coordinator.devices_of_type(GIZMO_REAL_TIME_METER)],
        [d.id for d in coordinator.devices_of_type(GIZMO_BATTERY)],
    )

    entry.runtime_data = TibberRuntimeData(coordinator=coordinator, live=live)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TibberConfigEntry) -> bool:
    """Unload a config entry."""
    await entry.runtime_data.live.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
