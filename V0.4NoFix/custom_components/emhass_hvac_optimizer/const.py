"""Constants — EMHASS HVAC Optimizer v0.4.0.

Archivo único de constantes. NINGÚN otro módulo define constantes
hardcodeadas; todo referencia este archivo.
"""
from __future__ import annotations

# ── Identificadores ───────────────────────────────────────────────────
DOMAIN      = "emhass_hvac_optimizer"
LOGGER_NAME = "custom_components.emhass_hvac_optimizer"

# ── Config entry keys ─────────────────────────────────────────────────
CONF_EMHASS_URL         = "emhass_url"
CONF_TEMP_EXTERIOR      = "temp_exterior_sensor"
CONF_HVAC_CURRENT       = "hvac_current_sensor"
CONF_THERMAL_BASE_TEMP  = "thermal_base_temp"
CONF_SIMULATION_MODE    = "simulation_mode"
CONF_AUTO_TUNE_ENABLED  = "auto_tune_enabled"
CONF_ATH_IMPULSION      = "ath_impulsion"
CONF_ATH_RETORNO        = "ath_retorno"
CONF_ATH_CAUDAL         = "ath_caudal"
CONF_ATH_COP            = "ath_cop"
CONF_ATH_MODE           = "ath_mode"
CONF_USE_COP            = "use_cop"
CONF_BATTERY_SOC        = "battery_soc_sensor"
CONF_PV_POWER           = "pv_power_sensor"
CONF_GRID_POWER         = "grid_power_sensor"
CONF_HOUSE_LOAD         = "house_load_sensor"
CONF_COMPANION_ENABLED  = "companion_app_enabled"
CONF_COMPANION_URL      = "companion_app_url"
CONF_USE_TANK           = "use_buffer_tank"
CONF_BUFFER_LITERS      = "buffer_tank_liters"
CONF_ZONES_CONFIG       = "zones_config"
CONF_ZONE_ID            = "zone_id"
CONF_ZONE_NAME          = "zone_name"
CONF_ZONE_CLIMATE       = "zone_climate"
CONF_ZONE_TEMP_PRIMARY  = "zone_temp_primary"
CONF_ZONE_TEMP_SECONDARY= "zone_temp_secondary"
CONF_ZONE_SENSOR_WEIGHTS= "zone_sensor_weights"
CONF_ZONE_DEMAND_WEIGHT = "zone_demand_weight"
CONF_ZONE_SCHEDULE_START= "zone_schedule_start"
CONF_ZONE_SCHEDULE_END  = "zone_schedule_end"
CONF_ZONE_ENABLED       = "zone_enabled"

# ── Sensor IDs (coordinator.data keys + unique_id suffix) ─────────────
SID_THERMAL_FACTOR      = "thermal_factor"
SID_DEGREE_DAYS_TODAY   = "degree_days_today"
SID_DEGREE_DAYS_TOMORROW= "degree_days_tomorrow"
SID_THERMAL_FORECAST    = "thermal_load_forecast"
SID_THERMAL_MODEL_ACTIVE= "thermal_model_active"
SID_THERMAL_CONFIDENCE  = "thermal_model_confidence"
SID_THERMAL_MAE         = "thermal_prediction_mae"
SID_THERMAL_POWER       = "thermal_power_kw"
SID_APPARENT_TEMP       = "apparent_temperature"
SID_COP_CURRENT         = "cop_current"
SID_BATT_CHARGE_LIMIT   = "battery_charge_limit"
SID_BATT_SOC_TARGET     = "battery_soc_target"
SID_BATT_EFFICIENCY     = "battery_efficiency"
SID_HVAC_MODE           = "hvac_mode_current"
SID_HVAC_SOLAR_OPP      = "hvac_solar_opportunity"
SID_PRICE_SOURCE        = "price_source"
SID_PRICE_GRID_STATUS   = "price_grid_status"
SID_SEASONAL_MODE       = "seasonal_mode"
SID_SELF_CONSUMPTION    = "self_consumption"
SID_SELF_SUFFICIENCY    = "self_sufficiency"
SID_SYSTEM_EFFICIENCY   = "system_efficiency_score"
SID_COMPANION_STATUS    = "companion_status"
# Zonas — usar .format(zone_id)
SID_ZONE_TEMP           = "zone_{}_operative_temperature"
SID_ZONE_WEIGHT         = "zone_{}_demand_weight"
SID_ZONE_DD             = "zone_{}_degree_days"

