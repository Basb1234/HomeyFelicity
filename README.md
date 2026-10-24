# Felicity Home Battery — Homey app

A native Python app for [Homey Pro](https://python-apps-sdk-v3.developer.homey.app/)
that monitors a Felicity TREX home battery over Modbus TCP on your local
network. It reads the inverter and nothing more: no registers are written, so
the app cannot change how your battery behaves.

Supported models: **T-REX-5K-P1G01**, **T-REX-10K-P3G01**, **T-REX-25KHP3G01**,
**T-REX-50KHP3G01**.

## What it exposes

Paired as a Homey device of class `battery` with `energy.homeBattery`, so it
shows up in Homey's Energy screen as a real home battery.

| Capability | Meaning |
| --- | --- |
| `measure_battery` | State of charge, % |
| `measure_power` | Battery power, W — positive charging, negative discharging |
| `battery_charging_state` | `charging` / `discharging` / `idle` |
| `measure_power.pv` | Solar power across all strings, W |
| `measure_power.grid` | Grid power, W — positive importing, negative exporting |
| `measure_power.load` | House load, W |
| `meter_power.charged` / `.discharged` | Lifetime battery energy, kWh |
| `meter_power.pv` / `.load` / `.grid_import` | Lifetime yield, consumption and import, kWh |
| `meter_power.*_today` | The same five meters for today only |
| `felicity_working_mode` | The mode the inverter reports |
| `felicity_fault_code` | Current fault code, 0 when healthy |
| `alarm_generic` | On while the inverter reports a fault or warning |
| `alarm_battery` | On below the configurable low-SOC level |

Switching on **Extended sensors** in the device settings adds battery voltage
and current, grid voltage and frequency, and inverter temperature — and starts
polling the extra registers behind them.

### Flow

Triggers: state of charge rises above / drops below a level, solar, grid and
house-load power changed, working mode changed, fault reported, fault cleared.
Homey also generates the usual cards for `measure_battery`, `measure_power` and
`battery_charging_state`.

Conditions: state of charge above a level, battery is charging, house is
exporting more than *n* watts, working mode is *x* (the dropdown is filled from
the paired device's own model), inverter has a fault.

Action: read the inverter now.

## Relationship to `ha_felicity`

The Modbus layer is not reimplemented here. `lib/felicity_core/` is a verbatim
copy of the platform-agnostic core of the
[`ha_felicity`](../ha_felicity) Home Assistant integration — the four register
maps, the decoder, and the per-model derivations. That integration ships
through HACS from `custom_components/ha_felicity/`, so the core cannot be moved
into a package both projects import; vendoring a copy is the way across.

Refresh the copy after changing the integration:

```bash
python tools/sync_core.py                 # copy from ../ha_felicity
python tools/sync_core.py --check         # exit 1 if the copy is stale
```

The script refuses to copy a module that has grown a `homeassistant.*` import,
which is the same boundary `ha_felicity`'s `tests/test_core_boundary.py`
guards from its side.

`lib/felicity.py` is this app's own layer on top: it picks which registers are
worth polling, runs the core's decoder and combined-register maths over them,
and flattens the result into a `Snapshot` whose fields mean the same thing on
every model. The two TREX families name their registers differently, but the
core already publishes the 5K/10K names as derived values on the 25K/50K, so
the fork between them is small and lives entirely in `_PROFILE_SINGLE` /
`_PROFILE_DUAL`.

What is **not** carried over: `ems.py`, `milp.py` and the scheduling stack.
This app monitors; it does not plan or control.

## Running it

```bash
npx homey app validate -l debug     # manifest, flow cards, capabilities
npx homey app run                   # on a paired Homey Pro
python -m pytest tests -q           # the reader, against a simulated inverter
```

`homey app validate -l publish` additionally requires cross-platform virtualenvs
for `pymodbus`, which the CLI builds through Docker:

```bash
npx homey app dependencies install   # needs Docker running
```

Without that step publish-level validation fails with
`Cross-platform venvs are required for Python apps with dependencies`. Everything
else passes at publish level.

## Notes and limitations

* **Modbus TCP only.** Serial/RTU is not supported; Homey's app sandbox does
  not expose USB serial, and this is a LAN deployment.
* **Energy totals on the 25K/50K.** Those models expose no lifetime battery
  counter, only day/month/year. The app uses the year counter and absorbs its
  New Year reset with a persistent offset (`lib/meters.py`), so Homey still
  sees a meter that only counts up. The same mechanism covers a counter reset
  after a firmware update or board swap on any model.
* **State of charge with two packs.** The lower of the two is reported, which is
  the conservative reading `ha_felicity` uses for scheduling.
* **Polling.** The fast registers follow the configured interval (default 10 s);
  the energy meters are read at most once a minute because they barely move.
  A poll that overruns its interval is skipped rather than queued.
* **Before publishing**, set `author.email` in `.homeycompose/app.json` and
  decide on the app `id` — it currently matches the proof-of-concept scaffold
  in the `ha_felicity` repo (`com.partach.felicity`), and two apps cannot share
  an ID.
