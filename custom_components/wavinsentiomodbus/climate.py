from datetime import timedelta
import logging
from typing import Any, Final
from enum import Enum

from homeassistant.core import callback

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)

from homeassistant.components.sensor import (
    SensorEntity, 
    SensorDeviceClass,
    SensorStateClass
)

from homeassistant.const import (
    CONF_HOST, 
    CONF_PORT, 
    CONF_TYPE, 
    CONF_SLAVE, 
    PERCENTAGE, 
    UnitOfTemperature
)

from homeassistant.const import (
    ATTR_TEMPERATURE,
    UnitOfTemperature,
)

from homeassistant.exceptions import ConfigEntryAuthFailed

import homeassistant.helpers.config_validation as cv

from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import(
    DOMAIN as SENTIO_CLIMATE_DOMAIN,
    _LOGGER, 
    DEFAULT_MAX_TEMPERATURE,
    DEFAULT_MIN_TEMPERATURE,
)


#from WavinSentioInterface.SentioApi import SentioApi, NoConnectionPossible
from WavinSentioModbus.SentioApi import SentioModbus, NoConnectionPossible, ModbusType 
from WavinSentioModbus.SentioTypes import SentioHeatingStates, SentioRoomMode, SentioRoomPreset
from WavinSentioModbus.SentioApi import SentioRoom

from . import SentioApiHandler
from .extras import BLOCKING_SOURCES, MODE_OVERRIDE, room_device_info

HVAC_MODE_HASS_TO_SENTIO: Final[dict[HVACMode, SentioHeatingStates]] = {
    #HVACMode.AUTO: SentioHeatingStates.AUTO,
    HVACMode.COOL: SentioHeatingStates.COOLING,
    HVACMode.HEAT: SentioHeatingStates.HEATING,
    HVACMode.OFF: SentioHeatingStates.IDLE,
}

HVAC_MODE_SENTIO_TO_HASS: Final[dict[SentioHeatingStates, HVACMode]] = {
    #SentioHeatingStates.HEATING: HVACMode.AUTO,
    SentioHeatingStates.COOLING: HVACMode.COOL,
    SentioHeatingStates.HEATING: HVACMode.HEAT,
    SentioHeatingStates.IDLE: HVACMode.OFF,
}


PRESET_MODES = {
    "Eco": {"profile": SentioRoomPreset.RP_ECO},
    "Comfort": {"profile": SentioRoomPreset.RP_COMFORT},
    "Extracomfort": {"profile": SentioRoomPreset.RP_EXTRA_COMFORT},
}

UPDATE_DELAY = timedelta(seconds=30)

async def async_setup_entry(hass, entry, async_add_entities):
    rooms=None

    sentioApi = hass.data[SENTIO_CLIMATE_DOMAIN]
    try:      
        status = await sentioApi.connect()
        if status != True: 
            raise ConfigEntryAuthFailed("Failed to connect")
        status = await sentioApi.initialize()
        if status != True:
            raise ConfigEntryAuthFailed("Failed to initialize")
        await sentioApi.update()

    except NoConnectionPossible as err:
        raise ConfigEntryAuthFailed(err) from err

    rooms = sentioApi.getAvailableRooms()
    #_LOGGER.debug("Found rooms: {0}".format(rooms))
    dataservice = WavinSentioClimateDataService(
        hass, sentioApi, rooms
    )

    dataservice.async_setup()
    await dataservice.coordinator.async_refresh()

    entities = []
    for room in rooms:
        ws = WavinSentioEntity(hass, room, dataservice)
        entities.append(ws)
        
    async_add_entities(entities)



class WavinSentioClimateDataService:
    """Get and update the latest data."""

    def __init__(self, hass, api:SentioApiHandler, roomdata):
        """Initialize the data object."""
        self._api = api

        self.roomdata = roomdata

        self.hass = hass
        self.coordinator = None
        self.extras = getattr(api, "extras", None)

    @callback
    def async_setup(self):
        """Coordinator creation."""
        self.coordinator = DataUpdateCoordinator(
            self.hass,
            _LOGGER,
            name="WavinSentioDataService",
            update_method=self.async_update_data,
            update_interval=self.update_interval,
        )
       

    @property
    def update_interval(self):
        return UPDATE_DELAY

    async def async_update_data(self):
        _LOGGER.debug("Auto update self called")
        try:
            await self._api.update()
        except KeyError as ex:
            raise UpdateFailed("Missing overview data, skipping update") from ex

    def get_room(self, roomIndex):
        return self._api.getRoom(roomIndex)

    def get_firmwareRevision(self):
        return "FW {0}.{1}".format(self._api.sentioData.firmware_version_major, self._api.sentioData.firmware_version_minor)
    
    def get_serialNumber(self):
        return self._api.sentioData.serial_number
    
    async def set_new_temperature(self, roomIndex, temperature):
        _LOGGER.debug("Setting temperature: {0} -> {1}".format(roomIndex,temperature))
        room = self._api.getRoom(roomIndex)
        await self.hass.async_add_executor_job(room.setRoomSetpoint, temperature)

    async def set_new_profile(self, roomIndex, profile):
        _LOGGER.debug("Setting profile: {0} -> {1}".format(roomIndex, profile))
        await self.hass.async_add_executor_job(self._api.set_profile, roomIndex, profile)


