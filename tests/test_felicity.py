"""Exercise the reader against a simulated inverter.

The fake client answers ``read_holding_registers`` out of a register bank
built from the model's own address map, so these tests check the whole chain
the real device depends on: register grouping, word/endian/scale decoding, the
combined-register maths, and the mapping onto `felicity.Snapshot`.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import felicity, meters  # noqa: E402
from lib.felicity_core import const  # noqa: E402

TREX_10 = const.INVERTER_MODEL_TREX_TEN
TREX_50 = const.INVERTER_MODEL_TREX_FIFTY


class FakeResult:
    def __init__(self, registers):
        self.registers = registers

    def isError(self) -> bool:  # noqa: N802 - pymodbus spells it this way
        return False


class FakeClient:
    """Serves a register bank the way a connected pymodbus client would."""

    def __init__(self, bank: dict[int, int]):
        self.bank = bank
        self.connected = True
        self.reads: list[tuple[int, int]] = []

    async def connect(self):
        self.connected = True

    def close(self):
        self.connected = False

    async def read_holding_registers(self, address, count, device_id):
        self.reads.append((address, count))
        return FakeResult([self.bank.get(address + i, 0) for i in range(count)])


def _encode(value, size, signed=False):
    """Split a value into ``size`` big-endian 16-bit words."""
    raw = int(round(value))
    if signed and raw < 0:
        raw += 1 << (16 * size)
    return [(raw >> (16 * (size - 1 - i))) & 0xFFFF for i in range(size)]


def build_bank(model: str, values: dict[str, float]) -> dict[int, int]:
    """Lay named values into a register bank using the model's own map."""
    register_map = const.MODEL_REGISTRY[model]["registers"]
    bank: dict[int, int] = {}
    for key, value in values.items():
        info = register_map[key]
        index = info.get("index", 0)
        size = info.get("size", 1)
        if index in (1, 8):
            scaled = value * 10
        elif index in (2, 9):
            scaled = value * 100
        elif index == 4:
            scaled = value * 1000
        else:
            scaled = value
        words = _encode(scaled, size, signed=index in (3, 8, 9))
        for offset, word in enumerate(words):
            bank[info["address"] + offset] = word
    return bank


def make_reader(
    model: str,
    values: dict[str, float],
    client_class=FakeClient,
    **kwargs,
) -> felicity.FelicityReader:
    """Build a reader whose Modbus client is the fake bank.

    Must run on an event loop: pymodbus builds its transport against the
    running loop, so `felicity.FelicityReader` can only be constructed from
    async code (which is where both the device and the pairing flow do it).
    """
    reader = felicity.FelicityReader("10.0.0.1", 502, 1, model, **kwargs)
    reader._client = client_class(build_bank(model, values))
    reader._handler.client = reader._client
    return reader


def read(model: str, values: dict[str, float], read_kwargs=None, **kwargs):
    """Build a reader, poll it once, and hand back (snapshot, reader)."""

    async def go():
        reader = make_reader(model, values, **kwargs)
        snapshot = await reader.read(**(read_kwargs or {}))
        return snapshot, reader

    snapshot, reader = asyncio.run(go())
    assert snapshot is not None
    return snapshot, reader


def snapshot_of(model: str, values: dict[str, float], **kwargs) -> felicity.Snapshot:
    return read(model, values, **kwargs)[0]


# --- TREX-10: single battery, watts, Wh meters ------------------------------

TREX_10_VALUES = {
    "working_mode": 2,  # "Bypass" in this model's option list
    "fault_code": 0,
    "battery_capacity": 64.5,
    "battery_power": 2400,
    "battery_voltage": 51.2,
    "battery_current": 46.9,
    "pv_input_power": 1800,
    "pv2_input_power": 1500,
    "pv3_input_power": 0,
    "total_ac_input_power": -900,
    "total_ac_output_active_power": 1200,
    "pv_generated_energy_total": 12_345_000,
    "pv_generated_energy_day": 18_400,
    "battery_charged_energy_total": 4_200_000,
    "battery_charged_energy_day": 9_100,
    "battery_discharged_energy_total": 3_900_000,
    "battery_discharged_energy_day": 7_300,
    "load_consumption_energy_total": 9_000_000,
    "load_consumption_energy_day": 14_000,
    "ac_input_energy_total": 5_500_000,
    "ac_input_energy_day": 2_100,
}


