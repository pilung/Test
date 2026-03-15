"""Sensor platform — EMHASS HVAC Optimizer v0.4.0.

28 sensores expuestos a Home Assistant:
  • 20 sensores globales (thermal, COP, battery, HVAC, prices, efficiency)
  •  8 sensores por zona  (operative_temp × 4 + demand_weight × 4)

Todos leen de coordinator.data[key] — ninguno accede a HA directamente.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE, UnitOfEnergy, UnitOfPower, UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN, LOGGER_NAME, ZONE_IDS,
    SID_APPARENT_TEMP, SID_BATT_CHARGE_LIMIT, SID_BATT_EFFICIENCY,
    SID_BATT_SOC_TARGET, SID_COMPANION_STATUS, SID_COP_CURRENT,
    SID_DEGREE_DAYS_TODAY, SID_DEGREE_DAYS_TOMORROW,
    SID_HVAC_MODE, SID_HVAC_SOLAR_OPP, SID_PRICE_GRID_STATUS,
    SID_PRICE_SOURCE, SID_SEASONAL_MODE, SID_SELF_CONSUMPTION,
    SID_SELF_SUFFICIENCY, SID_SYSTEM_EFFICIENCY,
    SID_THERMAL_CONFIDENCE, SID_THERMAL_FACTOR, SID_THERMAL_FORECAST,
    SID_THERMAL_MAE, SID_THERMAL_MODEL_ACTIVE, SID_THERMAL_POWER,
    SID_ZONE_DD, SID_ZONE_TEMP, SID_ZONE_WEIGHT,
)
from .coordinator import EMHASSHVACCoordinator

_LOGGER = logging.getLogger(LOGGER_NAME)


@dataclass(frozen=True, kw_only=True)
class EHVACSensorDescription(SensorEntityDescription):
    """Descripción extendida con atributos extra."""
    extra_attrs_key: str | None = None   # clave coordinator.data con lista→atributo


# ── Descriptores de sensores globales ────────────────────────────────
GLOBAL_SENSORS: tuple[EHVACSensorDescription, ...] = (
    EHVACSensorDescription(
        key=SID_THERMAL_FACTOR,
        name="Thermal Factor kWh/DD",
        icon="mdi:thermometer-lines",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
    ),
    EHVACSensorDescription(
        key=SID_DEGREE_DAYS_TODAY,
        name="Grados Día Hoy",
        icon="mdi:weather-snowy",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    EHVACSensorDescription(
        key=SID_DEGREE_DAYS_TOMORROW,
        name="Grados Día Forecast Mañana",
        icon="mdi:weather-snowy-rainy",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    EHVACSensorDescription(
        key=SID_THERMAL_FORECAST,
        name="Thermal Load Forecast 48h",
        icon="mdi:chart-areaspline",
        native_unit_of_measurement=UnitOfPower.WATT,
        extra_attrs_key=SID_THERMAL_FORECAST,
    ),
    EHVACSensorDescription(
        key=SID_THERMAL_MODEL_ACTIVE,
        name="Thermal Model Activo",
        icon="mdi:brain",
    ),
    EHVACSensorDescription(
        key=SID_THERMAL_CONFIDENCE,
        name="Thermal Model Confianza",
        icon="mdi:percent",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EHVACSensorDescription(
        key=SID_THERMAL_MAE,
        name="Thermal Prediction Error MAE",
        icon="mdi:delta",
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EHVACSensorDescription(
        key=SID_THERMAL_POWER,
        name="Potencia Térmica kW",
        icon="mdi:fire",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
    ),
    EHVACSensorDescription(
        key=SID_APPARENT_TEMP,
        name="Temperatura Aparente Exterior",
        icon="mdi:thermometer-water",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    EHVACSensorDescription(
        key=SID_COP_CURRENT,
        name="COP Estimado Actual",
        icon="mdi:heat-pump",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    EHVACSensorDescription(
        key=SID_BATT_CHARGE_LIMIT,
        name="Límite Carga Batería Dinámico",
        icon="mdi:battery-charging",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EHVACSensorDescription(
        key=SID_BATT_SOC_TARGET,
        name="SOC Target Batería 48h",
        icon="mdi:battery-clock",
        extra_attrs_key=SID_BATT_SOC_TARGET,
    ),
    EHVACSensorDescription(
        key=SID_BATT_EFFICIENCY,
        name="Eficiencia Round-Trip Batería",
        icon="mdi:battery-heart-variant",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    EHVACSensorDescription(
        key=SID_HVAC_MODE,
        name="HVAC Modo Actual",
        icon="mdi:hvac",
    ),
    EHVACSensorDescription(
        key=SID_HVAC_SOLAR_OPP,
        name="HVAC Oportunidad Solar",
        icon="mdi:solar-power",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
    ),
    EHVACSensorDescription(
        key=SID_PRICE_SOURCE,
        name="Fuente Precio Electricidad",
        icon="mdi:lightning-bolt-circle",
    ),
    EHVACSensorDescription(
        key=SID_PRICE_GRID_STATUS,
        name="Estado Precio Red",
        icon="mdi:currency-eur",
    ),
    EHVACSensorDescription(
        key=SID_SEASONAL_MODE,
        name="Modo Estacional Recomendado",
        icon="mdi:sun-snowflake-variant",
    ),
    EHVACSensorDescription(
        key=SID_SELF_CONSUMPTION,
        name="Autoconsumo Solar Hoy",
        icon="mdi:solar-panel",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    EHVACSensorDescription(
        key=SID_SELF_SUFFICIENCY,
        name="Autosuficiencia Solar Hoy",
        icon="mdi:home-battery",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    EHVACSensorDescription(
        key=SID_SYSTEM_EFFICIENCY,
        name="Eficiencia Sistema Score",
        icon="mdi:gauge",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    EHVACSensorDescription(
        key=SID_COMPANION_STATUS,
        name="Companion App Estado",
        icon="mdi:server-network",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EMHASSHVACCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []

    # Sensores globales
    for desc in GLOBAL_SENSORS:
        entities.append(EHVACSensor(coordinator, desc, entry.entry_id))

    # Sensores por zona (operative_temp + demand_weight + dd)
    for zone in coordinator.zones:
        entities.append(EHVACZoneSensor(
            coordinator, entry.entry_id, zone.id, zone.name,
            SID_ZONE_TEMP.format(zone.id),
            f"Temperatura Operativa {zone.name}",
            UnitOfTemperature.CELSIUS,
            SensorDeviceClass.TEMPERATURE,
            "mdi:thermometer",
        ))
        entities.append(EHVACZoneSensor(
            coordinator, entry.entry_id, zone.id, zone.name,
            SID_ZONE_WEIGHT.format(zone.id),
            f"Peso Demanda {zone.name}",
            None, None, "mdi:weight",
        ))

    async_add_entities(entities, update_before_add=True)
    _LOGGER.info(
        "Sensor platform: %d entidades registradas (%d globales + %d zonas)",
        len(entities), len(GLOBAL_SENSORS), len(entities) - len(GLOBAL_SENSORS),
    )


# ══════════════════════════════════════════════════════════════════════
# Clases sensor
# ══════════════════════════════════════════════════════════════════════

class EHVACSensor(CoordinatorEntity[EMHASSHVACCoordinator], SensorEntity):
    """Sensor global del coordinator."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EMHASSHVACCoordinator,
        description: EHVACSensorDescription,
        entry_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id    = f"{entry_id}_{description.key}"
        self._attr_device_info  = _device_info(entry_id)

    @property
    def native_value(self) -> Any:
        val = self.coordinator.data.get(self.entity_description.key)
        # Arrays → mostrar longitud como estado; datos reales en atributos
        if isinstance(val, list):
            return len(val)
        # Efficiency: multiply by 100 if < 1 (normalized)
        if (self.entity_description.key == SID_BATT_EFFICIENCY
                and val is not None and val < 1.0):
            return round(val * 100.0, 1)
        return val

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        desc = self.entity_description
        if desc.extra_attrs_key:
            raw = self.coordinator.data.get(desc.extra_attrs_key)
            if isinstance(raw, list):
                attrs["forecast"] = raw
                attrs["n_slots"]  = len(raw)
        # Añadir info modelo térmico en sensor thermal_factor
        if desc.key == SID_THERMAL_FACTOR:
            attrs["is_fitted"]    = self.coordinator.dd_model.is_fitted
            attrs["r2_score"]     = self.coordinator.dd_model.r2_score
            attrs["n_samples"]    = self.coordinator.dd_model.n_samples
            attrs["t_base"]       = self.coordinator.dd_model.t_base
            attrs["use_q_thermal"] = self.coordinator.dd_model.use_thermal_power
        return attrs


