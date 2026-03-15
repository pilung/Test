"""Degree Days thermal model — EMHASS HVAC Optimizer v0.4.0.

Pure Python. Zero external dependencies.

Algoritmos implementados:
  OLS        — Ordinary Least Squares (regresión lineal)
  Huber IRLS — Iteratively Reweighted Least Squares, función pérdida Huber
  z-score    — Filtrado outliers sobre pares (DD_dia, kWh_dia)
  DD local   — Grados Día desde medianoche en hora LOCAL (no UTC)
  Q [kW]     — caudal[L/min] × 60 × ΔT[°C] × 1.163 / 1000
  T_aparente — Wind-chill Steadman 1971  +  Heat-index Rothfusz NWS
  Recorder   — Doble fallback: get_significant_states → estado actual

Flujo de calibración:
  1. Obtiene 7 días de historial del recorder (T_ext + corriente HVAC)
  2. Agrega a resolución diaria alineando por medianoche LOCAL
  3. Filtra días atípicos con z-score bilateral
  4. Regresión Huber IRLS → thermal_factor [kWh/DD]
  5. Sanity check físico y publicación de R²

Flujo de predicción (48h):
  • weather.home.attributes.forecast → lista de T [°C] por slot
  • P[W] = thermal_factor × max(0, T_base − T_h) / 24 × 1000
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

from ..const import (
    FORECAST_HORIZON_HOURS,
    HEAT_INDEX_HR_MIN,
    HEAT_INDEX_T_MIN,
    HUBER_DELTA,
    HUBER_MAX_ITER,
    LOGGER_NAME,
    MIN_HISTORY_DAYS_FIT,
    RECORDER_HOURS_BACK,
    THERMAL_FACTOR_DEFAULT,
    THERMAL_FACTOR_MAX,
    THERMAL_FACTOR_MIN,
    WATER_SPECIFIC_HEAT,
    WIND_CHILL_T_MAX,
    WIND_CHILL_V_MIN,
    ZSCORE_THRESHOLD,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(LOGGER_NAME)


# ══════════════════════════════════════════════════════════════════════
# Helpers matemáticos — pure Python, sin ningún import externo
# ══════════════════════════════════════════════════════════════════════

def _safe_float(value, default=None):
    """Convierte a float descartando NaN / Inf / cadenas inválidas."""
    try:
        f = float(value)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _ols(x: list[float], y: list[float]) -> tuple[float, float]:
    """Ordinary Least Squares. Retorna (intercept, slope)."""
    n = len(x)
    if n < 2:
        return (0.0, THERMAL_FACTOR_DEFAULT)
    sx  = sum(x);  sy  = sum(y)
    sxx = sum(xi * xi for xi in x)
    sxy = sum(xi * yi for xi, yi in zip(x, y))
    den = n * sxx - sx * sx
    if abs(den) < 1e-12:
        return (sy / n, 0.0)
    slope     = (n * sxy - sx * sy) / den
    intercept = (sy - slope * sx) / n
    return (intercept, slope)


def _wls(
    x: list[float], y: list[float], w: list[float]
) -> tuple[float, float]:
    """Weighted Least Squares. Retorna (intercept, slope)."""
    sw = sum(w)
    if sw < 1e-12:
        return _ols(x, y)
    swx  = sum(wi * xi for wi, xi in zip(w, x))
    swy  = sum(wi * yi for wi, yi in zip(w, y))
    swxx = sum(wi * xi * xi for wi, xi in zip(w, x))
    swxy = sum(wi * xi * yi for wi, xi, yi in zip(w, x, y))
    den  = sw * swxx - swx * swx
    if abs(den) < 1e-12:
        return (swy / sw, 0.0)
    slope     = (sw * swxy - swx * swy) / den
    intercept = (swy - slope * swx) / sw
    return (intercept, slope)


def _huber_fit(
    x: list[float],
    y: list[float],
    delta: float = HUBER_DELTA,
    max_iter: int = HUBER_MAX_ITER,
) -> tuple[float, float]:
    """
    Huber IRLS. Robusto ante outliers. Retorna (intercept, slope).

    Pasos:
      1. Estimación inicial OLS
      2. Residuos → escala MAD = mediana(|r|) / 0.6745
      3. Pesos Huber: 1 si |r|/MAD ≤ δ,  δ·MAD/|r| si no
      4. WLS con pesos → repite hasta convergencia (|Δslope| < 1e-8)
    """
    if len(x) < 2:
        return (0.0, THERMAL_FACTOR_DEFAULT)
    b = _ols(x, y)
    for _ in range(max_iter):
        res     = [yi - b[0] - b[1] * xi for xi, yi in zip(x, y)]
        abs_res = sorted(abs(r) for r in res)
        mad     = abs_res[len(res) // 2] / 0.6745
        if mad < 1e-10:
            break
        weights = [
            1.0 if abs(r) / mad <= delta else delta * mad / abs(r)
            for r in res
        ]
        b_new = _wls(x, y, weights)
        if abs(b_new[1] - b[1]) < 1e-8:
            b = b_new
            break
        b = b_new
    return b


def _r2(y: list[float], y_pred: list[float]) -> float:
    """Coeficiente de determinación R²."""
    if len(y) < 2:
        return 0.0
    m      = _mean(y)
    ss_tot = sum((yi - m)   ** 2 for yi in y)
    ss_res = sum((yi - yp)  ** 2 for yi, yp in zip(y, y_pred))
    return 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0


def _zscore_filter(
    pairs: list[tuple[float, float]],
    threshold: float = ZSCORE_THRESHOLD,
) -> list[tuple[float, float]]:
    """
    Elimina pares (x, y) cuyos z-scores superan el umbral.
    Aplica el filtro bilateralmente (tanto a x como a y).
    Requiere ≥ 4 pares; con menos devuelve la lista intacta.
    """
    if len(pairs) < 4:
        return pairs
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx = _mean(xs);  sx = max((_mean([(xi - mx) ** 2 for xi in xs])) ** 0.5, 1e-10)
    my = _mean(ys);  sy = max((_mean([(yi - my) ** 2 for yi in ys])) ** 0.5, 1e-10)
    return [
        (x, y) for x, y in pairs
        if abs(x - mx) / sx <= threshold
        and abs(y - my) / sy <= threshold
    ]


# ══════════════════════════════════════════════════════════════════════
# Clase principal
# ══════════════════════════════════════════════════════════════════════

class DegreeDaysModel:
    """
    Modelo Degree Days con auto-calibración Huber IRLS.
    Pure Python — sin dependencias externas.

    Propiedades públicas (usadas por coordinator y sensor.py):
        thermal_factor    float  — kWh por Grado Día (calibrado)
        is_fitted         bool   — True tras calibración exitosa
        r2_score          float  — R² de la última calibración
        n_samples         int    — días usados en la última calibración
        use_thermal_power bool   — True si Q[kW] viene de caudal+ΔT
    """

    def __init__(
        self,
        hass: "HomeAssistant",
        temp_exterior_sensor: str,
        hvac_current_sensor: str,
        ath_impulsion: str,
        ath_retorno: str,
        ath_caudal: str,
        t_base: float,
    ) -> None:
        self.hass            = hass
        self._temp_ext       = temp_exterior_sensor
        self._hvac_current   = hvac_current_sensor
        self._ath_impulsion  = ath_impulsion
        self._ath_retorno    = ath_retorno
        self._ath_caudal     = ath_caudal
        self.t_base          = t_base

        self._thermal_factor:       float = THERMAL_FACTOR_DEFAULT
        self._intercept:            float = 0.0
        self._r2:                   float = 0.0
        self._is_fitted:            bool  = False
        self._n_samples:            int   = 0
        self._use_thermal_power_fl: bool  = False

    # ── Propiedades ───────────────────────────────────────────────────

    @property
    def thermal_factor(self) -> float:
        return self._thermal_factor

    @property
    def is_fitted(self) -> bool:
        return self._is_fitted

    @property
    def r2_score(self) -> float:
        return self._r2

    @property
    def n_samples(self) -> int:
        return self._n_samples

    @property
    def use_thermal_power(self) -> bool:
        """True si Q[kW] se calculó con caudal+ΔT (más preciso que corriente)."""
        return self._use_thermal_power_fl

    # ── Calibración online ────────────────────────────────────────────

    async def fit_online(self) -> str | None:
        """
        Calibra thermal_factor con historial del recorder.

        Retorna: ISO timestamp si éxito, None si datos insuficientes.
        """
        pairs = await self._build_daily_pairs()

        if len(pairs) < MIN_HISTORY_DAYS_FIT:
            _LOGGER.warning(
                "DD fit: %d días disponibles, necesita %d. "
                "Manteniendo thermal_factor=%.4f kWh/DD",
                len(pairs), MIN_HISTORY_DAYS_FIT, self._thermal_factor,
            )
            return None

        filtered = _zscore_filter(pairs)
        if len(filtered) < 2:
            _LOGGER.warning(
                "DD fit: z-score eliminó todos los pares "
                "(raw=%d). Revisar calidad sensores.", len(pairs),
            )
            return None

        xs = [p[0] for p in filtered]   # DD diarios [°C·día]
        ys = [p[1] for p in filtered]   # kWh HVAC diarios

        intercept, slope = _huber_fit(xs, ys)

        if not (THERMAL_FACTOR_MIN <= slope <= THERMAL_FACTOR_MAX):
            _LOGGER.warning(
                "DD fit: slope=%.4f fuera del rango físico "
                "[%.1f, %.1f] kWh/DD. Descartando.",
                slope, THERMAL_FACTOR_MIN, THERMAL_FACTOR_MAX,
            )
            return None

        y_pred = [intercept + slope * x for x in xs]

        self._thermal_factor = round(slope, 4)
        self._intercept      = round(intercept, 4)
        self._r2             = round(_r2(ys, y_pred), 4)
        self._is_fitted      = True
        self._n_samples      = len(filtered)

        ts = dt_util.now().isoformat()
        _LOGGER.info(
            "DD fit ✓ | thermal_factor=%.4f kWh/DD | "
            "R²=%.3f | n=%d días | intercept=%.3f",
            self._thermal_factor, self._r2,
            self._n_samples, self._intercept,
        )
        return ts

    # ── Predicción 48h ───────────────────────────────────────────────

    async def predict(
        self, horizon_hours: int = FORECAST_HORIZON_HOURS
    ) -> list[float]:
        """
        Forecast térmico [W] para las próximas horizon_hours horas.

        Fórmula por hora h:
          P_h [W] = thermal_factor [kWh/DD]
                    × max(0, T_base − T_forecast[h]) / 24
                    × 1000
        """
        temps = self._get_weather_forecast(horizon_hours)
        return [
            round(
                self._thermal_factor
                * max(0.0, self.t_base - t)
                / 24.0
                * 1_000.0,
                1,
            )
            for t in temps
        ]

    def _get_weather_forecast(self, hours: int) -> list[float]:
        """
        Extrae temperaturas forecast de weather.home.
        Fallback: replica la temperatura exterior actual.
        """
        t_now    = self._read_float(self._temp_ext) or 10.0
        fallback = [t_now] * hours

        state = self.hass.states.get("weather.home")
        if not state:
            return fallback

        fc = state.attributes.get("forecast") or []
        if not isinstance(fc, list) or not fc:
            return fallback

        temps: list[float] = []
        for slot in fc:
            t = _safe_float(slot.get("temperature") if isinstance(slot, dict) else None)
            if t is not None:
                temps.append(t)

        if not temps:
            return fallback

        # Completar hasta `hours` posiciones (puede ser forecast diario)
        while len(temps) < hours:
            temps.append(temps[-1])
        return temps[:hours]

    # ── Grados Día hoy ────────────────────────────────────────────────

    async def calculate_degree_days_today(self) -> float:
        """
        DD acumulados desde medianoche LOCAL hasta ahora.
        DD_hoy = max(0, T_base − T_media_dia) × (horas_transcurridas / 24)
        """
        now   = dt_util.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        samples = await self._fetch_states_window(self._temp_ext, start, now)
        values  = [v for _, v in samples]

        if not values:
            t = self._read_float(self._temp_ext)
            values = [t] if t is not None else []
        if not values:
            return 0.0

        hours_elapsed = max(1.0, (now - start).total_seconds() / 3_600.0)
        dd = max(0.0, self.t_base - _mean(values)) * (hours_elapsed / 24.0)
        return round(dd, 4)

    # ── Potencia térmica Q [kW] ───────────────────────────────────────

    def get_thermal_power_kw(self) -> float | None:
        """
        Calcula potencia térmica:
          Primario:  Q = caudal[L/min] × 60 × |ΔT|[°C] × 1.163 / 1000
          Fallback:  P_elec = I[A] × 230[V] / 1000  (sin COP)

        Actualiza use_thermal_power según la fuente usada.
        """
        caudal = self._read_float(self._ath_caudal)
        t_imp  = self._read_float(self._ath_impulsion)
        t_ret  = self._read_float(self._ath_retorno)

        if caudal is not None and t_imp is not None and t_ret is not None:
            delta_t = abs(t_imp - t_ret)
            if caudal > 0.1 and delta_t > 0.2:
                q = caudal * 60.0 * delta_t * WATER_SPECIFIC_HEAT / 1_000.0
                self._use_thermal_power_fl = True
                return round(q, 3)

        corriente = self._read_float(self._hvac_current)
        if corriente is not None and corriente > 0.1:
            self._use_thermal_power_fl = False
            return round(corriente * 0.230, 3)

        self._use_thermal_power_fl = False
        return None

    # ── Temperatura aparente ──────────────────────────────────────────

    def get_apparent_temp(self) -> float | None:
        """
        Temperatura aparente exterior:
          • Wind-chill Steadman 1971 : T < 10 °C  y  v ≥ 4.8 km/h
          • Heat-index  Rothfusz NWS : T > 27 °C  y  HR ≥ 40 %
          • Sin corrección en otro caso.
        """
        t = self._read_float(self._temp_ext)
        if t is None:
            return None

        attrs = (self.hass.states.get("weather.home") or type("_", (), {"attributes": {}})()).attributes

        # Wind-chill
        if t < WIND_CHILL_T_MAX:
            v = _safe_float(attrs.get("wind_speed"))
            if v is not None and v >= WIND_CHILL_V_MIN:
                v016 = v ** 0.16
                return round(
                    13.12 + 0.6215 * t - 11.37 * v016 + 0.3965 * t * v016, 1
                )

        # Heat-index (Rothfusz NWS)
        if t > HEAT_INDEX_T_MIN:
            hr = _safe_float(attrs.get("humidity"))
            if hr is not None and hr >= HEAT_INDEX_HR_MIN:
                hi = (
                    -8.78469475556
                    + 1.61139411       * t
                    + 2.33854883889    * hr
                    - 0.14611605       * t  * hr
                    - 0.012308094      * t  * t
                    - 0.0164248277778  * hr * hr
                    + 0.002211732      * t  * t  * hr
                    + 0.00072546       * t  * hr * hr
                    - 0.000003582      * t  * t  * hr * hr
                )
                return round(hi, 1)

        return round(t, 1)

    # ── Helpers privados ──────────────────────────────────────────────

    def _read_float(self, entity_id: str) -> float | None:
        """Lee el estado actual de un sensor como float. Filtra estados no numéricos."""
        if not entity_id:
            return None
        s = self.hass.states.get(entity_id)
        if s is None or s.state in ("unknown", "unavailable", "none", ""):
            return None
        return _safe_float(s.state)

    async def _build_daily_pairs(self) -> list[tuple[float, float]]:
        """
        Construye pares (DD_dia [°C·día], kWh_HVAC_dia) desde el recorder.
        Usa hora LOCAL para el corte de días (medianoche local, no UTC).
        """
        now   = dt_util.now()
        start = now - timedelta(hours=RECORDER_HOURS_BACK)

        t_samples = await self._fetch_states_window(self._temp_ext,    start, now)
        i_samples = await self._fetch_states_window(self._hvac_current, start, now)

        if not t_samples or not i_samples:
            _LOGGER.debug("DD build_pairs: historial insuficiente en uno o ambos sensores.")
            return []

        return self._aggregate_daily(t_samples, i_samples)

    def _aggregate_daily(
        self,
        temp_samples: list[tuple[datetime, float]],
        amp_samples:  list[tuple[datetime, float]],
    ) -> list[tuple[float, float]]:
        """
        Agrega muestras por día (clave = fecha LOCAL %Y-%m-%d).
        Descarta días con < 4 muestras (datos incompletos).
        Retorna: lista de (DD [°C·día], kWh_HVAC).
        """
        t_by_day: dict[str, list[float]] = defaultdict(list)
        i_by_day: dict[str, list[float]] = defaultdict(list)

        for dt_local, val in temp_samples:
            t_by_day[dt_local.strftime("%Y-%m-%d")].append(val)
        for dt_local, val in amp_samples:
            i_by_day[dt_local.strftime("%Y-%m-%d")].append(val)

        pairs: list[tuple[float, float]] = []
        for day in sorted(t_by_day):
            if day not in i_by_day:
                continue
            tv = t_by_day[day];  iv = i_by_day[day]
            if len(tv) < 4 or len(iv) < 4:
                continue
            dd  = max(0.0, self.t_base - _mean(tv))
            # kWh = I_media[A] × 230[V]/1000 × n_muestras_horarias
            kwh = _mean(iv) * 0.230 * len(iv)
            if dd > 0.05 and kwh > 0.1:
                pairs.append((dd, kwh))

        _LOGGER.debug("DD aggregate_daily: %d pares válidos de %d días raw",
                      len(pairs), len(t_by_day))
        return pairs

    async def _fetch_states_window(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
    ) -> list[tuple[datetime, float]]:
        """
        Obtiene estados históricos [start, end] del recorder.

        Primary : homeassistant.components.recorder.history.get_significant_states
        Fallback : estado actual del sensor (1 muestra, sin crash)

        Filtra: unknown / unavailable / none / cadenas vacías / NaN / Inf.
        Retorna: list[(datetime_local, float)] ordenado por tiempo.
        """
        if not entity_id:
            return []

        results: list[tuple[datetime, float]] = []

        # ── Primary: recorder ──────────────────────────────────────────
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import (
                get_significant_states,
            )

            instance  = get_instance(self.hass)
            state_map = await instance.async_add_executor_job(
                get_significant_states,
                self.hass,
                start,
                end,
                [entity_id],
                None,   # filters
                True,   # include_start_time_state
            )
            for s in state_map.get(entity_id, []):
                if s.state in ("unknown", "unavailable", "none", ""):
                    continue
                val = _safe_float(s.state)
                if val is None:
                    continue
                ts_local = dt_util.as_local(s.last_updated or end)
                results.append((ts_local, val))

            if results:
                _LOGGER.debug(
                    "recorder OK | %s | %d muestras", entity_id, len(results)
                )
                return sorted(results, key=lambda x: x[0])

        except Exception as exc:
            _LOGGER.debug(
                "recorder fallback (%s): %s", entity_id, exc
            )

        # ── Fallback: estado actual ────────────────────────────────────
        val = self._read_float(entity_id)
        if val is not None:
            results.append((dt_util.now(), val))
            _LOGGER.debug("fallback estado actual | %s | %.2f", entity_id, val)

        return results