def test_trex_10_core_readings():
    snapshot = snapshot_of(TREX_10, TREX_10_VALUES)

    assert snapshot.soc == pytest.approx(64.5)
    assert snapshot.battery_power == pytest.approx(2400)
    assert snapshot.battery_voltage == pytest.approx(51.2)
    assert snapshot.battery_current == pytest.approx(46.9)
    # The three strings are summed by the core's combined register.
    assert snapshot.pv_power == pytest.approx(3300)
    assert snapshot.load_power == pytest.approx(1200)
    assert snapshot.working_mode == "Bypass"
    assert snapshot.fault_active is False


def test_trex_10_grid_power_keeps_its_sign():
    """Negative grid power means the house is exporting."""
    snapshot = snapshot_of(TREX_10, TREX_10_VALUES)
    assert snapshot.grid_power == pytest.approx(-900)


def test_trex_10_meters_are_converted_to_kwh():
    snapshot = snapshot_of(TREX_10, TREX_10_VALUES)

    assert snapshot.meters["pv_total"] == pytest.approx(12345.0)
    assert snapshot.meters["pv_today"] == pytest.approx(18.4)
    assert snapshot.meters["charged_total"] == pytest.approx(4200.0)
    assert snapshot.meters["discharged_today"] == pytest.approx(7.3)
    assert snapshot.meters["grid_import_total"] == pytest.approx(5500.0)
    assert set(snapshot.meters) == set(felicity.METER_NAMES)


def test_charging_state_follows_the_sign_of_battery_power():
    charging = snapshot_of(TREX_10, TREX_10_VALUES)
    assert charging.charging_state == "charging"

    discharging = snapshot_of(TREX_10, {**TREX_10_VALUES, "battery_power": -1500})
    assert discharging.charging_state == "discharging"

    idle = snapshot_of(TREX_10, {**TREX_10_VALUES, "battery_power": 5})
    assert idle.charging_state == "idle"


def test_fault_and_warning_codes_are_surfaced():
    snapshot = snapshot_of(
        TREX_10,
        {**TREX_10_VALUES, "fault_code": 14, "warning_state_1": 3},
        include_detail=True,
    )
    assert snapshot.fault_code == 14
    assert snapshot.fault_active is True
    assert snapshot.warning_code == 3


def test_detail_registers_are_only_read_when_asked_for():
    _, plain = read(TREX_10, TREX_10_VALUES)
    without = {address for address, _ in plain._client.reads}

    snapshot, detailed = read(
        TREX_10,
        {**TREX_10_VALUES, "ac_input_voltage": 231.4, "inner_temperature_1": 38.5},
        include_detail=True,
    )
    with_detail = {address for address, _ in detailed._client.reads}

    assert len(with_detail) > len(without)
    assert snapshot.grid_voltage == pytest.approx(231.4)
    assert snapshot.temperature == pytest.approx(38.5)


def test_temperature_reports_the_hottest_sensor():
    snapshot = snapshot_of(
        TREX_10,
        {
            **TREX_10_VALUES,
            "inner_temperature_1": 31.0,
            "heatsink_temperature_1": 47.5,
            "heatsink_temperature_2": 44.0,
        },
        include_detail=True,
    )
    assert snapshot.temperature == pytest.approx(47.5)


def test_skipping_energy_halves_nothing_but_the_meters():
    snapshot = snapshot_of(TREX_10, TREX_10_VALUES, read_kwargs={"with_energy": False})

    assert snapshot.meters == {}
    assert snapshot.soc == pytest.approx(64.5)


# --- TREX-50: dual battery, kW, kWh meters ----------------------------------

TREX_50_VALUES = {
    "fault_codes": 0,
    "alarm_codes": 0,
    "system_mode": 1,  # "Zero Export To Load"
    "pv1_power": 2.4,
    "pv2_power": 1.6,
    "pv3_power": 0.0,
    "pv4_power": 0.0,
    "bat1_voltage": 409.6,
    "bat2_voltage": 410.0,
    "bat1_current": 12.5,
    "bat2_current": 11.5,
    "bat1_power": 5.1,
    "bat2_power": 4.9,
    "bat1_soc": 72.0,
    "bat2_soc": 68.0,
    "phase_a_load_active_power": 1.0,
    "phase_b_load_active_power": 0.8,
    "phase_c_load_active_power": 0.7,
    "phase_a_ct_active_power": -0.5,
    "phase_b_ct_active_power": -0.4,
    "phase_c_ct_active_power": -0.3,
    "total_grid_power": -1.2,
    "pv_total_energy": 24_680.0,
    "pv1_day_energy": 8.1,
    "pv2_day_energy": 6.3,
    "pv3_day_energy": 0.0,
    "pv4_day_energy": 0.0,
    "bat1_day_charge_energy": 5.5,
    "bat2_day_charge_energy": 5.1,
    "bat1_year_charge_energy": 820.0,
    "bat2_year_charge_energy": 790.0,
    "bat1_day_discharge_energy": 4.2,
    "bat2_day_discharge_energy": 4.0,
    "bat1_year_discharge_energy": 700.0,
    "bat2_year_discharge_energy": 680.0,
    "grid_day_cost_energy": 3.3,
    "grid_year_cost_energy": 1_100.0,
    "load_day_cost_energy": 12.0,
    "load_year_cost_energy": 4_400.0,
}


