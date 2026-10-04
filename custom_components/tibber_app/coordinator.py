"""Data update coordinator for the Tibber app integration.

Discovers homes + devices from ``me.home.gizmos`` once, then on each refresh issues
one combined GraphQL query per home (per-device sub-selections are aliased). Live
Pulse data arrives separately over WebSocket and is merged into ``data`` so the
meter sensors update in near real time.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta as _timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import queries
from .api import TibberApiError, TibberAppClient, TibberAuthError
from .const import (
    DOMAIN,
    GIZMO_BATTERY,
    GIZMO_ELECTRIC_VEHICLE,
    GIZMO_EV_CHARGER,
    GIZMO_INVERTER,
    GIZMO_REAL_TIME_METER,
    GIZMO_THERMOSTAT,
    SCAN_INTERVAL,
    STALE_GRACE,
    VEHICLE_DEPARTURE_SUFFIX,
    VEHICLE_SMART_CHARGING_SUFFIX,
    WEEKDAYS,
)
from .vehicle_settings import parse_departure_time

_LOGGER = logging.getLogger(__name__)

# Gizmo types we turn into HA devices; other gizmos (PRICE, WEATHER, …) are
# home-screen widgets, not physical devices.
_DEVICE_GIZMO_TYPES = frozenset(
    {
        GIZMO_ELECTRIC_VEHICLE,
        GIZMO_EV_CHARGER,
        GIZMO_REAL_TIME_METER,
        GIZMO_BATTERY,
        GIZMO_INVERTER,
        GIZMO_THERMOSTAT,
    }
)

# The backend rejects the peak-control block for homes without the feature, and
# fails the whole query with it rather than nulling just that field.
_PEAK_CONTROL_DENIED = "not allowed to fetch peakcontrol"

# Home-scoped devices: (gizmo type, alias prefix, GraphQL field, field selection).
_HOME_DEVICE_BLOCKS = (
    (GIZMO_EV_CHARGER, "charger", "vehicleCharger", queries.CHARGER_FIELDS),
    (GIZMO_BATTERY, "battery", "battery", queries.BATTERY_FIELDS),
    (GIZMO_INVERTER, "inverter", "inverter", queries.INVERTER_FIELDS),
    (GIZMO_THERMOSTAT, "thermostat", "thermostat", queries.THERMOSTAT_FIELDS),
)


@dataclass
class TibberDevice:
    """A device discovered from a home's gizmo list."""

    id: str
    name: str
    type: str
    home_id: str


@dataclass
class TibberData:
    """Parsed snapshot returned by the coordinator each refresh."""

    homes: dict[str, dict[str, Any]] = field(default_factory=dict)
    vehicles: dict[str, dict[str, Any]] = field(default_factory=dict)
    chargers: dict[str, dict[str, Any]] = field(default_factory=dict)
    batteries: dict[str, dict[str, Any]] = field(default_factory=dict)
    inverters: dict[str, dict[str, Any]] = field(default_factory=dict)
    thermostats: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Inverter production summary, keyed by inverter id (separate tolerant fetch).
    inverter_production: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Live Pulse measurements, keyed by pulse device id; filled by the WS listener.
    live: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Live battery state, keyed by battery device id; filled by the WS listener.
    battery_live: dict[str, dict[str, Any]] = field(default_factory=dict)


def _alias(prefix: str, device_id: str) -> str:
    """Return a GraphQL-safe alias for a UUID device id."""
    return f"{prefix}_{device_id.replace('-', '_')}"


def _device_block(prefix: str, gql_field: str, device_id: str, fields: str) -> str:
    """Build an aliased per-device sub-selection for the combined home query."""
    return f'{_alias(prefix, device_id)}: {gql_field}(id: "{device_id}") {{{fields}}}'


