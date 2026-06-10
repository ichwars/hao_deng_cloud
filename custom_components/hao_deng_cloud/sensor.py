"""Bridge sensors for Hao Deng Cloud."""

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .pocos import MqttControlData
from .rest_api_connector import RestApiConnector

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, add_entities: AddEntitiesCallback
) -> bool:
    """Set up Hao Deng bridge sensors."""
    rest_connector = RestApiConnector(
        config_entry.data["username"],
        config_entry.data["password"],
        config_entry.data["country"],
    )
    await rest_connector.connect()

    sensors: list[HaoDengBridgeSensor] = []
    place_ids = rest_connector.places or [rest_connector._placeUniID]

    for place_id in place_ids:
        rest_connector.set_place(place_id)
        control_data = await rest_connector.get_mqtt_control_data()
        hardware = next(
            (item for item in control_data if item.deviceType == "HARDWARE"),
            None,
        )
        if hardware is None:
            _LOGGER.warning("No Hao Deng bridge found for place %s", place_id)
            continue
        sensors.append(HaoDengBridgeSensor(place_id, hardware))

    add_entities(sensors)
    return True


class HaoDengBridgeSensor(SensorEntity):
    """Represent a Hao Deng bridge so Home Assistant shows the hub device."""

    _attr_should_poll = False
    _attr_native_value = "connected"

    def __init__(self, place_id: str, hardware: MqttControlData) -> None:
        """Initialize the bridge sensor."""
        bridge_key = hardware.macAddress or hardware.deviceName
        self._bridge_id = f"bridge_{place_id}_{bridge_key}"
        self._attr_unique_id = self._bridge_id
        self._attr_name = f"Hao Deng Bridge {hardware.deviceName}"
        self._attr_extra_state_attributes = {
            "place_id": place_id,
            "product_key": hardware.productKey,
            "device_name": hardware.deviceName,
            "mac_address": hardware.macAddress,
        }

    @property
    def device_info(self) -> DeviceInfo:
        """Return bridge device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._bridge_id)},
            name=self._attr_name,
            manufacturer="Hao Deng",
            model="Hao Deng Bridge",
        )
