"""Read a Felicity inverter and normalise it into one model-agnostic snapshot.

Everything Modbus-specific -- the register maps, the word/endian/scale
decoding, the combined-register maths and the per-model quirks -- comes from
``lib.felicity_core``, which is a verbatim copy of the platform-agnostic core
of the ``ha_felicity`` Home Assistant integration (see ``tools/sync_core.py``).
This module adds only the glue Homey needs: picking the subset of registers
worth polling, running the core's decoder over them, and flattening the result
into a `Snapshot` whose field names mean the same thing on every model.

The two TREX families expose the same physical quantities under different
register names. The core already papers over most of that through its
*combined* registers: on the 25K/50K, ``battery_power``, ``battery_voltage``,
``battery_current``, ``total_ac_input_power`` and ``total_ac_output_active_power``
exist as derived values under exactly the names the 5K/10K uses for raw ones.
So after `_merge_combined` one set of keys works for both, and the only genuine
fork left is which registers to ask for.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .felicity_core import const, modbus_client
from .felicity_core.type_specific import TypeSpecificHandler

_LOGGER = logging.getLogger(__name__)

#: TREX-5K / TREX-10K: one battery, powers already in watts.
FAMILY_SINGLE = (const.INVERTER_MODEL_TREX_FIVE, const.INVERTER_MODEL_TREX_TEN)
#: TREX-25K / TREX-50K: up to two batteries, powers in kW, per-phase CT meters.
FAMILY_DUAL = (const.INVERTER_MODEL_TREX_TWENTY_FIVE, const.INVERTER_MODEL_TREX_FIFTY)

#: Energy meters a `Snapshot` carries, in kWh. The ``*_today`` ones reset at midnight.
METER_NAMES = (
    "pv_total",
    "pv_today",
    "charged_total",
    "charged_today",
    "discharged_total",
    "discharged_today",
    "load_total",
    "load_today",
    "grid_import_total",
    "grid_import_today",
)

#: Battery power below this, either direction, counts as standing still.
IDLE_POWER_W = 25.0

#: A temperature register reading below this is reporting "no sensor".
_MIN_PLAUSIBLE_TEMPERATURE = -40.0


@dataclass(frozen=True)
class _Profile:
    """Which registers to read, and how to read them, for one model family."""

    core_keys: tuple[str, ...]
    """Always polled: everything behind the primary capabilities."""
    detail_keys: tuple[str, ...]
    """Polled only when the device enables the extended sensors."""
    energy_keys: tuple[str, ...]
    """The cumulative and daily meters."""
    combined_keys: tuple[str, ...]
    """Derived values to compute from the raw read (see `_merge_combined`)."""
    pv_power_key: str
    grid_voltage_key: str
    grid_frequency_key: str
    temperature_keys: tuple[str, ...]
    mode_key: str
    fault_key: str
    warning_keys: tuple[str, ...]
    energy_scale: float
    """Multiplier taking a raw meter value to kWh."""
    meter_sources: dict[str, tuple[str, ...]]
    """Meter name -> register keys to sum for it."""


_PROFILE_SINGLE = _Profile(
    core_keys=(
        "working_mode",
        "fault_code",
        "battery_voltage",
        "battery_current",
        "battery_power",
        "battery_capacity",
        "total_ac_input_power",
        "total_ac_output_active_power",
        "pv_input_power",
        "pv2_input_power",
        "pv3_input_power",
    ),
    detail_keys=(
        "warning_state_1",
        "warning_state_2",
        "warning_state_3",
        "ac_input_voltage",
        "ac_input_frequency",
        "inner_temperature_1",
        "heatsink_temperature_1",
        "heatsink_temperature_2",
    ),
    energy_keys=(
        "pv_generated_energy_total",
        "pv_generated_energy_day",
        "load_consumption_energy_total",
        "load_consumption_energy_day",
        "ac_input_energy_total",
        "ac_input_energy_day",
        "battery_charged_energy_total",
        "battery_charged_energy_day",
        "battery_discharged_energy_total",
        "battery_discharged_energy_day",
    ),
    combined_keys=("pv_total_power",),
    pv_power_key="pv_total_power",
    grid_voltage_key="ac_input_voltage",
    grid_frequency_key="ac_input_frequency",
    temperature_keys=(
        "inner_temperature_1",
        "heatsink_temperature_1",
        "heatsink_temperature_2",
    ),
    mode_key="working_mode",
    fault_key="fault_code",
    warning_keys=("warning_state_1", "warning_state_2", "warning_state_3"),
    energy_scale=0.001,  # the 5K/10K count in Wh
    meter_sources={
        "pv_total": ("pv_generated_energy_total",),
        "pv_today": ("pv_generated_energy_day",),
        "charged_total": ("battery_charged_energy_total",),
        "charged_today": ("battery_charged_energy_day",),
        "discharged_total": ("battery_discharged_energy_total",),
        "discharged_today": ("battery_discharged_energy_day",),
        "load_total": ("load_consumption_energy_total",),
        "load_today": ("load_consumption_energy_day",),
        "grid_import_total": ("ac_input_energy_total",),
        "grid_import_today": ("ac_input_energy_day",),
    },
)

_PROFILE_DUAL = _Profile(
    core_keys=(
        "fault_codes",
        "alarm_codes",
        "pv1_power",
        "pv2_power",
        "pv3_power",
        "pv4_power",
        "bat1_voltage",
        "bat2_voltage",
        "bat1_current",
        "bat2_current",
        "bat1_power",
        "bat2_power",
        "bat1_soc",
        "bat2_soc",
        "phase_a_load_active_power",
        "phase_b_load_active_power",
        "phase_c_load_active_power",
        "phase_a_ct_active_power",
        "phase_b_ct_active_power",
        "phase_c_ct_active_power",
        "total_grid_power",
        "system_mode",
    ),
    detail_keys=(
        "phase_a_grid_voltage",
        "grid_frequency",
        "inverter_temperature",
        "ambient_temperature",
        "bat1_soh",
    ),
    energy_keys=(
        "pv_total_energy",
        "pv1_day_energy",
        "pv2_day_energy",
        "pv3_day_energy",
        "pv4_day_energy",
        "bat1_day_charge_energy",
        "bat2_day_charge_energy",
        "bat1_year_charge_energy",
        "bat2_year_charge_energy",
        "bat1_day_discharge_energy",
        "bat2_day_discharge_energy",
        "bat1_year_discharge_energy",
        "bat2_year_discharge_energy",
        "grid_day_cost_energy",
        "grid_year_cost_energy",
        "load_day_cost_energy",
        "load_year_cost_energy",
    ),
    combined_keys=(
        "total_pv_power",
        "battery_power",
        "battery_voltage",
        "battery_current",
        "total_ac_input_power",
        "total_ac_output_active_power",
    ),
    pv_power_key="total_pv_power",
    grid_voltage_key="phase_a_grid_voltage",
    grid_frequency_key="grid_frequency",
    temperature_keys=("inverter_temperature", "ambient_temperature"),
    mode_key="system_mode",
    fault_key="fault_codes",
    warning_keys=("alarm_codes",),
    energy_scale=1.0,  # the 25K/50K already count in kWh
    meter_sources={
        "pv_total": ("pv_total_energy",),
        "pv_today": ("pv1_day_energy", "pv2_day_energy", "pv3_day_energy", "pv4_day_energy"),
        # The 25K/50K expose no lifetime battery counter, only day/month/year.
        # Year is the longest-running one; the caller absorbs its new-year
        # reset so Homey still sees a monotonic meter (see device.py).
        "charged_total": ("bat1_year_charge_energy", "bat2_year_charge_energy"),
        "charged_today": ("bat1_day_charge_energy", "bat2_day_charge_energy"),
        "discharged_total": ("bat1_year_discharge_energy", "bat2_year_discharge_energy"),
        "discharged_today": ("bat1_day_discharge_energy", "bat2_day_discharge_energy"),
        "load_total": ("load_year_cost_energy",),
        "load_today": ("load_day_cost_energy",),
        "grid_import_total": ("grid_year_cost_energy",),
        "grid_import_today": ("grid_day_cost_energy",),
    },
)

_PROFILES: dict[str, _Profile] = {model: _PROFILE_SINGLE for model in FAMILY_SINGLE}
_PROFILES.update({model: _PROFILE_DUAL for model in FAMILY_DUAL})


@dataclass
class Snapshot:
    """One poll's worth of readings, meaning the same thing on every model."""

    soc: float | None = None
    """Battery state of charge, %. The lower pack on dual-battery models."""
    battery_power: float | None = None
    """Watts. Positive while charging, negative while discharging."""
    battery_voltage: float | None = None
    battery_current: float | None = None
    pv_power: float | None = None
    """Watts across all PV strings."""
    grid_power: float | None = None
    """Watts. Positive while importing, negative while exporting."""
    load_power: float | None = None
    """Watts drawn by the house."""
    grid_voltage: float | None = None
    grid_frequency: float | None = None
    temperature: float | None = None
    """Hottest of the inverter's internal sensors, degrees Celsius."""
    working_mode: str | None = None
    fault_code: int | None = None
    warning_code: int | None = None
    meters: dict[str, float | None] = field(default_factory=dict)
    """kWh readings keyed by `METER_NAMES`."""
    raw: dict[str, Any] = field(default_factory=dict)
    """Every decoded register, for diagnostics."""

    @property
    def fault_active(self) -> bool:
        return bool(self.fault_code)

    @property
    def warning_active(self) -> bool:
        return bool(self.warning_code)

    @property
    def charging_state(self) -> str:
        """``charging`` / ``discharging`` / ``idle``, as Homey's enum spells them."""
        power = self.battery_power
        if power is None or abs(power) < IDLE_POWER_W:
            return "idle"
        return "charging" if power > 0 else "discharging"


