"""Write a room setpoint and verify the unit really applied it."""
from __future__ import annotations

import logging
import time

from homeassistant.exceptions import HomeAssistantError

from .extras import MODBUS_LOCK

_LOGGER = logging.getLogger(__name__)


def _first(value):
    return value[0] if isinstance(value, list) and value else None


def set_room_setpoint(io, index: int, temperature: float) -> float | None:
    """Switch the room to manual, clear any override, write the setpoint, read back the effective value."""
    base = index * 100
    raw = int(round(temperature * 100))
    with MODBUS_LOCK:
        mode = _first(io.read("hr", base + 117))
        override = _first(io.read("hr", base + 118))
        if mode == 0:  # schedule: the written setpoint would be ignored
            io.write(base + 117, 1)
        if override not in (None, 0):
            io.write(base + 118, 0)
        ok, code = io.write(base + 119, raw)
        if not ok:
            raise HomeAssistantError(
                f"The Wavin Sentio rejected the new setpoint {temperature} (Modbus exception {code}). "
                "Check that Modbus TCP is set to 'Slave Read/Write' on the Sentio."
            )
        time.sleep(0.7)
        written = _first(io.read("hr", base + 119))
        effective_raw = _first(io.read("ir", base + 101))
        standby = _first(io.read("hr", 26))
        vacation = _first(io.read("hr", 27))
        mode_after = _first(io.read("hr", base + 117))
        override_after = _first(io.read("hr", base + 118))
    effective = None if effective_raw is None else effective_raw / 100
    _LOGGER.warning(
        "Sentio room %s setpoint %.2f: mode %s->%s, override %s->%s, register 119=%s, effective(ir101)=%s, standby=%s, vacation=%s",
        index, temperature, mode, mode_after, override, override_after,
        None if written is None else written / 100, effective, standby, vacation,
    )
    if effective is not None and abs(effective - temperature) > 0.05:
        reason = []
        if standby:
            reason.append("the whole system is in Standby")
        if vacation:
            reason.append("Vacation mode is on")
        if mode_after == 0:
            reason.append("the room is in schedule mode")
        if override_after not in (None, 0):
            reason.append("the room has a temporary override")
        raise HomeAssistantError(
            f"The Wavin Sentio accepted the write but is using {effective} instead of {temperature}"
            + (f" ({', '.join(reason)})" if reason else "")
        )
    return effective
