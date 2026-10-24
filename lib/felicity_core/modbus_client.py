"""Platform-agnostic Modbus TCP read/decode helpers.

Shared verbatim with the planned Homey app (see CLAUDE.md "Homey Native
App") — contains no ``homeassistant.*`` imports (enforced by
``tests/test_core_boundary.py``; add this file to
``tools/core_modules.py`` if/when the Homey vendoring script is built).

Scope is Modbus **TCP only**: this deployment's inverters are TCP, and
serial/RTU is neither needed here nor apparently exposed to Homey's app
sandbox (see CLAUDE.md).  RTU support, where still needed for other HA
users, stays in ``__init__.py``'s ``FelicitySerialHub`` — untouched by
this module.

The register **write** path already lives in ``type_specific.py``
(``TypeSpecificHandler.async_write_register``/``async_write_registers``),
which is duck-typed against any client exposing an async
``write_registers(address=, values=, device_id=)`` — no pymodbus import
needed there.  This module is the read-side counterpart: connecting,
issuing the grouped ``read_holding_registers`` calls, and decoding the
raw register words back into named, scaled values using the same
register_map convention (address/size/endian/index) that the write path
consumes in reverse.
"""
from __future__ import annotations

import logging
from typing import Any

from pymodbus.client import AsyncModbusTcpClient

_LOGGER = logging.getLogger(__name__)


def create_tcp_client(host: str, port: int, timeout: int = 5) -> AsyncModbusTcpClient:
    """Build an unconnected Modbus TCP client for the given host/port."""
    return AsyncModbusTcpClient(host=host, port=port, timeout=timeout)


async def ensure_connected(client: Any) -> bool:
    """Connect ``client`` if not already connected.

    Returns the resulting connected state. Never raises — connection
    failures are logged and reported as ``False`` so callers can decide
    how to surface that (e.g. HA's ``UpdateFailed``, a Homey device
    "unavailable" state).
    """
    if not client.connected:
        try:
            await client.connect()
        except Exception as err:  # noqa: BLE001 - any transport failure is non-fatal here
            _LOGGER.error("Failed to connect to Felicity: %s", err)
            return False
    return client.connected


def apply_scaling(raw: int, index: int, size: int = 1) -> int | float:
    """Apply the register-map "index" scaling/sign convention to a raw integer.

    See ``const.py``'s "Precision and index" comment for the index legend.
    """
    if index == 1:  # /10 - only for size=1
        return raw / 10.0
    elif index == 2:  # /100 - only for size=1
        if size != 1:
            _LOGGER.warning("Index 2 (/100) used with size=%d - applying anyway", size)
        return raw / 100.0
    elif index == 3:  # signed
        if size == 1 and raw >= 0x8000:
            return raw - 0x10000
        elif size == 2 and raw >= 0x80000000:
            return raw - 0x100000000
        elif size == 4 and raw >= 0x8000000000000000:
            return raw - 0x10000000000000000
        return raw
    elif index == 4:  # /1000 - only for size=1
        if size != 1:
            _LOGGER.warning("Index 4 (/1000) used with size=%d - applying anyway", size)
        return raw / 1000.0
    elif index == 8:  # /10 (and signed possible)
        if size == 1 and raw >= 0x8000:
            raw -= 0x10000
        elif size == 2 and raw >= 0x80000000:
            raw -= 0x100000000
        elif size == 4 and raw >= 0x8000000000000000:
            raw -= 0x10000000000000000
        return raw / 10.0
    elif index == 9:  # /100 (and signed possible)
        if size == 1 and raw >= 0x8000:
            raw -= 0x10000
        elif size == 2 and raw >= 0x80000000:
            raw -= 0x100000000
        elif size == 4 and raw >= 0x8000000000000000:
            raw -= 0x10000000000000000
        return raw / 100.0
    else:
        return raw  # index 0,4,5,6,7 - raw


def _reconstruct_raw(reg_slice: list[int], size: int, endian: str, index: int) -> int | None:
    """Reassemble a contiguous slice of 16-bit register words into one integer.

    Handles big/little endian word order for 1/2/4-word (16/32/64-bit)
    values, and sign-extends 64-bit values when ``index == 3`` (the
    "signed" convention). Returns ``None`` for an unsupported size.
    """
    if size == 1:
        return reg_slice[0]
    elif size == 2:
        if endian == "big":
            return (reg_slice[0] << 16) | reg_slice[1]
        return (reg_slice[1] << 16) | reg_slice[0]
    elif size == 4:
        if endian == "big":
            raw = (reg_slice[0] << 48) | (reg_slice[1] << 32) | (reg_slice[2] << 16) | reg_slice[3]
        else:
            raw = (reg_slice[3] << 48) | (reg_slice[2] << 32) | (reg_slice[1] << 16) | reg_slice[0]
        if index == 3 and raw >= (1 << 63):
            raw -= 1 << 64
        return raw
    return None


def decode_register_group(
    registers: list[int], keys: list[str], register_map: dict
) -> dict[str, Any]:
    """Decode one contiguous block of raw register words into named values.

    ``registers`` is the raw word list returned for a group read starting
    at ``group["start"]``; ``keys`` is that group's ``register_map`` keys
    in address order (``const.build_groups``' output). Mirrors
    ``type_specific.TypeSpecificHandler.async_write_register``'s encode
    logic in the opposite direction.
    """
    decoded: dict[str, Any] = {}
    pos = 0
    for key in keys:
        info = register_map.get(key)
        if info is None:
            _LOGGER.warning("Key '%s' not in register_map, skipping", key)
            continue
        size = info.get("size", 1)
        endian = info.get("endian", "big")
        index = info.get("index", 0)
        precision = info.get("precision", 0)

        if pos + size > len(registers):
            _LOGGER.warning("Insufficient registers for %s", key)
            break

        reg_slice = registers[pos:pos + size]
        pos += size

        raw = _reconstruct_raw(reg_slice, size, endian, index)
        if raw is None:
            _LOGGER.warning("Unsupported register size %d for key %s", size, key)
            continue

        value = apply_scaling(raw, index, size)
        if isinstance(value, float):
            value = round(value, precision)

        decoded[key] = value
    return decoded


async def read_register_groups(
    client: Any, address_groups: list[dict], register_map: dict, slave_id: int
) -> tuple[dict[str, Any], bool]:
    """Read and decode every address group.

    Returns ``(decoded_data, any_read_ok)``. A group that fails to read
    (transport exception or a Modbus error response) is logged and
    skipped rather than aborting the whole poll — matches prior
    coordinator behavior, since a partial read is still useful.
    """
    new_data: dict[str, Any] = {}
    any_read_ok = False

    for group in address_groups:
        start_addr = group["start"]
        count = group["count"]
        try:
            result = await client.read_holding_registers(
                address=start_addr,
                count=count,
                device_id=slave_id,
            )
        except Exception as err:  # noqa: BLE001 - transport errors are per-group, non-fatal
            _LOGGER.error("Read error at address %d, count: %d error:%s", start_addr, count, err)
            continue

        if result.isError():
            _LOGGER.warning("Read error at address %d, skipping group", start_addr)
            continue

        any_read_ok = True
        new_data.update(decode_register_group(result.registers, group["keys"], register_map))

    return new_data, any_read_ok
