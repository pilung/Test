"""AutoTuner HA — EMHASS HVAC Optimizer v0.4.0.

Motor central de auto-calibración HA-side. Usa TODOS los sensores
disponibles. Se ejecuta una vez al día vía coordinator.

Parámetros calibrados automáticamente:
  ┌──────────────────────┬──────────────────────┬──────────────────────────────┐
  │ Parámetro            │ Método               │ Sensores usados              │
  ├──────────────────────┼──────────────────────┼──────────────────────────────┤
  │ T_base (DD)          │ Grid search R²       │ athtempexterior + corriente  │
  │ thermal_factor       │ Huber IRLS           │ athtempexterior + corriente  │
  │ COP curve (A,B,C)    │ Quadratic fit        │ athtempexterior + athcop     │
  │ Zone τ (h)           │ Step-response HVAC   │ sensores temp zona + climate │
  │ demand_weight/zona   │ Energía share real   │ corriente + ΔT setpoint      │
  │ preheat_minutes      │ Achievement error    │ temps zona + schedules       │
  │ deferrable power     │ Grid balance ON/OFF  │ grid + PV + house load       │
  │ battery η round-trip │ Ciclos carga/desc    │ SOC + potencia batería       │
  │ EMHASS socfinal      │ Historial SOC+precio │ SOC + prices + consumption   │
  │ AC charge limit      │ (ya en ac_dc_limits) │ —                            │
  └──────────────────────┴──────────────────────┴──────────────────────────────┘

Companion App payload:
  Construye paquete de datos historial completo (T_ext, Q_hvac, COP,
  step-response events, zone temps, ciclos batería) para que la
  Companion App entrene RC gray-box + ML + AutoTuner avanzado.

Pure Python. Zero external dependencies.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

from ..const import (
    DEFAULT_BASE_TEMP, LOGGER_NAME,
    RECORDER_HOURS_BACK, THERMAL_FACTOR_MAX, THERMAL_FACTOR_MIN,
    WATER_SPECIFIC_HEAT, ZSCORE_THRESHOLD,
)
from ..models.degree_days import (
    _huber_fit, _mean, _r2, _zscore_filter, _safe_float,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from ..models.cop_model    import COPModel
    from ..models.degree_days  import DegreeDaysModel
    from ..models.thermal_zone import ThermalZone

_LOGGER = logging.getLogger(LOGGER_NAME)

# ── Entidades del sistema real ────────────────────────────────────────
_E_TEMP_EXT    = "sensor.athtempexterior"
_E_CORRIENTE   = "sensor.athcorriente"
_E_CAUDAL      = "sensor.athcaudal"
_E_IMPULSION   = "sensor.athtempimpulsion"
_E_RETORNO     = "sensor.athtempretorno"
_E_COP         = "sensor.athcop"
_E_SOC         = "sensor.bateriasestadodelacapacidad"
_E_PV          = "sensor.inverterinputpower"
_E_GRID        = "sensor.powermeteractivepower"
_E_HOUSE_LOAD  = "sensor.powerhouseload"
_E_POOL_SWITCH = "switch.sonoff1000edfee64"
_E_PRICE       = "sensor.preciokwh"

# Rango búsqueda T_base
_TBASE_RANGE   = [x * 0.5 for x in range(28, 43)]   # 14.0 … 21.0 °C paso 0.5
_TBASE_MIN_R2  = 0.45    # R² mínimo para aceptar T_base nuevo
_TBASE_MIN_DAYS = 10     # días de historial mínimo para optimizar T_base

# Step-response
_STEP_WINDOW_MINUTES  = 90    # ventana de observación tras HVAC ON
_STEP_MIN_DELTA_T     = 0.8   # °C mínimo ΔT para ser evento válido
_STEP_MIN_EVENTS      = 3     # eventos mínimos para calcular τ

# Demanda por zona
_ZONE_WEIGHT_ALPHA    = 0.3   # exponential smoothing nuevas estimaciones
_ZONE_WEIGHT_MIN      = 0.05  # peso mínimo por zona

# Batería
_BATT_CYCLE_SOC_MIN   = 20.0  # % SOC mínimo para iniciar ciclo
_BATT_CYCLE_SOC_MAX   = 85.0  # % SOC máximo para cerrar ciclo carga

# Piscina
_POOL_MEASURE_MINUTES = 8     # ventana de estabilización tras ON

# EMHASS socfinal
_SOCFINAL_PERCENTILE  = 15    # percentil SOC amanecer → socfinal conservador
_SOCFINAL_MIN         = 0.30
_SOCFINAL_MAX         = 0.80


# ══════════════════════════════════════════════════════════════════════
# Resultados
# ══════════════════════════════════════════════════════════════════════

@dataclass
class AutoTunerResults:
    """Resumen de una ejecución de auto-calibración."""
    timestamp:              str  = ""
    # Modelos térmicos
    t_base_optimal:         float | None = None
    thermal_factor_new:     float | None = None
    thermal_r2:             float        = 0.0
    # COP
    cop_a:                  float | None = None
    cop_b:                  float | None = None
    cop_c:                  float | None = None
    cop_r2:                 float        = 0.0
    cop_n_samples:          int          = 0
    # Zonas
    zone_tau:               dict[str, float]  = field(default_factory=dict)
    zone_demand_weights:    dict[str, float]  = field(default_factory=dict)
    zone_preheat_error_min: dict[str, float]  = field(default_factory=dict)
    # Cargas
    deferrable_power_w:     float | None = None
    # Batería
    battery_efficiency_rt:  float | None = None
    emhass_socfinal:        float | None = None
    # Payload Companion App
    companion_payload_size: int          = 0
    companion_sent:         bool         = False
    # Errores
    errors:                 list[str]    = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════
# AutoTuner principal
# ══════════════════════════════════════════════════════════════════════

class HAAutoTuner:
    """
    Auto-tuner HA-side. Calibra todos los parámetros calibrables
    con pure Python usando historial del recorder.

    Se instancia una vez en el coordinator y se llama diariamente:
        results = await tuner.async_run_daily_cycle()
    """

    def __init__(
        self,
        hass:       "HomeAssistant",
        dd_model:   "DegreeDaysModel",
        cop_model:  "COPModel",
        zones:      list["ThermalZone"],
        simulation_mode: bool = True,
    ) -> None:
        self._hass           = hass
        self._dd_model       = dd_model
        self._cop_model      = cop_model
        self._zones          = zones
        self._sim            = simulation_mode
        self._last_results:  AutoTunerResults | None = None

    @property
    def last_results(self) -> AutoTunerResults | None:
        return self._last_results

    # ══════════════════════════════════════════════════════════════════
    # API pública
    # ══════════════════════════════════════════════════════════════════

    async def async_run_daily_cycle(self) -> AutoTunerResults:
        """
        Ejecuta todos los módulos de calibración secuencialmente.
        No lanza excepción: captura errores individuales en results.errors.
        """
        results = AutoTunerResults(timestamp=dt_util.now().isoformat())
        _LOGGER.info("AutoTuner HA: iniciando ciclo diario [sim=%s]", self._sim)

        # 1. T_base + thermal_factor
        await self._run_safe(results, "T_base", self._tune_t_base, results)
        await self._run_safe(results, "thermal_factor", self._tune_thermal_factor, results)

        # 2. COP
        await self._run_safe(results, "COP curve", self._tune_cop_curve, results)

        # 3. Zonas
        for zone in self._zones:
            await self._run_safe(results, f"τ zona {zone.id}",
                                 self._tune_zone_tau, results, zone)
            await self._run_safe(results, f"preheat zona {zone.id}",
                                 self._tune_preheat_accuracy, results, zone)
        await self._run_safe(results, "demand_weights",
                             self._tune_demand_weights, results)

        # 4. Cargas diferibles
        await self._run_safe(results, "deferrable power",
                             self._tune_deferrable_power, results)

        # 5. Batería
        await self._run_safe(results, "battery η",
                             self._tune_battery_efficiency, results)
        await self._run_safe(results, "EMHASS socfinal",
                             self._tune_emhass_socfinal, results)

        # 6. Payload Companion App
        await self._run_safe(results, "companion payload",
                             self._build_companion_payload, results)

        self._last_results = results
        _LOGGER.info(
            "AutoTuner HA: ciclo completado | errores=%d | "
            "T_base=%.1f | TF=%.4f | COP_R²=%.3f | payload=%d bytes",
            len(results.errors),
            results.t_base_optimal or self._dd_model.t_base,
            results.thermal_factor_new or self._dd_model.thermal_factor,
            results.cop_r2,
            results.companion_payload_size,
        )
        return results

    # ══════════════════════════════════════════════════════════════════
    # 1. T_base óptimo
    # ══════════════════════════════════════════════════════════════════

    async def _tune_t_base(self, results: AutoTunerResults) -> None:
        """
        Encuentra T_base que maximiza R² del modelo Degree Days.
        Grid search sobre [14.0, 21.0] con paso 0.5 °C.
        """
        pairs = await self._get_daily_t_kwh_pairs()
        if len(pairs) < _TBASE_MIN_DAYS:
            _LOGGER.debug("T_base tune: %d días (min %d)", len(pairs), _TBASE_MIN_DAYS)
            return

        t_ext_means = [p[0] for p in pairs]
        kwh_vals    = [p[1] for p in pairs]
        best_r2     = -1.0
        best_tbase  = self._dd_model.t_base

        for t_base_cand in _TBASE_RANGE:
            dd_vals  = [max(0.0, t_base_cand - t) for t in t_ext_means]
            valid    = [(dd, kw) for dd, kw in zip(dd_vals, kwh_vals) if dd > 0.1]
            if len(valid) < 5:
                continue
            xs = [v[0] for v in valid]
            ys = [v[1] for v in valid]
            xs, ys = zip(*_zscore_filter(list(zip(xs, ys)))) if len(valid) >= 4 else (xs, ys)
            intercept, slope = _huber_fit(list(xs), list(ys))
            if not (THERMAL_FACTOR_MIN <= slope <= THERMAL_FACTOR_MAX):
                continue
            y_pred = [intercept + slope * x for x in xs]
            r2     = _r2(list(ys), y_pred)
            if r2 > best_r2:
                best_r2    = r2
                best_tbase = t_base_cand

        if best_r2 >= _TBASE_MIN_R2 and best_tbase != self._dd_model.t_base:
            _LOGGER.info(
                "T_base optimizado: %.1f → %.1f °C | R²=%.3f (n=%d días)",
                self._dd_model.t_base, best_tbase, best_r2, len(pairs),
            )
            results.t_base_optimal = best_tbase
            results.thermal_r2     = best_r2
            # Actualizar modelo si no es simulación
            if not self._sim:
                self._dd_model.t_base = best_tbase
        else:
            _LOGGER.debug(
                "T_base sin cambio: %.1f °C sigue siendo óptimo (R²=%.3f)",
                self._dd_model.t_base, best_r2,
            )

    # ══════════════════════════════════════════════════════════════════
    # 2. Thermal factor
    # ══════════════════════════════════════════════════════════════════

    async def _tune_thermal_factor(self, results: AutoTunerResults) -> None:
        """Re-calibra thermal_factor con T_base actualizado."""
        ts = await self._dd_model.fit_online()
        if ts:
            results.thermal_factor_new = self._dd_model.thermal_factor
            results.thermal_r2         = self._dd_model.r2_score

    # ══════════════════════════════════════════════════════════════════
    # 3. COP curve
    # ══════════════════════════════════════════════════════════════════

    async def _tune_cop_curve(self, results: AutoTunerResults) -> None:
        """Re-calibra curva COP = A + B·T + C·T²."""
        ts = await self._cop_model.fit_online()
        if ts:
            results.cop_a        = self._cop_model.a
            results.cop_b        = self._cop_model.b
            results.cop_c        = self._cop_model.c
            results.cop_r2       = self._cop_model.r2_score
            results.cop_n_samples = self._cop_model.n_samples

    # ══════════════════════════════════════════════════════════════════
    # 4. Constante de tiempo τ por zona (step-response)
    # ══════════════════════════════════════════════════════════════════

    async def _tune_zone_tau(
        self, results: AutoTunerResults, zone: "ThermalZone"
    ) -> None:
        """
        Estima τ de la zona analizando respuestas a escalón HVAC OFF→ON.

        Modelo: T(t) = T∞ − (T∞ − T₀) · exp(−t/τ)
        τ = −Δt / ln[(T(t) − T∞) / (T₀ − T∞)]

        T∞ estimada como setpoint objetivo de la zona.
        """
        events = await self._find_step_response_events(zone)
        if len(events) < _STEP_MIN_EVENTS:
            _LOGGER.debug(
                "Zona %s: sólo %d eventos step-response (min %d)",
                zone.id, len(events), _STEP_MIN_EVENTS,
            )
            return

        tau_estimates: list[float] = []
        for ev in events:
            t0      = ev["t_start"]
            t_end   = ev["t_end"]
            t_inf   = ev.get("t_setpoint", t0 + 3.0)
            dt_min  = ev["duration_min"]

            if dt_min < 5 or abs(t_inf - t0) < _STEP_MIN_DELTA_T:
                continue

            delta_0 = t_inf - t0
            delta_t = t_inf - t_end

            # Verificar que delta_t > 0 (calentando) y < delta_0 (no sobrepasó)
            if delta_t <= 0 or delta_t >= delta_0:
                continue

            try:
                ratio = delta_t / delta_0
                if ratio <= 0 or ratio >= 1:
                    continue
                tau_h = (-dt_min / math.log(ratio)) / 60.0
                if 0.5 <= tau_h <= 24.0:
                    tau_estimates.append(tau_h)
            except (ValueError, ZeroDivisionError):
                continue

        if not tau_estimates:
            return

        # Filtrar con IQR y tomar mediana
        from ..models.thermal_zone import _iqr_filter
        filtered = _iqr_filter(tau_estimates)
        if not filtered:
            return

        sv       = sorted(filtered)
        tau_med  = sv[len(sv) // 2]
        zone.add_tau_sample(tau_med)
        results.zone_tau[zone.id] = round(zone.tau_hours, 2)
        _LOGGER.info(
            "Zona %s: τ calibrado → %.2f h (mediana de %d eventos)",
            zone.id, tau_med, len(filtered),
        )

    async def _find_step_response_events(
        self, zone: "ThermalZone"
    ) -> list[dict]:
        """
        Busca transiciones HVAC OFF→ON y registra la respuesta
        de temperatura en la ventana siguiente (_STEP_WINDOW_MINUTES).
        """
        if not zone.climate_entity or not zone._temp_primary:
            return []

        now   = dt_util.now()
        start = now - timedelta(hours=RECORDER_HOURS_BACK)
        events: list[dict] = []

        climate_hist = await self._fetch_history_raw(zone.climate_entity, start, now)
        temp_hist    = await self._fetch_history_raw(zone._temp_primary, start, now)

        if not climate_hist or not temp_hist:
            return []

        # Lookup temperatura por minuto
        temp_by_min: dict[int, float] = {}
        for ts, val in temp_hist:
            key = int(ts.timestamp() // 60)
            temp_by_min[key] = val

        def _temp_at(dt_: datetime) -> float | None:
            k = int(dt_.timestamp() // 60)
            for dk in (0, 1, -1, 2, -2):
                if k + dk in temp_by_min:
                    return temp_by_min[k + dk]
            return None

        prev_mode = None
        for ts, mode in climate_hist:
            # Detectar OFF→ON (heat o cool)
            if (prev_mode in (None, "off")
                    and mode in ("heat", "cool", "heat_cool", "auto")):
                t0 = _temp_at(ts)
                if t0 is None:
                    prev_mode = mode
                    continue

                ts_end = ts + timedelta(minutes=_STEP_WINDOW_MINUTES)
                t_end  = _temp_at(ts_end)
                if t_end is None:
                    prev_mode = mode
                    continue

                setpoint = zone.get_setpoint()
                events.append({
                    "t_start":      t0,
                    "t_end":        t_end,
                    "t_setpoint":   setpoint or (t0 + 3.0),
                    "duration_min": _STEP_WINDOW_MINUTES,
                    "ts":           ts.isoformat(),
                    "hvac_mode":    mode,
                })
            prev_mode = mode

        return events

    # ══════════════════════════════════════════════════════════════════
    # 5. Precisión preheat (ajuste τ desde error observado)
    # ══════════════════════════════════════════════════════════════════

    async def _tune_preheat_accuracy(
        self, results: AutoTunerResults, zone: "ThermalZone"
    ) -> None:
        """
        Compara tiempo predicho de preheat con tiempo real de achievement.
        Si real >> predicho → τ subestimado → sube τ.
        Si real << predicho → τ sobreestimado → baja τ.
        """
        if not zone._temp_primary:
            return

        now   = dt_util.now()
        start = now - timedelta(hours=72)   # últimas 72h
        temp_hist = await self._fetch_history_raw(zone._temp_primary, start, now)
        if not temp_hist or len(temp_hist) < 10:
            return

        errors: list[float] = []

        for i in range(len(temp_hist) - 1):
            ts_i, t_i   = temp_hist[i]
            ts_j, t_j   = temp_hist[i + 1]
            setpoint     = zone.get_setpoint()
            if setpoint is None:
                continue

            # Buscar ventana donde T subió de t_i hasta setpoint
            if t_i < setpoint - 1.5 and t_j > t_i + 0.5:
                predicted_min = zone.get_preheat_minutes(setpoint, t_i)
                real_min      = (ts_j - ts_i).total_seconds() / 60.0
                if 5 < real_min < 120 and predicted_min > 0:
                    error_pct = (real_min - predicted_min) / predicted_min
                    errors.append(error_pct)

        if not errors:
            return

        mean_error = _mean(errors)
        results.zone_preheat_error_min[zone.id] = round(mean_error * 100, 1)

        # Si error sistemático > 20% → ajustar τ
        if abs(mean_error) > 0.20 and zone.tau_hours > 0:
            new_tau = zone.tau_hours * (1.0 + mean_error * 0.5)
            new_tau = max(0.5, min(24.0, new_tau))
            zone.add_tau_sample(new_tau)
            _LOGGER.info(
                "Zona %s: preheat error=%.0f%% → τ ajustado a %.2f h",
                zone.id, mean_error * 100, new_tau,
            )

    # ══════════════════════════════════════════════════════════════════
    # 6. Demand weights por zona
    # ══════════════════════════════════════════════════════════════════

    async def _tune_demand_weights(self, results: AutoTunerResults) -> None:
        """
        Calibra demand_weight de cada zona basándose en su demanda
        térmica real estimada.

        Proxy de demanda zona i:
          D_i = Σ_t [ max(0, SP_i(t) - T_i(t)) × activa(t) ]

        Se normaliza: w_i = D_i / Σ D_j (suma = 1.0)
        Suavizado exponencial con α=0.3 para evitar saltos bruscos.
        """
        now   = dt_util.now()
        start = now - timedelta(hours=168)   # 7 días

        zone_demands: dict[str, float] = {}
        for zone in self._zones:
            if not zone.enabled or not zone._temp_primary:
                continue
            temp_hist = await self._fetch_history_raw(zone._temp_primary, start, now)
            demand    = 0.0
            for _, t_val in temp_hist:
                sp = zone.get_setpoint()
                if sp is None:
                    continue
                deficit = max(0.0, sp - t_val)
                if zone.is_in_schedule():
                    demand += deficit
            zone_demands[zone.id] = demand

        total = sum(zone_demands.values())
        if total < 1e-3:
            return

        new_weights: dict[str, float] = {}
        for zone in self._zones:
            if zone.id not in zone_demands:
                continue
            raw_w  = zone_demands[zone.id] / total
            raw_w  = max(raw_w, _ZONE_WEIGHT_MIN)
            # Suavizado exponencial
            smoothed = (
                _ZONE_WEIGHT_ALPHA * raw_w
                + (1 - _ZONE_WEIGHT_ALPHA) * zone.demand_weight
            )
            new_weights[zone.id] = round(smoothed, 4)

        # Renormalizar a 1.0
        total_w = sum(new_weights.values())
        if total_w > 0:
            for zid in new_weights:
                new_weights[zid] = round(new_weights[zid] / total_w, 4)

        results.zone_demand_weights = new_weights

        if not self._sim:
            for zone in self._zones:
                if zone.id in new_weights:
                    zone.demand_weight = new_weights[zone.id]
            _LOGGER.info("demand_weights actualizados: %s", new_weights)

    # ══════════════════════════════════════════════════════════════════
    # 7. Potencia real bomba piscina (deferrable load)
    # ══════════════════════════════════════════════════════════════════

    async def _tune_deferrable_power(self, results: AutoTunerResults) -> None:
        """
        Mide potencia real de la bomba piscina analizando el balance
        de red en ventanas de tiempo justo antes/después del encendido.

        Método:
          ΔP = P_grid(ON) − P_grid(OFF)  en ventana de _POOL_MEASURE_MINUTES
          Válido si PV y batería estables (ΔP_pv < 100W, ΔP_batt < 200W)
        """
        now   = dt_util.now()
        start = now - timedelta(hours=RECORDER_HOURS_BACK)

        pool_hist  = await self._fetch_history_raw(_E_POOL_SWITCH, start, now)
        grid_hist  = await self._fetch_history_raw(_E_GRID, start, now)
        pv_hist    = await self._fetch_history_raw(_E_PV, start, now)

        if not pool_hist or not grid_hist:
            return

        def _avg_around(hist, ts_event, window_min, offset_min=0):
            """Media de valores en [ts + offset, ts + offset + window_min]."""
            t0 = ts_event + timedelta(minutes=offset_min)
            t1 = t0 + timedelta(minutes=window_min)
            vals = [v for ts, v in hist if t0 <= ts <= t1]
            return _mean(vals) if vals else None

        pv_lookup = {int(ts.timestamp() // 60): v for ts, v in pv_hist}

        def _pv_stable(ts_on):
            k = int(ts_on.timestamp() // 60)
            before = [pv_lookup.get(k - i) for i in range(1, 6) if k - i in pv_lookup]
            after  = [pv_lookup.get(k + i) for i in range(1, 6) if k + i in pv_lookup]
            if not before or not after:
                return True
            return abs(_mean(after) - _mean(before)) < 150.0

        delta_powers: list[float] = []
        prev_state = None
        for ts, state in pool_hist:
            if prev_state == "off" and state == "on":
                if not _pv_stable(ts):
                    prev_state = state
                    continue
                p_before = _avg_around(grid_hist, ts, _POOL_MEASURE_MINUTES, -_POOL_MEASURE_MINUTES)
                p_after  = _avg_around(grid_hist, ts, _POOL_MEASURE_MINUTES, 1)
                if p_before is not None and p_after is not None:
                    delta = abs(p_after - p_before)
                    if 200.0 <= delta <= 1_500.0:   # rango físico bomba piscina
                        delta_powers.append(delta)
            prev_state = state

        if len(delta_powers) < 3:
            _LOGGER.debug(
                "Deferrable power: sólo %d mediciones válidas", len(delta_powers)
            )
            return

        # Mediana robusta
        sv     = sorted(delta_powers)
        median = sv[len(sv) // 2]
        results.deferrable_power_w = round(median, 0)
        _LOGGER.info(
            "Bomba piscina: potencia real medida = %.0f W "
            "(mediana de %d eventos, config actual 650 W)",
            median, len(delta_powers),
        )

    # ══════════════════════════════════════════════════════════════════
    # 8. Eficiencia round-trip batería
    # ══════════════════════════════════════════════════════════════════

    async def _tune_battery_efficiency(self, results: AutoTunerResults) -> None:
        """
        Mide eficiencia round-trip real de la batería.

        η_rt = kWh_descargados / kWh_cargados  para ciclos completos.

        Ciclo completo:
          Carga: SOC sube de SOC_lo hasta SOC_hi  → integrar P_batt > 0
          Descarga: SOC baja de SOC_hi a SOC_lo   → integrar P_batt < 0

        Filtra ciclos incompletos o con interrupciones.
        """
        now   = dt_util.now()
        start = now - timedelta(hours=RECORDER_HOURS_BACK)

        soc_hist  = await self._fetch_history_raw(_E_SOC, start, now)
        grid_hist = await self._fetch_history_raw(_E_GRID, start, now)
        pv_hist   = await self._fetch_history_raw(_E_PV, start, now)

        if len(soc_hist) < 20:
            return

        # Calcular potencia batería por balance (P_batt ≈ P_pv − P_house − P_grid)
        house_hist = await self._fetch_history_raw(_E_HOUSE_LOAD, start, now)

        def _make_lookup(hist):
            return {int(ts.timestamp() // 60): v for ts, v in hist}

        pv_lkp    = _make_lookup(pv_hist)
        grid_lkp  = _make_lookup(grid_hist)
        house_lkp = _make_lookup(house_hist)

        def _batt_power(ts):
            k = int(ts.timestamp() // 60)
            pv    = pv_lkp.get(k)
            grid  = grid_lkp.get(k)
            house = house_lkp.get(k)
            if None in (pv, grid, house):
                return None
            # P_batt = P_pv − P_house − P_grid  (positivo = carga)
            return pv - house - grid

        # Detectar ciclos por SOC
        efficiency_samples: list[float] = []
        charge_kwh = 0.0
        discharge_kwh = 0.0
        in_charge  = False
        soc_peak   = 0.0

        for i in range(1, len(soc_hist)):
            ts_prev, soc_prev = soc_hist[i - 1]
            ts_curr, soc_curr = soc_hist[i]
            dt_h = (ts_curr - ts_prev).total_seconds() / 3600.0
            if dt_h <= 0 or dt_h > 0.5:   # ignora gaps > 30 min
                continue

            p = _batt_power(ts_curr)
            if p is None:
                continue

            if p > 100 and soc_curr < _BATT_CYCLE_SOC_MAX:
                in_charge  = True
                charge_kwh += p * dt_h / 1000.0
                soc_peak    = max(soc_peak, soc_curr)

            elif p < -100 and in_charge and soc_curr < soc_peak - 10:
                discharge_kwh += abs(p) * dt_h / 1000.0

                if soc_curr <= _BATT_CYCLE_SOC_MIN and discharge_kwh > 0.5:
                    η = discharge_kwh / charge_kwh if charge_kwh > 0 else None
                    if η and 0.80 <= η <= 0.99:
                        efficiency_samples.append(η)
                    charge_kwh    = 0.0
                    discharge_kwh = 0.0
                    in_charge     = False
                    soc_peak      = 0.0

        if not efficiency_samples:
            return

        sv  = sorted(efficiency_samples)
        eta = sv[len(sv) // 2]
        results.battery_efficiency_rt = round(eta, 4)
        _LOGGER.info(
            "Batería η round-trip: %.3f (%.1f%%) | %d ciclos completos",
            eta, eta * 100, len(sv),
        )

    # ══════════════════════════════════════════════════════════════════
    # 9. EMHASS socfinal óptimo
    # ══════════════════════════════════════════════════════════════════

    async def _tune_emhass_socfinal(self, results: AutoTunerResults) -> None:
        """
        Optimiza socfinal de EMHASS basándose en el SOC al amanecer
        histórico y el precio nocturno.

        Estrategia:
          socfinal = percentil_15(SOC_amanecer)
          Si precio nocturno es caro → sube socfinal (más reserva)
          Si precio nocturno es barato → baja socfinal (carga más barato)

        Rango: [0.30, 0.80]
        """
        now   = dt_util.now()
        start = now - timedelta(hours=RECORDER_HOURS_BACK)

        soc_hist   = await self._fetch_history_raw(_E_SOC, start, now)
        price_hist = await self._fetch_history_raw(_E_PRICE, start, now)

        if len(soc_hist) < 48:
            return

        # SOC entre 06:00 y 09:00 (amanecer)
        dawn_soc: list[float] = []
        for ts, soc in soc_hist:
            if 6 <= ts.hour < 9:
                dawn_soc.append(soc)

        if len(dawn_soc) < 5:
            return

        sv              = sorted(dawn_soc)
        n               = len(sv)
        idx             = max(0, int(n * _SOCFINAL_PERCENTILE / 100))
        soc_p15         = sv[idx]
        socfinal_base   = soc_p15 / 100.0   # normalizar a [0,1]

        # Ajuste por precio nocturno medio
        night_prices: list[float] = []
        for ts, price in price_hist:
            if ts.hour >= 22 or ts.hour < 7:
                night_prices.append(price)
        night_avg = _mean(night_prices) if night_prices else 0.12

        # Si noche cara (> 0.15) → guardar más reserva, si barata → menos
        adjustment   = (night_avg - 0.12) * 2.0     # ±0.06 por €/kWh de diferencia
        socfinal_adj = socfinal_base + adjustment
        socfinal_adj = max(_SOCFINAL_MIN, min(_SOCFINAL_MAX, socfinal_adj))

        results.emhass_socfinal = round(socfinal_adj, 2)
        _LOGGER.info(
            "EMHASS socfinal → %.2f | SOC amanecer P15=%.1f%% | "
            "precio noche avg=%.3f €/kWh | ajuste=%.3f",
            socfinal_adj, soc_p15, night_avg, adjustment,
        )

    # ══════════════════════════════════════════════════════════════════
    # 10. Payload Companion App
    # ══════════════════════════════════════════════════════════════════

    async def _build_companion_payload(self, results: AutoTunerResults) -> None:
        """
        Construye paquete de datos rico para entrenamiento RC/ML en
        la Companion App. Incluye historial completo + eventos
        step-response + parámetros HA-side ya calibrados.

        El coordinator envía este payload via CompanionClient.async_thermal_train().
        """
        now   = dt_util.now()
        start = now - timedelta(hours=RECORDER_HOURS_BACK)

        # Historial sensores globales
        t_ext_hist   = await self._fetch_history_raw(_E_TEMP_EXT,  start, now)
        corriente_hist = await self._fetch_history_raw(_E_CORRIENTE, start, now)
        caudal_hist  = await self._fetch_history_raw(_E_CAUDAL,    start, now)
        imp_hist     = await self._fetch_history_raw(_E_IMPULSION, start, now)
        ret_hist     = await self._fetch_history_raw(_E_RETORNO,   start, now)
        cop_hist     = await self._fetch_history_raw(_E_COP,       start, now)
        soc_hist     = await self._fetch_history_raw(_E_SOC,       start, now)
        pv_hist      = await self._fetch_history_raw(_E_PV,        start, now)

        def _ser(hist):
            return [(ts.isoformat(), round(v, 3)) for ts, v in hist]

        # Historial por zona
        zone_temps = {}
        for zone in self._zones:
            if zone._temp_primary:
                zh = await self._fetch_history_raw(zone._temp_primary, start, now)
                zone_temps[zone.id] = _ser(zh)

        # Step-response events para todas las zonas
        step_events_all = {}
        for zone in self._zones:
            ev = await self._find_step_response_events(zone)
            if ev:
                step_events_all[zone.id] = ev

        payload = {
            "version":      "0.4.0",
            "ts_built":     now.isoformat(),
            "hours_history": RECORDER_HOURS_BACK,
            # Series temporales
            "series": {
                "t_ext":       _ser(t_ext_hist),
                "corriente":   _ser(corriente_hist),
                "caudal":      _ser(caudal_hist),
                "t_impulsion": _ser(imp_hist),
                "t_retorno":   _ser(ret_hist),
                "cop":         _ser(cop_hist),
                "soc":         _ser(soc_hist),
                "pv_power":    _ser(pv_hist),
                "zones":       zone_temps,
            },
            # Parámetros ya calibrados HA-side
            "ha_calibrated": {
                "t_base":          results.t_base_optimal or self._dd_model.t_base,
                "thermal_factor":  results.thermal_factor_new or self._dd_model.thermal_factor,
                "cop_a":           results.cop_a or self._cop_model.a,
                "cop_b":           results.cop_b or self._cop_model.b,
                "cop_c":           results.cop_c or self._cop_model.c,
                "zone_tau":        results.zone_tau,
                "demand_weights":  results.zone_demand_weights,
                "battery_eta":     results.battery_efficiency_rt,
                "emhass_socfinal": results.emhass_socfinal,
            },
            # Eventos step-response (para RC gray-box)
            "step_events": step_events_all,
        }

        import json
        payload_json            = json.dumps(payload)
        results.companion_payload_size = len(payload_json)
        # El coordinator accede a este payload via autotuner.last_results
        self._last_payload = payload
        _LOGGER.info(
            "Companion payload construido: %d bytes | "
            "series: t_ext=%d, corriente=%d, cop=%d | step_events zonas=%d",
            results.companion_payload_size,
            len(t_ext_hist), len(corriente_hist), len(cop_hist),
            len(step_events_all),
        )

    @property
    def last_companion_payload(self) -> dict | None:
        return getattr(self, "_last_payload", None)

    # ══════════════════════════════════════════════════════════════════
    # Helpers
    # ══════════════════════════════════════════════════════════════════

    async def _get_daily_t_kwh_pairs(self) -> list[tuple[float, float]]:
        """
        Construye pares (T_ext_media_diaria, kWh_HVAC_diario)
        para el análisis de T_base.
        Reutiliza la lógica de DegreeDaysModel pero sin filtro DD.
        """
        return await self._dd_model._build_daily_pairs()

    async def _fetch_history_raw(
        self, entity_id: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, float]]:
        """
        Obtiene historial del recorder.
        Retorna lista de (datetime_local, float).
        """
        if not entity_id:
            return []
        results = []
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import get_significant_states

            instance  = get_instance(self._hass)
            state_map = await instance.async_add_executor_job(
                get_significant_states,
                self._hass, start, end, [entity_id], None, True,
            )
            for s in state_map.get(entity_id, []):
                if s.state in ("unknown", "unavailable", "none", ""):
                    continue
                val = _safe_float(s.state)
                if val is not None:
                    ts = dt_util.as_local(s.last_updated or end)
                    results.append((ts, val))
        except Exception as exc:
            _LOGGER.debug("AutoTuner _fetch_history_raw (%s): %s", entity_id, exc)

        return sorted(results, key=lambda x: x[0])

    @staticmethod
    async def _run_safe(
        results: AutoTunerResults,
        name: str,
        coro_fn,
        *args,
    ) -> None:
        """Ejecuta una corutina capturando cualquier excepción."""
        try:
            await coro_fn(*args)
        except Exception as exc:
            msg = f"{name}: {exc}"
            results.errors.append(msg)
            _LOGGER.warning("AutoTuner [%s] error: %s", name, exc)
