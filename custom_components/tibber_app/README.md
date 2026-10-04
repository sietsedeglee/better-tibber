# Better Tibber — Home Assistant integration

A custom component that exposes the **Tibber mobile app** GraphQL API
(`app.tibber.com`) as native Home Assistant entities — EV/charger control, a live
real-time meter, prices, and (where present) battery, solar and thermostats.

> Not affiliated with or endorsed by Tibber. The app API is undocumented and may
> change without notice.

## Installation

1. Copy `custom_components/tibber_app/` into your Home Assistant `config/custom_components/`.
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → "Better Tibber"**.
4. Enter your Tibber app email + password. The integration logs in, stores the
   token, and auto-discovers your homes and devices.

## How it works

- **One config entry per account.** Each home becomes an HA device; each physical
  device (vehicle, charger, Pulse, battery, inverter, thermostat) becomes its own
  device, linked to its home.
- **Polling** (every 60 s) issues *one combined GraphQL query per home* — the
  per-device sub-selections are aliased into a single request to stay well under
  the API's ~20 req/s limit.
- **Live meter** data (`liveMeasurement`) arrives over WebSocket
  (`wss://app.tibber.com/v4/gql/ws`) and updates the Pulse sensors in near real
  time. The socket is best-effort and reconnects on drop; polling stays the source
  of truth.

## Entities

