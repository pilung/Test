"""ML Thermal Forecaster — Companion App v0.4.0.

Modelo ML para predicción de carga térmica 48h.

Pipeline:
  Features → StandardScaler → PolynomialFeatures(2) → Ridge(α=tuned)

Features de entrada:
  • T_ext           — temperatura exterior actual
  • T_ext_lag1/3/6  — retardos temperatura (inercia)
  • delta_T_ext     — gradiente temperatura
  • hour_sin/cos    — hora del día codificada armónicamente
  • day_sin/cos     — día de la semana
  • solar_fc        — forecast irradiancia FV
  • T_base_dd       — temperatura base grados día

Target: P_thermal [W] — potencia térmica requerida

Ventajas sobre DD puro:
  • Captura inercia térmica (retardos)
  • Captura efectos hora/día
  • Adapta a cambios estacionales automáticamente
"""
from __future__ import annotations

import logging
import math
import os
import pickle
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

import numpy as np
from sklearn.linear_model import Ridge, RidgeCV
from sklearn.metrics import r2_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import PolynomialFeatures, StandardScaler

_LOGGER    = logging.getLogger(__name__)
DATA_DIR   = Path(os.getenv("DATA_DIR", "/app/data"))
MODEL_PATH = DATA_DIR / "ml_forecaster.pkl"

_POLY_DEGREE = 2
_ALPHAS      = [0.01, 0.1, 1.0, 10.0, 100.0]
_MIN_SAMPLES = 48   # mínimo 48 h de historial para entrenar


# ══════════════════════════════════════════════════════════════════════
# Feature engineering
# ══════════════════════════════════════════════════════════════════════

def _build_features(
    t_ext:      list[float],
    solar:      list[float] | None,
    timestamps: list[datetime] | None,
    t_base:     float = 18.5,
    lags:       tuple[int, ...] = (1, 3, 6),
) -> np.ndarray:
    """
    Construye matriz de features para el ML forecaster.
    n = len(t_ext) filas, F columnas.
    """
    n       = len(t_ext)
    solar_v = solar if solar and len(solar) == n else [0.0] * n
    ts_list = timestamps if timestamps and len(timestamps) == n else None

    rows: list[list[float]] = []
    for i in range(n):
        row: list[float] = []

        # Temperatura exterior y derivada
        row.append(t_ext[i])
        delta = t_ext[i] - t_ext[i - 1] if i > 0 else 0.0
        row.append(delta)

        # Grados Día instantáneo
        row.append(max(0.0, t_base - t_ext[i]))

        # Retardos temperatura
        for lag in lags:
            row.append(t_ext[max(0, i - lag)])

        # Solar forecast
        row.append(solar_v[i])

        # Hora del día (encoding armónico)
        if ts_list and ts_list[i]:
            h   = ts_list[i].hour + ts_list[i].minute / 60.0
            dow = ts_list[i].weekday()
        else:
            h   = (i % 24) * 1.0
            dow = 0.0
        row.append(math.sin(2 * math.pi * h / 24.0))
        row.append(math.cos(2 * math.pi * h / 24.0))
        row.append(math.sin(2 * math.pi * dow / 7.0))
        row.append(math.cos(2 * math.pi * dow / 7.0))

        rows.append(row)

    return np.array(rows, dtype=np.float64)


# ══════════════════════════════════════════════════════════════════════
# Modelo
# ══════════════════════════════════════════════════════════════════════

@dataclass
class MLForecaster:
    pipeline:   Pipeline | None = None
    r2:         float           = 0.0
    n_samples:  int             = 0
    alpha_best: float           = 1.0
    fitted:     bool            = False
    t_base:     float           = 18.5

    def fit(
        self,
        t_ext:       list[float],
        power_w:     list[float],
        solar:       list[float] | None = None,
        timestamps:  list[datetime] | None = None,
        t_base:      float = 18.5,
    ) -> float:
        """
        Entrena el pipeline ML.

        t_ext:   historial temperaturas exteriores [°C]
        power_w: historial potencia térmica medida  [W]
        Retorna: R² sobre conjunto de validación (última 20%)
        """
        self.t_base = t_base
        n = min(len(t_ext), len(power_w))
        if n < _MIN_SAMPLES:
            _LOGGER.warning("ML fit: %d muestras (min %d)", n, _MIN_SAMPLES)
            return 0.0

        X = _build_features(t_ext[:n], solar, timestamps, t_base)
        y = np.array(power_w[:n], dtype=np.float64)

        # Train / val split 80 / 20
        split = int(n * 0.80)
        X_tr, X_val = X[:split], X[split:]
        y_tr, y_val = y[:split], y[split:]

        # RidgeCV para encontrar α óptimo
        ridge_cv = RidgeCV(alphas=_ALPHAS, cv=5)
        pipe = Pipeline([
            ("scaler", StandardScaler()),
            ("poly",   PolynomialFeatures(degree=_POLY_DEGREE, include_bias=False)),
            ("ridge",  ridge_cv),
        ])
        pipe.fit(X_tr, y_tr)

        y_pred  = pipe.predict(X_val)
        r2      = float(r2_score(y_val, y_pred))
        alpha   = float(pipe["ridge"].alpha_)

        self.pipeline   = pipe
        self.r2         = round(r2,  4)
        self.n_samples  = n
        self.alpha_best = alpha
        self.fitted     = r2 > 0.40

        _LOGGER.info(
            "ML fit ✓ | R²=%.3f | α=%.2f | n=%d | poly_deg=%d",
            r2, alpha, n, _POLY_DEGREE,
        )
        return r2

    def predict(
        self,
        t_ext_fc:    list[float],
        solar_fc:    list[float] | None = None,
        timestamps:  list[datetime] | None = None,
    ) -> list[float]:
        """Predice potencia térmica [W] para el forecast de temperatura."""
        if not self.fitted or self.pipeline is None:
            _LOGGER.debug("ML predict: modelo no entrenado, retornando ceros")
            return [0.0] * len(t_ext_fc)

        X   = _build_features(t_ext_fc, solar_fc, timestamps, self.t_base)
        raw = self.pipeline.predict(X)
        return [round(max(0.0, float(v)), 1) for v in raw]

    # ── Persistencia ─────────────────────────────────────────────────

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(MODEL_PATH, "wb") as f:
            pickle.dump(self, f)

    @classmethod
    def load(cls) -> "MLForecaster":
        if MODEL_PATH.exists():
            try:
                with open(MODEL_PATH, "rb") as f:
                    return pickle.load(f)
            except Exception as exc:
                _LOGGER.warning("ML load: %s", exc)
        return cls()

    def feature_importance(self) -> dict[str, float]:
        """Retorna importancia relativa de cada feature (|coef| normalizado)."""
        if not self.fitted or self.pipeline is None:
            return {}
        names = [
            "T_ext", "ΔT_ext", "DD_inst",
            "T_ext_lag1", "T_ext_lag3", "T_ext_lag6",
            "solar", "hour_sin", "hour_cos", "day_sin", "day_cos",
        ]
        coefs = np.abs(self.pipeline["ridge"].coef_)
        total = coefs.sum()
        if total == 0:
            return {}
        # Sólo primeros N coeficientes (antes de poly expansion)
        n = min(len(names), len(coefs))
        return {names[i]: round(float(coefs[i] / total), 4) for i in range(n)}
