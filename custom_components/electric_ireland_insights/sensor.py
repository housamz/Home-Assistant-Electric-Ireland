import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CURRENCY_EURO, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import DiscoveryInfoType

from .api import ElectricIrelandScraper
from .const import DOMAIN
from .sensor_base import Sensor

PLATFORM = "sensor"

LOGGER = logging.getLogger(__name__)

# How often to refresh appliance data from Electric Ireland (once per day is plenty)
APPLIANCE_REFRESH_INTERVAL = timedelta(hours=24)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_devices: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,  # noqa: ARG001
):
    username = config_entry.data["username"]
    password = config_entry.data["password"]
    account_number = config_entry.data["account_number"]

    ei_api = ElectricIrelandScraper(username, password, account_number)
    appliance_store = ApplianceUsageStore(ei_api)
    await appliance_store.async_refresh()

    # Schedule periodic refresh of appliance data centrally, so individual
    # sensors don't each trigger a full login cycle on every update.
    async def _scheduled_refresh(now=None):
        await appliance_store.async_refresh()

    async_track_time_interval(hass, _scheduled_refresh, APPLIANCE_REFRESH_INTERVAL)

    sensors = [
        HourlyConsumptionSensor(device_id=config_entry.entry_id, ei_api=ei_api),
        HourlyCostSensor(device_id=config_entry.entry_id, ei_api=ei_api),
        DailyConsumptionSensor(device_id=config_entry.entry_id, ei_api=ei_api),
        DailyCostSensor(device_id=config_entry.entry_id, ei_api=ei_api),
        ApplianceUsageSensor(device_id=config_entry.entry_id, appliance_store=appliance_store),
    ]
    sensors.extend(
        ApplianceConsumptionSensor(
            device_id=config_entry.entry_id,
            appliance_store=appliance_store,
            category=category,
        )
        for category in appliance_store.categories
    )
    async_add_devices(sensors)


class HourlyConsumptionSensor(Sensor):
    def __init__(self, device_id: str, ei_api: ElectricIrelandScraper):
        super().__init__(
            device_id=device_id,
            ei_api=ei_api,
            name="Hourly Consumption",
            entity_key="hourly_consumption",
            metric_key="consumption",
            measurement_unit=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            history_granularity="hourly",
        )


class HourlyCostSensor(Sensor):
    def __init__(self, device_id: str, ei_api: ElectricIrelandScraper):
        super().__init__(
            device_id=device_id,
            ei_api=ei_api,
            name="Hourly Cost",
            entity_key="hourly_cost",
            metric_key="cost",
            measurement_unit=CURRENCY_EURO,
            device_class=SensorDeviceClass.MONETARY,
            history_granularity="hourly",
        )


class DailyConsumptionSensor(Sensor):
    def __init__(self, device_id: str, ei_api: ElectricIrelandScraper):
        super().__init__(
            device_id=device_id,
            ei_api=ei_api,
            name="Daily Consumption",
            entity_key="daily_consumption",
            metric_key="consumption",
            measurement_unit=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            history_granularity="daily",
        )


class DailyCostSensor(Sensor):
    def __init__(self, device_id: str, ei_api: ElectricIrelandScraper):
        super().__init__(
            device_id=device_id,
            ei_api=ei_api,
            name="Daily Cost",
            entity_key="daily_cost",
            metric_key="cost",
            measurement_unit=CURRENCY_EURO,
            device_class=SensorDeviceClass.MONETARY,
            history_granularity="daily",
        )


class ApplianceUsageStore:
    def __init__(self, ei_api: ElectricIrelandScraper):
        self._api = ei_api
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = {
            "bill_period_start": None,
            "bill_period_end": None,
            "total_consumption": None,
            "total_cost": None,
            "appliances": {},
        }

    @property
    def categories(self) -> list[str]:
        return sorted(self._data["appliances"])

    def get_appliance(self, category: str) -> dict[str, Any] | None:
        return self._data["appliances"].get(category)

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    async def async_refresh(self) -> dict[str, Any]:
        async with self._lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._api.refresh_credentials)
            scraper = self._api.scraper

            if not scraper:
                LOGGER.warning("Appliance usage refresh failed because no scraper is available")
                self._data = {
                    "bill_period_start": None,
                    "bill_period_end": None,
                    "total_consumption": None,
                    "total_cost": None,
                    "appliances": {},
                }
                return self._data

            bill_start, bill_end = _get_latest_bill_period()
            appliance_payload = await loop.run_in_executor(
                None,
                scraper.get_appliance_usage,
                bill_start,
                bill_end,
            )
            appliances = appliance_payload["appliances"] if appliance_payload else []

            appliance_map: dict[str, dict[str, Any]] = {}
            total_consumption = None
            total_cost = None

            for appliance in appliances:
                category = appliance.get("category")
                if not category:
                    continue

                if category == "total":
                    total_consumption = appliance.get("consumption")
                    total_cost = appliance.get("cost")
                    continue

                appliance_map[category] = {
                    "display_name": _format_category_name(category),
                    "consumption": appliance.get("consumption"),
                    "cost": appliance.get("cost"),
                }

            self._data = {
                "bill_period_start": _iso_date_from_api(appliance_payload, "bill_start_date"),
                "bill_period_end": _iso_date_from_api(appliance_payload, "bill_end_date"),
                "total_consumption": total_consumption,
                "total_cost": total_cost,
                "appliances": appliance_map,
            }
            return self._data


