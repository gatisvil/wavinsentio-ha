"""Extra Sentio registers (Wavin "Sentio Modbus Manual") read/written directly with pymodbus.

The bundled WavinSentioModbus library only covers part of the register map. This module reads the
rest of it. Every register is *probed once at start-up*: an entity is only created when the unit
answers with a valid value, so a unit without cooling, DHW, ventilation, ... never gets those entities.
"""
from __future__ import annotations

import inspect
import logging
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Every Modbus transaction on the (single, synchronous) pymodbus client goes through this lock:
# the library's own coordinators, the climate writes and the extra reads all run in executor threads.
MODBUS_LOCK = threading.RLock()

EXTRAS_UPDATE_INTERVAL = timedelta(seconds=60)

HEATING_STATES = {1: "Idle", 2: "Heating", 3: "Cooling", 4: "Blocked heating", 5: "Blocked cooling"}
DHW_STATES = {1: "Idle", 2: "Heating", 3: "Bypass", 4: "Blocked heating", 5: "Blocked bypass"}
VENT_STATES = {
    0: "Stopped", 1: "Unoccupied", 2: "Economy", 3: "Comfort", 4: "Boost",
    5: "Blocked stopped", 6: "Blocked unoccupied", 7: "Blocked economy", 8: "Blocked comfort",
    9: "Blocked boost", 10: "Failure", 11: "Maintenance",
}
BLOCKING_SOURCES = {
    0: "None", 1: "Unknown", 2: "Contact", 3: "Floor temperature", 4: "Low energy", 5: "Air temperature",
    6: "Dew point", 7: "Outdoor temperature", 8: "Fault", 9: "Fault (HTCO)", 10: "Periodic activation",
    11: "BMS", 12: "Deadband", 13: "Drying", 14: "Heating/cooling mode", 15: "Insufficient demand",
    16: "Cooldown period", 17: "H/C source not released", 18: "Room mode", 19: "System initializing",
    20: "System shutting down", 21: "No output", 22: "First open activation", 23: "Room without temperature",
}
HC_MODE = {0: "Heating", 1: "Cooling"}
MODBUS_MODE = {0: "Disabled", 1: "Read only", 2: "Read/write", 3: "Write with password"}
BMS_OVERRIDE = {0: "Disabled", 1: "Heating", 2: "Cooling", 3: "External switch"}
LOAD_STATES = {0: "Not used", **HEATING_STATES}
TANK_STATES = {0: "Off", 1: "Idle", 2: "Heating", 3: "Cleaning", 4: "Blocked heating", 5: "Blocked cleaning", 6: "Failure"}
SOURCE_STATES = {1: "Idle", 2: "Heating prepare", 3: "Heating active", 4: "Heating terminate", 5: "Cooling prepare",
                 6: "Cooling active", 7: "Cooling terminate", 8: "Blocked heating", 9: "Blocked cooling", 10: "Failure"}
DHW_MODES = {0: "Schedule", 1: "Schedule (adaptive)", 2: "Eco", 3: "Comfort"}
ACCESS_LEVELS = {8: "Locked (read only)", 16: "Hotel", 32: "Unlocked"}
DEHUM_DRYING = {0: "Not available", 1: "Idle", 2: "Drying", 3: "Blocked drying"}
PUMP = {1: "Idle", 2: "On"}
MODE_OVERRIDE = {0: "None", 1: "Temporary", 2: "Vacation/away", 3: "Adjust"}

MODBUS_MODE_RW = 2


@dataclass(frozen=True)
class Reg:
    """Description of one register of the Sentio Modbus map (address of object index 0)."""

    key: str
    scope: str  # location | room | outdoor | dhw | tank | hcc | vent | dehum
    kind: str  # ir (input register) | hr (holding register) | di (discrete input)
    addr: int
    dtype: str  # u1 | u2 | fp100 | fp10 | d2
    platform: str  # sensor | binary_sensor | switch | number | select
    name: str
    device_class: str | None = None
    unit: str | None = None
    states: dict[int, str] | None = None
    category: str | None = None  # "diagnostic" | "config"
    enabled: bool = True
    normal_only: bool = False  # room registers that do not exist for DUMMY rooms
    vmin: float = 0
    vmax: float = 100
    step: float = 0.5
    icon: str | None = None
    measurement: bool = False


