"""Check the Homey-facing half: capability mapping, Flow listeners, pairing.

The SDK itself is stubbed in ``conftest.py``; what is under test here is this
app's own logic, not Homey's.
"""
from __future__ import annotations

import asyncio

import pytest

from drivers.battery import device as device_module
from drivers.battery import driver as driver_module
from lib import felicity

BatteryDevice = device_module.BatteryDevice
BatteryDriver = driver_module.BatteryDriver

ALL_CAPABILITIES = {
    "measure_battery",
    "measure_power",
    "battery_charging_state",
    "measure_power.pv",
    "measure_power.grid",
    "measure_power.load",
    "felicity_working_mode",
    "felicity_fault_code",
    "alarm_generic",
    "alarm_battery",
    *device_module.DETAIL_CAPABILITIES,
    *device_module.CUMULATIVE_METERS.values(),
    *device_module.DAILY_METERS.values(),
}


def make_device(settings=None, store=None) -> BatteryDevice:
    """A device with its state wired up but no lifecycle run."""
    instance = BatteryDevice.__new__(BatteryDevice)
    instance.state = {}
    instance.capabilities = set(ALL_CAPABILITIES)
    instance.store = dict(store or {})
    instance.settings = {"low_battery_soc": 10, "model": "T-REX-10K-P3G01"}
    instance.settings.update(settings or {})
    instance._meter_state = {}
    instance._meter_persisted = {}
    instance._previous = None
    return instance


def snapshot(**overrides) -> felicity.Snapshot:
    base = dict(
        soc=55.0,
        battery_power=1200.0,
        battery_voltage=51.0,
        battery_current=23.5,
        pv_power=2600.0,
        grid_power=-400.0,
        load_power=1000.0,
        grid_voltage=230.5,
        grid_frequency=50.01,
        temperature=36.5,
        working_mode="Economic mode",
        fault_code=0,
        warning_code=0,
    )
    base.update(overrides)
    return felicity.Snapshot(**base)


# --- Capability mapping -----------------------------------------------------


def test_apply_writes_every_primary_capability():
    device = make_device()
    asyncio.run(device._apply(snapshot()))

    assert device.state["measure_battery"] == 55.0
    assert device.state["measure_power"] == 1200.0
    assert device.state["battery_charging_state"] == "charging"
    assert device.state["measure_power.pv"] == 2600.0
    assert device.state["measure_power.grid"] == -400.0
    assert device.state["measure_power.load"] == 1000.0
    assert device.state["felicity_working_mode"] == "Economic mode"
    assert device.state["alarm_generic"] is False


def test_apply_skips_capabilities_the_device_does_not_have():
    device = make_device()
    device.capabilities -= set(device_module.DETAIL_CAPABILITIES)

    asyncio.run(device._apply(snapshot()))

    assert "measure_temperature" not in device.state
    assert device.state["measure_battery"] == 55.0


def test_missing_readings_leave_the_previous_value_alone():
    device = make_device()
    asyncio.run(device._apply(snapshot()))
    asyncio.run(device._apply(snapshot(pv_power=None)))

    assert device.state["measure_power.pv"] == 2600.0


def test_low_battery_alarm_follows_the_configured_threshold():
    device = make_device({"low_battery_soc": 20})

    asyncio.run(device._apply(snapshot(soc=25.0)))
    assert device.state["alarm_battery"] is False

    asyncio.run(device._apply(snapshot(soc=15.0)))
    assert device.state["alarm_battery"] is True


def test_warnings_raise_the_generic_alarm_too():
    device = make_device()
    asyncio.run(device._apply(snapshot(warning_code=4)))
    assert device.state["alarm_generic"] is True


# --- Meters -----------------------------------------------------------------


def test_meters_are_written_and_their_state_persisted():
    device = make_device()
    readings = {
        "charged_total": 1000.0,
        "discharged_total": 900.0,
        "pv_total": 5000.0,
        "load_total": 4000.0,
        "grid_import_total": 2000.0,
        "charged_today": 5.5,
        "discharged_today": 4.0,
        "pv_today": 12.0,
        "load_today": 9.0,
        "grid_import_today": 1.5,
    }
    asyncio.run(device._apply_meters(readings))

    assert device.state["meter_power.charged"] == 1000.0
    assert device.state["meter_power.charged_today"] == 5.5
    assert device.store[device_module.STORE_METERS]["charged_total"]["last"] == 1000.0


def test_a_counter_reset_does_not_make_a_meter_go_backwards():
    device = make_device()
    asyncio.run(device._apply_meters({"charged_total": 820.0}))
    asyncio.run(device._apply_meters({"charged_total": 2.5}))  # new year

    assert device.state["meter_power.charged"] == pytest.approx(822.5)


def test_meter_state_is_restored_from_the_store():
    store = {device_module.STORE_METERS: {"charged_total": {"offset": 500.0, "last": 20.0, "seen": 1}}}
    device = make_device(store=store)
    # on_init is what normally loads the store, so do what it does.
    device._meter_state = {
        name: dict(state) for name, state in store[device_module.STORE_METERS].items()
    }

    asyncio.run(device._apply_meters({"charged_total": 30.0}))
    assert device.state["meter_power.charged"] == pytest.approx(530.0)