class EHVACZoneSensor(CoordinatorEntity[EMHASSHVACCoordinator], SensorEntity):
    """Sensor por zona."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EMHASSHVACCoordinator,
        entry_id: str,
        zone_id: str,
        zone_name: str,
        data_key: str,
        friendly_name: str,
        unit: str | None,
        device_class: SensorDeviceClass | None,
        icon: str,
    ) -> None:
        super().__init__(coordinator)
        self._data_key               = data_key
        self._attr_unique_id         = f"{entry_id}_{data_key}"
        self._attr_name              = friendly_name
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class      = device_class
        self._attr_icon              = icon
        self._attr_state_class       = (
            SensorStateClass.MEASUREMENT if unit else None
        )
        self._attr_device_info       = _device_info(entry_id)

    @property
    def native_value(self) -> Any:
        return self.coordinator.data.get(self._data_key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Buscar zona correspondiente
        for zone in self.coordinator.zones:
            if self._data_key.startswith(f"zone_{zone.id}"):
                return {
                    "zone_id":       zone.id,
                    "schedule":      f"{zone.schedule_start}–{zone.schedule_end}",
                    "in_schedule":   zone.is_in_schedule(),
                    "tau_hours":     zone.tau_hours,
                    "hvac_mode":     zone.get_hvac_mode(),
                    "setpoint":      zone.get_setpoint(),
                    "dew_point":     zone.get_dew_point(),
                    "schedule_factor": zone.get_schedule_factor(),
                }
        return {}


def _device_info(entry_id: str) -> dict:
    return {
        "identifiers":  {(DOMAIN, entry_id)},
        "name":         "EMHASS HVAC Optimizer",
        "manufacturer": "Custom Integration",
        "model":        "v0.4.0 — Huawei + Daikin Altherma",
        "sw_version":   "0.4.0",
    }