C = "\u00b0C"
T = dict(device_class="temperature", unit=C, measurement=True)


def _temps(scope: str, base: int, prefix: str, items: list[tuple[str, int, str]], **kw) -> list[Reg]:
    return [Reg(f"{scope}_{k}", scope, "ir", base + off, "fp100", "sensor", f"{prefix}{n}", **T, **kw) for k, off, n in items]


REGS: list[Reg] = [
    # ---------------------------------------------------------------- location (CCU)
    Reg("hc_mode", "location", "ir", 20, "u1", "sensor", "Heating/cooling mode", states=HC_MODE, icon="mdi:sun-snowflake-variant"),
    Reg("hc_source_state", "location", "ir", 8101, "u2", "sensor", "Heat/cool source state", states=HEATING_STATES, icon="mdi:radiator"),
    Reg("modbus_mode", "location", "hr", 5, "u1", "sensor", "Modbus access mode", states=MODBUS_MODE, category="diagnostic", icon="mdi:lock-check"),
    Reg("standby", "location", "hr", 26, "u1", "switch", "Standby", icon="mdi:power-standby"),
    Reg("vacation", "location", "hr", 27, "u1", "switch", "Vacation", icon="mdi:beach"),
    Reg("bms_override", "location", "hr", 34, "u1", "select", "Heating/cooling mode override", states=BMS_OVERRIDE, category="config", enabled=False, icon="mdi:sun-snowflake-variant"),
    Reg("heat_max_outdoor", "location", "hr", 32, "fp100", "number", "Heating max outdoor temperature", unit=C, category="config", enabled=False, vmin=-40, vmax=50),
    Reg("cool_min_outdoor", "location", "hr", 31, "fp100", "number", "Cooling min outdoor temperature", unit=C, category="config", enabled=False, vmin=-40, vmax=50),
    Reg("cool_to_heat_outdoor", "location", "hr", 36, "fp100", "number", "Cooling to heating changeover temperature", unit=C, category="config", enabled=False, vmin=-40, vmax=50),
    Reg("heat_to_cool_outdoor", "location", "hr", 37, "fp100", "number", "Heating to cooling changeover temperature", unit=C, category="config", enabled=False, vmin=-40, vmax=50),
    Reg("warning", "location", "di", 1, "u1", "binary_sensor", "Warning", device_class="problem", category="diagnostic"),
    Reg("error", "location", "di", 2, "u1", "binary_sensor", "Error", device_class="problem", category="diagnostic"),
    *[Reg(f"thermistor_t{i}", "location", "ir", 12800 + i, "fp100", "sensor", f"Thermistor input T{i}", **T) for i in range(1, 6)],
    # ---------------------------------------------------------------- outdoor zone
    Reg("outdoor_filtered", "outdoor", "ir", 3302, "fp100", "sensor", "Outdoor temperature (filtered)", **T),
    Reg("outdoor_geometric", "outdoor", "ir", 3303, "fp100", "sensor", "Outdoor temperature (geometric)", enabled=False, **T),
    Reg("outdoor_warning", "outdoor", "di", 3301, "u1", "binary_sensor", "Outdoor sensor warning", device_class="problem", category="diagnostic"),
    Reg("outdoor_error", "outdoor", "di", 3302, "u1", "binary_sensor", "Outdoor sensor error", device_class="problem", category="diagnostic"),
    Reg("outdoor_battery", "outdoor", "di", 3303, "u1", "binary_sensor", "Outdoor sensor low battery", device_class="battery", category="diagnostic"),
    Reg("outdoor_lost", "outdoor", "di", 3304, "u1", "binary_sensor", "Outdoor sensor lost", device_class="problem", category="diagnostic"),
    # ---------------------------------------------------------------- rooms (address = index * 100 + base)
    Reg("room_state", "room", "ir", 102, "u1", "sensor", "Heating state", states=HEATING_STATES, icon="mdi:radiator"),
    Reg("room_blocking", "room", "ir", 103, "u1", "sensor", "Blocked by", states=BLOCKING_SOURCES, icon="mdi:cancel"),
    Reg("room_mode_override", "room", "hr", 118, "u1", "sensor", "Mode override", states=MODE_OVERRIDE, category="diagnostic", icon="mdi:timer-cog-outline"),
    Reg("room_warning", "room", "di", 101, "u1", "binary_sensor", "Warning", device_class="problem", category="diagnostic"),
    Reg("room_error", "room", "di", 102, "u1", "binary_sensor", "Error", device_class="problem", category="diagnostic"),
    Reg("room_battery", "room", "di", 103, "u1", "binary_sensor", "Low battery", device_class="battery", category="diagnostic", normal_only=True),
    Reg("room_lost", "room", "di", 104, "u1", "binary_sensor", "Thermostat lost", device_class="problem", category="diagnostic", normal_only=True),
    Reg("room_standby_temp", "room", "hr", 121, "fp100", "number", "Standby temperature", unit=C, category="config", vmin=4, vmax=40),
    Reg("room_vacation_temp", "room", "hr", 122, "fp100", "number", "Vacation temperature", unit=C, category="config", vmin=4, vmax=40),
    Reg("room_excl_vacation", "room", "hr", 123, "u1", "switch", "Exclude from vacation", category="config", icon="mdi:beach"),
    Reg("room_adaptive", "room", "hr", 124, "u1", "switch", "Adaptive mode", category="config", normal_only=True, icon="mdi:brain"),
    # ---------------------------------------------------------------- optional objects (only if present)
    Reg("room_radiator_state", "room", "ir", 117, "u1", "sensor", "Radiator state", states=LOAD_STATES, category="diagnostic", icon="mdi:radiator"),
    Reg("room_floor_state", "room", "ir", 118, "u1", "sensor", "Underfloor heating state", states=LOAD_STATES, category="diagnostic", icon="mdi:heating-coil"),
    Reg("room_access", "room", "hr", 120, "u1", "select", "User interface access level", states=ACCESS_LEVELS, category="config", enabled=False, normal_only=True, icon="mdi:lock"),
    Reg("dhw_mode", "dhw", "hr", 6517, "u1", "select", "Mode", states=DHW_MODES, category="config", icon="mdi:water-boiler"),
    Reg("dhw_circ", "dhw", "ir", 6504, "u1", "sensor", "Circulation state", states={0: "Disabled", 1: "Idle", 2: "On"}, icon="mdi:pump"),
    Reg("itc_pump_demand", "itc", "ir", 7303, "u1", "sensor", "Pump demand", states=PUMP, icon="mdi:pump"),
    Reg("itc_servo", "itc", "ir", 7309, "fp100", "sensor", "Servo position", unit="%", measurement=True, icon="mdi:valve"),
    Reg("itc_warning", "itc", "di", 7301, "u1", "binary_sensor", "Warning", device_class="problem", category="diagnostic"),
    Reg("itc_error", "itc", "di", 7302, "u1", "binary_sensor", "Error", device_class="problem", category="diagnostic"),
    Reg("hcc_pump_demand", "hcc", "ir", 7703, "u1", "sensor", "Pump demand", states=PUMP, icon="mdi:pump"),
    Reg("hcc_pump_state", "hcc", "ir", 7704, "u1", "sensor", "Pump state", states=PUMP, icon="mdi:pump"),
    Reg("hcc_warning", "hcc", "di", 7701, "u1", "binary_sensor", "Warning", device_class="problem", category="diagnostic"),
    Reg("hcc_error", "hcc", "di", 7702, "u1", "binary_sensor", "Error", device_class="problem", category="diagnostic"),
    Reg("vent_intake", "vent", "ir", 61031, "fp100", "sensor", "Intake air temperature", **T),
    Reg("vent_supply", "vent", "ir", 61032, "fp100", "sensor", "Supply air temperature", **T),
    Reg("vent_extract", "vent", "ir", 61033, "fp100", "sensor", "Extract air temperature", **T),
    Reg("vent_exhaust", "vent", "ir", 61034, "fp100", "sensor", "Exhaust air temperature", **T),
    Reg("vent_filter", "vent", "di", 61003, "u1", "binary_sensor", "Air filter lifetime expired", device_class="problem", category="diagnostic"),
    Reg("vent_warning", "vent", "di", 61001, "u1", "binary_sensor", "Warning", device_class="problem", category="diagnostic"),
    Reg("vent_error", "vent", "di", 61002, "u1", "binary_sensor", "Error", device_class="problem", category="diagnostic"),
    Reg("source_warning", "location", "di", 8101, "u1", "binary_sensor", "H/C source warning", device_class="problem", category="diagnostic"),
    Reg("source_error", "location", "di", 8102, "u1", "binary_sensor", "H/C source error", device_class="problem", category="diagnostic"),
    Reg("boiler_state", "location", "ir", 8201, "u1", "sensor", "Boiler/heat pump state", states=SOURCE_STATES, icon="mdi:heat-pump"),
    Reg("boiler_inlet", "location", "ir", 8203, "fp100", "sensor", "Boiler/heat pump inlet temperature", **T),
    Reg("boiler_requested", "location", "ir", 8204, "fp100", "sensor", "Boiler/heat pump requested temperature", **T),
    Reg("buffer_state", "location", "ir", 8301, "u1", "sensor", "Buffer tank state", states=SOURCE_STATES, icon="mdi:storage-tank"),
    Reg("buffer_inlet", "location", "ir", 8303, "fp100", "sensor", "Buffer tank inlet temperature", **T),
    Reg("buffer_upper", "location", "ir", 8304, "fp100", "sensor", "Buffer tank upper temperature", **T),
    Reg("buffer_lower", "location", "ir", 8305, "fp100", "sensor", "Buffer tank lower temperature", **T),
    Reg("dhw_state", "dhw", "ir", 6502, "u1", "sensor", "State", states=DHW_STATES, icon="mdi:water-boiler"),
    Reg("dhw_desired", "dhw", "ir", 6501, "fp100", "sensor", "Desired temperature", **T),
    Reg("dhw_measured", "dhw", "ir", 6505, "fp100", "sensor", "Temperature", **T),
    Reg("dhw_inlet", "dhw", "ir", 6506, "fp100", "sensor", "Source inlet temperature", **T),
    Reg("dhw_return", "dhw", "ir", 6507, "fp100", "sensor", "Source return temperature", **T),
    Reg("dhw_cold", "dhw", "ir", 6509, "fp100", "sensor", "Cold water temperature", **T),
    Reg("dhw_valve", "dhw", "ir", 6511, "fp100", "sensor", "Valve position", unit="%", measurement=True),
    Reg("dhw_setpoint", "dhw", "hr", 6521, "fp100", "number", "Temperature setpoint", unit=C, category="config", vmin=10, vmax=80),
    Reg("dhw_warning", "dhw", "di", 6501, "u1", "binary_sensor", "Warning", device_class="problem", category="diagnostic"),
    Reg("dhw_error", "dhw", "di", 6502, "u1", "binary_sensor", "Error", device_class="problem", category="diagnostic"),
    Reg("tank_measured", "tank", "ir", 6601, "fp100", "sensor", "Temperature", **T),
    Reg("tank_desired", "tank", "ir", 6602, "fp100", "sensor", "Desired temperature", **T),
    Reg("tank_state", "tank", "ir", 6603, "u1", "sensor", "State", states=TANK_STATES, icon="mdi:water-boiler"),
    Reg("tank_circ_return", "tank", "ir", 6606, "fp100", "sensor", "Circulation return temperature", **T),
    Reg("tank_inlet", "tank", "ir", 6607, "fp100", "sensor", "Source inlet temperature", **T),
    Reg("tank_return", "tank", "ir", 6608, "fp100", "sensor", "Source return temperature", **T),
    Reg("tank_setpoint", "tank", "hr", 6621, "fp100", "number", "Temperature setpoint", unit=C, category="config", vmin=10, vmax=90),
    Reg("hcc_state", "hcc", "ir", 7701, "u1", "sensor", "State", states=HEATING_STATES, icon="mdi:radiator"),
    Reg("hcc_measured", "hcc", "ir", 7705, "fp100", "sensor", "Measured temperature", **T),
    Reg("hcc_desired", "hcc", "ir", 7706, "fp100", "sensor", "Desired inlet temperature", **T),
    Reg("vent_state", "vent", "ir", 61023, "u1", "sensor", "State", states=VENT_STATES, icon="mdi:fan"),
    Reg("vent_rpm", "vent", "ir", 61025, "u2", "sensor", "Supply fan speed", unit="rpm", measurement=True, icon="mdi:fan"),
    Reg("vent_pct", "vent", "ir", 61027, "fp100", "sensor", "Supply fan setpoint", unit="%", measurement=True, icon="mdi:fan"),
    Reg("dehum_drying", "dehum", "ir", 65003, "u1", "sensor", "Drying status", states=DEHUM_DRYING, icon="mdi:air-humidifier-off"),
    Reg("dehum_thermal", "dehum", "ir", 65005, "u1", "sensor", "Thermal integration status", states=LOAD_STATES, icon="mdi:thermometer-lines"),
]

