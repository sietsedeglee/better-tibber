# Better Tibber — Home Assistant integration

A Home Assistant custom component that connects to the **Tibber mobile app** GraphQL
API (`app.tibber.com`) and exposes it as native entities: EV and charger control, a
live real-time meter, electricity prices, Grid Rewards, weather, and — where present
— home battery, solar inverter and thermostats. Everything the app shows you, in
Home Assistant.

> Not affiliated with or endorsed by Tibber. The app API is undocumented and may
> change without notice.

## Features

- **Vehicles** — battery %, range, charging status, session energy/cost, manual
  state-of-charge, smart-charging toggle and a **weekly departure schedule** (a
  `time` entity per weekday), including selective and clear-all removal of
  departure times.
- **Chargers** — status, preferred-vehicle selector, cable lock, load balancing,
  fuse/current settings.
- **Live meter (Pulse)** — power, production, per-phase current/voltage and running
  consumption/cost, streamed over WebSocket.
- **Home** — current electricity price (+ today/tomorrow), month-to-date
  consumption & cost, Grid Rewards, an hourly `weather` entity, away mode and peak
  control.
- **Battery / solar / thermostat** — state of charge and power flows, production,
  and a full `climate` entity where such devices exist.

See the [component README](custom_components/tibber_app/README.md) for the full
entity list and notes.

## Installation

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories** → add this repository, category **Integration**.
2. Install **Better Tibber**, then restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → “Better Tibber”**
   and sign in with your Tibber app email + password.

HACS tracks the repository's **GitHub releases**: it offers the newest tag, and
shows an update whenever a newer one is published. (Pick "Redownload" → a specific
version in HACS to pin or roll back.)

### Manual

Download `tibber_app.zip` from the
[latest release](https://github.com/Donkie/better-tibber/releases/latest) and unpack
it into `config/custom_components/tibber_app/`, or copy
`custom_components/tibber_app/` from a checkout. Restart Home Assistant afterwards.

## Development

- [QUICKSTART.md](QUICKSTART.md) — run the integration in a local Home Assistant.
- CI runs `hassfest`, the HACS action, `ruff` and `pytest` (see
  `.github/workflows/`).

### Releasing

Versions are [semver](https://semver.org/) and live in **two places that must
agree**: the `version` field in `custom_components/tibber_app/manifest.json` (what
Home Assistant reports as installed) and the git tag (what HACS offers). To cut a
release:

```bash
# 1. bump "version" in custom_components/tibber_app/manifest.json, e.g. 0.2.0
git commit -am "Release 0.2.0"
git tag v0.2.0
git push origin main --tags
```

`.github/workflows/release.yml` then verifies the tag matches the manifest —
failing the release if it doesn't — and publishes a GitHub release with
auto-generated notes and a `tibber_app.zip` asset. HACS users see the update on
their next refresh.

### Minimum charge level

Vehicles exposing a setting ending in `smartCharging.minChargeLimit` receive a
configuration number named **Minimum charge level** (%). This is Tibber's reserve
for spontaneous driving, separate from current battery level, Tesla's charge
limit and `targetedStateOfCharge`. Online/offline setting keys are resolved from
each vehicle's own `userSettings`.

#### Usage

Find **Minimum charge level** under the vehicle's configuration entities in
**Settings → Devices & services → Better Tibber → your vehicle**, or add the
number entity to a dashboard. Enter an allowed percentage and confirm the
change. The displayed value is then read back from Tibber.

The entity ID depends on the vehicle name and your entity registry. For
example, `number.my_car_minimum_charge_level` can be changed from
**Developer tools → Actions** or an automation:

```yaml
action: number.set_value
target:
  entity_id: number.my_car_minimum_charge_level
data:
  value: 20
```

Replace the example entity ID with your actual entity ID and choose a value
within that entity's reported minimum, maximum and step. On the live-tested
Tesla, the backend allows 0–75% in steps of 5%; 0 corresponds to **Off** in the
Tibber app. Other vehicles use their own backend-provided bounds.

This changes Tibber's reserve level, not the vehicle's final charge limit.
It does not toggle Smart Charging. Renaming a dashboard label does not change
the entity ID or its internal unique ID.

#### Backend behaviour

The allowed minimum, maximum and step come from the same backend
`inputOptions.rangeOptions`, `pickerOptions.values` or a regular numeric
`selectOptions` list used by the Android app. No limits are guessed:
missing/invalid bounds or read-only settings make the number unavailable. Writes
use the existing `setVehicleSettings` helper with an integer, followed by a
coordinator refresh requiring fresh vehicle data. A write is sent only once,
without mutation retries. State always comes from backend readback; it is never
updated optimistically. Mutation and readback errors are reported to the caller.
