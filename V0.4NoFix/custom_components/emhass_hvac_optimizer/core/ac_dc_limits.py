"""AC/DC Dynamic Limits — EMHASS HVAC Optimizer v0.4.0.

Diferencia automáticamente el límite de carga de la batería según fuente:
  • Carga DC (solar diurna): hasta 5 000 W  — sin restricción inversor
  • Carga AC (red nocturna): ≈ 2 500 W      — límite real LUNA2000

Problema resuelto:
  EMHASS planifica carga de 5 kW desde la red nocturna, pero la
  LUNA2000 sólo acepta 2 000-3 000 W en AC. Resultado: baterías
  se cargan a ~2.5 kW aunque EMHASS ordene 5 kW → plan SOC irreal.

Auto-calibración:
  Observa eventos de carga nocturna (PV ≈ 0, forciblecharge activo)
  y mide la potencia máxima sostenida real → percentil 90 → AC limit.

Pure Python. Zero external dependencies.
"""
from __future__ import annotations

import logging
import math
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

from ..const import (
    AC_CHARGE_LIMIT_DEFAULT, AC_SOLAR_THRESHOLD,
    DC_CHARGE_LIMIT_MAX, LOGGER_NAME,
    RECORDER_HOURS_BACK, SUN_ELEVATION_THRESHOLD,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(LOGGER_NAME)

# Entidades Huawei conocidas del sistema
_ENTITY_BATT_CHARGE_POWER  = "sensor.bateriaspotenciamaximadecargadesdelared"
_ENTITY_PV_POWER           = "sensor.inverterinputpower"
_ENTITY_BATT_SOC           = "sensor.bateriasestadodelacapacidad"
_ENTITY_BATT_POWER         = "sensor.pbattforecast"          # potencia real batería
_ENTITY_SUN                = "sun.sun"

# Mínimo muestras nocturnas para intentar calibración
_MIN_NIGHT_SAMPLES = 10


def _safe_float(v, default=None):
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _percentile(values: list[float], p: float) -> float:
    """Percentil p (0-100) en pure Python."""
    if not values:
        return 0.0
    sv  = sorted(values)
    n   = len(sv)
    idx = (n - 1) * p / 100.0
    lo  = int(idx)
    hi  = min(lo + 1, n - 1)
    return sv[lo] + (idx - lo) * (sv[hi] - sv[lo])


class ACDCLimitsManager:
    """
    Gestor de límites AC/DC dinámicos para LUNA2000.

    Integración con coordinator:
        limits = ACDCLimitsManager(hass)
        charge_max_w = limits.get_dynamic_limit()
        # → 5000 W si hay solar, 2500 W si es noche
    """

    def __init__(self, hass: "HomeAssistant") -> None:
        self._hass               = hass
        self._ac_limit_calibrated: float = AC_CHARGE_LIMIT_DEFAULT
        self._ac_limit_source    = "default"   # "default" | "native" | "calibrated"
        self._is_calibrated      = False
        self._n_calibration_events = 0

    # ── Propiedad pública ─────────────────────────────────────────────

    @property
    def ac_limit_w(self) -> float:
        return self._ac_limit_calibrated

    @property
    def is_calibrated(self) -> bool:
        return self._is_calibrated

    @property
    def calibration_source(self) -> str:
        return self._ac_limit_source

    # ── Límite dinámico ───────────────────────────────────────────────

    def get_dynamic_limit(self) -> float:
        """
        Retorna el límite de carga adecuado según fuente disponible.

          Solar disponible → DC → DC_CHARGE_LIMIT_MAX (5 000 W)
          Sin solar (noche / nublado) → AC → ac_limit_calibrated

        Fuente de verdad:
          1. Sensor nativo Huawei (si existe y > 0)
          2. Límite auto-calibrado por observación
          3. Default conservador (AC_CHARGE_LIMIT_DEFAULT)
        """
        if self.is_solar_available():
            return float(DC_CHARGE_LIMIT_MAX)

        # Intentar leer límite nativo desde Huawei
        native = self._read_native_limit()
        if native is not None and native > 100:
            self._ac_limit_source = "native"
            return native

        return self._ac_limit_calibrated

    def is_solar_available(self) -> bool:
        """
        True si hay producción solar activa.
        Método 1: potencia PV > AC_SOLAR_THRESHOLD.
        Método 2 (fallback): elevación solar > SUN_ELEVATION_THRESHOLD.
        """
        pv = _safe_float(self._read_entity(_ENTITY_PV_POWER))
        if pv is not None:
            return pv > AC_SOLAR_THRESHOLD

        # Fallback: elevación solar
        sun_state = self._hass.states.get(_ENTITY_SUN)
        if sun_state:
            elev = _safe_float(sun_state.attributes.get("elevation"))
            if elev is not None:
                return elev > SUN_ELEVATION_THRESHOLD

        return False

    def _read_native_limit(self) -> float | None:
        """Lee el límite máximo de carga declarado por Huawei vía Modbus."""
        return _safe_float(self._read_entity(_ENTITY_BATT_CHARGE_POWER))

    # ── Auto-calibración AC limit ─────────────────────────────────────

    async def async_autocalibrate_ac_limit(self) -> bool:
        """
        Mide el límite AC real observando cargas nocturnas del recorder.

        Algoritmo:
          1. Obtiene 7 días de potencia de carga (P < 0 en convención EMHASS)
             solo en periodos sin solar (noche).
          2. Extrae picos de carga sostenida (ventana 5 muestras).
          3. Percentil 90 → límite calibrado.

        Retorna: True si calibración exitosa.
        """
        night_powers = await self._fetch_night_charge_powers()

        if len(night_powers) < _MIN_NIGHT_SAMPLES:
            _LOGGER.debug(
                "AC limit autocalibrate: %d muestras nocturnas "
                "(mínimo %d). Manteniendo %d W.",
                len(night_powers), _MIN_NIGHT_SAMPLES,
                int(self._ac_limit_calibrated),
            )
            return False

        # Percentil 90 de potencias de carga observadas
        p90 = _percentile(night_powers, 90)

        # Sanity check: entre 500 W y DC_CHARGE_LIMIT_MAX
        if not (500.0 <= p90 <= DC_CHARGE_LIMIT_MAX):
            _LOGGER.warning(
                "AC limit autocalibrate: P90=%.0fW fuera de rango. Descartando.", p90
            )
            return False

        old = self._ac_limit_calibrated
        self._ac_limit_calibrated    = round(p90, 0)
        self._is_calibrated          = True
        self._ac_limit_source        = "calibrated"
        self._n_calibration_events   = len(night_powers)

        _LOGGER.info(
            "AC limit calibrado ✓ | %.0f W → %.0f W | "
            "n=%d eventos nocturnos | P90=%.0f W",
            old, self._ac_limit_calibrated,
            self._n_calibration_events, p90,
        )
        return True

    async def _fetch_night_charge_powers(self) -> list[float]:
        """
        Obtiene potencias de carga en periodos nocturnos del recorder.
        Filtra: solo cuando PV ≈ 0 y batería está cargando activamente.
        """
        now   = dt_util.now()
        start = now - timedelta(hours=RECORDER_HOURS_BACK)
        powers: list[float] = []

        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import get_significant_states

            instance = get_instance(self._hass)

            # Potencia batería (convención EMHASS: negativo = carga)
            state_map = await instance.async_add_executor_job(
                get_significant_states,
                self._hass, start, now,
                [_ENTITY_BATT_POWER, _ENTITY_PV_POWER],
                None, True,
            )
            batt_states = state_map.get(_ENTITY_BATT_POWER, [])
            pv_states   = state_map.get(_ENTITY_PV_POWER, [])

            # Construir lookup PV por timestamp (resolución 1 min)
            pv_by_ts: dict[int, float] = {}
            for s in pv_states:
                if s.state in ("unknown", "unavailable", "none", ""):
                    continue
                v = _safe_float(s.state)
                if v is not None and s.last_updated:
                    minute_key = int(s.last_updated.timestamp() // 60)
                    pv_by_ts[minute_key] = v

            # Filtrar muestras nocturnas con carga activa
            rolling: list[float] = []
            for s in batt_states:
                if s.state in ("unknown", "unavailable", "none", ""):
                    continue
                p = _safe_float(s.state)
                if p is None or p >= -100:        # no está cargando significativamente
                    rolling.clear()
                    continue
                if not s.last_updated:
                    continue

                ts_min = int(s.last_updated.timestamp() // 60)
                pv_now = pv_by_ts.get(ts_min, pv_by_ts.get(ts_min - 1, None))
                if pv_now is not None and pv_now > AC_SOLAR_THRESHOLD:
                    rolling.clear()             # hay solar → descartamos
                    continue

                rolling.append(abs(p))
                # Ventana deslizante de 5 muestras → pico sostenido
                if len(rolling) >= 5:
                    powers.append(max(rolling[-5:]))

        except Exception as exc:
            _LOGGER.debug("AC limit fetch_night_powers: %s", exc)

        return powers

    def update_from_companion(self, calibrated_w: float) -> None:
        """Actualiza límite desde resultado Companion App AutoTuner."""
        if 500.0 <= calibrated_w <= DC_CHARGE_LIMIT_MAX:
            self._ac_limit_calibrated = round(calibrated_w, 0)
            self._is_calibrated       = True
            self._ac_limit_source     = "companion_app"
            _LOGGER.info(
                "AC limit actualizado desde Companion App: %.0f W",
                self._ac_limit_calibrated,
            )

    def _read_entity(self, entity_id: str):
        s = self._hass.states.get(entity_id)
        if s is None or s.state in ("unknown", "unavailable", "none", ""):
            return None
        return s.state