# scope -> (name register [holding, string], number of registers, max objects, address stride)
OBJECT_SCOPES: dict[str, tuple[int, int, int, str]] = {
    "dhw": (6501, 16, 4, "DHW controller"),
    "tank": (6601, 16, 4, "DHW tank"),
    "hcc": (7701, 16, 4, "Heating/cooling circuit"),
    "itc": (7301, 16, 2, "Sentio ITC"),
    "vent": (61001, 16, 2, "Ventilation"),
    "dehum": (65001, 16, 4, "Dehumidifier"),
}


@dataclass
class Active:
    """A register that answered during the probe, for one object index."""

    reg: Reg
    index: int
    address: int
    obj_name: str

    @property
    def uid(self) -> str:
        return f"{self.reg.key}:{self.index}"


def _decode(dtype: str, regs: list[int]) -> float | int | None:
    v = regs[0]
    if dtype in ("fp100", "fp10", "d2"):
        if v in (0x7FFF, 0xFFFF):
            return None
        if v >= 0x8000:
            v -= 0x10000
        return round(v / {"fp100": 100, "fp10": 10, "d2": 1}[dtype], 2)
    if dtype == "u1":
        return None if v in (0xFFFF,) or (v & 0xFF) == 0xFF and v < 0x100 else v
    if dtype == "u2":
        return None if v == 0xFFFF else v
    return None


