"""Integration-level tests: setup, unload, and auth-failure → reauth."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er

from custom_components.tibber_app import TibberRuntimeData
from custom_components.tibber_app.api import TibberApiError, TibberAuthError
from custom_components.tibber_app.const import DOMAIN


class TestSetupAndUnload:
    async def test_entry_is_loaded(self, hass, setup_integration):
        """Config entry reaches LOADED state after a successful setup."""
        assert setup_integration.state is ConfigEntryState.LOADED

    async def test_platforms_are_forwarded(self, hass, setup_integration):
        """At least the sensor platform is forwarded (entities exist)."""
        reg = er.async_get(hass)
        sensor_entries = [
            e for e in reg.entities.values() if e.domain == "sensor"
        ]
        assert len(sensor_entries) > 0

    async def test_unload_succeeds(self, hass, setup_integration):
        """Entry can be unloaded cleanly without errors."""
        result = await hass.config_entries.async_unload(
            setup_integration.entry_id
        )
        assert result is True
        assert setup_integration.state is ConfigEntryState.NOT_LOADED

    async def test_runtime_data_has_coordinator_and_live(
        self, hass, setup_integration
    ):
        assert isinstance(setup_integration.runtime_data, TibberRuntimeData)
        assert setup_integration.runtime_data.coordinator is not None
        assert setup_integration.runtime_data.live is not None


class TestAuthFailureReauth:
    async def test_poll_auth_error_starts_reauth_flow(
        self, hass, setup_integration
    ):
        """When a poll raises TibberAuthError, HA starts a reauth flow."""
        coordinator = setup_integration.runtime_data.coordinator
        coordinator.client.gql = AsyncMock(
            side_effect=TibberAuthError("token expired")
        )

        await coordinator.async_request_refresh()
        await hass.async_block_till_done()

        flows = hass.config_entries.flow.async_progress()
        reauth = [
            f
            for f in flows
            if f["context"]["source"] == config_entries.SOURCE_REAUTH
        ]
        assert len(reauth) == 1
        assert reauth[0]["context"]["entry_id"] == setup_integration.entry_id

    async def test_entry_state_after_auth_failure(
        self, hass, setup_integration
    ):
        """Entry stays LOADED (so poll can resume once reauth completes)."""
        coordinator = setup_integration.runtime_data.coordinator
        coordinator.client.gql = AsyncMock(
            side_effect=TibberAuthError("token expired")
        )

        await coordinator.async_request_refresh()
        await hass.async_block_till_done()

        assert setup_integration.state is ConfigEntryState.LOADED


class TestPeakControlNotAllowed:
    async def test_setup_drops_peak_control_and_retries_once(
        self, hass, config_entry, mock_client, caplog
    ):
        """A home without peak control (live: Tesla via car API, no charger)."""
        dispatch = mock_client.gql.side_effect
        polls: list[str] = []

        async def gql(query, variables=None, *, partial_ok=False):
            if "vehicle_ev_1" in query:
                polls.append(query)
                if "peakControlData" in query:
                    raise TibberApiError(
                        "GraphQL error: Home is not allowed to fetch "
                        "PeakControl Gizmo data"
                    )
            return await dispatch(query, variables, partial_ok=partial_ok)

        mock_client.gql = AsyncMock(side_effect=gql)
        with (
            patch(
                "custom_components.tibber_app.TibberAppClient",
                return_value=mock_client,
            ),
            patch("custom_components.tibber_app.LiveMeterManager") as live,
        ):
            live.return_value.async_stop = AsyncMock()
            config_entry.add_to_hass(hass)
            await hass.config_entries.async_setup(config_entry.entry_id)
            await hass.async_block_till_done()

        assert config_entry.state is ConfigEntryState.LOADED
        assert len(polls) == 2
        assert "peakControlData" not in polls[1]

        coordinator = config_entry.runtime_data.coordinator
        await coordinator.async_refresh()
        assert len(polls) == 3
        assert "peakControlData" not in polls[2]
        assert caplog.text.count("does not allow peak control") == 1

        reg = er.async_get(hass)
        prefix = f"{config_entry.entry_id}_home-1_"
        assert not reg.async_get_entity_id("switch", DOMAIN, prefix + "peak_control")
        assert not reg.async_get_entity_id("number", DOMAIN, prefix + "peak_limit")
        assert reg.async_get_entity_id("switch", DOMAIN, prefix + "away_mode")