# ── Default values ────────────────────────────────────────────────────
DEFAULT_EMHASS_URL      = "http://homeassistant.local:5000"
DEFAULT_COMPANION_URL   = "http://homeassistant.local:8765"
DEFAULT_TEMP_EXTERIOR   = "sensor.athtempexterior"
DEFAULT_HVAC_CURRENT    = "sensor.athcorriente"
DEFAULT_ATH_IMPULSION   = "sensor.athtempimpulsion"
DEFAULT_ATH_RETORNO     = "sensor.athtempretorno"
DEFAULT_ATH_CAUDAL      = "sensor.athcaudal"
DEFAULT_ATH_COP         = "sensor.athcop"
DEFAULT_BATTERY_SOC     = "sensor.bateriasestadodelacapacidad"
DEFAULT_PV_POWER        = "sensor.inverterinputpower"
DEFAULT_GRID_POWER      = "sensor.powermeteractivepower"
DEFAULT_HOUSE_LOAD      = "sensor.powerhouseload"
DEFAULT_BASE_TEMP       = 18.5
DEFAULT_SIMULATION_MODE = True
DEFAULT_AUTO_TUNE       = True
DEFAULT_BUFFER_LITERS   = 100.0
DEFAULT_DEMAND_WEIGHT   = 0.25
DEFAULT_SCHEDULE_START  = "07:00"
DEFAULT_SCHEDULE_END    = "23:00"

# ── Modelo térmico ────────────────────────────────────────────────────
MODEL_DD                = "degree_days"
MODEL_RC                = "rc_model"
MODEL_ML                = "ml_model"
THERMAL_FACTOR_MIN      = 0.01     # kWh/DD mínimo físicamente posible
THERMAL_FACTOR_MAX      = 3.0      # kWh/DD máximo (casa muy mal aislada)
ZSCORE_THRESHOLD        = 2.5
FORECAST_HORIZON_HOURS  = 48
RECORDER_HOURS_BACK     = 168      # 7 días de historial para calibración
WATER_SPECIFIC_HEAT     = 4_186.0  # J/(kg·K)

# ── Zonas ─────────────────────────────────────────────────────────────
MAX_ZONES               = 4
ZONE_IDS                = ["salon", "hab_alvaro", "hab_inv", "hab_mat"]

# ── Batería / AC-DC ───────────────────────────────────────────────────
AC_CHARGE_LIMIT_DEFAULT = 2_500.0  # W — límite conservador LUNA2000 AC
DC_CHARGE_LIMIT_MAX     = 5_000.0  # W — límite DC (solar)
AC_SOLAR_THRESHOLD      = 500.0    # W — umbral PV "hay solar"
SUN_ELEVATION_THRESHOLD = 5.0      # ° — elevación solar mínima para "día"

# ── Modos estacionales ────────────────────────────────────────────────
SEASONAL_MODE_MSC       = "MSC"
SEASONAL_MODE_TOU       = "TOU"
MONTHS_SUMMER           = {6, 7, 8}
MONTHS_WINTER           = {12, 1, 2}
PV_RATIO_MSC_MIN        = 1.2      # ratio PV/Load mínimo para recomendar MSC

# ── HVAC controller ───────────────────────────────────────────────────
SOLAR_EXCESS_THRESHOLD_W = 1_000   # W exceso PV para solar-assisted
SOLAR_EXPORT_PRICE_MAX   = 0.08    # €/kWh — si export ≤ esto es "barato"
SOLAR_BOOST_DEG          = 2.0     # °C boost setpoint solar-assisted
SOLAR_UNDERSHOOT_DEG     = 1.5     # °C por debajo SP para activar solar-assist
BATT_PRICE_THRESHOLD     = 0.26    # €/kWh — precio caro (batt-assisted)
BATT_SOC_MIN_COOLING     = 60.0    # % SOC mínimo para batt-assisted cooling
BATT_COOL_UNDERSHOOT     = 1.0     # °C reducción setpoint batt-assisted

# ── Eficiencia sistema ────────────────────────────────────────────────
EFFICIENCY_W_ROUNDTRIP  = 0.25     # peso η round-trip en score
EFFICIENCY_W_SELF_CONS  = 0.30     # peso autoconsumo en score
EFFICIENCY_W_SELF_SUFF  = 0.45     # peso autosuficiencia en score

# ── Ciclos temporales ─────────────────────────────────────────────────
UPDATE_INTERVAL_SECONDS  = 300     # 5 min — ciclo principal coordinator
UPDATE_INTERVAL_AUTOTUNER= 86_400  # 24 h  — auto-calibración diaria
EMHASS_INTERVAL_SECONDS  = 1_800   # 30 min — ciclo EMHASS MPC
COMPANION_TRAIN_SECONDS  = 604_800 # 7 días — entrenamiento Companion App

# ── Plataformas registradas ───────────────────────────────────────────
PLATFORMS                = ["sensor"]