def _string(regs: list[int]) -> str:
    raw = b"".join(r.to_bytes(2, "big") for r in regs).split(b"\x00")[0]
    return raw.decode("utf-8", "ignore").strip()


class ModbusIO:
    """Thin synchronous helper around the library's pymodbus client."""

    def __init__(self, client: Any, device_id: int) -> None:
        self.client = client
        self.device_id = device_id

    def _kw(self, fn: Any) -> dict[str, int]:
        params = inspect.signature(fn).parameters
        return {"device_id": self.device_id} if "device_id" in params or "slave" not in params else {"slave": self.device_id}

    def read(self, kind: str, addr: int, count: int = 1) -> list[int] | bool | None:
        fn = {"ir": self.client.read_input_registers, "hr": self.client.read_holding_registers, "di": self.client.read_discrete_inputs}[kind]
        for attempt in range(2):
            with MODBUS_LOCK:
                try:
                    rsp = fn(addr, count=count, **self._kw(fn))
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Modbus read %s %s failed: %s", kind, addr, err)
                    return None
            if rsp is None:
                return None
            if rsp.isError():
                if getattr(rsp, "exception_code", None) == 6 and attempt == 0:  # device busy: repeat the request
                    time.sleep(0.3)
                    continue
                return None
            return bool(rsp.bits[0]) if kind == "di" else list(rsp.registers)
        return None

    def write(self, addr: int, value: int) -> tuple[bool, int | None]:
        fn = self.client.write_register
        code = None
        for attempt in range(3):
            with MODBUS_LOCK:
                try:
                    rsp = fn(addr, value, **self._kw(fn))
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Modbus write %s failed: %s", addr, err)
                    return False, None
            if rsp is not None and not rsp.isError():
                return True, None
            code = getattr(rsp, "exception_code", None)
            if code != 6:
                break
            time.sleep(0.3)
        return False, code


