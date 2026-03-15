"""AutoTuner Companion — Companion App v0.4.0.

AutoTuner avanzado con scipy. Coordina RC + ML y expone resultados
al endpoint /autotuner/run de la API.

Funciones:
  1. Identificación RC full (R, C, τ) con datos de payload HA
  2. Entrenamiento ML con validación cruzada
  3. Ensemble RC + ML con pesos adaptativos por R²
  4. Extracción τ por zona desde step-response events
  5. Recomendaciones EMHASS (socfinal, buffer tank, T_base)
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from .ml_forecaster import MLForecaster
from .thermal_rc import RCParams, fit_rc, load_params, save_params

_LOGGER = logging.getLogger(__name__)

# Pesos ensemble por defecto
_W_RC_DEFAULT  = 0.55
_W_ML_DEFAULT  = 0.45
_MIN_R2_RC     = 0.50
_MIN_R2_ML     = 0.40
_CONF_HIGH     = 85
_CONF_MED      = 65
_CONF_LOW      = 45


@dataclass
class AutoTunerCompanionResults:
    """Resultados de un ciclo de auto-calibración Companion App."""
    rc_params:         RCParams     = field(default_factory=RCParams)
    ml_r2:             float        = 0.0
    ml_n_samples:      int          = 0
    ensemble_w_rc:     float        = _W_RC_DEFAULT
    ensemble_w_ml:     float        = _W_ML_DEFAULT
    model_active:      str          = "degree_days"
    confidence:        int          = _CONF_LOW
    zone_tau:          dict[str, float] = field(default_factory=dict)
    recommendations:   dict[str, Any]  = field(default_factory=dict)
    errors:            list[str]       = field(default_factory=list)
    ts:                str             = ""


class CompanionAutoTuner:
    """
    Orquestador de modelos RC + ML para la Companion App.

    Instanciado una vez en api_server.py y reutilizado entre requests.
    """

    def __init__(self) -> None:
        self.rc_params  = load_params()
        self.ml_model   = MLForecaster.load()
        self._w_rc      = _W_RC_DEFAULT
        self._w_ml      = _W_ML_DEFAULT

    # ══════════════════════════════════════════════════════════════════
    # Health check
    # ══════════════════════════════════════════════════════════════════

    def health(self) -> dict:
        active = self._active_model_name()
        return {
            "status":      "ok",
            "version":     "0.4.0",
            "model_active": active,
            "rc_fitted":   self.rc_params.fitted,
            "ml_fitted":   self.ml_model.fitted,
            "rc_r2":       self.rc_params.r2,
            "ml_r2":       self.ml_model.r2,
            "rc_tau_h":    round(self.rc_params.tau_hours, 2),
            "rc_ua":       round(self.rc_params.ua, 3),
        }

    # ══════════════════════════════════════════════════════════════════
    # Predicción ensemble
    # ══════════════════════════════════════════════════════════════════

    def predict(
        self,
        t_ext_fc:   list[float],
        t_setpoint: float,
        t_current:  float,
        solar_fc:   list[float] | None = None,
        t_base:     float = 18.5,
    ) -> dict:
        """
        Predicción ensemble RC + ML con pesos adaptativos.

        Retorna dict con:
          forecast_w:   lista de 48 valores [W]
          confidence:   int [0-100]
          model_active: str
        """
        from .thermal_rc import predict_power_rc

        fc_rc  = predict_power_rc(self.rc_params, t_ext_fc, t_setpoint, t_current)
        fc_ml  = self.ml_model.predict(t_ext_fc, solar_fc)

        # Ensemble ponderado adaptativo
        w_rc, w_ml = self._ensemble_weights()
        n   = len(t_ext_fc)
        fc_rc  = fc_rc[:n] + [0.0] * max(0, n - len(fc_rc))
        fc_ml  = fc_ml[:n] + [0.0] * max(0, n - len(fc_ml))

        if not self.rc_params.fitted and not self.ml_model.fitted:
            # Fallback: DD lineal
            forecast = [max(0.0, t_base - t) * 150.0 for t in t_ext_fc]
            model    = "degree_days"
            conf     = _CONF_LOW
        elif not self.rc_params.fitted:
            forecast = fc_ml
            model    = "ml_model"
            conf     = _CONF_MED if self.ml_model.r2 > 0.60 else _CONF_LOW
        elif not self.ml_model.fitted:
            forecast = fc_rc
            model    = "rc_model"
            conf     = _CONF_HIGH if self.rc_params.r2 > 0.80 else _CONF_MED
        else:
            forecast = [w_rc * r + w_ml * m for r, m in zip(fc_rc, fc_ml)]
            model    = "rc_ml_ensemble"
            conf     = self._ensemble_confidence()

        return {
            "forecast_w":   [round(v, 1) for v in forecast],
            "confidence":   conf,
            "model_active": model,
            "w_rc":         round(w_rc, 3),
            "w_ml":         round(w_ml, 3),
        }

    # ══════════════════════════════════════════════════════════════════
    # Entrenamiento
    # ══════════════════════════════════════════════════════════════════

    def train(
        self,
        payload: dict,
    ) -> AutoTunerCompanionResults:
        """
        Entrena RC + ML desde el payload enviado por HAAutoTuner.

        payload estructura:
          series: {t_ext, corriente, caudal, t_impulsion, t_retorno,
                   cop, soc, pv_power, zones: {zone_id: [[ts, val], ...]}}
          ha_calibrated: {t_base, thermal_factor, cop_a/b/c, zone_tau,
                          demand_weights, battery_eta, emhass_socfinal}
          step_events: {zone_id: [{t_start, t_end, t_setpoint, duration_min}]}
        """
        results = AutoTunerCompanionResults(ts=datetime.now().isoformat())
        series  = payload.get("series", {})
        ha_cal  = payload.get("ha_calibrated", {})

        t_base  = float(ha_cal.get("t_base", 18.5))

        # 1. Extraer series temporales
        t_ext_raw   = [v for _, v in series.get("t_ext", [])]
        corriente   = [v for _, v in series.get("corriente", [])]
        caudal_raw  = [v for _, v in series.get("caudal", [])]
        t_imp_raw   = [v for _, v in series.get("t_impulsion", [])]
        t_ret_raw   = [v for _, v in series.get("t_retorno", [])]

        # 2. Calcular potencia térmica real = m_dot × Cp × ΔT
        #    Q [W] = ρ × Q_vol/60 × Cp_agua × (T_imp - T_ret)
        #    ρ=1000 kg/m³, Cp=4186 J/(kg·K), Q_vol en L/min
        n = min(len(t_ext_raw), len(t_imp_raw), len(t_ret_raw), len(caudal_raw))
        if n >= 24:
            p_thermal = []
            for i in range(n):
                q_lpm  = float(caudal_raw[i]) if i < len(caudal_raw) else 10.0
                t_imp  = float(t_imp_raw[i])
                t_ret  = float(t_ret_raw[i])
                q_w    = (q_lpm / 60.0) * 1000 * 4186 * max(0.0, t_imp - t_ret)
                p_thermal.append(round(q_w, 1))
        else:
            # Fallback: corriente → potencia estimada (COP ~3)
            _LOGGER.info("train: usando corriente×230V/COP como proxy potencia térmica")
            p_thermal = [v * 230.0 * 3.0 for v in corriente[:len(t_ext_raw)]]

        # 3. Entrenar RC
        if len(t_ext_raw) >= 24:
            results.rc_params = fit_rc(
                T_in_meas   = _smooth(p_thermal, 3),   # T_in proxy no aplicable
                T_ext_data  = t_ext_raw,
                P_hvac_data = p_thermal,
            )
            save_params(results.rc_params)
            self.rc_params = results.rc_params
        else:
            results.errors.append(f"RC train: n={len(t_ext_raw)} < 24")

        # 4. Entrenar ML
        if len(t_ext_raw) >= 48:
            ts_list = [datetime.fromisoformat(ts)
                       for ts, _ in series.get("t_ext", [])
                       if ts]
            ml_r2 = self.ml_model.fit(
                t_ext      = t_ext_raw,
                power_w    = p_thermal,
                solar      = [v for _, v in series.get("pv_power", [])],
                timestamps = ts_list or None,
                t_base     = t_base,
            )
            results.ml_r2      = ml_r2
            results.ml_n_samples = len(t_ext_raw)
            self.ml_model.save()
        else:
            results.errors.append(f"ML train: n={len(t_ext_raw)} < 48")

        # 5. Actualizar pesos ensemble
        w_rc, w_ml = self._ensemble_weights()
        results.ensemble_w_rc = w_rc
        results.ensemble_w_ml = w_ml
        results.model_active  = self._active_model_name()
        results.confidence    = self._ensemble_confidence()

        # 6. Extraer τ por zona desde step_events
        step_events = payload.get("step_events", {})
        for zone_id, events in step_events.items():
            tau = self._extract_tau_from_events(events)
            if tau:
                results.zone_tau[zone_id] = round(tau, 3)

        # 7. Recomendaciones
        results.recommendations = self._build_recommendations(
            rc   = results.rc_params,
            ha   = ha_cal,
            tau  = results.zone_tau,
        )

        _LOGGER.info(
            "train ✓ | RC R²=%.3f | ML R²=%.3f | τ global=%.1fh | "
            "modelo=%s | conf=%d%%",
            results.rc_params.r2, results.ml_r2,
            results.rc_params.tau_hours, results.model_active, results.confidence,
        )
        return results

    # ══════════════════════════════════════════════════════════════════
    # Extracción τ desde step-response
    # ══════════════════════════════════════════════════════════════════

    def _extract_tau_from_events(self, events: list[dict]) -> float | None:
        """
        Ajuste no lineal τ por mínimos cuadrados sobre eventos step-response.
        T(t) = T_inf - (T_inf - T_0) × exp(-t/τ)
        """
        tau_estimates: list[float] = []
        for ev in events:
            t0   = float(ev.get("t_start",   0.0))
            t_end= float(ev.get("t_end",     0.0))
            t_sp = float(ev.get("t_setpoint",t0 + 3.0))
            dt_h = float(ev.get("duration_min", 90)) / 60.0

            delta_0 = t_sp - t0
            delta_t = t_sp - t_end
            if delta_0 <= 0.5 or delta_t <= 0 or delta_t >= delta_0:
                continue
            try:
                tau_h = -dt_h / math.log(delta_t / delta_0)
                if 0.3 <= tau_h <= 24.0:
                    tau_estimates.append(tau_h)
            except (ValueError, ZeroDivisionError):
                pass

        if not tau_estimates:
            return None
        sv = sorted(tau_estimates)
        return sv[len(sv) // 2]   # mediana

    # ══════════════════════════════════════════════════════════════════
    # Recomendaciones automáticas
    # ══════════════════════════════════════════════════════════════════

    def _build_recommendations(
        self,
        rc:  RCParams,
        ha:  dict,
        tau: dict,
    ) -> dict[str, Any]:
        recs: dict[str, Any] = {}

        # UA (pérdidas) — comparar con estimado DD
        if rc.fitted:
            recs["ua_wk"]      = round(rc.ua, 2)
            recs["tau_global_h"] = round(rc.tau_hours, 2)
            # Si UA > 50 W/K → casa mal aislada → recomendar más inercia
            if rc.ua > 50:
                recs["insulation_alert"] = (
                    f"UA={rc.ua:.1f} W/K — pérdidas elevadas. "
                    "Considera mejora de aislamiento o doble vidrio."
                )
            # Buffer tank óptimo según C
            liters_optimal = rc.C / (4186 * 1000 * 30)   # ΔT=30°C
            recs["buffer_tank_liters_optimal"] = round(liters_optimal, 0)

        # T_base desde RC
        if rc.fitted and rc.ua > 0:
            # T_base óptima ≈ T_interior_deseada - Q_int/UA
            q_int_estimate = 500.0   # W — ganancias internas típicas
            recs["t_base_rc"] = round(20.0 - q_int_estimate / rc.ua, 1)

        # Tau zonas
        if tau:
            recs["zone_tau"]       = tau
            max_tau_zone = max(tau, key=tau.get)
            recs["slowest_zone"]   = f"{max_tau_zone} (τ={tau[max_tau_zone]:.2f}h) → preheat anticipado"

        # socfinal desde HA
        if "emhass_socfinal" in ha:
            recs["emhass_socfinal"] = ha["emhass_socfinal"]

        return recs

    # ══════════════════════════════════════════════════════════════════
    # Helpers
    # ══════════════════════════════════════════════════════════════════

    def _ensemble_weights(self) -> tuple[float, float]:
        rc_ok = self.rc_params.fitted
        ml_ok = self.ml_model.fitted
        if rc_ok and ml_ok:
            r2_rc  = max(0.0, self.rc_params.r2)
            r2_ml  = max(0.0, self.ml_model.r2)
            total  = r2_rc + r2_ml
            if total > 0:
                w_rc = r2_rc / total
                w_ml = r2_ml / total
                return round(w_rc, 3), round(w_ml, 3)
        if rc_ok:
            return 1.0, 0.0
        if ml_ok:
            return 0.0, 1.0
        return 0.5, 0.5

    def _active_model_name(self) -> str:
        w_rc, w_ml = self._ensemble_weights()
        rc_ok = self.rc_params.fitted
        ml_ok = self.ml_model.fitted
        if rc_ok and ml_ok:
            return "rc_ml_ensemble"
        if rc_ok:
            return "rc_model"
        if ml_ok:
            return "ml_model"
        return "degree_days"

    def _ensemble_confidence(self) -> int:
        w_rc, w_ml = self._ensemble_weights()
        rc_ok = self.rc_params.fitted and self.rc_params.r2 >= _MIN_R2_RC
        ml_ok = self.ml_model.fitted and self.ml_model.r2 >= _MIN_R2_ML
        if rc_ok and ml_ok:
            weighted_r2 = w_rc * self.rc_params.r2 + w_ml * self.ml_model.r2
            return min(95, int(weighted_r2 * 100))
        if rc_ok:
            return min(80, int(self.rc_params.r2 * 90))
        if ml_ok:
            return min(70, int(self.ml_model.r2 * 80))
        return _CONF_LOW


def _smooth(values: list[float], window: int) -> list[float]:
    """Moving average para suavizar señal ruidosa."""
    out = []
    for i in range(len(values)):
        lo  = max(0, i - window // 2)
        hi  = min(len(values), i + window // 2 + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out
