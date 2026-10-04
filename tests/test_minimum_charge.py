"""Minimum reserve number; synthetic bounds verify backend-defined behavior."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.tibber_app import number, queries
from custom_components.tibber_app.const import DOMAIN, GIZMO_ELECTRIC_VEHICLE


@pytest.fixture(autouse=True)
def reserve_setting(poll_data, request):
    setting = {
        "key": "online.vehicle.smartCharging.minChargeLimit",
        "value": 30,
        "isReadOnly": False,
        "valueType": "NUMBER",
        "inputOptions": {"rangeOptions": {"min": 10, "max": 90, "step": 5}},
    }
    if hasattr(request, "param"):
        setting["inputOptions"] = request.param
    poll_data["me"]["vehicle_ev_1"]["userSettings"].append(setting)
    return setting


def entity(entry):
    coordinator = entry.runtime_data.coordinator
    device = coordinator.devices_of_type(GIZMO_ELECTRIC_VEHICLE)[0]
    return number.TibberVehicleMinimumChargeNumber(coordinator, device)


@pytest.mark.parametrize("namespace", ["online", "offline"])
async def test_created_and_write_readback(
    hass, reserve_setting, namespace, setup_integration
):
    entry = setup_integration
    control = entity(entry)
    reserve_setting["key"] = f"{namespace}.vehicle.smartCharging.minChargeLimit"
    uid = f"{entry.entry_id}_ev-1_minimum_charge_level"
    eid = er.async_get(hass).async_get_entity_id("number", DOMAIN, uid)
    assert eid is not None
    assert control.unique_id == uid
    assert control.native_value == 30
    assert (
        control.native_min_value,
        control.native_max_value,
        control.native_step,
    ) == (10, 90, 5)
    assert control.entity_category.value == "config"
    assert control.native_unit_of_measurement == "%"
    coordinator = entry.runtime_data.coordinator

    async def refresh():
        reserve_setting["value"] = 40
        coordinator._fresh(
            "vehicle ev-1",
            coordinator.data.vehicles["ev-1"],
            coordinator._last_seen["vehicle ev-1"][1] + 1,
        )

    with (
        patch.object(coordinator.client, "gql", new_callable=AsyncMock) as mutation,
        patch.object(coordinator, "async_refresh", side_effect=refresh) as update,
    ):
        await control.async_set_native_value(35.0)
    mutation.assert_awaited_once_with(
        queries.SET_VEHICLE_SETTINGS,
        {
            "vehicleId": "ev-1",
            "homeId": control._device.home_id,
            "settings": [{"key": reserve_setting["key"], "value": 35}],
        },
        retries=1,
    )
    assert type(mutation.call_args.args[1]["settings"][0]["value"]) is int
    update.assert_awaited_once()
    assert control.native_value == 40  # backend readback wins over requested 35


async def test_no_setting_no_entity(hass, setup_integration):
    coordinator = setup_integration.runtime_data.coordinator
    coordinator.data.vehicles["ev-1"]["userSettings"] = []
    added = []
    await number.async_setup_entry(hass, setup_integration, added.extend)
    assert not any(
        isinstance(e, number.TibberVehicleMinimumChargeNumber) for e in added
    )


@pytest.mark.parametrize(
    "value,expected",
    [("30", 30), (30, 30), (None, None), (True, None), ("bad", None), ("nan", None)],
)
async def test_numeric_readback(value, expected, reserve_setting, setup_integration):
    reserve_setting["value"] = value
    assert entity(setup_integration).native_value == expected


@pytest.mark.parametrize(
    "options",
    [None, {}, {"min": 10, "max": 90, "step": 0}, {"min": 10, "max": 90, "step": "5"}],
)
async def test_missing_invalid_bounds(options, reserve_setting, setup_integration):
    reserve_setting["inputOptions"] = {"rangeOptions": options}
    control = entity(setup_integration)
    assert not control.available
    with pytest.raises(HomeAssistantError):
        await control.async_set_native_value(35.0)


@pytest.mark.parametrize("value", [9.0, 95.0, 32.0, 35.5, float("nan")])
async def test_invalid_value_no_mutation(value, setup_integration):
    control = entity(setup_integration)
    with (
        patch.object(control.coordinator.client, "gql") as mutation,
        pytest.raises(HomeAssistantError),
    ):
        await control.async_set_native_value(value)
    mutation.assert_not_called()


async def test_readonly(reserve_setting, setup_integration):
    reserve_setting["isReadOnly"] = True
    assert not entity(setup_integration).available


async def test_backend_error_no_retry_or_refresh(setup_integration):
    control = entity(setup_integration)
    with (
        patch.object(
            control.coordinator.client,
            "gql",
            side_effect=HomeAssistantError("rejected"),
        ) as mutation,
        patch.object(control.coordinator, "async_refresh") as refresh,
        pytest.raises(HomeAssistantError, match="rejected"),
    ):
        await control.async_set_native_value(35.0)
    mutation.assert_called_once()
    refresh.assert_not_called()
    assert control.native_value == 30


async def test_failed_readback_propagates(setup_integration):
    control = entity(setup_integration)
    coordinator = control.coordinator

    async def failed_refresh():
        coordinator.last_update_success = False

    with (
        patch.object(coordinator.client, "gql", new_callable=AsyncMock),
        patch.object(coordinator, "async_refresh", side_effect=failed_refresh),
        pytest.raises(HomeAssistantError, match="readback failed"),
    ):
        await control.async_set_native_value(35.0)
    assert control.native_value == 30


def test_english_strings_and_documentation():
    root = Path(__file__).parents[1]
    for filename in ("strings.json", "translations/en.json"):
        data = json.loads(
            (root / "custom_components/tibber_app" / filename).read_text()
        )
        assert (
            data["entity"]["number"]["minimum_charge_level"]["name"]
            == "Minimum charge level"
        )
    docs = (root / "README.md").read_text()
    assert "spontaneous driving" in docs
    assert "targetedStateOfCharge" in docs
    assert "inputOptions.rangeOptions" in docs


@pytest.mark.parametrize("reserve_setting", [None, {}], indirect=True)
async def test_registers_unavailable_without_metadata(hass, setup_integration):
    entry = setup_integration
    uid = f"{entry.entry_id}_ev-1_minimum_charge_level"
    eid = er.async_get(hass).async_get_entity_id("number", DOMAIN, uid)
    assert eid is not None
    assert hass.states.get(eid).state == "unavailable"


@pytest.mark.parametrize("field", ["selectOptions", "pickerOptions"])
async def test_discrete_app_options(field, reserve_setting, setup_integration):
    values = list(range(0, 76, 5))
    options = (
        [{"value": v} for v in values]
        if field == "selectOptions"
        else {"values": values}
    )
    reserve_setting["inputOptions"] = {field: options}
    control = entity(setup_integration)
    assert control.available
    assert control.native_min_value == 0
    assert control.native_max_value == 75
    assert control.native_step == 5


async def test_cached_vehicle_does_not_count_as_readback(setup_integration):
    control = entity(setup_integration)
    with (
        patch.object(control.coordinator.client, "gql", new_callable=AsyncMock),
        patch.object(control.coordinator, "async_refresh", new_callable=AsyncMock),
        pytest.raises(HomeAssistantError, match="readback failed"),
    ):
        await control.async_set_native_value(35.0)


async def test_poll_during_mutation_does_not_count_as_readback(setup_integration):
    control = entity(setup_integration)
    coordinator = control.coordinator

    async def mutation(*args, **kwargs):
        coordinator._fresh(
            "vehicle ev-1",
            coordinator.data.vehicles["ev-1"],
            coordinator._last_seen["vehicle ev-1"][1] + 1,
        )

    with (
        patch.object(coordinator.client, "gql", side_effect=mutation),
        patch.object(coordinator, "async_refresh", new_callable=AsyncMock),
        pytest.raises(HomeAssistantError, match="readback failed"),
    ):
        await control.async_set_native_value(35.0)
