"""Flow Helpers — EMHASS HVAC Optimizer v0.4.0.

Utilidades compartidas por config_flow y options_flow:
  • Esquemas voluptuous por paso
  • Defaults pre-cargados con entidades conocidas del sistema
  • Zonas Daikin Altherma pre-configuradas
  • Selectors HA modernos (EntitySelector, TimeSelector, …)
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
import homeassistant.helpers.selector as sel

from .const import (
    CONF_ATH_CAUDAL, CONF_ATH_COP, CONF_ATH_IMPULSION,
    CONF_ATH_MODE, CONF_ATH_RETORNO,
    CONF_AUTO_TUNE_ENABLED, CONF_BATTERY_SOC,
    CONF_BUFFER_LITERS, CONF_COMPANION_ENABLED, CONF_COMPANION_URL,
    CONF_EMHASS_URL, CONF_GRID_POWER, CONF_HOUSE_LOAD,
    CONF_HVAC_CURRENT, CONF_PV_POWER,
    CONF_SIMULATION_MODE, CONF_TEMP_EXTERIOR,
    CONF_THERMAL_BASE_TEMP, CONF_USE_COP, CONF_USE_TANK,
    CONF_ZONE_CLIMATE, CONF_ZONE_DEMAND_WEIGHT, CONF_ZONE_ENABLED,
    CONF_ZONE_ID, CONF_ZONE_NAME, CONF_ZONE_SCHEDULE_END,
    CONF_ZONE_SCHEDULE_START, CONF_ZONE_TEMP_PRIMARY,
    CONF_ZONE_TEMP_SECONDARY, CONF_ZONE_SENSOR_WEIGHTS,
    CONF_ZONES_CONFIG,
    DEFAULT_ATH_CAUDAL, DEFAULT_ATH_COP,
    DEFAULT_ATH_IMPULSION, DEFAULT_ATH_RETORNO,
    DEFAULT_AUTO_TUNE, DEFAULT_BASE_TEMP, DEFAULT_BATTERY_SOC,
    DEFAULT_BUFFER_LITERS, DEFAULT_COMPANION_URL,
    DEFAULT_DEMAND_WEIGHT, DEFAULT_EMHASS_URL,
    DEFAULT_GRID_POWER, DEFAULT_HOUSE_LOAD,
    DEFAULT_HVAC_CURRENT, DEFAULT_PV_POWER,
    DEFAULT_SCHEDULE_END, DEFAULT_SCHEDULE_START,
    DEFAULT_SIMULATION_MODE, DEFAULT_TEMP_EXTERIOR,
    MAX_ZONES, ZONE_IDS,
)

# ── Zonas Daikin Altherma pre-configuradas ──────────────────────────
KNOWN_ZONES: list[dict] = [
    {
        CONF_ZONE_ID:             "salon",
        CONF_ZONE_NAME:           "Salón",
        CONF_ZONE_CLIMATE:        "climate.climasalonlocal",
        CONF_ZONE_TEMP_PRIMARY:   "sensor.salontemperature",
        CONF_ZONE_TEMP_SECONDARY: [],
        CONF_ZONE_SENSOR_WEIGHTS: [],
        CONF_ZONE_DEMAND_WEIGHT:  0.40,
        CONF_ZONE_SCHEDULE_START: "07:00",
        CONF_ZONE_SCHEDULE_END:   "23:00",
        CONF_ZONE_ENABLED:        True,
    },
    {
        CONF_ZONE_ID:             "hab_alvaro",
        CONF_ZONE_NAME:           "Habitación Álvaro",
        CONF_ZONE_CLIMATE:        "climate.climahabitacionlocal",
        CONF_ZONE_TEMP_PRIMARY:   "sensor.habalvarotemperature",
        CONF_ZONE_TEMP_SECONDARY: [],
        CONF_ZONE_SENSOR_WEIGHTS: [],
        CONF_ZONE_DEMAND_WEIGHT:  0.20,
        CONF_ZONE_SCHEDULE_START: "22:00",
        CONF_ZONE_SCHEDULE_END:   "08:00",
        CONF_ZONE_ENABLED:        True,
    },
    {
        CONF_ZONE_ID:             "hab_inv",
        CONF_ZONE_NAME:           "Habitación Invitados",
        CONF_ZONE_CLIMATE:        "climate.climahabitacionlocal",
        CONF_ZONE_TEMP_PRIMARY:   "sensor.habinvtemperature",
        CONF_ZONE_TEMP_SECONDARY: [],
        CONF_ZONE_SENSOR_WEIGHTS: [],
        CONF_ZONE_DEMAND_WEIGHT:  0.20,
        CONF_ZONE_SCHEDULE_START: "22:00",
        CONF_ZONE_SCHEDULE_END:   "08:00",
        CONF_ZONE_ENABLED:        False,
    },
    {
        CONF_ZONE_ID:             "hab_mat",
        CONF_ZONE_NAME:           "Habitación Matrimonio",
        CONF_ZONE_CLIMATE:        "climate.climahabitacionlocal",
        CONF_ZONE_TEMP_PRIMARY:   "sensor.habmattemperature",
        CONF_ZONE_TEMP_SECONDARY: [],
        CONF_ZONE_SENSOR_WEIGHTS: [],
        CONF_ZONE_DEMAND_WEIGHT:  0.20,
        CONF_ZONE_SCHEDULE_START: "22:00",
        CONF_ZONE_SCHEDULE_END:   "08:00",
        CONF_ZONE_ENABLED:        True,
    },
]


# ── Paso 1: Basic ────────────────────────────────────────────────────

def schema_step_basic(defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema({
        vol.Required(CONF_EMHASS_URL,
                     default=d.get(CONF_EMHASS_URL, DEFAULT_EMHASS_URL)): sel.TextSelector(
            sel.TextSelectorConfig(type=sel.TextSelectorType.URL)
        ),
        vol.Required(CONF_TEMP_EXTERIOR,
                     default=d.get(CONF_TEMP_EXTERIOR, DEFAULT_TEMP_EXTERIOR)): sel.EntitySelector(
            sel.EntitySelectorConfig(domain="sensor")
        ),
        vol.Required(CONF_HVAC_CURRENT,
                     default=d.get(CONF_HVAC_CURRENT, DEFAULT_HVAC_CURRENT)): sel.EntitySelector(
            sel.EntitySelectorConfig(domain="sensor")
        ),
        vol.Required(CONF_THERMAL_BASE_TEMP,
                     default=d.get(CONF_THERMAL_BASE_TEMP, DEFAULT_BASE_TEMP)): sel.NumberSelector(
            sel.NumberSelectorConfig(min=14.0, max=21.0, step=0.5, mode=sel.NumberSelectorMode.SLIDER)
        ),
        vol.Required(CONF_SIMULATION_MODE,
                     default=d.get(CONF_SIMULATION_MODE, DEFAULT_SIMULATION_MODE)): sel.BooleanSelector(),
        vol.Required(CONF_AUTO_TUNE_ENABLED,
                     default=d.get(CONF_AUTO_TUNE_ENABLED, DEFAULT_AUTO_TUNE)): sel.BooleanSelector(),
    })


# ── Paso 2: Sensores ESPAltherma ─────────────────────────────────────

def schema_step_ath(defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    _sens = sel.EntitySelectorConfig(domain="sensor")
    return vol.Schema({
        vol.Required(CONF_ATH_IMPULSION,
                     default=d.get(CONF_ATH_IMPULSION, DEFAULT_ATH_IMPULSION)): sel.EntitySelector(_sens),
        vol.Required(CONF_ATH_RETORNO,
                     default=d.get(CONF_ATH_RETORNO, DEFAULT_ATH_RETORNO)): sel.EntitySelector(_sens),
        vol.Required(CONF_ATH_CAUDAL,
                     default=d.get(CONF_ATH_CAUDAL, DEFAULT_ATH_CAUDAL)): sel.EntitySelector(_sens),
        vol.Required(CONF_ATH_COP,
                     default=d.get(CONF_ATH_COP, DEFAULT_ATH_COP)): sel.EntitySelector(_sens),
        vol.Optional(CONF_ATH_MODE,
                     default=d.get(CONF_ATH_MODE, "")): sel.EntitySelector(
            sel.EntitySelectorConfig(domain=["sensor", "binary_sensor"])
        ),
        vol.Required(CONF_USE_COP,
                     default=d.get(CONF_USE_COP, True)): sel.BooleanSelector(),
    })


# ── Paso 3: Sensores globales ─────────────────────────────────────────

def schema_step_global_sensors(defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    _sens = sel.EntitySelectorConfig(domain="sensor")
    return vol.Schema({
        vol.Required(CONF_BATTERY_SOC,
                     default=d.get(CONF_BATTERY_SOC, DEFAULT_BATTERY_SOC)): sel.EntitySelector(_sens),
        vol.Required(CONF_PV_POWER,
                     default=d.get(CONF_PV_POWER, DEFAULT_PV_POWER)): sel.EntitySelector(_sens),
        vol.Required(CONF_GRID_POWER,
                     default=d.get(CONF_GRID_POWER, DEFAULT_GRID_POWER)): sel.EntitySelector(_sens),
        vol.Required(CONF_HOUSE_LOAD,
                     default=d.get(CONF_HOUSE_LOAD, DEFAULT_HOUSE_LOAD)): sel.EntitySelector(_sens),
    })


# ── Paso 4: Zona individual ───────────────────────────────────────────

def schema_step_zone(zone_idx: int, defaults: dict | None = None) -> vol.Schema:
    """
    Esquema para la configuración de una zona (índice 0-3).
    defaults: dict con valores actuales de la zona.
    """
    kz   = KNOWN_ZONES[zone_idx] if zone_idx < len(KNOWN_ZONES) else {}
    d    = defaults or kz
    _sens = sel.EntitySelectorConfig(domain="sensor")

    return vol.Schema({
        vol.Required(CONF_ZONE_NAME,
                     default=d.get(CONF_ZONE_NAME, f"Zona {zone_idx + 1}")): sel.TextSelector(
            sel.TextSelectorConfig(type=sel.TextSelectorType.TEXT)
        ),
        vol.Required(CONF_ZONE_CLIMATE,
                     default=d.get(CONF_ZONE_CLIMATE, "")): sel.EntitySelector(
            sel.EntitySelectorConfig(domain="climate")
        ),
        vol.Required(CONF_ZONE_TEMP_PRIMARY,
                     default=d.get(CONF_ZONE_TEMP_PRIMARY, "")): sel.EntitySelector(_sens),
        vol.Optional(CONF_ZONE_TEMP_SECONDARY,
                     default=d.get(CONF_ZONE_TEMP_SECONDARY, [])): sel.EntitySelector(
            sel.EntitySelectorConfig(domain="sensor", multiple=True)
        ),
        vol.Required(CONF_ZONE_DEMAND_WEIGHT,
                     default=float(d.get(CONF_ZONE_DEMAND_WEIGHT, DEFAULT_DEMAND_WEIGHT))): sel.NumberSelector(
            sel.NumberSelectorConfig(min=0.05, max=1.0, step=0.05, mode=sel.NumberSelectorMode.SLIDER)
        ),
        vol.Required(CONF_ZONE_SCHEDULE_START,
                     default=d.get(CONF_ZONE_SCHEDULE_START, DEFAULT_SCHEDULE_START)): sel.TimeSelector(),
        vol.Required(CONF_ZONE_SCHEDULE_END,
                     default=d.get(CONF_ZONE_SCHEDULE_END, DEFAULT_SCHEDULE_END)): sel.TimeSelector(),
        vol.Required(CONF_ZONE_ENABLED,
                     default=bool(d.get(CONF_ZONE_ENABLED, True))): sel.BooleanSelector(),
    })


# ── Paso 5: Companion App + Advanced ─────────────────────────────────

def schema_step_companion_advanced(defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema({
        # Companion App
        vol.Required(CONF_COMPANION_ENABLED,
                     default=d.get(CONF_COMPANION_ENABLED, False)): sel.BooleanSelector(),
        vol.Optional(CONF_COMPANION_URL,
                     default=d.get(CONF_COMPANION_URL, DEFAULT_COMPANION_URL)): sel.TextSelector(
            sel.TextSelectorConfig(type=sel.TextSelectorType.URL)
        ),
        # Depósito inercia
        vol.Required(CONF_USE_TANK,
                     default=d.get(CONF_USE_TANK, False)): sel.BooleanSelector(),
        vol.Optional(CONF_BUFFER_LITERS,
                     default=float(d.get(CONF_BUFFER_LITERS, DEFAULT_BUFFER_LITERS))): sel.NumberSelector(
            sel.NumberSelectorConfig(min=50, max=500, step=10, mode=sel.NumberSelectorMode.SLIDER, unit_of_measurement="L")
        ),
    })


# ── Helpers de validación ─────────────────────────────────────────────

def zones_from_flow_data(
    flow_data: dict[str, Any]
) -> list[dict]:
    """
    Reconstruye la lista CONF_ZONES_CONFIG a partir de los campos
    guardados zona por zona (zone_0_data, zone_1_data, ...).
    """
    zones = []
    for i in range(MAX_ZONES):
        key = f"_zone_{i}_data"
        if key in flow_data:
            z = dict(flow_data[key])
            z[CONF_ZONE_ID] = ZONE_IDS[i] if i < len(ZONE_IDS) else f"zone_{i}"
            zones.append(z)
    return zones


def flatten_config(config_data: dict, zones: list[dict]) -> dict:
    """Combina toda la configuración en el dict final de la config_entry."""
    result = {k: v for k, v in config_data.items() if not k.startswith("_zone_")}
    result[CONF_ZONES_CONFIG] = zones
    return result
