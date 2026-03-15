"""COP Model — EMHASS HVAC Optimizer v0.4.0.

Pure Python. Zero external dependencies.

Modelo cuadrático Daikin Altherma:
  COP(T) = A + B·T + C·T²    (T en °C exterior)

Algoritmos:
  Regresión cuadrática por ecuaciones normales (sistema 3×3)
  Eliminación gaussiana con pivoteo parcial
  Sanity check: COP(0°C) entre COP_SANITY_MIN y COP_SANITY_MAX

FIX aplicado:
  Método predict_cop_list() expuesto (bug crítico anterior:
  el coordinator llamaba a un método que no existía).
"""
from __future__ import annotations

import logging
import math
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

from ..const import (
    COP_DEFAULT_A, COP_DEFAULT_B, COP_DEFAULT_C,
    COP_MIN_SAMPLES, COP_SANITY_MAX, COP_SANITY_MIN,
    COP_T_MAX, COP_T_MIN,
    LOGGER_NAME, RECORDER_HOURS_BACK,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(LOGGER_NAME)


# ── helpers matemáticos pure Python ──────────────────────────────────

def _safe_float(v, default=None):
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _solve_3x3(M: list[list[float]], b: list[float]) -> list[float] | None:
    """
    Resuelve Mx = b (3×3) con eliminación gaussiana + pivoteo parcial.
    Retorna [x0, x1, x2] o None si la matriz es singular.
    """
    mat = [[M[i][j] for j in range(3)] + [b[i]] for i in range(3)]

    for col in range(3):
        # Pivoteo parcial
        pivot_row = max(range(col, 3), key=lambda r: abs(mat[r][col]))
        if abs(mat[pivot_row][col]) < 1e-14:
            return None
        mat[col], mat[pivot_row] = mat[pivot_row], mat[col]

        for row in range(col + 1, 3):
            f = mat[row][col] / mat[col][col]
            for j in range(col, 4):
                mat[row][j] -= f * mat[col][j]

    # Sustitución hacia atrás
    x = [0.0] * 3
    for i in range(2, -1, -1):
        x[i] = mat[i][3]
        for j in range(i + 1, 3):
            x[i] -= mat[i][j] * x[j]
        if abs(mat[i][i]) < 1e-14:
            return None
        x[i] /= mat[i][i]

    return x


def _quadratic_fit(
    T: list[float], Y: list[float]
) -> tuple[float, float, float] | None:
    """
    Ajusta Y = A + B·T + C·T² por ecuaciones normales.
    Retorna (A, B, C) o None si falla.
    """
    n = len(T)
    if n < 3:
        return None

    s1   = float(n)
    sT   = sum(T)
    sT2  = sum(t * t for t in T)
    sT3  = sum(t ** 3 for t in T)
    sT4  = sum(t ** 4 for t in T)
    sY   = sum(Y)
    sTY  = sum(t * y for t, y in zip(T, Y))
    sT2Y = sum(t * t * y for t, y in zip(T, Y))

    M = [
        [s1,  sT,  sT2],
        [sT,  sT2, sT3],
        [sT2, sT3, sT4],
    ]
    b = [sY, sTY, sT2Y]

    result = _solve_3x3(M, b)
    return tuple(result) if result else None


# ══════════════════════════════════════════════════════════════════════

class COPModel:
    """
    Modelo COP cuadrático para aerotermia Daikin Altherma.
    Pure Python — sin dependencias externas.

    Propiedades públicas:
        a, b, c      float  — coeficientes calibrados
        is_fitted    bool
        r2_score     float
        n_samples    int
    """

    def __init__(
        self,
        hass: "HomeAssistant",
        temp_exterior_sensor: str,
        cop_sensor: str,
        hvac_current_sensor: str,
    ) -> None:
        self.hass             = hass
        self._temp_ext        = temp_exterior_sensor
        self._cop_sensor      = cop_sensor
        self._hvac_current    = hvac_current_sensor

        self._a: float = COP_DEFAULT_A
        self._b: float = COP_DEFAULT_B
        self._c: float = COP_DEFAULT_C
        self._r2: float       = 0.0
        self._is_fitted: bool = False
        self._n_samples: int  = 0

    # ── Propiedades ───────────────────────────────────────────────────

    @property
    def a(self) -> float:
        return self._a

    @property
    def b(self) -> float:
        return self._b

    @property
    def c(self) -> float:
        return self._c

    @property
    def is_fitted(self) -> bool:
        return self._is_fitted

    @property
    def r2_score(self) -> float:
        return self._r2

    @property
    def n_samples(self) -> int:
        return self._n_samples

    # ── Predicción ───────────────────────────────────────────────────

    def predict_cop(self, t_ext: float) -> float:
        """COP estimado a temperatura exterior t_ext [°C]."""
        t = max(COP_T_MIN, min(COP_T_MAX, t_ext))
        cop = self._a + self._b * t + self._c * t * t
        return round(max(1.0, cop), 3)

    def predict_cop_list(self, temps: list[float]) -> list[float]:
        """COP estimado para una lista de temperaturas exteriores [°C]."""
        return [self.predict_cop(t) for t in temps]

    def predict_cop_current(self) -> float | None:
        """COP estimado en base a la temperatura exterior actual."""
        t = _safe_float(self._read_sensor(self._temp_ext))
        return self.predict_cop(t) if t is not None else None

    # ── Calibración ───────────────────────────────────────────────────

    async def fit_online(self) -> str | None:
        """
        Calibra la curva COP = A + B·T + C·T² con historial recorder.

        Retorna: ISO timestamp si éxito, None si datos insuficientes.
        """
        pairs = await self._collect_pairs()

        if len(pairs) < COP_MIN_SAMPLES:
            _LOGGER.warning(
                "COP fit: %d muestras disponibles (mínimo %d). "
                "Usando curva Daikin default (A=%.2f, B=%.3f, C=%.4f)",
                len(pairs), COP_MIN_SAMPLES,
                self._a, self._b, self._c,
            )
            return None

        T_vals   = [p[0] for p in pairs]
        COP_vals = [p[1] for p in pairs]

        result = _quadratic_fit(T_vals, COP_vals)
        if result is None:
            _LOGGER.warning("COP fit: regresión cuadrática singular. Manteniendo defaults.")
            return None

        a, b, c = result

        # Sanity check: COP(0°C) debe ser físicamente plausible
        cop_at_zero = a   # COP(0) = A + B·0 + C·0 = A
        if not (COP_SANITY_MIN <= cop_at_zero <= COP_SANITY_MAX):
            _LOGGER.warning(
                "COP fit: COP(0°C)=%.2f fuera de rango [%.1f, %.1f]. "
                "Descartando. Usando defaults.",
                cop_at_zero, COP_SANITY_MIN, COP_SANITY_MAX,
            )
            return None

        # R² manualmente
        mean_cop = sum(COP_vals) / len(COP_vals)
        ss_tot   = sum((y - mean_cop) ** 2 for y in COP_vals)
        y_pred   = [a + b * t + c * t * t for t in T_vals]
        ss_res   = sum((y - yp) ** 2 for y, yp in zip(COP_vals, y_pred))
        r2       = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0

        self._a        = round(a, 4)
        self._b        = round(b, 4)
        self._c        = round(c, 6)
        self._r2       = round(r2, 4)
        self._is_fitted = True
        self._n_samples = len(pairs)

        ts = dt_util.now().isoformat()
        _LOGGER.info(
            "COP fit ✓ | COP(T) = %.3f + %.4f·T + %.6f·T² | "
            "COP(0°C)=%.2f | R²=%.3f | n=%d",
            self._a, self._b, self._c,
            cop_at_zero, self._r2, self._n_samples,
        )
        return ts

    # ── Privados ──────────────────────────────────────────────────────

    async def _collect_pairs(self) -> list[tuple[float, float]]:
        """
        Recopila pares (T_ext, COP_medido) del recorder.
        Filtra: COP < 1.0 o > 8.0, T fuera de [COP_T_MIN, COP_T_MAX].
        """
        now   = dt_util.now()
        start = now - timedelta(hours=RECORDER_HOURS_BACK)

        t_samples   = await self._fetch_history(self._temp_ext, start, now)
        cop_samples = await self._fetch_history(self._cop_sensor, start, now)

        if not t_samples or not cop_samples:
            return []

        # Alinear por timestamp más cercano (ventana ±5 min)
        pairs: list[tuple[float, float]] = []
        cop_lookup = {ts: v for ts, v in cop_samples}
        for ts_t, t_val in t_samples:
            if not (COP_T_MIN <= t_val <= COP_T_MAX):
                continue
            # Buscar COP en ventana de ±5 minutos
            best_cop: float | None = None
            best_diff = timedelta(minutes=5)
            for ts_c, c_val in cop_samples:
                diff = abs(ts_t - ts_c)
                if diff <= best_diff:
                    best_diff = diff
                    best_cop  = c_val
            if best_cop is not None and 1.0 <= best_cop <= 8.0:
                pairs.append((t_val, best_cop))

        return pairs

    def _read_sensor(self, entity_id: str):
        if not entity_id:
            return None
        s = self.hass.states.get(entity_id)
        if s is None or s.state in ("unknown", "unavailable", "none", ""):
            return None
        return s.state

    async def _fetch_history(
        self, entity_id: str, start, end
    ) -> list[tuple]:
        if not entity_id:
            return []
        results = []
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import get_significant_states

            instance  = get_instance(self.hass)
            state_map = await instance.async_add_executor_job(
                get_significant_states,
                self.hass, start, end, [entity_id], None, True,
            )
            for s in state_map.get(entity_id, []):
                if s.state in ("unknown", "unavailable", "none", ""):
                    continue
                val = _safe_float(s.state)
                if val is not None:
                    ts = dt_util.as_local(s.last_updated or end)
                    results.append((ts, val))
        except Exception as exc:
            _LOGGER.debug("COP _fetch_history (%s): %s", entity_id, exc)
        return results
