"""Entities for the extra Sentio registers (see extras.py)."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .extras import Active, SentioExtras, ccu_device_info, object_device_info, room_device_info

_CATEGORY = {"diagnostic": EntityCategory.DIAGNOSTIC, "config": EntityCategory.CONFIG}


def get_extras(hass) -> SentioExtras | None:
    handler = hass.data.get(DOMAIN)
    extras = getattr(handler, "extras", None)
    if extras is None or extras.coordinator is None:
        return None
    return extras


class ExtraEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, extras: SentioExtras, act: Active) -> None:
        super().__init__(extras.coordinator)
        self._extras = extras
        self._act = act
        reg = act.reg
        self._attr_unique_id = f"{extras.serial}_{reg.key}_{act.index}"
        self._attr_entity_registry_enabled_default = reg.enabled
        self._attr_entity_category = _CATEGORY.get(reg.category or "")
        if reg.icon:
            self._attr_icon = reg.icon
        scope = reg.scope
        if scope == "room":
            self._attr_name = reg.name
            self._attr_device_info = room_device_info(extras.serial, act.index, act.obj_name)
        elif scope in ("location", "outdoor"):
            self._attr_name = reg.name
            self._attr_device_info = ccu_device_info(extras.serial, extras.firmware)  # identifiers match the CCU device
        else:  # dhw / tank / hcc / itc / vent / dehum: their own device
            self._attr_name = reg.name
            self._attr_device_info = object_device_info(extras.serial, scope, act.index)

    @property
    def _value(self):
        return self._extras.values.get(self._act.uid)


class ExtraSensor(ExtraEntity, SensorEntity):
    def __init__(self, extras, act):
        super().__init__(extras, act)
        reg = act.reg
        if reg.states:
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = list(reg.states.values())
        else:
            if reg.device_class:
                self._attr_device_class = SensorDeviceClass(reg.device_class)
            self._attr_native_unit_of_measurement = reg.unit
            if reg.measurement:
                self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        v = self._value
        if self._act.reg.states:
            return self._act.reg.states.get(v) if v is not None else None
        return v


class ExtraBinarySensor(ExtraEntity, BinarySensorEntity):
    def __init__(self, extras, act):
        super().__init__(extras, act)
        if act.reg.device_class:
            self._attr_device_class = BinarySensorDeviceClass(act.reg.device_class)

    @property
    def is_on(self):
        v = self._value
        return None if v is None else bool(v)


class ExtraSwitch(ExtraEntity, SwitchEntity):
    @property
    def is_on(self):
        v = self._value
        return None if v is None else bool(v)

    async def async_turn_on(self, **kwargs):
        await self._extras.async_write(self._act, 1)

    async def async_turn_off(self, **kwargs):
        await self._extras.async_write(self._act, 0)


class ExtraNumber(ExtraEntity, NumberEntity):
    _attr_mode = NumberMode.BOX

    def __init__(self, extras, act):
        super().__init__(extras, act)
        reg = act.reg
        self._attr_native_min_value = reg.vmin
        self._attr_native_max_value = reg.vmax
        self._attr_native_step = reg.step
        self._attr_native_unit_of_measurement = reg.unit
        if reg.unit == "\u00b0C":
            self._attr_device_class = "temperature"

    @property
    def native_value(self):
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        await self._extras.async_write(self._act, value)


class ExtraSelect(ExtraEntity, SelectEntity):
    def __init__(self, extras, act):
        super().__init__(extras, act)
        self._attr_options = list(act.reg.states.values())

    @property
    def current_option(self):
        v = self._value
        return self._act.reg.states.get(v) if v is not None else None

    async def async_select_option(self, option: str) -> None:
        for raw, label in self._act.reg.states.items():
            if label == option:
                await self._extras.async_write(self._act, raw)
                return


async def setup_platform(hass, async_add_entities, platform: str, cls) -> None:
    extras = get_extras(hass)
    if extras is None:
        return
    async_add_entities([cls(extras, act) for act in extras.active_for(platform)])