def room_device_info(serial: str, index: int, name: str) -> dict[str, Any]:
    # Only the identifier: the device itself (name, parent, ...) is registered once in register_devices().
    return {"identifiers": {(DOMAIN, f"{serial}_room_{index}")}}


def object_device_info(serial: str, scope: str, index: int) -> dict[str, Any]:
    return {"identifiers": {(DOMAIN, f"{serial}_{scope}_{index}")}}


def register_devices(hass: HomeAssistant, entry: Any, serial: str, firmware: str | None, rooms: dict[int, str], objects: dict[str, str]) -> None:
    """Create the CCU device and its room / circuit child devices (children point at the CCU)."""
    from homeassistant.helpers import device_registry as dr

    reg = dr.async_get(hass)
    ccu = reg.async_get_or_create(config_entry_id=entry.entry_id, **ccu_device_info(serial, firmware))

    def child(identifier: str, name: str, model: str) -> None:
        kwargs = dict(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, identifier)},
            name=name,
            manufacturer="Wavin",
            model=model,
        )
        try:
            reg.async_get_or_create(via_device_id=ccu.id, **kwargs)
        except TypeError:  # older Home Assistant: only the identifier form exists
            reg.async_get_or_create(via_device=(DOMAIN, serial), **kwargs)

    for idx, name in rooms.items():
        child(f"{serial}_room_{idx}", name, "Sentio room")
    for key, name in objects.items():
        scope, idx = key.split(":")
        child(f"{serial}_{scope}_{idx}", name, scope.upper())


