import asyncio
import logging
import colorsys
import math
import time

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .mqtt_connector import MqttConnector
from .pocos import Device, ExternalColorData, MqttControlData
from .rest_api_connector import RestApiConnector

_LOGGER = logging.getLogger(__name__)

UNSUPPORTED_LIGHT_NAME_PARTS = (
    "remote",
    "fernbedienung",
)


def _bridge_identifier(control_data: list[MqttControlData], place_id: str) -> str | None:
    """Return the Home Assistant device identifier for a bridge."""
    hardware = next((item for item in control_data if item.deviceType == "HARDWARE"), None)
    if hardware is None:
        return None
    return f"bridge_{place_id}_{hardware.macAddress or hardware.deviceName}"


def _is_supported_light_device(device: Device) -> bool:
    """Return whether a cloud device should be exposed as a light."""
    if device.wiringType == 0:
        return False
    normalized_name = device.displayName.casefold()
    return not any(part in normalized_name for part in UNSUPPORTED_LIGHT_NAME_PARTS)


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, add_entities: AddEntitiesCallback
) -> bool:
    """Set up the light platform."""

    rest_connector = RestApiConnector(
        config_entry.data["username"],
        config_entry.data["password"],
        config_entry.data["country"],
    )
    await rest_connector.connect()

    lights = []
    mqtt_connectors = []

    place_ids = rest_connector.places or [rest_connector._placeUniID]

    for place_id in place_ids:
        _LOGGER.info("Setting up Hao Deng place: %s", place_id)
        rest_connector.set_place(place_id)

        devices: list[Device] = await rest_connector.devices()
        if len(devices) == 0:
            _LOGGER.info("No Hao Deng devices found for place %s", place_id)
            continue

        controlData = await rest_connector.get_mqtt_control_data()
        if len(controlData) == 0:
            _LOGGER.warning("No MQTT control data found for Hao Deng place %s", place_id)
            continue

        bridge_id = _bridge_identifier(controlData, place_id)
        mqtt_connector = MqttConnector(controlData, config_entry.data["country"], devices)
        mqtt_connector.connect()

        while mqtt_connector.client_connected is False:
            await asyncio.sleep(0.1)

        place_lights = []
        for device in devices:
            _LOGGER.debug(
                "Discovered Hao Deng device: name=%s, mesh=%s, deviceType=%s, "
                "controlType=%s, wiringType=%s, groups=%s",
                device.displayName,
                device.meshAddress,
                device.deviceType,
                device.controlType,
                device.wiringType,
                device.groups,
            )
            if not _is_supported_light_device(device):
                _LOGGER.info(
                    "Skipping unsupported Hao Deng light candidate: %s "
                    "(mesh=%s, deviceType=%s, controlType=%s, wiringType=%s)",
                    device.displayName,
                    device.meshAddress,
                    device.deviceType,
                    device.controlType,
                    device.wiringType,
                )
                continue
            light = HaoDengLight(config_entry, device, mqtt_connector, bridge_id)
            place_lights.append(light)

        if len(place_lights) == 0:
            _LOGGER.info("No Hao Deng lights found for place %s", place_id)
            continue

        lights.extend(place_lights)
        mqtt_connectors.append(mqtt_connector)

    add_entities(lights)

    for mqtt_connector in mqtt_connectors:
        mqtt_connector.request_status()  # Get initial status of lights
        hass.async_create_task(_request_initial_status_retries(mqtt_connector))

    return True

async def _request_initial_status_retries(mqtt_connector: MqttConnector) -> None:
    """Request initial status multiple times after startup."""
    for delay in (2, 10, 30):
        await asyncio.sleep(delay)
        try:
            mqtt_connector.request_status()
        except Exception:
            _LOGGER.exception("Failed to request Hao Deng initial light status")

