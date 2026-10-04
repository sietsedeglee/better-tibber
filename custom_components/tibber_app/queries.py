"""GraphQL query/mutation strings for the Tibber app API.

The polling coordinator builds one combined query per home by aliasing per-device
sub-selections, so the account stays well under the ~20 req/s rate limit.
"""

from __future__ import annotations

# --- discovery -------------------------------------------------------------
DISCOVERY = """
{
  me {
    id
    homes {
      id
      title
      gizmos {
        ... on Gizmo { id title type context { key value } }
        ... on GizmoGroup { gizmos { id title type } }
      }
    }
  }
}
"""

# --- per-device field selections (used inside aliased combined queries) -----

VEHICLE_FIELDS = """
  id name isAlive chargingStatus smartChargingStatus
  battery { level estimatedRange canReadLevel }
  charging {
    summary { energyConsumed total }
    targetedStateOfCharge targetedDepartureTime
  }
  userSettings {
    key value valueType isReadOnly
    inputOptions {
      type rangeOptions { min max step }
      selectOptions { value } pickerOptions { values }
    }
  }
"""

CHARGER_FIELDS = """
  id name isAlive chargingStatus lastSeen
  vehicles { id name isPreferred }
  userSettings { key value valueType }
"""

PRICE_FIELDS = """
  hasSignedEnergyDeal
  subscription {
    priceRating {
      isAvailable
      thresholdPercentages { high low highBreakpoint }
      hourly { currency entries { time energy total } }
    }
  }
"""

CONSUMPTION_GIZMO_FIELDS = """
  consumptionGizmoData { from cost consumption currency }
"""

WEATHER_FIELDS = """
  weather {
    minTemperature maxTemperature
    entries { time temperature type precipitation }
  }
"""

PEAK_CONTROL_FIELDS = """
  peakControlData {
    isActive hasRealTimeDevice consumptionLimit
    lowerBound upperBound incrementSize recommendedValue
  }
"""

BATTERY_FIELDS = """
  id name shortName brand isGridRewardsEnabled
  currentOperationMode { value }
  supportedOperationModes { value enabled }
"""

INVERTER_FIELDS = """
  id siteTitle description
  bubble { value valueText unit }
"""

THERMOSTAT_FIELDS = """
  id name brandName connectivity
  state { mode comfortTemperature fanLevel onOff }
  modes { name capabilities }
  temperatureSensor { measurement { value unit } }
"""

# --- grid rewards (fetched separately; the period query errors when a home has
#     no rewards data, so it must NOT share the combined poll query) -----------
GRID_REWARDS_HISTORY = """
query($homeId: String!) {
  me { home(id: $homeId) { gridRewardsHistory { valuesFrom valuesTo } } }
}
"""

GRID_REWARDS_PERIOD = """
query($homeId: String!, $from: String!, $to: String!, $resolution: Resolution!) {
  me {
    home(id: $homeId) {
      gridRewardsHistoryPeriod(from: $from, to: $to, resolution: $resolution) {
        from to totalReward batteryRewards vehicleRewards currency
      }
    }
  }
}
"""

# --- inverter production (separate; needs from/to/resolution) ---------------
INVERTER_PRODUCTION = """
query($homeId: String!, $inverterId: String!, $from: String!, $to: String!, $resolution: String!) {
  me {
    home(id: $homeId) {
      inverterProduction(id: $inverterId, from: $from, to: $to, resolution: $resolution) {
        keyFigures { valueText unitText description }
        items { from to value }
      }
    }
  }
}
"""

# --- subscriptions (WebSocket) ---------------------------------------------
SUB_LIVE_MEASUREMENT = """
subscription($deviceId: String!) {
  liveMeasurement(deviceId: $deviceId) {
    timestamp power powerProduction minPower maxPower averagePower
    accumulatedConsumption accumulatedCost accumulatedProduction accumulatedReward
    peakControlConsumptionState effectiveHourlyConsumptionLimit currency
    currentPhase1 currentPhase2 currentPhase3
    voltagePhase1 voltagePhase2 voltagePhase3
    powerFactor signalStrength
  }
}
"""

SUB_BATTERY_STATE = """
subscription($deviceId: String!) {
  batteryState(deviceId: $deviceId) {
    status stateOfCharge
    powerFromSolar powerFromGrid powerToHome powerToGrid
    percentPowerFromSolar percentPowerFromGrid percentPowerToHome percentPowerToGrid
  }
}
"""

# --- mutations -------------------------------------------------------------
SET_VEHICLE_SETTINGS = """
mutation($vehicleId: String!, $homeId: String!, $settings: [SettingsItemInput!]) {
  me {
    setVehicleSettings(id: $vehicleId, homeId: $homeId, settings: $settings) {
      id battery { level }
    }
  }
}
"""

SET_CHARGER_SETTINGS = """
mutation($homeId: String!, $chargerId: String!, $settings: [SettingsItemInput!]) {
  me {
    home(id: $homeId) {
      setVehicleChargerSettings(id: $chargerId, homeId: $homeId, settings: $settings) {
        id vehicles { id name isPreferred } userSettings { key value }
      }
    }
  }
}
"""

SET_AWAY_MODE = """
mutation($homeId: String!, $enabled: Boolean!, $from: String!, $to: String!) {
  me { home(id: $homeId) { setAwayMode(isEnabled: $enabled, from: $from, to: $to) { __typename } } }
}
"""

SET_PEAK_CONTROL = """
mutation($homeId: String!, $isActive: Boolean!, $consumptionLimit: Float!) {
  me { home(id: $homeId) { setPeakControl(isActive: $isActive, consumptionLimit: $consumptionLimit) } }
}
"""

SET_BATTERY_OPERATION_MODE = """
mutation($homeId: String!, $deviceId: String!, $operationMode: HomeBatteryOperationMode!) {
  me {
    home(id: $homeId) {
      battery(id: $deviceId) {
        setCurrentOperationMode(operationMode: $operationMode) { currentOperationMode { value } }
      }
    }
  }
}
"""

SET_THERMOSTAT_STATE = """
mutation($homeId: String!, $deviceId: String!, $mode: String, $comfortTemperature: Float, $fanLevel: String, $onOff: String) {
  me {
    home(id: $homeId) {
      thermostat(id: $deviceId) {
        setState(mode: $mode, comfortTemperature: $comfortTemperature, fanLevel: $fanLevel, onOff: $onOff)
      }
    }
  }
}
"""