| Device | Entities |
|---|---|
| **Vehicle** | battery %, range, charging/smart-charging status, session energy, session cost, target charge/departure · online + charging binary sensors · **manual state-of-charge number** (only where Tibber can't read the level itself) · **smart-charging switch** · **weekly departure schedule and clear-all button** (see below) |
| **Charger** | charging status, last seen, active vehicle · online binary sensor · **preferred-vehicle select** · permanent-cable-lock & fuse-load-balancing switches · max-current / main-fuse / offline-fallback numbers |
| **Pulse (live)** | power, production, phase currents/voltages, consumption/production/cost today, signal · online* / peak-exceeded binary sensors |
| **Home** | electricity price (+ today/tomorrow arrays), consumption & cost this month · Grid Rewards this month (where available) · **hourly forecast `weather` entity** · away-mode / peak-control switches · peak-limit number · refresh button |
| **Battery** | state of charge, status, power from solar/grid, power to home/grid (live via WebSocket) · operation-mode sensor + **select** · Grid Rewards enabled |
| **Inverter** | current production · production today |
| **Thermostat** | full `climate` entity (mode, target/current temp, on/off) |

### Weekly smart-charging schedule

Tibber's smart charging stores a **per-weekday departure time** plus a master
on/off, all as vehicle user settings (`…smartCharging.isEnabled` and
`…departureTimes.<weekday>`). These are exposed as real, read-write entities on
each vehicle:

- **Smart charging** — a `switch` reflecting the actual `isEnabled` flag (not
  optimistic — it reads the stored value back).
- **Departure Monday … Departure Sunday** — seven `time` entities, one per weekday.
  Setting one writes `"HH:MM"` back via `setVehicleSettings`.
- **Clear all departure times** — a vehicle button that clears only weekdays
  currently containing a time. Tibber requires one explicit GraphQL `null`
  mutation per weekday; empty weekdays are skipped.

Both settings are **namespaced by how the vehicle was added**: manually added
vehicles use `offline.vehicle.smartCharging.isEnabled` /
`offline.vehicle.departureTimes.<weekday>`, manufacturer-connected ones
`online.vehicle.smartCharging.isEnabled` /
`online.vehicle.smartCharging.departureTimes.<weekday>`. Writing the wrong
namespace is rejected by the backend with a *Request Validation Error*, so — like
the Tibber app itself — the full key is matched by suffix against each vehicle's
own `userSettings`, and the entities only appear when the vehicle has them.

The manual state-of-charge number is the exception: its key
(`offline.vehicle.batteryLevel`) really is fixed, because the override only
exists for vehicles Tibber can't read the level from. The app shows that editor
only when `battery.canReadLevel` is false, so a manufacturer-connected vehicle —
which reports its own level — gets no number entity.

So the weekly schedule is just the seven `time.<vehicle>_departure_<weekday>`
entities; automate or adjust them like any other HA time helper. (Target
state-of-charge is reported by the *Target charge* sensor; the app exposes no
writable per-day SoC setting, only the departure times.)

Home Assistant's standard `time.set_value` action only accepts a real time, so
it cannot restore Tibber's *No departure time* state. Better Tibber therefore
adds the `tibber_app.clear_departure_times` action. Target one or more of the
vehicle's departure `time` entities:

```yaml
action: tibber_app.clear_departure_times
target:
  entity_id:
    - time.my_car_departure_monday
    - time.my_car_departure_tuesday
```

The selected entities identify both the vehicle and weekdays; no Tibber vehicle
ID or Home Assistant device ID is required. Use the vehicle's *Clear all
departure times* button when the entire weekly schedule should be emptied.

#### Setting the whole schedule in one call

`tibber_app.set_departure_schedule` targets the vehicle device and writes any
number of weekdays at once. Each weekday maps to `"HH:MM"` (set) or `null`
(clear); weekdays not listed stay as they are. `clear_all: true` clears every
weekday not listed, so `schedule: {}` with `clear_all: true` empties the week.
The optional `smart_charging` flag is written after the schedule.

```yaml
action: tibber_app.set_departure_schedule
target:
  device_id: <vehicle device id>
data:
  schedule:
    friday: "06:45"
  clear_all: true
  smart_charging: true
response_variable: tibber_schedule
```

Times are written first, then every cleared day as its own `null` mutation
(clearing several days in one mutation is accepted but does nothing), then one
refresh. Each mutation is sent once, without automatic retries. The action then
reads the schedule back and fails, naming the weekdays, if Tibber does not hold
what was requested. The response holds all seven weekdays and the
smart-charging flag as Tibber stores them after the call.

**Departure times repeat every week.** A time set for a one-off trip charges
to that deadline on the same weekday every following week until it is cleared,
so an automation that sets one should also clear it afterwards and check the
response (or the error) of that clearing call.

\* Phase voltages/currents and signal strength are disabled by default — enable
them per entity if you want them.

## Known limitations

- **No start/stop charging.** The app API exposes no explicit start/stop-charge
  mutation, so charging is controlled via the preferred-vehicle selector, the
  charger settings, the smart-charging switch and the weekly departure schedule —
  there is intentionally no fake start/stop control.
- **Away-mode switch is optimistic** (`assumed_state`): the API has no field to
  read its current on/off state back. (The smart-charging switch, by contrast,
  reads its real state from the vehicle settings.)
- **Grid Rewards** is fetched in a separate, error-tolerant request and only
  exposed for homes that actually have rewards history — the `gridRewardsHistoryPeriod`
  query errors for homes without data, so it must not share the combined poll query.
- Battery, inverter and thermostat entities are implemented from the documented
  schema and **field-name-verified against the live API** (e.g. the `batteryState`
  subscription validates), but their *values* are untested because the account has
  no such device. The thermostat HVAC-mode string mapping is a best-effort guess.

### Intentionally not implemented

- **Generic home `sensors`** (temperature/humidity) — the `Sensor` type exposes no
  name/type, only a bare measurement, so entities would be unidentifiable.
- **Per-device consumption history** (`evChargerConsumption`,
  `electricVehicleConsumption`, battery timeline/aggregated history) — chart data
  better suited to the app; live power + month-to-date totals are covered instead.
- **Account wallet/invoices** — the `Wallet` type exposes no balance field.
- **Bridge (Zigbee gateway), device pairing/onboarding, messages/offers/checklist,
  gizmo visibility, push-notification inbox** — app-management actions, not
  smart-home entities.
- **Grid Rewards live WS state / all-time total** — only the polled current-month
  total is exposed.