def ccu_device_info(serial: str, fw: str | None = None) -> dict[str, Any]:
    info: dict[str, Any] = {
        "identifiers": {(DOMAIN, serial)},
        "name": "Wavin Sentio",
        "manufacturer": "Wavin",
        "model": "Sentio CCU",
    }
    if fw:
        info["sw_version"] = fw
    return info


class SentioExtras:
    """Probes and polls the extra registers; owns the coordinator the extra entities use."""

    def __init__(self, hass: HomeAssistant, handler: Any, entry: Any = None) -> None:
        self.hass = hass
        self.handler = handler
        self.entry = entry
        self.io: ModbusIO | None = None
        self.active: dict[str, Active] = {}
        self.values: dict[str, Any] = {}
        self.objects: dict[str, str] = {}  # "scope:index" -> name
        self.coordinator: DataUpdateCoordinator | None = None
        self.serial = "sentio"
        self.firmware: str | None = None

    # ------------------------------------------------------------------ helpers
    def get(self, key: str, index: int = 0) -> Any:
        return self.values.get(f"{key}:{index}")

    @property
    def modbus_writable(self) -> bool:
        mode = self.get("modbus_mode")
        return mode is None or mode == MODBUS_MODE_RW

    def object_name(self, scope: str, index: int) -> str:
        return self.objects.get(f"{scope}:{index}", f"{scope} {index + 1}")

    def active_for(self, platform: str) -> list[Active]:
        return [a for a in self.active.values() if a.reg.platform == platform]

    # ------------------------------------------------------------------ probe / refresh (blocking)
    def _probe(self) -> None:
        api = self.handler._api  # noqa: SLF001  (SentioModbus instance of the library)
        self.io = ModbusIO(api.client, api.slaveId)
        data = self.handler.sentioData
        self.serial = str(data.serial_number or "sentio")
        self.firmware = f"FW {data.firmware_version_major}.{data.firmware_version_minor}"
        rooms = {r.index: r.name for r in (self.handler.getAvailableRooms() or [])}
        started = time.monotonic()

        targets: dict[str, dict[int, str]] = {"location": {0: "Wavin Sentio"}, "outdoor": {0: "Outdoor"}, "room": rooms}
        for scope, (name_addr, count, maxn, label) in OBJECT_SCOPES.items():
            for idx in range(maxn):
                regs = self.io.read("hr", name_addr + idx * 100, count)
                name = _string(regs) if regs else ""
                if name:
                    targets.setdefault(scope, {})[idx] = name
                    self.objects[f"{scope}:{idx}"] = name

        dummy_rooms = set()
        for idx in rooms:
            rt = self.io.read("ir", idx * 100 + 127, 1)
            if rt and rt[0] == 1:
                dummy_rooms.add(idx)

        for reg in REGS:
            for idx, name in targets.get(reg.scope, {}).items():
                if reg.scope == "room" and reg.normal_only and idx in dummy_rooms:
                    continue
                address = reg.addr + (idx * 100 if reg.scope not in ("location",) else 0)
                raw = self.io.read(reg.kind, address, 1)
                if raw is None:
                    continue
                value = int(raw) if reg.kind == "di" else _decode(reg.dtype, raw)  # type: ignore[arg-type]
                if value is None:
                    continue
                act = Active(reg, idx, address, name)
                self.active[act.uid] = act
                self.values[act.uid] = value
        _LOGGER.info(
            "Sentio extras: %d registers available (%d rooms, objects: %s) in %.1fs",
            len(self.active), len(rooms), ", ".join(sorted(self.objects.values())) or "none", time.monotonic() - started,
        )

    def _refresh(self) -> dict[str, Any]:
        assert self.io is not None
        values = dict(self.values)
        for act in self.active.values():
            raw = self.io.read(act.reg.kind, act.address, 1)
            if raw is None:
                continue  # keep the previous value on a single failed read
            value = int(raw) if act.reg.kind == "di" else _decode(act.reg.dtype, raw)  # type: ignore[arg-type]
            values[act.uid] = value
        return values

    def _write(self, act: Active, raw: int) -> None:
        assert self.io is not None
        ok, code = self.io.write(act.address, raw & 0xFFFF)
        if not ok:
            hint = ""
            if code in (1, 4) or not self.modbus_writable:
                hint = " The Sentio's Modbus access mode is probably 'Read only'; set it to 'Read/write' on the Sentio."
            elif code == 6:
                hint = " The Sentio reported it is busy; try again."
            raise HomeAssistantError(f"Writing '{act.reg.name}' to the Wavin Sentio failed (Modbus exception {code}).{hint}")

    # ------------------------------------------------------------------ async API
    async def async_setup(self) -> None:
        await self.hass.async_add_executor_job(self._probe)
        self.coordinator = DataUpdateCoordinator(
            self.hass,
            _LOGGER,
            config_entry=self.entry,
            name="WavinSentioExtras",
            update_method=self._async_update,
            update_interval=EXTRAS_UPDATE_INTERVAL,
        )
        self.coordinator.async_set_updated_data(dict(self.values))

    async def _async_update(self) -> dict[str, Any]:
        self.values = await self.hass.async_add_executor_job(self._refresh)
        return self.values

    async def async_write(self, act: Active, value: float | int) -> None:
        raw = round(value * {"fp100": 100, "fp10": 10, "d2": 1}.get(act.reg.dtype, 1)) if act.reg.dtype != "u1" and act.reg.dtype != "u2" else int(value)
        await self.hass.async_add_executor_job(self._write, act, int(raw))
        self.values[act.uid] = value
        if self.coordinator is not None:
            self.coordinator.async_set_updated_data(dict(self.values))
