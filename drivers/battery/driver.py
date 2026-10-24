"""Pairing, repair and the Flow cards shared by every Felicity battery device."""
from __future__ import annotations

from typing import Any

from homey.driver import Driver, ListDeviceProperties
from homey.pair_session import PairSession

try:  # Homey mounts the app root as the top-level package ``app``.
    from app.lib import felicity
except ImportError:  # pragma: no cover - depends on how the runtime roots the app
    from lib import felicity

DEFAULT_PORT = 502
DEFAULT_SLAVE_ID = 1
DEFAULT_MODEL = "T-REX-10K-P3G01"

#: A grid reading this close to zero counts as neither import nor export.
EXPORT_DEADBAND_W = 10.0


async def _always(_args, **_state) -> bool:
    """Run listener for trigger cards that have nothing to filter on."""
    return True


class BatteryDriver(Driver):
    """Drives the Felicity home battery devices."""

    async def on_init(self) -> None:
        await super().on_init()
        self._register_flow_cards()
        self.log("Felicity battery driver initialised")

    ### Flow ###

    def _register_flow_cards(self) -> None:
        flow = self.homey.flow

        flow.get_device_trigger_card("soc_rises_above").register_run_listener(
            self._on_soc_rises_above
        )
        flow.get_device_trigger_card("soc_drops_below").register_run_listener(
            self._on_soc_drops_below
        )

        # The remaining triggers carry no arguments to match on, but the card
        # still needs a listener: FlowCard._run raises when none is registered.
        for card_id in (
            "solar_power_changed",
            "grid_power_changed",
            "house_load_changed",
            "working_mode_changed",
            "fault_occurred",
            "fault_cleared",
        ):
            flow.get_device_trigger_card(card_id).register_run_listener(_always)

        flow.get_condition_card("soc_is_above").register_run_listener(
            self._on_soc_is_above
        )
        flow.get_condition_card("is_charging").register_run_listener(self._on_is_charging)
        flow.get_condition_card("is_exporting").register_run_listener(
            self._on_is_exporting
        )
        flow.get_condition_card("has_fault").register_run_listener(self._on_has_fault)

        mode_card = flow.get_condition_card("working_mode_is")
        mode_card.register_run_listener(self._on_working_mode_is)
        mode_card.register_argument_autocomplete_listener("mode", self._modes_for_device)

        flow.get_action_card("refresh").register_run_listener(self._on_refresh)

    async def _on_soc_rises_above(self, args, soc=None, previous=None, **_) -> bool:
        """True only on the poll where the battery crossed the level upwards."""
        if soc is None or previous is None:
            return False
        threshold = float(args["soc"])
        return previous <= threshold < soc

    async def _on_soc_drops_below(self, args, soc=None, previous=None, **_) -> bool:
        if soc is None or previous is None:
            return False
        threshold = float(args["soc"])
        return previous >= threshold > soc

    async def _on_soc_is_above(self, args, **_) -> bool:
        value = args["device"].get_capability_value("measure_battery")
        return value is not None and value > float(args["soc"])

    async def _on_is_charging(self, args, **_) -> bool:
        return args["device"].get_capability_value("battery_charging_state") == "charging"

    async def _on_is_exporting(self, args, **_) -> bool:
        power = args["device"].get_capability_value("measure_power.grid")
        if power is None:
            return False
        # Grid power is positive while importing, so exporting is the negative side.
        return -power > max(float(args["power"]), EXPORT_DEADBAND_W)

    async def _on_has_fault(self, args, **_) -> bool:
        return bool(args["device"].get_capability_value("alarm_generic"))

    async def _on_working_mode_is(self, args, **_) -> bool:
        selected = args["mode"]
        wanted = selected.get("name") if isinstance(selected, dict) else selected
        return args["device"].get_capability_value("felicity_working_mode") == wanted

    async def _modes_for_device(self, query: str, **kwargs) -> list[dict[str, Any]]:
        """Offer the modes the selected device's own model can report."""
        device = kwargs.get("device")
        model = getattr(device, "model", "") if device is not None else ""
        try:
            options = felicity.mode_options(model)
        except ValueError:
            options = ()
        if not options:
            # No device picked yet, or an unknown model: offer every known mode.
            options = tuple(
                dict.fromkeys(
                    mode
                    for known in felicity.supported_models()
                    for mode in felicity.mode_options(known)
                )
            )
        needle = (query or "").lower()
        return [{"name": mode} for mode in options if needle in mode.lower()]

    async def _on_refresh(self, args, **_) -> None:
        await args["device"].poll_now()

    ### Pairing ###

    async def on_pair(self, session: PairSession) -> None:
        # Defaults the custom view starts from, and the box the tested
        # settings land in before the user confirms the device.
        candidate: dict[str, Any] = {
            "host": "",
            "port": DEFAULT_PORT,
            "slave_id": DEFAULT_SLAVE_ID,
            "model": DEFAULT_MODEL,
        }

        async def get_defaults(_data=None) -> dict[str, Any]:
            return {"settings": candidate, "models": list(felicity.supported_models())}

        async def test_connection(data) -> dict[str, Any]:
            settings = _clean(data, candidate)
            result = await _probe(self, settings)
            if result["ok"]:
                candidate.update(settings)
            return result

        async def list_devices(_view_data=None) -> list[ListDeviceProperties]:
            return [_describe(candidate)]

        session.set_handler("get_defaults", get_defaults)
        session.set_handler("test_connection", test_connection)
        session.set_handler("list_devices", list_devices)

    async def on_repair(self, session: PairSession, device=None) -> None:
        """Re-run the connect view against a device that moved or went quiet."""
        if device is None:
            return

        async def get_defaults(_data=None) -> dict[str, Any]:
            settings = device.get_settings()
            return {
                "settings": {
                    "host": settings.get("host", ""),
                    "port": settings.get("port", DEFAULT_PORT),
                    "slave_id": settings.get("slave_id", DEFAULT_SLAVE_ID),
                    "model": settings.get("model", DEFAULT_MODEL),
                },
                "models": list(felicity.supported_models()),
            }

        async def test_connection(data) -> dict[str, Any]:
            settings = _clean(data, await get_defaults())
            result = await _probe(self, settings)
            if result["ok"]:
                await device.set_settings(settings)
                # Homey does not call on_settings for a programmatic write, so
                # the device has to be told to pick the new address up.
                await device.reconfigure()
                result = {**result, "done": True}
            return result

        session.set_handler("get_defaults", get_defaults)
        session.set_handler("test_connection", test_connection)


