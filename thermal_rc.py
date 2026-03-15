"""RC Gray-Box Thermal Model (2R1C) — Companion App v0.4.0.

Modelo paramétrico de primer orden para la envolvente del edificio.

Ecuación de estado continua:
  C · dT_in/dt = (T_ext - T_in) / R_env + P_hvac

Donde:
  C      [J/K]  — capacitancia térmica del edificio (masa + aire)
  R_env  [K/W]  — resistencia térmica envolvente (inverso de UA)
  T_in   [°C]   — temperatura interior media
  T_ext  [°C]   — temperatura exterior
  P_hvac [W]    — potencia calor/frío entregada

Identificación de parámetros:
  Scipy L-BFGS-B minimize sobre residuos T_in_model vs T_in_medida.
  Bootstrap ×50 para intervalos de confianza.

Predicción de potencia requerida:
  P_req(t) = (T_setpoint - T_ext(t)) / R_env   (steady-state)
  Ajuste dinámico si C es grande (edificio con mucha inercia).
"""
from __future__ import annotations

import logging
import math
import os
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np
from scipy.optimize import minimize

_LOGGER    = logging.getLogger(__name__)
DATA_DIR   = Path(os.getenv("DATA_DIR", "/app/data"))
MODEL_PATH = DATA_DIR / "rc_model.pkl"

# Límites físicos del edificio
_R_MIN, _R_MAX = 1e2,  5e5    # K/W — resistencia mínima (muy bien aislado) / máxima
_C_MIN, _C_MAX = 1e5,  1e9    # J/K — masa térmica mínima / máxima casa grande
_DT_S          = 3_600         # paso de tiempo 1 hora en segundos


# ══════════════════════════════════════════════════════════════════════
# Parámetros RC
# ══════════════════════════════════════════════════════════════════════

@dataclass
class RCParams:
    R:          float = 2_000.0   # K/W — valor inicial razonable casa mediana
    C:          float = 8_000_000.0  # J/K
    r2:         float = 0.0
    n_samples:  int   = 0
    ci_R_lo:    float = 0.0
    ci_R_hi:    float = 0.0
    ci_C_lo:    float = 0.0
    ci_C_hi:    float = 0.0
    fitted:     bool  = False

    @property
    def tau_hours(self) -> float:
        """τ = R·C en horas."""
        return (self.R * self.C) / 3_600.0

    @property
    def ua(self) -> float:
        """UA [W/K] — coeficiente global de pérdidas."""
        return 1.0 / self.R if self.R > 0 else 0.0


# ══════════════════════════════════════════════════════════════════════
# Simulación RC
# ══════════════════════════════════════════════════════════════════════

def simulate_rc(
    R: float,
    C: float,
    t_ext: Sequence[float],
    p_hvac: Sequence[float],
    t0: float,
    dt_s: float = _DT_S,
) -> list[float]:
    """
    Integra el modelo 2R1C con método Euler implícito (estable).

    T[k+1] = (T[k] + dt/C × (T_ext[k]/R + P_hvac[k])) /
             (1 + dt/(R·C))
    """
    rc   = R * C
    a    = dt_s / rc                    # τ_normalizado
    T    = [t0]
    for i in range(len(t_ext) - 1):
        T_new = (T[-1] + a * t_ext[i] + dt_s / C * p_hvac[i]) / (1.0 + a)
        T.append(T_new)
    return T


def predict_power_rc(
    params: RCParams,
    t_ext_fc: Sequence[float],
    t_setpoint: float,
    t_current:  float,
    dt_s: float = _DT_S,
) -> list[float]:
    """
    Predice potencia HVAC requerida hora a hora para mantener T_setpoint.

    Método:
      P_hvac(t) = C/dt × (T_sp - T_sim[t]) + (T_sim[t] - T_ext[t]) / R
      donde T_sim[t] es la simulación con el modelo RC.
    """
    R, C = params.R, params.C
    a    = dt_s / (R * C)
    T    = t_current
    powers: list[float] = []
    for t_e in t_ext_fc:
        # Potencia necesaria para llevar T → T_setpoint en este slot
        p_req = C / dt_s * (t_setpoint - T) + (T - t_e) / R
        p_req = max(0.0, p_req)   # no enfriamos (modo calefacción)
        # Simular siguiente estado con esa potencia
        T = (T + a * t_e + dt_s / C * p_req) / (1.0 + a)
        powers.append(round(p_req, 1))
    return powers


# ══════════════════════════════════════════════════════════════════════
# Identificación de parámetros
# ══════════════════════════════════════════════════════════════════════

def _objective(
    log_params: np.ndarray,
    T_meas: np.ndarray,
    T_ext:  np.ndarray,
    P_hvac: np.ndarray,
    dt_s:   float,
) -> float:
    """Función objetivo: RMSE(T_model, T_medida) en espacio log."""
    R = math.exp(log_params[0])
    C = math.exp(log_params[1])
    T_model = simulate_rc(R, C, T_ext, P_hvac, T_meas[0], dt_s)
    residuals = T_meas - np.array(T_model)
    return float(np.mean(residuals ** 2))