def supported_models() -> tuple[str, ...]:
    """Model identifiers this app can talk to."""
    return tuple(const.SUPPORTED_MODELS)


def mode_options(model: str) -> tuple[str, ...]:
    """Every working-mode label the given model can report."""
    profile = _profile(model)
    register_map = const.MODEL_REGISTRY[model]["registers"]
    return tuple(register_map.get(profile.mode_key, {}).get("options") or ())


def _profile(model: str) -> _Profile:
    try:
        return _PROFILES[model]
    except KeyError:
        raise ValueError(f"Unsupported inverter model: {model}") from None


def _merge_combined(data: dict[str, Any], combined_map: dict, keys) -> None:
    """Evaluate the core's combined registers in place over ``data``.

    Mirrors how ``ha_felicity``'s combined sensors work: gather the named
    sources, skip the value entirely if any of them is missing, otherwise hand
    them to the register map's own ``calc``.
    """
    for key in keys:
        info = combined_map.get(key)
        if info is None:
            continue
        values = [data.get(src) for src in info["sources"]]
        if any(value is None for value in values):
            continue
        try:
            data[key] = info["calc"](*values)
        except Exception as err:  # noqa: BLE001 - one bad derivation must not kill the poll
            _LOGGER.warning("Could not derive %s: %s", key, err)