def test_trex_50_normalises_to_the_same_fields():
    snapshot = snapshot_of(TREX_50, TREX_50_VALUES)

    # The conservative of the two packs, as the HA integration does it.
    assert snapshot.soc == pytest.approx(68.0)
    # kW on the wire, watts in the snapshot.
    assert snapshot.battery_power == pytest.approx(10_000)
    assert snapshot.pv_power == pytest.approx(4000)
    assert snapshot.load_power == pytest.approx(2500)
    assert snapshot.grid_power == pytest.approx(-1200)
    assert snapshot.battery_voltage == pytest.approx(409.6)
    assert snapshot.working_mode == "Zero Export To Load"


def test_trex_50_meters_are_already_kwh():
    snapshot = snapshot_of(TREX_50, TREX_50_VALUES)

    assert snapshot.meters["pv_total"] == pytest.approx(24_680.0)
    assert snapshot.meters["pv_today"] == pytest.approx(14.4)
    # Both packs are summed.
    assert snapshot.meters["charged_today"] == pytest.approx(10.6)
    assert snapshot.meters["charged_total"] == pytest.approx(1610.0)
    assert snapshot.meters["discharged_total"] == pytest.approx(1380.0)


def test_both_families_expose_the_same_snapshot_fields():
    single = snapshot_of(TREX_10, TREX_10_VALUES)
    dual = snapshot_of(TREX_50, TREX_50_VALUES)

    for field in ("soc", "battery_power", "pv_power", "grid_power", "load_power"):
        assert getattr(single, field) is not None, field
        assert getattr(dual, field) is not None, field


# --- Failure handling -------------------------------------------------------


class DeadClient(FakeClient):
    async def read_holding_registers(self, address, count, device_id):
        raise OSError("connection reset")


def test_read_returns_none_when_nothing_answers():
    async def go():
        reader = make_reader(TREX_10, TREX_10_VALUES, client_class=DeadClient)
        return await reader.read()

    assert asyncio.run(go()) is None


def test_unsupported_model_is_rejected_up_front():
    async def go():
        felicity.FelicityReader("10.0.0.1", 502, 1, "T-REX-9000")

    with pytest.raises(ValueError):
        asyncio.run(go())


def test_mode_options_come_from_the_register_map():
    assert "Bypass" in felicity.mode_options(TREX_10)
    assert "Zero Export To CT" in felicity.mode_options(TREX_50)


# --- Monotonic meters -------------------------------------------------------


def test_meter_passes_a_rising_counter_through():
    total, state = meters.advance(None, 100.0)
    assert total == pytest.approx(100.0)

    total, state = meters.advance(state, 140.0)
    assert total == pytest.approx(140.0)


def test_meter_absorbs_a_counter_reset():
    """A year rollover must not make Homey see the meter run backwards."""
    _, state = meters.advance(None, 820.0)
    _, state = meters.advance(state, 905.5)

    total, state = meters.advance(state, 2.5)  # New Year's Day
    assert total == pytest.approx(908.0)

    total, _ = meters.advance(state, 11.0)
    assert total == pytest.approx(916.5)


def test_meter_ignores_noise_sized_dips():
    _, state = meters.advance(None, 500.0)
    total, _ = meters.advance(state, 499.98)
    assert total == pytest.approx(499.98)


def test_meter_state_survives_a_round_trip_through_the_store():
    _, state = meters.advance(None, 300.0)
    _, state = meters.advance(state, 12.0)  # reset banked 300

    # Homey stores it as plain JSON, so it comes back as a fresh dict.
    revived = dict(state)
    total, _ = meters.advance(revived, 20.0)
    assert total == pytest.approx(320.0)
