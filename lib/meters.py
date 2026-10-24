"""Keep a resettable inverter counter looking monotonic to Homey.

Homey's Energy screen treats a ``meter_power`` capability as a meter that only
ever goes up; a value that drops reads as a correction, not as "the counter
started over". Two things make Felicity counters drop:

* On the TREX-25K/50K there is no lifetime battery counter at all, only
  day/month/year ones, so the best available total resets every New Year.
* Any inverter's counters go back to zero after a firmware reset or a board
  swap, on every model.

`advance` absorbs both by carrying an offset: when the raw reading falls, the
last reading before the fall is banked and added to everything that follows.
The state is a plain dict so the caller can park it in Homey's device store
and survive an app restart.
"""
from __future__ import annotations

#: A drop smaller than this is measurement noise, not a reset.
_NOISE_KWH = 0.05


def new_state() -> dict[str, float]:
    """Starting state for a meter that has never been read."""
    return {"offset": 0.0, "last": 0.0, "seen": 0}


def advance(state: dict | None, raw: float) -> tuple[float, dict[str, float]]:
    """Fold ``raw`` into ``state``.

    Returns the monotonic total and the state to persist. The first reading
    establishes the baseline rather than being treated as a jump, so adding a
    device to Homey never invents a huge first delta.
    """
    current = new_state()
    if state:
        current.update(
            {
                "offset": float(state.get("offset", 0.0)),
                "last": float(state.get("last", 0.0)),
                "seen": int(state.get("seen", 0)),
            }
        )

    if not current["seen"]:
        current["seen"] = 1
    elif raw < current["last"] - _NOISE_KWH:
        # The counter rolled over: bank what it had reached before the drop.
        current["offset"] += current["last"]

    current["last"] = raw
    return round(current["offset"] + raw, 3), current