class HaoDengLight(LightEntity):
    """Hao Deng Light."""

    def __init__(
        self,
        config_entry: ConfigEntry,
        device: Device,
        mqtt_connector: MqttConnector,
        bridge_id: str | None,
    ) -> None:
        """Initialize the light."""
        _LOGGER.info("Initializing Light %s", device.displayName)
        self._config_entry = config_entry
        self._mqtt_connector = mqtt_connector
        self._attr_unique_id = device.uniID  # Use config entry ID for uniqueness
        self._attr_name = device.displayName
        self._mesh_id = device.meshAddress
        self._bridge_id = bridge_id
        self._attr_is_on = False
        self._attr_hs_color = (0, 0)
        self._attr_color_temp_kelvin = 5000
        self._attr_supported_color_modes = [
            ColorMode.HS,
            ColorMode.COLOR_TEMP,
        ]
        self._attr_color_mode = ColorMode.UNKNOWN
        self._attr_brightness = 255
        self._attr_should_poll = False
        self._ignore_next_update = False
        self._last_update = 0

        self._attr_max_color_temp_kelvin = 6535
        self._attr_min_color_temp_kelvin = 2500

        self._attr_available = False

        def update_light(a, d):
            if a == self._mesh_id:
                self._update_light(d)

        mqtt_connector.subscribe(update_light)

    def get_base_colors(self, rgb: tuple[int, int, int]) -> tuple[int, int, int]:
        """Get what the colors would be at brightness 100%."""
        multiplier = max(rgb) / 255
        # brightest_color_value = max(rgb[0], rgb[1], rgb[2])
        adjusted_colors = []
        for color in rgb:
            adjusted_value = min(math.ceil(color / multiplier), 255)
            adjusted_colors.append(adjusted_value)
        return adjusted_colors

    def _update_hsv_values(self, color_data: ExternalColorData):
        # _LOGGER.info(
        #     "Updating HSV of %s %s %s ",
        #     color_data.hsv[0],
        #     color_data.hsv[1],
        #     color_data.hsv[2],
        # )
        if color_data.hsv[0] == 0 and color_data.hsv[1] == 0 and color_data.hsv[2] == 0:
            self._attr_is_on = False
            return
        # _LOGGER.info("%s is on", self._attr_name)
        self._attr_is_on = True
        # _LOGGER.info("New Bright  PRE %s", color_data.hsv[2])
        self._attr_brightness = color_data.hsv[2] * 255
        self._attr_hs_color = [color_data.hsv[0], color_data.hsv[1] * 100]
        # _LOGGER.info("New Bright %s", self._attr_brightness)
        # _LOGGER.info("New Hs %s", self._attr_hs_color)
        self._attr_color_mode = ColorMode.HS

    def _update_light_color_temp(self, color_data: ExternalColorData):
        self._attr_is_on = color_data.colorTempBrightness[1] > 0
        if self._attr_is_on is False:
            return
        self._attr_color_mode = ColorMode.COLOR_TEMP
        self._attr_color_temp_kelvin = color_data.colorTempBrightness[0]
        self._attr_brightness = min(
            math.ceil(color_data.colorTempBrightness[1] * 255), 255
        )

    def _update_light(self, color_data: ExternalColorData):
        """Update light from fetched cloud data."""
        try:
            if color_data.isAvailable is False:
                _LOGGER.debug(
                    "Update timestamp for %s is 00, treating status as stale but usable",
                    self._attr_name,
                )

            if (
                time.time() - self._last_update < 5
                and self._attr_color_mode != ColorMode.UNKNOWN
            ):
                return

            if color_data.isHsv and color_data.hsv is not None:
                _LOGGER.info("Updating %s", self._attr_name)
                self._update_hsv_values(color_data)
            elif color_data.colorTempBrightness is not None:
                _LOGGER.info("Updating %s", self._attr_name)
                self._update_light_color_temp(color_data)
            else:
                _LOGGER.debug(
                    "Ignoring status update without usable color data for %s: %s",
                    self._attr_name,
                    repr(color_data.__dict__),
                )
                return

            self._attr_available = True
            self.schedule_update_ha_state()

        except Exception as e:
            _LOGGER.error(
                "Error updating light %s with data %s. Error was %s",
                self._attr_name,
                repr(color_data.__dict__),
                e,
            )

    def _hsv_to_rgb(self, hs: tuple[float, float], brightness: float):
        brightness_scale_100 = brightness / 255
        rgb_float = colorsys.hsv_to_rgb(hs[0] / 365, hs[1] / 100, brightness_scale_100)
        rgb = [rgb_float[0] * 255, rgb_float[1] * 255, rgb_float[2] * 255]
        # _LOGGER.info(
        #     "Convert HSL of %s %s %s to RGB of %s %s %s",
        #     hs[0],
        #     hs[1],
        #     brightness,
        #     rgb[0],
        #     rgb[1],
        #     rgb[2],
        # )
        return rgb

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the light on, set options."""
        self._attr_is_on = True
        if ATTR_BRIGHTNESS in kwargs:
            self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
        self._attr_brightness = self._attr_brightness or 255
        if ATTR_HS_COLOR in kwargs:
            # _LOGGER.info("Setting Color ON %s", repr(kwargs[ATTR_HS_COLOR]))
            self._attr_color_mode = ColorMode.HS
            # _LOGGER.info("HS %s", kwargs[ATTR_HS_COLOR])
            self._attr_hs_color = kwargs[ATTR_HS_COLOR]
            rgb = self._hsv_to_rgb(self._attr_hs_color, self._attr_brightness)
            self.async_write_ha_state()
            await self._mqtt_connector.set_color(self._mesh_id, rgb[0], rgb[1], rgb[2])
        elif ATTR_COLOR_TEMP_KELVIN in kwargs:
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_color_temp_kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            self.async_write_ha_state()
            await self._mqtt_connector.set_color_temp(
                self._mesh_id,
                self._attr_color_temp_kelvin,
                self._attr_brightness,
            )
        elif ATTR_BRIGHTNESS in kwargs:
            self.async_write_ha_state()
            if self._attr_color_mode == ColorMode.COLOR_TEMP:
                await self._mqtt_connector.set_color_temp(
                    self._mesh_id, self._attr_color_temp_kelvin, self._attr_brightness
                )
            else:
                rgb = self._hsv_to_rgb(self._attr_hs_color, self._attr_brightness)
                await self._mqtt_connector.set_color(
                    self._mesh_id, rgb[0], rgb[1], rgb[2]
                )
        else:
            _LOGGER.info("Just turned on %s ", self._attr_name)
            self.async_write_ha_state()
            if self._attr_color_mode == ColorMode.COLOR_TEMP:
                await self._mqtt_connector.set_color_temp(
                    self._mesh_id, self._attr_color_temp_kelvin, self._attr_brightness
                )
            else:
                self._attr_color_mode = ColorMode.HS
                rgb = self._hsv_to_rgb(self._attr_hs_color, self._attr_brightness)
                await self._mqtt_connector.set_color(
                    self._mesh_id, rgb[0], rgb[1], rgb[2]
                )
            await asyncio.sleep(0.1)
            self._mqtt_connector.request_status()
        self._last_update = time.time()

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the light off."""
        _LOGGER.info("TURN OFF ASYNC %s", self._attr_name)
        self._attr_is_on = False
        self.async_write_ha_state()
        if self._attr_color_mode == ColorMode.COLOR_TEMP:
            await self._mqtt_connector.set_color_temp(
                self._mesh_id, self._attr_color_temp_kelvin, 0
            )
        else:
            await self._mqtt_connector.set_color(self._mesh_id, 0, 0, 0)
        self._last_update = time.time()

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        device_info = DeviceInfo(
            identifiers={
                (DOMAIN, self._attr_unique_id)
            },
            name=self.name,
            manufacturer="Hao Deng",
            model="Hao Deng Light",
            sw_version="1.0.0",
        )
        if self._bridge_id is not None:
            device_info["via_device"] = (DOMAIN, self._bridge_id)
        return device_info
