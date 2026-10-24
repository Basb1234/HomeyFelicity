"""One paired Felicity inverter, mirrored into Homey capabilities and Flow.

The device owns a `felicity.FelicityReader`, polls it on the interval the user
picked, and translates each `felicity.Snapshot` into capability values and Flow
triggers. All Modbus knowledge lives in ``lib/``; this module is only about
Homey's side of the contract.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from homey.device import Device

try:  # Homey mounts the app root as the top-level package ``app``.
    from app.lib import felicity, meters
except ImportError:  # pragma: no cover - depends on how the runtime roots the app
    from lib import felicity, meters

#: Meters that only ever count up, so Homey can treat them as real meters.
#: Mapped through `meters.advance` to survive the inverter's counter resets.
CUMULATIVE_METERS = {
    "charged_total": "meter_power.charged",
    "discharged_total": "meter_power.discharged",
    "pv_total": "meter_power.pv",
    "load_total": "meter_power.load",
    "grid_import_total": "meter_power.grid_import",
}

#: Meters the inverter zeroes at midnight; passed through as they are.
DAILY_METERS = {
    "charged_today": "meter_power.charged_today",
    "discharged_today": "meter_power.discharged_today",
    "pv_today": "meter_power.pv_today",
    "load_today": "meter_power.load_today",
    "grid_import_today": "meter_power.grid_import_today",
}

#: Added or removed as the "Extended sensors" setting is toggled.
DETAIL_CAPABILITIES = (
    "measure_voltage.battery",
    "measure_current.battery",
    "measure_voltage.grid",
    "measure_frequency.grid",
    "measure_temperature",
)

#: The energy registers move slowly, so they get their own slower cycle.
ENERGY_INTERVAL_S = 60.0

#: Device store key holding the `meters` bookkeeping for every cumulative meter.
STORE_METERS = "meter_state"

#: Persist that bookkeeping only after this much drift, to spare flash writes.
METER_PERSIST_KWH = 0.1

#: Refresh the diagnostics labels in Settings no more often than this. They are
#: a convenience, not a log, and every refresh is a write to Homey's database.
DIAGNOSTICS_INTERVAL_S = 300.0


class BatteryDevice(Device):
    """A Felicity TREX inverter seen as a Homey home battery."""

    async def on_init(self) -> None:
        await super().on_init()

        self._reader: felicity.FelicityReader | None = None
        self._poll_lock = asyncio.Lock()
        self._interval_id: int | None = None
        self._previous: felicity.Snapshot | None = None
        self._next_energy_at = 0.0
        self._next_diagnostics_at = 0.0
        self._diagnostics: dict[str, str] = {}
        stored = self.get_store().get(STORE_METERS)
        self._meter_state: dict[str, dict] = {
            name: dict(state)
            for name, state in (stored or {}).items()
            if isinstance(state, dict)
        }
        self._meter_persisted: dict[str, float] = {
            name: float(state.get("last", 0.0))
            for name, state in self._meter_state.items()
        }

        await self._sync_detail_capabilities()
        await self._rebuild_reader()
        self._restart_interval()

    async def on_uninit(self) -> None:
        self.homey.clear_interval(self._interval_id)
        self._interval_id = None
        if self._reader is not None:
            self._reader.close()
            self._reader = None

    async def on_settings(self, old_settings, new_settings, changed_keys) -> None:
        if "extended_sensors" in changed_keys:
            await self._sync_detail_capabilities(new_settings)

        if any(key in changed_keys for key in ("host", "port", "slave_id", "model")):
            # A fresh reader already picks the extended setting up.
            await self._rebuild_reader(new_settings)
        elif "extended_sensors" in changed_keys and self._reader is not None:
            self._reader.update_detail(bool(new_settings.get("extended_sensors")))

        if "poll_interval" in changed_keys:
            self._restart_interval(new_settings)

        # Re-read with the new settings applied, so the user sees the effect at
        # once instead of at the next tick. It has to be deferred rather than
        # awaited here: Homey refuses set_settings (the diagnostics labels)
        # while on_settings is still pending.
        self.homey.set_timeout(self._schedule_poll, 500)

    def _schedule_poll(self) -> None:
        """Kick off a poll from a non-async callback.

        ``Homey.set_timeout`` calls its callback without awaiting it, unlike
        ``set_interval``, so a coroutine handed to it would never run.
        """
        asyncio.ensure_future(self.poll_now())

    async def reconfigure(self) -> None:
        """Re-read the settings from scratch.

        `on_settings` covers a user editing the settings screen; this covers a
        programmatic write (the repair flow), which Homey does not announce.
        """
        await self._sync_detail_capabilities()
        await self._rebuild_reader()
        self._restart_interval()
        self._schedule_poll()

    ### Configuration ###

    def _settings(self, override=None) -> dict[str, Any]:
        return dict(override if override is not None else self.get_settings())

    async def _rebuild_reader(self, override=None) -> None:
        """Point the reader at whatever the settings now say."""
        if self._reader is not None:
            self._reader.close()
            self._reader = None

        settings = self._settings(override)
        host = str(settings.get("host") or "").strip()
        if not host:
            await self.set_unavailable("No IP address configured")
            return

        model = str(settings.get("model") or "")
        try:
            self._reader = felicity.FelicityReader(
                host=host,
                port=int(settings.get("port") or 502),
                slave_id=int(settings.get("slave_id") or 1),
                model=model,
                include_detail=bool(settings.get("extended_sensors")),
            )
        except ValueError as err:
            self.error("Cannot build a reader:", err)
            await self.set_unavailable(str(err))

    def _restart_interval(self, override=None) -> None:
        settings = self._settings(override)
        seconds = max(5, int(settings.get("poll_interval") or 10))
        self.homey.clear_interval(self._interval_id)
        # Homey's interval fires straight away and then every `seconds`, so
        # this doubles as the first poll.
        self._interval_id = self.homey.set_interval(self.poll_now, seconds * 1000)

    async def _sync_detail_capabilities(self, override=None) -> None:
        """Add or drop the extended sensors to match the setting."""
        wanted = bool(self._settings(override).get("extended_sensors"))
        for capability in DETAIL_CAPABILITIES:
            try:
                if wanted and not self.has_capability(capability):
                    await self.add_capability(capability)
                elif not wanted and self.has_capability(capability):
                    await self.remove_capability(capability)
            except Exception as err:  # noqa: BLE001 - never block start-up over a capability
                self.error(f"Could not sync capability {capability}:", err)

    ### Polling ###

    async def poll_now(self) -> None:
        """Read the inverter once. Safe to call from a Flow or a timer."""
        if self._poll_lock.locked():
            # A slow or stuck read is still running; skipping beats queueing.
            self.debug("Skipping poll, the previous one has not finished")
            return

        async with self._poll_lock:
            if self._reader is None:
                await self._rebuild_reader()
                if self._reader is None:
                    return

            now = time.monotonic()
            with_energy = now >= self._next_energy_at

            try:
                snapshot = await self._reader.read(with_energy=with_energy)
            except Exception as err:  # noqa: BLE001 - a poll must never kill the app
                self.error("Error while reading the inverter:", err)
                await self._mark_unreachable(str(err))
                return

            if snapshot is None:
                await self._mark_unreachable("No response from the inverter")
                return

            if with_energy:
                self._next_energy_at = now + ENERGY_INTERVAL_S

            await self.set_available()
            await self._apply(snapshot)
            await self._trigger_flows(snapshot)
            self._previous = snapshot
            await self._update_diagnostics(error="-")

    async def _mark_unreachable(self, reason: str) -> None:
        self._previous = None
        await self._update_diagnostics(error=reason, force=True)
        await self.set_unavailable(reason)

    async def _update_diagnostics(self, *, error: str, force: bool = False) -> None:
        """Keep the read-only labels in Settings roughly current."""
        now = time.monotonic()
        if not force and now < self._next_diagnostics_at:
            return
        self._next_diagnostics_at = now + DIAGNOSTICS_INTERVAL_S

        labels = {"last_error": error[:120]}
        if error == "-":
            labels["last_poll"] = time.strftime("%Y-%m-%d %H:%M:%S")

        if labels == self._diagnostics:
            return
        self._diagnostics = labels
        try:
            await self.set_settings(labels)
        except Exception as err:  # noqa: BLE001 - diagnostics are never worth failing over
            self.debug("Could not update the diagnostics labels:", err)

    ### Capabilities ###

    async def _set(self, capability: str, value) -> None:
        """Write a capability, skipping unknown readings and absent capabilities."""
        if value is None or not self.has_capability(capability):
            return
        try:
            await self.set_capability_value(capability, value)
        except Exception as err:  # noqa: BLE001 - one bad capability must not stop the rest
            self.error(f"Could not set {capability}:", err)

    async def _apply(self, snapshot: felicity.Snapshot) -> None:
        await self._set("measure_battery", snapshot.soc)
        await self._set("measure_power", snapshot.battery_power)
        await self._set("battery_charging_state", snapshot.charging_state)
        await self._set("measure_power.pv", snapshot.pv_power)
        await self._set("measure_power.grid", snapshot.grid_power)
        await self._set("measure_power.load", snapshot.load_power)
        await self._set("felicity_working_mode", snapshot.working_mode)
        await self._set("felicity_fault_code", snapshot.fault_code)
        await self._set("alarm_generic", snapshot.fault_active or snapshot.warning_active)

        if snapshot.soc is not None:
            threshold = float(self.get_setting("low_battery_soc") or 0)
            await self._set("alarm_battery", snapshot.soc < threshold)

        await self._set("measure_voltage.battery", snapshot.battery_voltage)
        await self._set("measure_current.battery", snapshot.battery_current)
        await self._set("measure_voltage.grid", snapshot.grid_voltage)
        await self._set("measure_frequency.grid", snapshot.grid_frequency)
        await self._set("measure_temperature", snapshot.temperature)

        if snapshot.meters:
            await self._apply_meters(snapshot.meters)

    async def _apply_meters(self, readings: dict[str, float | None]) -> None:
        dirty = False
        for name, capability in CUMULATIVE_METERS.items():
            raw = readings.get(name)
            if raw is None:
                continue
            total, state = meters.advance(self._meter_state.get(name), raw)
            self._meter_state[name] = state
            await self._set(capability, total)

            persisted = self._meter_persisted.get(name)
            if persisted is None or abs(raw - persisted) >= METER_PERSIST_KWH:
                self._meter_persisted[name] = raw
                dirty = True

        for name, capability in DAILY_METERS.items():
            await self._set(capability, readings.get(name))

        if dirty:
            # One write for all of them; the worst a missed write costs is
            # METER_PERSIST_KWH of drift after a restart.
            try:
                await self.set_store_value(STORE_METERS, self._meter_state)
            except Exception as err:  # noqa: BLE001
                self.debug("Could not persist the meter state:", err)

    ### Flow ###

    async def _trigger_flows(self, snapshot: felicity.Snapshot) -> None:
        previous = self._previous
        flow = self.homey.flow

        if snapshot.soc is not None and previous is not None and previous.soc is not None:
            if snapshot.soc != previous.soc:
                for card_id in ("soc_rises_above", "soc_drops_below"):
                    await flow.get_device_trigger_card(card_id).trigger(
                        self,
                        {"soc": float(snapshot.soc)},
                        soc=float(snapshot.soc),
                        previous=float(previous.soc),
                    )

        for card_id, value, before in (
            ("solar_power_changed", snapshot.pv_power, previous.pv_power if previous else None),
            ("grid_power_changed", snapshot.grid_power, previous.grid_power if previous else None),
            ("house_load_changed", snapshot.load_power, previous.load_power if previous else None),
        ):
            if value is not None and (before is None or round(value) != round(before)):
                await flow.get_device_trigger_card(card_id).trigger(
                    self, {"power": float(value)}
                )

        if snapshot.working_mode and (
            previous is None or snapshot.working_mode != previous.working_mode
        ):
            await flow.get_device_trigger_card("working_mode_changed").trigger(
                self, {"mode": snapshot.working_mode}
            )

        was_faulted = bool(previous and previous.fault_active)
        if snapshot.fault_active and not was_faulted:
            await flow.get_device_trigger_card("fault_occurred").trigger(
                self, {"code": float(snapshot.fault_code or 0)}
            )
        elif was_faulted and not snapshot.fault_active:
            await flow.get_device_trigger_card("fault_cleared").trigger(self)

    ### Queried by the driver's Flow listeners ###

    @property
    def model(self) -> str:
        return str(self.get_setting("model") or "")

    def latest(self) -> felicity.Snapshot | None:
        """The most recent successful reading, or ``None`` while unreachable."""
        return self._previous


homey_export = BatteryDevice