def _decode_mode(register_map: dict, key: str, value: Any) -> str | None:
    """Turn a mode register into its label, using the map's own option list."""
    if value is None:
        return None
    index = int(value)
    options = register_map.get(key, {}).get("options")
    if options and 0 <= index < len(options):
        return options[index]
    return f"Unknown ({index})"


class FelicityReader:
    """Polls one inverter over Modbus TCP and returns `Snapshot` objects.

    Construct this from async code only: pymodbus binds its transport to the
    running event loop, so building a reader off-loop raises ``RuntimeError``.
    """

    def __init__(
        self,
        host: str,
        port: int,
        slave_id: int,
        model: str,
        *,
        include_detail: bool = False,
        timeout: int = 5,
    ) -> None:
        self.host = host
        self.port = port
        self.slave_id = slave_id
        self.model = model
        self.include_detail = include_detail

        self._profile = _profile(model)
        registry = const.MODEL_REGISTRY[model]
        self._register_map: dict = registry["registers"]
        self._combined_map: dict = registry["combined"]
        self._client = modbus_client.create_tcp_client(host, port, timeout=timeout)
        # Used only for its model-aware derivations; this app never writes.
        self._handler = TypeSpecificHandler(
            self._client, slave_id, model, self._register_map
        )
        self._fast_groups = self._build_groups(self._fast_keys())
        self._energy_groups = self._build_groups(self._profile.energy_keys)

    ### Internal ###

    def _fast_keys(self) -> tuple[str, ...]:
        keys = self._profile.core_keys
        if self.include_detail:
            keys += self._profile.detail_keys
        return keys

    def _build_groups(self, keys) -> list[dict]:
        """Turn a key list into contiguous read blocks via the core's grouper."""
        subset = {
            key: info for key, info in self._register_map.items() if key in keys
        }
        missing = set(keys) - set(subset)
        if missing:
            _LOGGER.debug(
                "Model %s has no register for: %s",
                self.model,
                ", ".join(sorted(missing)),
            )
        return const.build_groups(subset)

    def _meter(self, data: dict[str, Any], name: str) -> float | None:
        values = [data.get(src) for src in self._profile.meter_sources.get(name, ())]
        present = [value for value in values if value is not None]
        if not present:
            return None
        return round(sum(present) * self._profile.energy_scale, 3)

    def _temperature(self, data: dict[str, Any]) -> float | None:
        readings = [
            data[key]
            for key in self._profile.temperature_keys
            if data.get(key) is not None
        ]
        plausible = [t for t in readings if t > _MIN_PLAUSIBLE_TEMPERATURE]
        return max(plausible) if plausible else None

    def _warning_code(self, data: dict[str, Any]) -> int:
        for key in self._profile.warning_keys:
            value = data.get(key)
            if value:
                return int(value)
        return 0

    ### Public API ###

    def update_detail(self, include_detail: bool) -> None:
        """Start or stop polling the extended registers."""
        if include_detail == self.include_detail:
            return
        self.include_detail = include_detail
        self._fast_groups = self._build_groups(self._fast_keys())

    async def connect(self) -> bool:
        """Connect if needed; ``False`` when the inverter is unreachable."""
        return await modbus_client.ensure_connected(self._client)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception as err:  # noqa: BLE001 - closing must never raise
            _LOGGER.debug("Error while closing the Modbus client: %s", err)

    async def read(self, *, with_energy: bool = True) -> Snapshot | None:
        """Poll the inverter. ``None`` means not a single register came back.

        ``with_energy=False`` skips the meter registers, which barely move
        between polls, and so cuts the Modbus traffic of a routine cycle.
        """
        if not await self.connect():
            return None

        groups = list(self._fast_groups)
        if with_energy:
            groups += self._energy_groups

        data, any_read_ok = await modbus_client.read_register_groups(
            self._client, groups, self._register_map, self.slave_id
        )
        if not any_read_ok:
            return None

        _merge_combined(data, self._combined_map, self._profile.combined_keys)

        profile = self._profile
        snapshot = Snapshot(
            soc=self._handler.determine_battery_soc(data),
            battery_power=data.get("battery_power"),
            battery_voltage=self._handler.determine_battery_voltage(data),
            battery_current=data.get("battery_current"),
            pv_power=data.get(profile.pv_power_key),
            grid_power=self._handler.determine_grid_power(data),
            load_power=data.get("total_ac_output_active_power"),
            grid_voltage=data.get(profile.grid_voltage_key),
            grid_frequency=data.get(profile.grid_frequency_key),
            temperature=self._temperature(data),
            working_mode=_decode_mode(
                self._register_map, profile.mode_key, data.get(profile.mode_key)
            ),
            fault_code=data.get(profile.fault_key),
            warning_code=self._warning_code(data),
            raw=data,
        )
        if with_energy:
            snapshot.meters = {name: self._meter(data, name) for name in METER_NAMES}
        return snapshot


async def probe(host: str, port: int, slave_id: int, model: str) -> Snapshot | None:
    """One-shot read, used by pairing to prove the entered settings work."""
    reader = FelicityReader(host, port, slave_id, model)
    try:
        return await reader.read(with_energy=False)
    finally:
        reader.close()