def fit_rc(
    T_in_meas:  list[float],
    T_ext_data: list[float],
    P_hvac_data: list[float] | None = None,
    dt_s:       float = _DT_S,
    n_bootstrap: int  = 50,
) -> RCParams:
    """
    Identifica R y C usando L-BFGS-B en espacio logarítmico.
    Bootstrap para intervalos de confianza.

    Retorna RCParams con parámetros, R² y CIs.
    """
    n = min(len(T_in_meas), len(T_ext_data))
    if n < 24:
        _LOGGER.warning("RC fit: sólo %d muestras (mínimo 24)", n)
        return RCParams()

    T_m = np.array(T_in_meas[:n])
    T_e = np.array(T_ext_data[:n])
    P_h = np.array(P_hvac_data[:n]) if P_hvac_data else np.zeros(n)

    # Estimación inicial usando análisis de autovalores
    R0  = (T_e.mean() - T_m.mean()) / max(P_h.mean(), 1.0) if P_h.mean() > 10 else 2_000.0
    tau0 = _estimate_tau(T_m, dt_s)
    C0  = (tau0 * 3_600) / max(R0, 1.0)
    R0  = max(_R_MIN, min(_R_MAX, R0))
    C0  = max(_C_MIN, min(_C_MAX, C0))

    x0 = np.array([math.log(R0), math.log(C0)])
    bounds = [(math.log(_R_MIN), math.log(_R_MAX)),
              (math.log(_C_MIN), math.log(_C_MAX))]

    result = minimize(
        _objective, x0,
        args=(T_m, T_e, P_h, dt_s),
        method="L-BFGS-B",
        bounds=bounds,
        options={"maxiter": 500, "ftol": 1e-12},
    )

    R_opt = math.exp(result.x[0])
    C_opt = math.exp(result.x[1])

    T_fit  = np.array(simulate_rc(R_opt, C_opt, T_e, P_h, T_m[0], dt_s))
    ss_res = float(np.sum((T_m - T_fit) ** 2))
    ss_tot = float(np.sum((T_m - T_m.mean()) ** 2))
    r2     = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    # Bootstrap CIs
    R_boot, C_boot = _bootstrap_rc(T_m, T_e, P_h, dt_s, n_bootstrap)

    params = RCParams(
        R         = round(R_opt,  1),
        C         = round(C_opt,  0),
        r2        = round(r2,     4),
        n_samples = n,
        ci_R_lo   = round(float(np.percentile(R_boot, 5)),  1),
        ci_R_hi   = round(float(np.percentile(R_boot, 95)), 1),
        ci_C_lo   = round(float(np.percentile(C_boot, 5)),  0),
        ci_C_hi   = round(float(np.percentile(C_boot, 95)), 0),
        fitted    = r2 > 0.50,
    )
    _LOGGER.info(
        "RC fit ✓ | R=%.0f K/W | C=%.0f J/K | τ=%.1fh | R²=%.3f | n=%d",
        params.R, params.C, params.tau_hours, r2, n,
    )
    return params


def _estimate_tau(T_series: np.ndarray, dt_s: float) -> float:
    """Estimación rápida de τ [h] por análisis de autocorrelación."""
    if len(T_series) < 4:
        return 2.0
    ac = np.correlate(T_series - T_series.mean(), T_series - T_series.mean(), mode="full")
    ac = ac[len(ac) // 2:]
    ac_norm = ac / ac[0]
    # Buscar primer índice donde autocorr < 1/e
    for i, v in enumerate(ac_norm):
        if v <= 1 / math.e:
            return max(0.5, (i * dt_s) / 3_600.0)
    return 2.0


def _bootstrap_rc(
    T_m: np.ndarray,
    T_e: np.ndarray,
    P_h: np.ndarray,
    dt_s: float,
    n_iter: int,
) -> tuple[list[float], list[float]]:
    n        = len(T_m)
    R_boots, C_boots = [], []
    rng = np.random.default_rng(42)
    bounds = [(math.log(_R_MIN), math.log(_R_MAX)),
              (math.log(_C_MIN), math.log(_C_MAX))]
    for _ in range(n_iter):
        idx = rng.integers(0, n, n)
        T_b = T_m[idx]; T_eb = T_e[idx]; P_b = P_h[idx]
        try:
            r = minimize(
                _objective,
                np.array([math.log(2000.0), math.log(8e6)]),
                args=(T_b, T_eb, P_b, dt_s),
                method="L-BFGS-B", bounds=bounds,
                options={"maxiter": 100},
            )
            R_boots.append(math.exp(r.x[0]))
            C_boots.append(math.exp(r.x[1]))
        except Exception:
            pass
    if not R_boots:
        R_boots = [2000.0]; C_boots = [8e6]
    return R_boots, C_boots


# ══════════════════════════════════════════════════════════════════════
# Persistencia
# ══════════════════════════════════════════════════════════════════════

def save_params(params: RCParams) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(params, f)


def load_params() -> RCParams:
    if MODEL_PATH.exists():
        try:
            with open(MODEL_PATH, "rb") as f:
                return pickle.load(f)
        except Exception as exc:
            _LOGGER.warning("RC load_params: %s", exc)
    return RCParams()
