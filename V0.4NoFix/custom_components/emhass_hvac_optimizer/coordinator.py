"""Coordinator — EMHASS HVAC Optimizer v0.4.0.

Data Update Coordinator central. Orquesta todos los módulos:
  • DegreeDaysModel   — predicción térmica HA-side
  • COPModel          — curva COP aerotermia
  • ThermalZone × 4  — zonas Daikin Altherma
  • HVACController    — estrategias solar/battery assisted
  • PriceManager      — precios multi-fuente
  • ACDCLimitsManager — límite carga dinámica LUNA2000
  • SeasonalManager   — modo MSC / TOU estacional
  • CompanionClient   — RC/ML/AutoTuner Companion App
  • HAAutoTuner       — calibración diaria HA-side

Ciclos temporales:
  Cada 5 min  → _async_update_data()  lectura sensores + derivados
  Cada 30 min → _trigger_emhass_mpc() payload enriquecido → EMHASS
  Cada 24 h   → _run_daily_autotune() calibración completa
  Semanal     → _send_companion_training() envío historial Companion App
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ATH_CAUDAL, CONF_ATH_COP, CONF_ATH_IMPULSION, CONF_ATH_RETORNO,
    CONF_AUTO_TUNE_ENABLED, CONF_BATTERY_SOC, CONF_COMPANION_ENABLED,
    CONF_COMPANION_URL, CONF_EMHASS_URL, CONF_GRID_POWER, CONF_HOUSE_LOAD,
    CONF_HVAC_CURRENT, CONF_PV_POWER, CONF_SIMULATION_MODE,
    CONF_TEMP_EXTERIOR, CONF_THERMAL_BASE_TEMP, CONF_USE_COP,
    CONF_ZONES_CONFIG,
    DEFAULT_COMPANION_URL, DEFAULT_EMHASS_URL,
    DOMAIN, EFFICIENCY_W_ROUNDTRIP, EFFICIENCY_W_SELF_CONS,
    EFFICIENCY_W_SELF_SUFF, FORECAST_HORIZON_HOURS, LOGGER_NAME,
    MODEL_DD, SID_APPARENT_TEMP, SID_BATT_CHARGE_LIMIT,
    SID_BATT_EFFICIENCY, SID_BATT_SOC_TARGET, SID_COMPANION_STATUS,
    SID_COP_CURRENT, SID_DEGREE_DAYS_TODAY, SID_DEGREE_DAYS_TOMORROW,
    SID_HVAC_MODE, SID_HVAC_SOLAR_OPP, SID_PRICE_GRID_STATUS,
    SID_PRICE_SOURCE, SID_SEASONAL_MODE, SID_SELF_CONSUMPTION,
    SID_SELF_SUFFICIENCY, SID_SYSTEM_EFFICIENCY,
    SID_THERMAL_CONFIDENCE, SID_THERMAL_FACTOR, SID_THERMAL_FORECAST,
    SID_THERMAL_MAE, SID_THERMAL_MODEL_ACTIVE, SID_THERMAL_POWER,
    SID_ZONE_DD, SID_ZONE_TEMP, SID_ZONE_WEIGHT,
    UPDATE_INTERVAL_AUTOTUNER,
)
from .core.ac_dc_limits import ACDCLimitsManager
from .core.autotuner_ha import HAAutoTuner
from .core.companion_client import CompanionClient
from .core.hvac_controller import HVACController
from .core.price_manager import PriceManager
from .core.seasonal_manager import SeasonalModeManager
from .models.cop_model import COPModel
from .models.degree_days import DegreeDaysModel
from .models.thermal_zone import ThermalZone

_LOGGER = logging.getLogger(LOGGER_NAME)

_EMHASS_INTERVAL_S  = 1_800   # 30 min
_COMPANION_TRAIN_S  = 604_800  # 7 días
_SAFE_FLOAT_NONE    = None


def _safe_float(v, default=None):
    import math
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


class EMHASSHVACCoordinator(DataUpdateCoordinator):
    """Coordinator central de EMHASS HVAC Optimizer."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        update_interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
        )
        self._entry            = entry
        cfg                    = {**entry.data, **entry.options}

        # ── Configuración ──────────────────────────────────────────
        self._emhass_url       = cfg.get(CONF_EMHASS_URL,      DEFAULT_EMHASS_URL)
        self._sim              = cfg.get(CONF_SIMULATION_MODE,  True)
        self._auto_tune        = cfg.get(CONF_AUTO_TUNE_ENABLED, True)
        self._use_cop          = cfg.get(CONF_USE_COP,          True)
        self._t_base           = float(cfg.get(CONF_THERMAL_BASE_TEMP, 18.5))

        # ── Sensores HA ────────────────────────────────────────────
        self._e_temp_ext  = cfg.get(CONF_TEMP_EXTERIOR,  "sensor.athtempexterior")
        self._e_corriente = cfg.get(CONF_HVAC_CURRENT,   "sensor.athcorriente")
        self._e_soc       = cfg.get(CONF_BATTERY_SOC,    "sensor.bateriasestadodelacapacidad")
        self._e_pv        = cfg.get(CONF_PV_POWER,       "sensor.inverterinputpower")
        self._e_grid      = cfg.get(CONF_GRID_POWER,     "sensor.powermeteractivepower")
        self._e_house     = cfg.get(CONF_HOUSE_LOAD,     "sensor.powerhouseload")

        # ── Modelos ────────────────────────────────────────────────
        self.dd_model = DegreeDaysModel(
            hass              = hass,
            temp_exterior_sensor = self._e_temp_ext,
            hvac_current_sensor  = self._e_corriente,
            ath_impulsion     = cfg.get(CONF_ATH_IMPULSION, "sensor.athtempimpulsion"),
            ath_retorno       = cfg.get(CONF_ATH_RETORNO,   "sensor.athtempretorno"),
            ath_caudal        = cfg.get(CONF_ATH_CAUDAL,    "sensor.athcaudal"),
            t_base            = self._t_base,
        )
        self.cop_model = COPModel(
            hass                 = hass,
            temp_exterior_sensor = self._e_temp_ext,
            cop_sensor           = cfg.get(CONF_ATH_COP, "sensor.athcop"),
            hvac_current_sensor  = self._e_corriente,
        )

        # ── Zonas ──────────────────────────────────────────────────
        zones_cfg   = cfg.get(CONF_ZONES_CONFIG, [])
        self.zones: list[ThermalZone] = [
            ThermalZone.from_config(hass, z)
            for z in zones_cfg
            if z.get("enabled", True)
        ]

        # ── Core ───────────────────────────────────────────────────
        self.price_manager  = PriceManager(hass)
        self.ac_dc_limits   = ACDCLimitsManager(hass)
        self.seasonal_mgr   = SeasonalModeManager(hass, simulation_mode=self._sim)
        self.hvac_ctrl      = HVACController(hass, simulation_mode=self._sim)

        comp_url            = cfg.get(CONF_COMPANION_URL, DEFAULT_COMPANION_URL)
        comp_enabled        = cfg.get(CONF_COMPANION_ENABLED, False)
        self.companion      = CompanionClient(comp_url) if comp_enabled else None

        self.autotuner      = HAAutoTuner(
            hass            = hass,
            dd_model        = self.dd_model,
            cop_model       = self.cop_model,
            zones           = self.zones,
            simulation_mode = self._sim,
        )

        # ── Estado interno ─────────────────────────────────────────
        self._thermal_forecast: list[float]  = [0.0] * FORECAST_HORIZON_HOURS
        self._thermal_confidence: int        = 50
        self._thermal_model_active: str      = MODEL_DD
        self._thermal_mae: float             = 0.0
        self._last_emhass: datetime | None   = None
        self._last_autotune: datetime | None = None
        self._last_companion_train: datetime | None = None
        self._socfinal: float                = 0.60
        self._soc_target_48h: list[float]    = []

    # ── Propiedades públicas ───────────────────────────────────────

    @property
    def simulation_mode(self) -> bool:
        return self._sim

    @property
    def entry_id(self) -> str:
        return self._entry.entry_id

    # ══════════════════════════════════════════════════════════════
    # Ciclo principal (cada 5 min)
    # ══════════════════════════════════════════════════════════════

    async def _async_update_data(self) -> dict[str, Any]:
        """
        Actualiza todos los sensores derivados y dispara ciclos
        temporales (EMHASS 30 min, auto-tune 24 h, training semanal).
        """
        now = dt_util.now()
        data: dict[str, Any] = {}

        try:
            # 1. Forecast térmico ─────────────────────────────────
            await self._refresh_thermal_forecast()

            # 2. Valores derivados ────────────────────────────────
            data[SID_THERMAL_FACTOR]       = round(self.dd_model.thermal_factor, 4)
            data[SID_THERMAL_FORECAST]     = self._thermal_forecast
            data[SID_THERMAL_MODEL_ACTIVE] = self._thermal_model_active
            data[SID_THERMAL_CONFIDENCE]   = self._thermal_confidence
            data[SID_THERMAL_MAE]          = self._thermal_mae
            data[SID_THERMAL_POWER]        = self.dd_model.get_thermal_power_kw()
            data[SID_APPARENT_TEMP]        = self.dd_model.get_apparent_temp()

            # DD hoy y mañana
            data[SID_DEGREE_DAYS_TODAY]    = await self.dd_model.calculate_degree_days_today()
            dd_fc = await self.dd_model.predict(24)
            data[SID_DEGREE_DAYS_TOMORROW] = round(
                sum(max(0.0, self.dd_model.t_base - self._get_t_forecast_avg()) * 1 for _ in range(24)) / 24 * 1, 4
            )

            # 3. COP ──────────────────────────────────────────────
            data[SID_COP_CURRENT] = (
                self.cop_model.predict_cop_current() if self._use_cop else None
            )

            # 4. AC/DC límite ──────────────────────────────────────
            data[SID_BATT_CHARGE_LIMIT]   = self.ac_dc_limits.get_dynamic_limit()
            data[SID_BATT_EFFICIENCY]     = (
                self.autotuner.last_results.battery_efficiency_rt
                if self.autotuner.last_results else None
            )
            data[SID_BATT_SOC_TARGET]     = self._soc_target_48h

            # 5. HVAC ─────────────────────────────────────────────
            data[SID_HVAC_MODE]           = self.hvac_ctrl.current_mode
            data[SID_HVAC_SOLAR_OPP]      = self._calc_solar_opportunity()

            # 6. Precio ───────────────────────────────────────────
            data[SID_PRICE_SOURCE]        = self.price_manager.get_active_source()
            data[SID_PRICE_GRID_STATUS]   = self.price_manager.get_price_grid_status()

            # 7. Modo estacional ───────────────────────────────────
            data[SID_SEASONAL_MODE]       = self.seasonal_mgr.recommended_mode

            # 8. Self-consumption / sufficiency ───────────────────
            sc, ss                        = self._calc_self_rates()
            data[SID_SELF_CONSUMPTION]    = sc
            data[SID_SELF_SUFFICIENCY]    = ss
            data[SID_SYSTEM_EFFICIENCY]   = self._calc_efficiency_score(sc, ss)

            # 9. Companion App status ──────────────────────────────
            data[SID_COMPANION_STATUS]    = (
                "online" if (self.companion and self.companion.is_available)
                else "offline"
            )

            # 10. Sensores por zona ────────────────────────────────
            for zone in self.zones:
                data[SID_ZONE_TEMP.format(zone.id)]   = zone.get_operative_temperature()
                data[SID_ZONE_WEIGHT.format(zone.id)] = zone.demand_weight
                data[SID_ZONE_DD.format(zone.id)]     = zone.get_zone_dd(self.dd_model.t_base)

        except Exception as exc:
            raise UpdateFailed(f"Error actualizando datos: {exc}") from exc

        # ── Ciclos temporales (non-blocking) ─────────────────────
        asyncio.ensure_future(self._check_timed_tasks(now))
        return data

    # ══════════════════════════════════════════════════════════════
    # Forecast térmico — Companion App con fallback DD
    # ══════════════════════════════════════════════════════════════

    async def _refresh_thermal_forecast(self) -> None:
        """
        Obtiene forecast térmico 48h.
        Jerarquía:
          1. Companion App online + modelo convergido  → RC/ML
          2. Companion App offline o sin datos          → DD local
        """
        if self.companion:
            alive = await self.companion.async_health_check()
            if alive:
                t_ext_fc = self.dd_model._get_weather_forecast(FORECAST_HORIZON_HOURS)
                result   = await self.companion.async_thermal_predict(
                    temp_forecast  = t_ext_fc,
                    t_base         = self.dd_model.t_base,
                    solar_forecast = self._get_solcast_forecast(),
                )
                if result and "forecast_w" in result:
                    self._thermal_forecast     = result["forecast_w"]
                    self._thermal_confidence   = result.get("confidence", 80)
                    self._thermal_model_active = result.get("model_active", MODEL_DD)
                    _LOGGER.debug(
                        "Thermal forecast: Companion App | model=%s | conf=%d%%",
                        self._thermal_model_active, self._thermal_confidence,
                    )
                    return

        # Fallback: Degree Days local
        self._thermal_forecast     = await self.dd_model.predict(FORECAST_HORIZON_HOURS)
        self._thermal_confidence   = 60 if self.dd_model.is_fitted else 40
        self._thermal_model_active = MODEL_DD
        _LOGGER.debug(
            "Thermal forecast: DD local | fitted=%s | conf=%d%%",
            self.dd_model.is_fitted, self._thermal_confidence,
        )

    # ══════════════════════════════════════════════════════════════
    # EMHASS MPC — payload enriquecido
    # ══════════════════════════════════════════════════════════════

    async def async_trigger_emhass_mpc(self) -> bool:
        """
        Envía payload enriquecido a EMHASS MPC (naive-mpc-optim).

        Mejoras vs payload actual del sistema:
          • loadpowerforecast: base_load + thermal separados
          • batterychargepowermax: límite AC/DC dinámico
          • nominalpowerofdeferrableloads: potencia real bomba
          • socfinal: valor calibrado por AutoTuner
        """
        now  = dt_util.now()
        h    = now.hour

        # Vectores de precios
        fc   = self.price_manager.get_forecast(FORECAST_HORIZON_HOURS)
        imp  = fc["import"]
        exp  = fc["export"]

        # SOC actual
        soc_raw = _safe_float(self._read_entity(self._e_soc), 50.0)
        soc_init = soc_raw / 100.0

        # PV forecast Solcast
        pv_fc = self._get_solcast_forecast()

        # Load forecast: base + térmica
        base_load  = [self._get_base_load()] * FORECAST_HORIZON_HOURS
        thermal_fc = self._thermal_forecast
        load_total = [b + t for b, t in zip(base_load, thermal_fc)]

        # Método carga (naive en madrugada, mlforecaster de día)
        is_early = (h < 6) or (h == 6 and now.minute < 30)
        load_method = "naive" if is_early else "mlforecaster"

        # Potencia real bomba piscina
        pool_power = (
            self.autotuner.last_results.deferrable_power_w
            if self.autotuner.last_results and self.autotuner.last_results.deferrable_power_w
            else 650.0
        )
        # socfinal calibrado
        socfinal = (
            self.autotuner.last_results.emhass_socfinal
            if self.autotuner.last_results and self.autotuner.last_results.emhass_socfinal
            else self._socfinal
        )
        # Límite carga batería AC/DC
        charge_limit_w = self.ac_dc_limits.get_dynamic_limit()

        payload = {
            "pv_power_forecast":                  pv_fc,
            "load_cost_forecast":                  imp,
            "prod_price_forecast":                 exp,
            "load_power_forecast":                 load_total,
            "prediction_horizon":                  FORECAST_HORIZON_HOURS,
            "soc_init":                            round(soc_init, 3),
            "soc_final":                           round(socfinal, 2),
            "operating_hours_of_each_deferrable_load": [4],
            "nominal_power_of_deferrable_loads":   [pool_power],
            "weather_forecast_method":             "list",
            "load_forecast_method":                load_method,
            "battery_charge_power_max":            charge_limit_w,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._emhass_url}/action/naive-mpc-optim",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        self._last_emhass = now
                        _LOGGER.info(
                            "EMHASS MPC ✓ | soc_init=%.2f | socfinal=%.2f | "
                            "pool=%.0fW | charge_limit=%.0fW | method=%s",
                            soc_init, socfinal, pool_power,
                            charge_limit_w, load_method,
                        )
                        # Publicar datos EMHASS
                        asyncio.ensure_future(self._publish_emhass_data())
                        return True
                    _LOGGER.warning("EMHASS MPC → HTTP %d", resp.status)
                    return False
        except Exception as exc:
            _LOGGER.warning("EMHASS MPC error: %s", exc)
            return False

    async def _publish_emhass_data(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._emhass_url}/action/publish-data",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        _LOGGER.debug("EMHASS publish-data ✓")
        except Exception as exc:
            _LOGGER.debug("EMHASS publish-data: %s", exc)

    # ══════════════════════════════════════════════════════════════
    # Ciclos temporales
    # ══════════════════════════════════════════════════════════════

    async def _check_timed_tasks(self, now: datetime) -> None:
        """Comprueba y dispara ciclos temporales en background."""
        # EMHASS cada 30 min
        if (self._last_emhass is None
                or (now - self._last_emhass).total_seconds() >= _EMHASS_INTERVAL_S):
            await self.async_trigger_emhass_mpc()
            await self._run_hvac_strategies()
            await self._run_seasonal_evaluation()

        # Auto-tune diario
        if (self._auto_tune
                and (self._last_autotune is None
                     or (now - self._last_autotune).total_seconds() >= UPDATE_INTERVAL_AUTOTUNER)):
            await self._run_daily_autotune()

        # Training Companion App semanal
        if (self.companion
                and (self._last_companion_train is None
                     or (now - self._last_companion_train).total_seconds() >= _COMPANION_TRAIN_S)):
            await self._send_companion_training()

    async def _run_hvac_strategies(self) -> None:
        """Ejecuta estrategias HVAC: solar-assisted + battery-assisted + schedules."""
        pv_w     = _safe_float(self._read_entity(self._e_pv), 0.0)
        soc      = _safe_float(self._read_entity(self._e_soc), 50.0)
        price    = self.price_manager.get_current_price() or 0.18
        exp_p    = self.price_manager.get_export_price()
        base_w   = self._get_base_load()

        await self.hvac_ctrl.async_solar_assisted_heating(
            self.zones, pv_w, base_w, exp_p
        )
        await self.hvac_ctrl.async_battery_assisted_cooling(
            self.zones, price, soc
        )
        await self.hvac_ctrl.async_apply_zone_schedules(self.zones)

    async def _run_seasonal_evaluation(self) -> None:
        """Evalúa modo estacional y actualiza recomendación."""
        await self.seasonal_mgr.async_evaluate_mode()

    async def _run_daily_autotune(self) -> None:
        _LOGGER.info("AutoTuner HA: iniciando ciclo diario")
        try:
            results = await self.autotuner.async_run_daily_cycle()
            # Aplicar T_base optimizado al dd_model
            if results.t_base_optimal and not self._sim:
                self.dd_model.t_base = results.t_base_optimal
            # Aplicar socfinal sugerido
            if results.emhass_socfinal:
                self._socfinal = results.emhass_socfinal
            # Calibrar AC limit
            await self.ac_dc_limits.async_autocalibrate_ac_limit()
            self._last_autotune = dt_util.now()
        except Exception as exc:
            _LOGGER.warning("AutoTuner daily cycle error: %s", exc)

    async def _send_companion_training(self) -> None:
        """Envía payload de entrenamiento a la Companion App."""
        if not self.companion:
            return
        payload = self.autotuner.last_companion_payload
        if not payload:
            await self.autotuner._build_companion_payload(
                self.autotuner.last_results or __import__("dataclasses").replace
            )
            payload = self.autotuner.last_companion_payload
        if payload:
            series = payload.get("series", {})
            t_ext  = [v for _, v in series.get("t_ext",  [])]
            energy = [v for _, v in series.get("corriente", [])]
            ts     = [t for t, _ in series.get("t_ext", [])]
            result = await self.companion.async_thermal_train(t_ext, energy, ts)
            if result and result.get("ok"):
                self._last_companion_train = dt_util.now()
                _LOGGER.info(
                    "Companion App entrenado ✓ | R²=%.3f | model=%s",
                    result.get("r2", 0), result.get("model_used"),
                )
                # Actualizar τ en zonas desde Companion App
                status = await self.companion.async_thermal_status()
                if status:
                    cthermal = status.get("cthermal")
                    if cthermal:
                        for zone in self.zones:
                            zone.add_tau_sample(cthermal / len(self.zones))

    # ══════════════════════════════════════════════════════════════
    # Helpers de cálculo
    # ══════════════════════════════════════════════════════════════

    def _get_solcast_forecast(self) -> list[float]:
        s = self.hass.states.get("sensor.solcastpvforecastpronosticohoy")
        if not s:
            return [0.0] * FORECAST_HORIZON_HOURS
        fc = s.attributes.get("detailedForecast") or s.attributes.get("forecast", [])
        if not isinstance(fc, list):
            return [0.0] * FORECAST_HORIZON_HOURS
        vals = []
        for item in fc:
            if isinstance(item, dict):
                v = _safe_float(item.get("pv_estimate", item.get("value")))
                if v is not None:
                    vals.append(v * 1000)  # kW → W
        while len(vals) < FORECAST_HORIZON_HOURS:
            vals.append(0.0)
        return vals[:FORECAST_HORIZON_HOURS]

    def _get_base_load(self) -> float:
        """Carga base de la casa sin HVAC [W]. Lee sensor o usa 800 W default."""
        house = _safe_float(self._read_entity(self._e_house), 800.0)
        thermal = self.dd_model.get_thermal_power_kw()
        thermal_w = (thermal or 0.0) * 1000.0
        return max(0.0, (house or 0.0) - thermal_w)

    def _get_t_forecast_avg(self) -> float:
        """Temperatura forecast media para mañana."""
        temps = self.dd_model._get_weather_forecast(24)
        return sum(temps) / len(temps) if temps else 10.0

    def _calc_self_rates(self) -> tuple[float, float]:
        """Calcula self-consumption y self-sufficiency actuales [%]."""
        pv   = _safe_float(self._read_entity(self._e_pv),    0.0) or 0.0
        load = _safe_float(self._read_entity(self._e_house), 0.0) or 0.0
        sc   = min(pv, load) / pv   * 100.0 if pv   > 10 else 0.0
        ss   = min(pv, load) / load * 100.0 if load > 10 else 0.0
        return round(sc, 1), round(ss, 1)

    def _calc_solar_opportunity(self) -> float:
        t_ops = [z.get_operative_temperature() for z in self.zones if z.get_operative_temperature()]
        sps   = [z.get_setpoint() for z in self.zones if z.get_setpoint()]
        t_avg  = sum(t_ops) / len(t_ops) if t_ops else None
        sp_avg = sum(sps)   / len(sps)   if sps   else None
        pv_fc  = self._get_solcast_forecast()
        return self.hvac_ctrl.calculate_solar_opportunity(
            pv_fc, self._get_base_load(), t_avg, sp_avg
        )

    def _calc_efficiency_score(self, sc: float, ss: float) -> float:
        """Puntuación global eficiencia sistema [0-100]."""
        eta = (
            self.autotuner.last_results.battery_efficiency_rt
            if self.autotuner.last_results else 0.94
        ) or 0.94
        score = (
            EFFICIENCY_W_ROUNDTRIP * (eta / 0.96)
            + EFFICIENCY_W_SELF_CONS * (sc / 100.0)
            + EFFICIENCY_W_SELF_SUFF * (ss / 100.0)
        ) * 100.0
        return round(min(100.0, score), 1)

    def _read_entity(self, entity_id: str):
        s = self.hass.states.get(entity_id)
        if s is None or s.state in ("unknown", "unavailable", "none", ""):
            return None
        return s.state