class TibberDataUpdateCoordinator(DataUpdateCoordinator[TibberData]):
    """Polls the app API and holds the merged device state."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: TibberAppClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
            config_entry=entry,
        )
        self.client = client
        self.devices: list[TibberDevice] = []
        self.home_titles: dict[str, str] = {}
        # Homes that actually have Grid Rewards data (set at discovery).
        self.grid_reward_homes: set[str] = set()
        # Homes whose peak-control data the backend refuses (set on first denial).
        self.peak_control_denied: set[str] = set()
        # Last fetched grid rewards per home (kept across polls; the period query
        # is fetched separately because it errors for homes without data).
        self._grid_rewards: dict[str, dict[str, Any]] = {}
        # Last fetched inverter production summary, keyed by inverter id.
        self._inverter_production: dict[str, dict[str, Any]] = {}
        # Preserve live WS data across polls.
        self._live: dict[str, dict[str, Any]] = {}
        self._battery_live: dict[str, dict[str, Any]] = {}
        # Last-known node per home/device, with the monotonic time it arrived.
        # Used to bridge polls where the backend nulls a device out; see _fresh().
        self._last_seen: dict[str, tuple[dict[str, Any], float]] = {}

    # -- discovery ----------------------------------------------------------
    async def async_discover(self) -> None:
        """Enumerate homes and their devices. Called once before first refresh."""
        try:
            data = await self.client.gql(queries.DISCOVERY)
        except TibberAuthError as err:
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except TibberApiError as err:
            raise ConfigEntryNotReady(f"Discovery failed: {err}") from err

        devices: dict[str, TibberDevice] = {}
        for home in data.get("me", {}).get("homes") or []:
            home_id = home["id"]
            self.home_titles[home_id] = home.get("title") or "Home"
            for gizmo in home.get("gizmos") or []:
                # Flatten gizmo groups into their member gizmos.
                members = gizmo.get("gizmos") or [gizmo]
                for member in members:
                    gid, gtype = member.get("id"), member.get("type")
                    if not gid or not gtype or gtype not in _DEVICE_GIZMO_TYPES:
                        continue
                    # Account-level devices (e.g. vehicles) show up under every
                    # home; keep the first sighting so we create one HA device.
                    if gid in devices:
                        continue
                    devices[gid] = TibberDevice(
                        id=gid,
                        name=member.get("title") or gtype.title(),
                        type=gtype,
                        home_id=home_id,
                    )
        self.devices = list(devices.values())
        await self._discover_grid_rewards()
        _LOGGER.debug(
            "Discovered %d devices across %d homes",
            len(devices),
            len(self.home_titles),
        )

    async def _discover_grid_rewards(self) -> None:
        """Flag homes that have a non-empty Grid Rewards history."""
        for home_id in self.home_titles:
            try:
                res = await self.client.gql(
                    queries.GRID_REWARDS_HISTORY, {"homeId": home_id}, partial_ok=True
                )
            except TibberApiError:
                continue
            history = ((res.get("me") or {}).get("home") or {}).get(
                "gridRewardsHistory"
            ) or {}
            # A home with no rewards reports valuesFrom == valuesTo (empty range).
            if history.get("valuesFrom") and history.get("valuesFrom") != history.get(
                "valuesTo"
            ):
                self.grid_reward_homes.add(home_id)

    def devices_of_type(self, gizmo_type: str) -> list[TibberDevice]:
        """Return discovered devices of a given gizmo type."""
        return [d for d in self.devices if d.type == gizmo_type]

    def vehicle_setting_key(self, vehicle_id: str, suffix: str) -> str | None:
        """Return the vehicle's own setting key ending in ``suffix``, if any.

        Setting keys are namespaced by how the vehicle was added (``offline.``
        for manually added ones, ``online.`` for manufacturer-connected ones),
        so the key to read and write has to come from the vehicle's own
        userSettings rather than a hardcoded namespace.
        """
        node = (self.data.vehicles.get(vehicle_id) if self.data else None) or {}
        for setting in node.get("userSettings") or []:
            key = setting.get("key")
            if isinstance(key, str) and key.endswith(suffix):
                return key
        return None

    async def _fetch_grid_rewards(self) -> None:
        """Fetch current-month rewards for eligible homes, tolerating errors."""
        if not self.grid_reward_homes:
            return
        now = dt_util.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (month_start + _timedelta(days=32)).replace(day=1)
        for home_id in self.grid_reward_homes:
            try:
                res = await self.client.gql(
                    queries.GRID_REWARDS_PERIOD,
                    {
                        "homeId": home_id,
                        "from": month_start.isoformat(),
                        "to": next_month.isoformat(),
                        "resolution": "monthly",
                    },
                    partial_ok=True,
                )
            except TibberApiError as err:
                _LOGGER.debug("Grid rewards fetch failed for %s: %s", home_id, err)
                continue
            period = ((res.get("me") or {}).get("home") or {}).get(
                "gridRewardsHistoryPeriod"
            )
            if period:
                self._grid_rewards[home_id] = period

    # -- polling ------------------------------------------------------------
    async def _async_update_data(self) -> TibberData:
        query = self._build_query()
        # The first poll decides which entities exist (number.py reads the
        # vehicle's canReadLevel to pick the manual-SoC entity), so it has to be
        # complete — a partial one here would bake a gap in until the next
        # reload. Failing instead leaves HA to retry setup. Later polls take
        # whatever resolved and carry the rest over.
        first = self.data is None
        try:
            try:
                raw = await self.client.gql(query, partial_ok=not first)
            except TibberApiError as err:
                if not self._deny_peak_control(err):
                    raise
                raw = await self.client.gql(self._build_query(), partial_ok=not first)
        except TibberAuthError as err:
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except TibberApiError as err:
            raise UpdateFailed(f"Update failed: {err}") from err
        await self._fetch_grid_rewards()
        await self._fetch_inverter_production()
        return self._parse(raw)

    def _deny_peak_control(self, err: TibberApiError) -> bool:
        """Drop peak control from the query if ``err`` is its permission error.

        The error does not say which home it is about, so every home still
        asking for it stops doing so. Returns False when there is nothing left
        to drop, so the caller re-raises instead of retrying the same query.
        """
        homes = set(self.home_titles) - self.peak_control_denied
        if _PEAK_CONTROL_DENIED not in str(err).lower() or not homes:
            return False
        _LOGGER.warning(
            "Tibber does not allow peak control data for this account; "
            "peak control entities are disabled (%s)",
            err,
        )
        self.peak_control_denied |= homes
        return True

    def _build_query(self) -> str:
        """Assemble one combined query: account vehicles + per-home device blocks."""
        me_parts: list[str] = ["id"]

        # Vehicles live at me.vehicle(id:), not under a home.
        for dev in self.devices_of_type(GIZMO_ELECTRIC_VEHICLE):
            me_parts.append(
                f'{_alias("vehicle", dev.id)}: vehicle(id: "{dev.id}") {{'
                f"{queries.VEHICLE_FIELDS}}}"
            )

        for home_id in self.home_titles:
            home_parts = [
                queries.PRICE_FIELDS,
                queries.CONSUMPTION_GIZMO_FIELDS,
                queries.WEATHER_FIELDS,
            ]
            if home_id not in self.peak_control_denied:
                home_parts.append(queries.PEAK_CONTROL_FIELDS)
            for gtype, prefix, gql_field, fields in _HOME_DEVICE_BLOCKS:
                for dev in self.devices_of_type(gtype):
                    if dev.home_id == home_id:
                        home_parts.append(
                            _device_block(prefix, gql_field, dev.id, fields)
                        )
            me_parts.append(
                f'{_alias("home", home_id)}: home(id: "{home_id}") {{'
                + "\n".join(home_parts)
                + "}"
            )

        return "{ me { " + "\n".join(me_parts) + " } }"

    def _fresh(
        self, key: str, node: dict[str, Any] | None, now: float
    ) -> dict[str, Any] | None:
        """Return ``node``, or the last-known one when this poll didn't carry it.

        The backend answers a partly-failed query with the timed-out field set to
        null and everything else intact, so a device dropping out of one poll says
        nothing about the device — only about that request. Reusing the previous
        payload for up to ``STALE_GRACE`` keeps the entity on its last reading;
        after that it's dropped so a device that really went away stops reporting.
        """
        if node:
            self._last_seen[key] = (node, now)
            return node
        cached = self._last_seen.get(key)
        if cached is None:
            return None
        previous, seen_at = cached
        if now - seen_at > STALE_GRACE.total_seconds():
            del self._last_seen[key]
            return None
        _LOGGER.debug("%s missing from this poll, reusing last known values", key)
        return previous

    def stale_ages(self) -> dict[str, int]:
        """Seconds since each home/device last came back in a poll.

        Everything is refreshed every poll, so a non-trivial age here means that
        node has been timing out upstream — which is what to look at when an
        entity is holding a value longer than it should. Labelled by device name
        and home position so the result can be shared without leaking ids.
        """
        now = time.monotonic()
        labels = {d.id: d.name for d in self.devices}
        labels |= {hid: str(n) for n, hid in enumerate(self.home_titles, 1)}
        ages: dict[str, int] = {}
        for key, (_, seen_at) in self._last_seen.items():
            kind, _, ident = key.partition(" ")
            ages[f"{kind} {labels.get(ident, '?')}"] = int(now - seen_at)
        return ages

    def _parse(self, raw: dict[str, Any]) -> TibberData:
        me = raw.get("me") or {}
        now = time.monotonic()
        data = TibberData(
            live=dict(self._live),
            battery_live=dict(self._battery_live),
            inverter_production=dict(self._inverter_production),
        )

        for dev in self.devices_of_type(GIZMO_ELECTRIC_VEHICLE):
            node = self._fresh(
                f"vehicle {dev.id}", me.get(_alias("vehicle", dev.id)), now
            )
            if node:
                data.vehicles[dev.id] = node

        for home_id in self.home_titles:
            home_node = self._fresh(
                f"home {home_id}", me.get(_alias("home", home_id)), now
            )
            if not home_node:
                continue
            data.homes[home_id] = {
                "title": self.home_titles[home_id],
                "price": home_node.get("subscription", {}).get("priceRating"),
                "hasSignedEnergyDeal": home_node.get("hasSignedEnergyDeal"),
                "consumption": home_node.get("consumptionGizmoData"),
                "peakControl": home_node.get("peakControlData"),
                "gridRewards": self._grid_rewards.get(home_id),
                "weather": home_node.get("weather"),
            }
            for gtype, prefix, bucket in (
                (GIZMO_EV_CHARGER, "charger", data.chargers),
                (GIZMO_BATTERY, "battery", data.batteries),
                (GIZMO_INVERTER, "inverter", data.inverters),
                (GIZMO_THERMOSTAT, "thermostat", data.thermostats),
            ):
                for dev in self.devices_of_type(gtype):
                    # Only this home's devices are selected into its block, so
                    # skipping the rest keeps _fresh() from treating a device
                    # that was never asked for here as one that dropped out.
                    if dev.home_id != home_id:
                        continue
                    node = self._fresh(
                        f"{prefix} {dev.id}",
                        home_node.get(_alias(prefix, dev.id)),
                        now,
                    )
                    if node:
                        bucket[dev.id] = node

        return data

    # -- live (WebSocket) ---------------------------------------------------
    def update_live(self, pulse_id: str, measurement: dict[str, Any]) -> None:
        """Merge a live Pulse measurement and notify entities.

        Uses ``async_update_listeners`` rather than ``async_set_updated_data`` on
        purpose: the latter reschedules the poll timer, and at ~6 frames/min the
        regular poll would never fire. This just tells entities to re-read state.
        """
        self._live[pulse_id] = measurement
        if self.data is None:
            # First poll hasn't completed yet; value is kept and applied later.
            return
        self.data.live[pulse_id] = measurement
        self.async_update_listeners()

    def update_battery_live(self, battery_id: str, state: dict[str, Any]) -> None:
        """Merge a live battery state and notify entities (see ``update_live``)."""
        self._battery_live[battery_id] = state
        if self.data is None:
            return
        self.data.battery_live[battery_id] = state
        self.async_update_listeners()

    # -- inverter production (separate fetch) --------------------------------
    async def _fetch_inverter_production(self) -> None:
        """Fetch today's production summary per inverter, tolerating errors."""
        inverters = self.devices_of_type(GIZMO_INVERTER)
        if not inverters:
            return
        now = dt_util.now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        for dev in inverters:
            try:
                res = await self.client.gql(
                    queries.INVERTER_PRODUCTION,
                    {
                        "homeId": dev.home_id,
                        "inverterId": dev.id,
                        "from": day_start.isoformat(),
                        "to": now.isoformat(),
                        "resolution": "HOURLY",
                    },
                    partial_ok=True,
                )
            except TibberApiError as err:
                _LOGGER.debug(
                    "Inverter production fetch failed for %s: %s", dev.id, err
                )
                continue
            home = (res.get("me") or {}).get("home") or {}
            prod = home.get("inverterProduction")
            if prod:
                self._inverter_production[dev.id] = prod

    # -- mutation helpers ---------------------------------------------------
    async def async_set_vehicle_setting(
        self,
        vehicle_id: str,
        home_id: str,
        key: str,
        value: Any,
        *,
        require_fresh_readback: bool = False,
    ) -> None:
        kwargs = {"retries": 1} if require_fresh_readback else {}
        await self.client.gql(
            queries.SET_VEHICLE_SETTINGS,
            {
                "vehicleId": vehicle_id,
                "homeId": home_id,
                "settings": [{"key": key, "value": value}],
            },
            **kwargs,
        )
        seen_before = self._last_seen.get(f"vehicle {vehicle_id}", (None, 0))[1]
        if require_fresh_readback:
            await self.async_refresh()
        else:
            await self.async_request_refresh()
        if require_fresh_readback:
            seen_after = self._last_seen.get(f"vehicle {vehicle_id}", (None, 0))[1]
            if not self.last_update_success or seen_after <= seen_before:
                raise HomeAssistantError("Minimum charge level readback failed")

    async def async_clear_vehicle_departure_times(
        self,
        vehicle_id: str,
        home_id: str,
        setting_keys: list[str],
        *,
        single_attempt: bool = False,
        refresh: bool = True,
    ) -> None:
        """Clear departure settings one per request, then refresh once.

        Tibber's Android app clears a departure time with one explicit null
        value per mutation. Sending several null settings in one mutation is
        accepted by the backend but does not clear existing times.

        ``single_attempt`` sends each mutation exactly once (no client retries),
        and ``refresh=False`` leaves the refresh to the caller.
        """
        kwargs = {"retries": 1} if single_attempt else {}
        for key in dict.fromkeys(setting_keys):
            await self.client.gql(
                queries.SET_VEHICLE_SETTINGS,
                {
                    "vehicleId": vehicle_id,
                    "homeId": home_id,
                    "settings": [{"key": key, "value": None}],
                },
                **kwargs,
            )
        if setting_keys and refresh:
            await self.async_request_refresh()

    def vehicle_departure_keys(self, vehicle_id: str) -> dict[str, str] | None:
        """Return the departure setting key per weekday, or None without a schedule.

        Monday's key pins down the namespace the whole week lives in, the same
        way the time entities resolve it.
        """
        monday_key = self.vehicle_setting_key(
            vehicle_id, VEHICLE_DEPARTURE_SUFFIX.format(day="monday")
        )
        if monday_key is None:
            return None
        prefix = monday_key[: -len("monday")]
        return {
            day: self.vehicle_setting_key(
                vehicle_id, VEHICLE_DEPARTURE_SUFFIX.format(day=day)
            )
            or prefix + day
            for day in WEEKDAYS
        }

    def vehicle_departure_schedule(self, vehicle_id: str) -> dict[str, str | None]:
        """Return the stored weekly schedule as weekday -> "HH:MM" or None."""
        node = (self.data.vehicles.get(vehicle_id) if self.data else None) or {}
        values = {
            setting.get("key"): setting.get("value")
            for setting in node.get("userSettings") or []
        }
        schedule: dict[str, str | None] = {}
        for day, key in (self.vehicle_departure_keys(vehicle_id) or {}).items():
            parsed = parse_departure_time(values.get(key))
            schedule[day] = parsed.strftime("%H:%M") if parsed else None
        return schedule

    def vehicle_smart_charging(self, vehicle_id: str) -> bool | None:
        """Return the stored smart-charging flag, if the vehicle has one."""
        key = self.vehicle_setting_key(vehicle_id, VEHICLE_SMART_CHARGING_SUFFIX)
        node = (self.data.vehicles.get(vehicle_id) if self.data else None) or {}
        for setting in node.get("userSettings") or []:
            if key is not None and setting.get("key") == key:
                return str(setting.get("value")).lower() in ("true", "1")
        return None

    async def async_set_vehicle_departure_schedule(
        self,
        vehicle_id: str,
        home_id: str,
        schedule: dict[str, str | None],
        smart_charging: bool | None = None,
    ) -> dict[str, Any]:
        """Write weekdays to the schedule, refresh once and verify the readback.

        ``schedule`` maps weekday to "HH:MM" (set) or None (clear); weekdays not
        listed are left alone. Times are written first, then each clear goes out
        as its own null mutation, then the smart-charging flag. Every mutation is
        sent once: a retried write after a lost response could land twice, and
        the readback below is what tells whether it took. A weekly entry that
        silently stays behind charges every week, so a mismatch raises.
        """
        keys = self.vehicle_departure_keys(vehicle_id)
        if keys is None:
            raise HomeAssistantError("Vehicle has no departure schedule")
        smart_key = None
        if smart_charging is not None:
            smart_key = self.vehicle_setting_key(
                vehicle_id, VEHICLE_SMART_CHARGING_SUFFIX
            )
            if smart_key is None:
                raise HomeAssistantError("Vehicle has no smart-charging setting")

        seen_before = self._last_seen.get(f"vehicle {vehicle_id}", (None, 0))[1]
        try:
            await self._write_departure_schedule(
                vehicle_id, home_id, keys, schedule, smart_key, smart_charging
            )
        except TibberApiError as err:
            # Earlier writes may have landed; pick up what Tibber holds now.
            await self.async_request_refresh()
            raise HomeAssistantError(
                f"Writing the departure schedule failed: {err}"
            ) from err

        await self.async_refresh()
        seen_after = self._last_seen.get(f"vehicle {vehicle_id}", (None, 0))[1]
        if not self.last_update_success or seen_after <= seen_before:
            raise HomeAssistantError("Departure schedule readback failed")

        result = {
            "schedule": self.vehicle_departure_schedule(vehicle_id),
            "smart_charging": self.vehicle_smart_charging(vehicle_id),
        }
        mismatched = [
            day for day, value in schedule.items() if result["schedule"][day] != value
        ]
        if smart_key is not None and result["smart_charging"] != smart_charging:
            mismatched.append("smart_charging")
        if mismatched:
            raise HomeAssistantError(
                "Tibber did not apply the departure schedule for: "
                + ", ".join(mismatched)
            )
        return result

    async def _write_departure_schedule(
        self,
        vehicle_id: str,
        home_id: str,
        keys: dict[str, str],
        schedule: dict[str, str | None],
        smart_key: str | None,
        smart_charging: bool | None,
    ) -> None:
        """Send the schedule mutations, each exactly once, without refreshing."""
        for day, value in schedule.items():
            if value is not None:
                await self.client.gql(
                    queries.SET_VEHICLE_SETTINGS,
                    {
                        "vehicleId": vehicle_id,
                        "homeId": home_id,
                        "settings": [{"key": keys[day], "value": value}],
                    },
                    retries=1,
                )
        await self.async_clear_vehicle_departure_times(
            vehicle_id,
            home_id,
            [keys[day] for day, value in schedule.items() if value is None],
            single_attempt=True,
            refresh=False,
        )
        if smart_key is not None:
            await self.client.gql(
                queries.SET_VEHICLE_SETTINGS,
                {
                    "vehicleId": vehicle_id,
                    "homeId": home_id,
                    "settings": [{"key": smart_key, "value": smart_charging}],
                },
                retries=1,
            )

    async def async_set_charger_setting(
        self, charger_id: str, home_id: str, key: str, value: Any
    ) -> None:
        await self.client.gql(
            queries.SET_CHARGER_SETTINGS,
            {
                "chargerId": charger_id,
                "homeId": home_id,
                "settings": [{"key": key, "value": value}],
            },
        )
        await self.async_request_refresh()

    async def async_set_away_mode(
        self, home_id: str, enabled: bool, from_iso: str, to_iso: str
    ) -> None:
        await self.client.gql(
            queries.SET_AWAY_MODE,
            {"homeId": home_id, "enabled": enabled, "from": from_iso, "to": to_iso},
        )
        await self.async_request_refresh()

    async def async_set_peak_control(
        self, home_id: str, is_active: bool, limit: float
    ) -> None:
        await self.client.gql(
            queries.SET_PEAK_CONTROL,
            {"homeId": home_id, "isActive": is_active, "consumptionLimit": limit},
        )
        await self.async_request_refresh()

    async def async_set_battery_mode(
        self, home_id: str, device_id: str, mode: str
    ) -> None:
        await self.client.gql(
            queries.SET_BATTERY_OPERATION_MODE,
            {"homeId": home_id, "deviceId": device_id, "operationMode": mode},
        )
        await self.async_request_refresh()

    async def async_set_thermostat_state(
        self,
        home_id: str,
        device_id: str,
        *,
        mode: str | None = None,
        comfort_temperature: float | None = None,
        fan_level: str | None = None,
        on_off: str | None = None,
    ) -> None:
        await self.client.gql(
            queries.SET_THERMOSTAT_STATE,
            {
                "homeId": home_id,
                "deviceId": device_id,
                "mode": mode,
                "comfortTemperature": comfort_temperature,
                "fanLevel": fan_level,
                "onOff": on_off,
            },
        )
        await self.async_request_refresh()
