"""The set_departure_schedule action: write, refresh once, verify, respond."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from custom_components.tibber_app import queries
from custom_components.tibber_app.const import DOMAIN, WEEKDAYS

PREFIX = "online.vehicle.smartCharging."


def _vehicle_device_id(hass) -> str:
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "ev-1")})
    assert device is not None
    return device.id


def _backend(poll_data: dict, *, apply_writes: bool = True):
    """Fake Tibber: records mutations, optionally stores them, serves polls."""
    mutations: list[tuple[list[dict], int]] = []
    polls: list[str] = []

    async def gql(query, variables=None, retries=4, *, partial_ok=False):
        if query == queries.SET_VEHICLE_SETTINGS:
            mutations.append((variables["settings"], retries))
            if apply_writes:
                stored = poll_data["me"]["vehicle_ev_1"]["userSettings"]
                for change in variables["settings"]:
                    for setting in stored:
                        if setting["key"] == change["key"]:
                            value = change["value"]
                            setting["value"] = (
                                "No departure time" if value is None else value
                            )
            return {}
        polls.append(query)
        return poll_data

    return AsyncMock(side_effect=gql), mutations, polls


async def _call(hass, data: dict):
    return await hass.services.async_call(
        DOMAIN,
        "set_departure_schedule",
        data,
        target={"device_id": _vehicle_device_id(hass)},
        blocking=True,
        return_response=True,
    )


async def test_trip_sets_one_day_and_clears_the_others(
    hass, connected_vehicle_poll_data, setup_integration
):
    """A trip: Friday gets a time, the rest of the week is emptied."""
    coordinator = setup_integration.runtime_data.coordinator
    gql, mutations, polls = _backend(connected_vehicle_poll_data)
    schedule = dict.fromkeys(WEEKDAYS) | {"friday": "06:45"}

    with patch.object(coordinator.client, "gql", new=gql):
        response = await _call(hass, {"schedule": schedule, "smart_charging": True})

    # Each write is its own single-attempt mutation: the time first, then one
    # null per cleared day, then the smart-charging flag.
    assert all(retries == 1 for _, retries in mutations)
    assert [settings for settings, _ in mutations] == [
        [{"key": f"{PREFIX}departureTimes.friday", "value": "06:45"}],
        *(
            [{"key": f"{PREFIX}departureTimes.{day}", "value": None}]
            for day in WEEKDAYS
            if day != "friday"
        ),
        [{"key": f"{PREFIX}isEnabled", "value": True}],
    ]
    assert len(polls) == 1
    assert response == {
        "schedule": {day: "06:45" if day == "friday" else None for day in WEEKDAYS},
        "smart_charging": True,
    }


async def test_clear_all_empties_the_whole_week(
    hass, connected_vehicle_poll_data, setup_integration
):
    """The default state: no departure time on any weekday."""
    coordinator = setup_integration.runtime_data.coordinator
    gql, mutations, polls = _backend(connected_vehicle_poll_data)

    with patch.object(coordinator.client, "gql", new=gql):
        response = await _call(hass, {"schedule": {}, "clear_all": True})

    assert [settings for settings, _ in mutations] == [
        [{"key": f"{PREFIX}departureTimes.{day}", "value": None}] for day in WEEKDAYS
    ]
    assert all(retries == 1 for _, retries in mutations)
    assert len(polls) == 1
    assert response == {
        "schedule": dict.fromkeys(WEEKDAYS),
        "smart_charging": True,
    }


async def test_raises_when_tibber_keeps_a_time(
    hass, connected_vehicle_poll_data, setup_integration
):
    """Tibber accepts the clear, but Monday still holds its time afterwards."""
    coordinator = setup_integration.runtime_data.coordinator
    gql, mutations, polls = _backend(connected_vehicle_poll_data, apply_writes=False)

    with (
        patch.object(coordinator.client, "gql", new=gql),
        pytest.raises(HomeAssistantError, match="monday") as err,
    ):
        await _call(hass, {"schedule": {}, "clear_all": True})

    assert "tuesday" not in str(err.value)
    assert len(mutations) == 7
    assert len(polls) == 1