def test_small_meter_moves_do_not_trigger_a_store_write():
    device = make_device()
    asyncio.run(device._apply_meters({"charged_total": 100.0}))
    device.store.clear()

    asyncio.run(device._apply_meters({"charged_total": 100.01}))
    assert device.store == {}

    asyncio.run(device._apply_meters({"charged_total": 100.5}))
    assert device_module.STORE_METERS in device.store


# --- Flow listeners ---------------------------------------------------------


def make_driver() -> BatteryDriver:
    return BatteryDriver.__new__(BatteryDriver)


def run(coro):
    return asyncio.run(coro)


def test_soc_rises_above_only_fires_on_the_crossing():
    driver = make_driver()
    assert run(driver._on_soc_rises_above({"soc": 80}, soc=81.0, previous=79.0)) is True
    assert run(driver._on_soc_rises_above({"soc": 80}, soc=85.0, previous=82.0)) is False
    assert run(driver._on_soc_rises_above({"soc": 80}, soc=79.0, previous=81.0)) is False


def test_soc_drops_below_only_fires_on_the_crossing():
    driver = make_driver()
    assert run(driver._on_soc_drops_below({"soc": 20}, soc=19.0, previous=21.0)) is True
    assert run(driver._on_soc_drops_below({"soc": 20}, soc=15.0, previous=18.0)) is False
    assert run(driver._on_soc_drops_below({"soc": 20}, soc=25.0, previous=19.0)) is False


def test_crossing_triggers_need_a_previous_reading():
    driver = make_driver()
    assert run(driver._on_soc_rises_above({"soc": 80}, soc=90.0, previous=None)) is False


def test_conditions_read_the_device_capabilities():
    driver = make_driver()
    device = make_device()
    asyncio.run(device._apply(snapshot(soc=62.0, grid_power=-1500.0)))

    assert run(driver._on_soc_is_above({"device": device, "soc": 50})) is True
    assert run(driver._on_soc_is_above({"device": device, "soc": 70})) is False
    assert run(driver._on_is_charging({"device": device})) is True
    assert run(driver._on_has_fault({"device": device})) is False


def test_is_exporting_reads_the_negative_side_of_grid_power():
    driver = make_driver()
    device = make_device()

    asyncio.run(device._apply(snapshot(grid_power=-1500.0)))
    assert run(driver._on_is_exporting({"device": device, "power": 1000})) is True
    assert run(driver._on_is_exporting({"device": device, "power": 2000})) is False

    # Importing is never exporting, however large.
    asyncio.run(device._apply(snapshot(grid_power=3000.0)))
    assert run(driver._on_is_exporting({"device": device, "power": 0})) is False


def test_working_mode_condition_accepts_an_autocomplete_result():
    driver = make_driver()
    device = make_device()
    asyncio.run(device._apply(snapshot(working_mode="Economic mode")))

    args = {"device": device, "mode": {"name": "Economic mode"}}
    assert run(driver._on_working_mode_is(args)) is True

    args = {"device": device, "mode": {"name": "Backup mode"}}
    assert run(driver._on_working_mode_is(args)) is False


def test_mode_autocomplete_offers_the_devices_own_model():
    driver = make_driver()
    device = make_device({"model": "T-REX-10K-P3G01"})

    results = run(driver._modes_for_device("", device=device))
    names = [row["name"] for row in results]
    assert "Economic mode" not in names  # that is the operating mode, not the working mode
    assert "Bypass" in names

    filtered = run(driver._modes_for_device("off", device=device))
    assert [row["name"] for row in filtered] == ["Off-grid"]


def test_mode_autocomplete_falls_back_when_no_device_is_picked_yet():
    driver = make_driver()
    results = run(driver._modes_for_device(""))
    names = {row["name"] for row in results}
    assert "Bypass" in names
    assert "Zero Export To CT" in names


# --- Pairing ----------------------------------------------------------------


def test_clean_coerces_and_clamps_what_the_pairing_view_sends():
    settings = driver_module._clean(
        {"host": "  192.168.1.50 ", "port": "502", "slave_id": 9999, "model": "nope"},
        {"host": "", "port": 502, "slave_id": 1, "model": "T-REX-10K-P3G01"},
    )

    assert settings["host"] == "192.168.1.50"
    assert settings["port"] == 502
    assert settings["slave_id"] == 247
    assert settings["model"] == driver_module.DEFAULT_MODEL


def test_clean_falls_back_to_the_defaults_for_junk():
    settings = driver_module._clean(
        {"port": "not a port"},
        {"host": "10.0.0.5", "port": 1502, "slave_id": 3, "model": "T-REX-50KHP3G01"},
    )

    assert settings["host"] == "10.0.0.5"
    assert settings["port"] == driver_module.DEFAULT_PORT
    assert settings["slave_id"] == 3
    assert settings["model"] == "T-REX-50KHP3G01"


def test_described_device_carries_a_stable_id_and_its_settings():
    described = driver_module._describe(
        {"host": "192.168.1.50", "port": 502, "slave_id": 1, "model": "T-REX-10K-P3G01"}
    )

    assert described["data"]["id"] == "felicity-192.168.1.50-502-1"
    assert described["settings"]["host"] == "192.168.1.50"
    assert "T-REX-10K-P3G01" in described["name"]


def test_pairing_refuses_an_empty_address_without_touching_the_network():
    driver = make_driver()
    result = run(
        driver_module._probe(driver, {"host": "", "port": 502, "slave_id": 1, "model": "x"})
    )
    assert result["ok"] is False
