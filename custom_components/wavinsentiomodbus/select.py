from .entity import ExtraSelect, setup_platform


async def async_setup_entry(hass, entry, async_add_entities):
    await setup_platform(hass, async_add_entities, "select", ExtraSelect)