def _clean(data: Any, fallback: dict) -> dict[str, Any]:
    """Coerce whatever the pairing view sent into valid connection settings."""
    source = data if isinstance(data, dict) else {}
    defaults = fallback.get("settings", fallback)

    def _int(key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(source.get(key, defaults.get(key, default)))
        except (TypeError, ValueError):
            return default
        return min(max(value, low), high)

    model = str(source.get("model") or defaults.get("model") or DEFAULT_MODEL)
    if model not in felicity.supported_models():
        model = DEFAULT_MODEL

    return {
        "host": str(source.get("host") or defaults.get("host") or "").strip(),
        "port": _int("port", DEFAULT_PORT, 1, 65535),
        "slave_id": _int("slave_id", DEFAULT_SLAVE_ID, 1, 247),
        "model": model,
    }


async def _probe(driver: Driver, settings: dict[str, Any]) -> dict[str, Any]:
    """Read the inverter once so the user finds out now, not after pairing."""
    if not settings["host"]:
        return {"ok": False, "message": "Enter the IP address of the inverter."}

    try:
        snapshot = await felicity.probe(
            settings["host"], settings["port"], settings["slave_id"], settings["model"]
        )
    except Exception as err:  # noqa: BLE001 - every failure is the user's to see
        driver.error("Pairing probe failed:", err)
        return {"ok": False, "message": f"Could not reach the inverter: {err}"}

    if snapshot is None:
        return {
            "ok": False,
            "message": (
                "Connected, but the inverter did not answer. Check the Modbus "
                "unit ID and the selected model."
            ),
        }

    return {
        "ok": True,
        "message": "Connected.",
        "soc": snapshot.soc,
        "battery_power": snapshot.battery_power,
        "working_mode": snapshot.working_mode,
    }


def _describe(settings: dict[str, Any]) -> ListDeviceProperties:
    return {
        "name": f"Felicity {settings['model']}",
        # The address is the only stable identifier the inverter offers over
        # plain Modbus, so it doubles as the device's immutable ID.
        "data": {
            "id": f"felicity-{settings['host']}-{settings['port']}-{settings['slave_id']}"
        },
        "settings": dict(settings),
    }


homey_export = BatteryDriver