class ApplianceUsageSensor(SensorEntity):
    def __init__(self, device_id: str, appliance_store: ApplianceUsageStore):
        self._appliance_store = appliance_store
        self._attr_has_entity_name = True
        self._attr_name = "Electric Ireland Appliance Usage"
        self._attr_unique_id = f"{DOMAIN}_appliance_usage_{device_id}"
        self._attr_entity_id = f"{DOMAIN}_appliance_usage_{device_id}"
        self._attr_icon = "mdi:power-plug"
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}

    async def async_update(self):
        # Read from the cached data — refresh is handled centrally by
        # async_track_time_interval so we don't trigger a new login here.
        data = self._appliance_store.data
        appliances = data["appliances"]
        if not appliances and data["total_consumption"] is None:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return

        attributes = {
            "bill_period_start": data["bill_period_start"],
            "bill_period_end": data["bill_period_end"],
        }

        named_appliances = []
        for category, appliance in appliances.items():
            display_name = appliance["display_name"]
            consumption = appliance.get("consumption")
            cost = appliance.get("cost")

            named_appliances.append(display_name)
            attributes[f"{category}_name"] = display_name
            attributes[f"{category}_consumption"] = consumption
            attributes[f"{category}_cost"] = cost
            attributes[display_name] = f"{consumption} kWh (€{cost})"

        attributes["appliance_count"] = len(named_appliances)
        attributes["appliances"] = named_appliances
        attributes["total_cost"] = data["total_cost"]
        attributes["total_consumption"] = data["total_consumption"]

        if data["total_consumption"] is not None:
            self._attr_native_value = f"{data['total_consumption']} kWh total"
        else:
            self._attr_native_value = f"{len(named_appliances)} appliances"

        self._attr_extra_state_attributes = attributes


class ApplianceConsumptionSensor(SensorEntity):
    def __init__(self, device_id: str, appliance_store: ApplianceUsageStore, category: str):
        self._appliance_store = appliance_store
        self._category = category
        self._attr_has_entity_name = True
        self._attr_name = f"{_format_category_name(category)} Consumption"
        self._attr_unique_id = f"{DOMAIN}_appliance_{_slugify(category)}_consumption_{device_id}"
        self._attr_entity_id = f"{DOMAIN}_appliance_{_slugify(category)}_consumption_{device_id}"
        self._attr_icon = "mdi:home-lightning-bolt"
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}

    async def async_update(self):
        # Read from the cached data — refresh is handled centrally by
        # async_track_time_interval so we don't trigger a new login here.
        data = self._appliance_store.data
        appliance = data["appliances"].get(self._category)
        if appliance is None:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return

        consumption = appliance.get("consumption")
        self._attr_native_value = (
            f"{consumption} kWh this bill"
            if consumption is not None
            else "No consumption data"
        )
        self._attr_extra_state_attributes = {
            "appliance": appliance["display_name"],
            "consumption_kwh": consumption,
            "cost_eur": appliance.get("cost"),
            "bill_period_start": data["bill_period_start"],
            "bill_period_end": data["bill_period_end"],
        }


def _get_latest_bill_period() -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    bill_end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    bill_start = (bill_end - timedelta(days=1)).replace(day=1)
    return bill_start, bill_end


def _format_category_name(category: str) -> str:
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", category.replace("_", " "))
    return words.strip().title()


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _iso_date_from_api(payload: dict[str, Any] | None, key: str) -> str | None:
    if not payload:
        return None
    value = payload.get(key)
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except (ValueError, TypeError, AttributeError):
        return None


class ConsumptionSensor(HourlyConsumptionSensor):
    """Backward-compatible alias for the legacy hourly consumption sensor."""


class CostSensor(HourlyCostSensor):
    """Backward-compatible alias for the legacy hourly cost sensor."""