class WavinSentioEntity(CoordinatorEntity, ClimateEntity):
    """Representation of a Wavin Sentio device."""

    def __init__(self, hass, room, dataservice):
        super().__init__(dataservice.coordinator)
        """Initialize the climate device."""
        self._name = room.name
        self._attr_name = room.name
        self._attr_unique_id = f"{room.name}_{room.index}"

        self._roomcode = room.index
        self._hass = hass
        self._dataservice = dataservice

        self._enable_turn_on_off_backwards_compatibility = False
        self._attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON 
        self._attr_hvac_modes = [HVACMode.AUTO, HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL]
        self._attr_hvac_action = None
        self._attr_hvac_mode = HVACMode.AUTO
        self._attr_min_temp = DEFAULT_MIN_TEMPERATURE
        self._attr_max_temp = DEFAULT_MAX_TEMPERATURE
        self._attr_preset_modes = ["Manual", "Auto"]
        self._attr_precision = 0.1
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS

        self._current_temperature = None
        self._current_humidity = None
        self._preset_mode = "Manual"
        self._hvac_mode = HVACMode.OFF
        self._away = False
        self._on = True
        self._current_operation_mode = SentioRoomMode.MANUAL
        
        self._operation = None
        self.updateSentioData()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update attributes when the coordinator updates."""
        self.updateSentioData()
        super()._handle_coordinator_update()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        _LOGGER.debug("--------------------> Set Temperature {0}".format(temperature))
        if self._hvac_mode == HVACMode.AUTO:
            temp_room = self._dataservice.get_room(self._roomcode)
            await self._hass.async_add_executor_job(temp_room.setRoomMode, SentioRoomMode.MANUAL)
        await self._dataservice.set_new_temperature(self._roomcode, temperature)
        self.updateSentioData()

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.AUTO)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode (AUTO = schedule, HEAT/COOL = manual, OFF = manual at minimum temperature)."""
        room = self._dataservice.get_room(self._roomcode)
        if room is None:
            _LOGGER.debug("Failed to get room with index {0}".format(self._roomcode))
            return
        if hvac_mode == HVACMode.AUTO:
            await self._hass.async_add_executor_job(room.setRoomMode, SentioRoomMode.SCHEDULE)
        else:
            await self._hass.async_add_executor_job(room.setRoomMode, SentioRoomMode.MANUAL)
            if hvac_mode == HVACMode.OFF:
                await self._dataservice.set_new_temperature(self._roomcode, self._attr_min_temp)
            elif (room.getRoomSetpoint() or 0) <= self._attr_min_temp:
                await self._dataservice.set_new_temperature(self._roomcode, 21)
        self.updateSentioData()
        self.async_write_ha_state()

    def updateSentioData(self) -> None:
        """Retrieve latest state."""
        temp_room = self._dataservice.get_room(self._roomcode)
        if temp_room is None:
            return
        if temp_room.getRoomRelativeHumidity() != None:
            self._attr_current_humidity = int(temp_room.getRoomRelativeHumidity())
        self._current_humidity = getattr(self, "_attr_current_humidity", None)

        self._attr_target_temperature = temp_room.getRoomSetpoint()
        self._attr_current_temperature = temp_room.getRoomActualTemperature()
        self._current_temperature = self._attr_current_temperature

        extras = self._dataservice.extras
        cooling_season = extras is not None and extras.get("hc_mode") == 1

        try:
            state = temp_room.getRoomHeatingState()
        except Exception:  # noqa: BLE001  unknown enum value from the unit
            state = None
        if state == SentioHeatingStates.HEATING:
            self._attr_hvac_action = HVACAction.HEATING
        elif state == SentioHeatingStates.COOLING:
            self._attr_hvac_action = HVACAction.COOLING
        elif state is None:
            self._attr_hvac_action = None
        else:
            # IDLE and BLOCKED_* : the room is on but currently not demanding anything
            self._attr_hvac_action = HVACAction.IDLE

        if temp_room.getRoomMode() == SentioRoomMode.SCHEDULE:
            self._hvac_mode = HVACMode.AUTO
        elif (self._attr_target_temperature or 0) <= self._attr_min_temp:
            self._hvac_mode = HVACMode.OFF
        else:
            self._hvac_mode = HVACMode.COOL if cooling_season else HVACMode.HEAT
        self._attr_hvac_mode = self._hvac_mode

        attrs = {}
        if extras is not None:
            blocked = extras.get("room_blocking", self._roomcode)
            if blocked is not None:
                attrs["blocked_by"] = BLOCKING_SOURCES.get(blocked, blocked)
            override = extras.get("room_mode_override", self._roomcode)
            if override is not None:
                attrs["mode_override"] = MODE_OVERRIDE.get(override, override)
        self._attr_extra_state_attributes = attrs

    @property
    def device_info(self):
        return room_device_info(str(self._dataservice.get_serialNumber()), self._roomcode, self._name)
