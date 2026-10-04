"""Integration-level actions for the Tibber app integration.

``set_departure_schedule`` writes a vehicle's weekly departure schedule in one
call and answers with what Tibber holds afterwards. It targets the vehicle
device rather than its time entities, so one call can cover the whole week.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, GIZMO_ELECTRIC_VEHICLE, WEEKDAYS
from .coordinator import TibberDataUpdateCoordinator, TibberDevice

SERVICE_SET_DEPARTURE_SCHEDULE = "set_departure_schedule"

ATTR_SCHEDULE = "schedule"
ATTR_CLEAR_ALL = "clear_all"
ATTR_SMART_CHARGING = "smart_charging"

SET_DEPARTURE_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_SCHEDULE, default=dict): vol.Schema(
            {vol.Optional(day): vol.Any(None, cv.time) for day in WEEKDAYS}
        ),
        vol.Optional(ATTR_CLEAR_ALL, default=False): cv.boolean,
        vol.Optional(ATTR_SMART_CHARGING): cv.boolean,
    }
)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration-level actions."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_DEPARTURE_SCHEDULE,
        _async_set_departure_schedule,
        schema=SET_DEPARTURE_SCHEDULE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def _resolve_vehicle(
    hass: HomeAssistant, device_ids: list[str]
) -> tuple[TibberDataUpdateCoordinator, TibberDevice]:
    """Map the targeted HA device to its coordinator and Tibber vehicle."""
    if len(device_ids) != 1:
        raise ServiceValidationError("Target exactly one Tibber vehicle")
    device = dr.async_get(hass).async_get(device_ids[0])
    if device is None:
        raise ServiceValidationError(f"Unknown device: {device_ids[0]}")
    tibber_ids = {ident for domain, ident in device.identifiers if domain == DOMAIN}
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.state is not ConfigEntryState.LOADED
        ):
            continue
        coordinator: TibberDataUpdateCoordinator = entry.runtime_data.coordinator
        for vehicle in coordinator.devices_of_type(GIZMO_ELECTRIC_VEHICLE):
            if vehicle.id in tibber_ids:
                return coordinator, vehicle
    raise ServiceValidationError(f"{device.name} is not a loaded Tibber vehicle")


async def _async_set_departure_schedule(call: ServiceCall) -> ServiceResponse:
    """Apply the requested weekdays, verify them and return the stored week."""
    coordinator, vehicle = _resolve_vehicle(call.hass, call.data[ATTR_DEVICE_ID])
    schedule: dict[str, str | None] = {
        day: value.strftime("%H:%M") if value is not None else None
        for day, value in call.data[ATTR_SCHEDULE].items()
    }
    if call.data[ATTR_CLEAR_ALL]:
        # Listed days win, every other day is cleared.
        schedule = {day: schedule.get(day) for day in WEEKDAYS}
    smart_charging = call.data.get(ATTR_SMART_CHARGING)
    if not schedule and smart_charging is None:
        raise ServiceValidationError(
            "Nothing to do: give a schedule, clear_all or smart_charging"
        )
    if coordinator.vehicle_departure_keys(vehicle.id) is None:
        raise ServiceValidationError(f"{vehicle.name} has no departure schedule")

    result: dict[str, Any] = await coordinator.async_set_vehicle_departure_schedule(
        vehicle.id, vehicle.home_id, schedule, smart_charging
    )
    return result
