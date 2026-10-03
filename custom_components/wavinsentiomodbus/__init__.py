import functools
import inspect
import logging

from homeassistant import config_entries, core

from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, Unauthorized

from .const import DOMAIN
from .extras import MODBUS_LOCK, SentioExtras, register_devices

from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TYPE, CONF_SLAVE, Platform
from homeassistant.core import HomeAssistant

from WavinSentioModbus.SentioApi import SentioModbus, NoConnectionPossible, ModbusType

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.CLIMATE,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SELECT,
]


def _slave_compat(orig, map_slave):
    """Wrap a pymodbus client method: serialise access and map legacy ``slave=`` to ``device_id=``."""

    @functools.wraps(orig)
    def wrapper(self, *args, slave=None, **kwargs):
        if slave is not None:
            kwargs.setdefault("device_id" if map_slave else "slave", slave)
        with MODBUS_LOCK:
            return orig(self, *args, **kwargs)

    wrapper._wavin_slave_compat = True
    return wrapper


def _install_pymodbus_slave_compat() -> None:
    """Make the synchronous pymodbus clients safe for WavinSentioModbus 0.9.0.

    * pymodbus >= 3.11 (shipped with current Home Assistant) only accepts ``device_id=``, the library
      still calls with ``slave=``: map it.
    * The sync client is not thread-safe and several coordinators/entities use it from executor threads:
      serialise every request with one process-wide lock.
    """
    try:
        from pymodbus.client import ModbusSerialClient, ModbusTcpClient
    except ImportError:  # pragma: no cover
        return
    for cls in (ModbusTcpClient, ModbusSerialClient):
        for name in (
            "read_holding_registers",
            "read_input_registers",
            "read_discrete_inputs",
            "write_register",
            "write_registers",
        ):
            orig = getattr(cls, name, None)
            if orig is None or getattr(orig, "_wavin_slave_compat", False):
                continue
            params = inspect.signature(orig).parameters
            map_slave = "slave" not in params and "device_id" in params
            setattr(cls, name, _slave_compat(orig, map_slave))


_install_pymodbus_slave_compat()


async def async_setup_entry(
    hass: HomeAssistant, entry: config_entries.ConfigEntry
) -> bool:
    """Set up platform from a ConfigEntry."""
    hass.data.setdefault(DOMAIN, {})
    #hass_data = dict(entry.data)
    _LOGGER.debug("__INIT__ Setting up with data --> {0}".format(entry.data))
    
    hass.data[DOMAIN] = SentioApiHandler(entry.data[CONF_TYPE], entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data[CONF_SLAVE], logging.DEBUG, hass)
    api = hass.data[DOMAIN]

    # Connect + initialize once, here, BEFORE the platforms start. The climate and sensor
    # platforms are set up concurrently and would otherwise race on the same Modbus client.
    try:
        if not await api.connect():
            raise ConfigEntryNotReady(
                "Cannot connect to Wavin Sentio at {0}:{1}".format(entry.data[CONF_HOST], entry.data[CONF_PORT])
            )
        if not await api.initialize():
            raise ConfigEntryNotReady("Wavin Sentio connected but initialization failed")
    except NoConnectionPossible as err:
        raise ConfigEntryNotReady(err) from err

    extras = SentioExtras(hass, api, entry)
    api.extras = extras
    try:
        await extras.async_setup()
    except Exception as err:  # noqa: BLE001  extras are optional: never break the core integration
        _LOGGER.warning("Wavin Sentio extra registers unavailable: %s", err)
        extras.coordinator = None

    # The CCU is the parent device of every room / circuit device: register them all up-front.
    rooms = {r.index: r.name for r in (api.getAvailableRooms() or [])}
    register_devices(
        hass, entry, str(api.sentioData.serial_number), getattr(extras, "firmware", None), rooms, extras.objects
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS)
    if unload_ok:
        api = hass.data.pop(DOMAIN, None)
        if api is not None:
            await api.disconnect()
    return unload_ok


class SentioApiHandler:

    def __init__(self, type, host, port, slave, loglevel, hass: HomeAssistant):
        self._data = {}
        self._connected = False
        self._initialized = False
        self._value = 0
        self._hass = hass
        self.extras = None
        self._api = SentioModbus(type, host, port, slave, port, loglevel)
        _LOGGER.debug("Sentio API class {0}".format(self._value))

    async def connect(self):
        if self._connected:
            _LOGGER.info("Sentio connection already established")
            return self._connected
        else:
            status = await self._hass.async_add_executor_job(self._api.connect)
            if status == 0:
                self._connected = True
            else:
                _LOGGER.debug("Sentio connection failed")
        return self._connected

    async def disconnect(self):
        if self._connected:
            try:
                await self._hass.async_add_executor_job(self._api.disconnect)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Sentio disconnect failed: {0}".format(err))
            self._connected = False
            self._initialized = False

    async def initialize(self):
        if self._initialized:
            _LOGGER.info("Sentio data already initialized")
            return self._initialized
        else:
            status = await self._hass.async_add_executor_job(self._api.initialize)
            if status == 0:
                self._initialized = True
        return self._initialized

    async def update(self):
        _LOGGER.debug("Calling Update")
        if self._connected == False or self._initialized == False:
            _LOGGER.debug("Connect and initialize first!")
        else:
            await self._hass.async_add_executor_job(self._api.updateData)
    
    async def setRoomTemperature(self, roomIndex, temperature):
        room = self.getRoom(roomIndex)
        await self._hass.async_add_executor_job(room.setRoomSetpoint, temperature)

    @property
    def sentioData(self):
        return self._api.sentioData

    def getAvailableRooms(self):
        return self._api.availableRooms
    
    def getItcData(self):
        return self._api.availableItcs
    
    @property
    def outdoorTemperature(self):
        return self._api.sentioData.outdoor_temperature

    @property 
    def hcSourceState(self):
        return self._api.sentioData.hc_source_state


    def getRoom(self, index):
        for room in self._api.availableRooms:
            if room.index == index:
                return room
        return None
    
    def getItcCircuit(self, index):
        for itc in self._api.availableItcs:
            if itc.index == index:
                return itc
        return None


async def async_setup(hass: core.HomeAssistant, config: dict) -> bool:
    """Set up the Wavin Sentio component."""
    # @TODO: Add setup code.
    _LOGGER.debug("__INIT__ : Calling async setup for INIT file ")
    return True
