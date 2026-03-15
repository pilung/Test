"""ThermalZone — EMHASS HVAC Optimizer v0.4.0.

Modelo de zona de confort Daikin Altherma.

Responsabilidades:
  • Temperatura operativa multi-sensor (ponderada)
  • Setpoint dinámico con offset temporal
  • Constante de tiempo τ (IQR-filtrada)
  • Horario cruce medianoche
  • Factor de schedule (fracción de horas activas)
  • Pre-calentamiento predictivo
  • PMV simplificado (ISO 7730)
  • Punto de rocío (Magnus)
  • Grados Día por zona

Pure Python. Zero external dependencies.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, time as dtime
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

from ..const import (
    DEFAULT_DEMAND_WEIGHT, DEFAULT_SCHEDULE_END,
    DEFAULT_SCHEDULE_START, LOGGER_NAME,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(LOGGER_NAME)
_IQR_K  = 1.5   # factor IQR para filtrado


# ══════════════════════════════════════════════════════════════════════
# Helpers puros
# ══════════════════════════════════════════════════════════════════════

def _safe_float(v, default=None):
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _iqr_filter(values: list[float]) -> list[float]:
    """Elimina outliers usando criterio IQR × 1.5."""
    if len(values) < 4:
        return list(values)
    sv    = sorted(values)
    n     = len(sv)
    q1    = sv[n // 4]
    q3    = sv[(3 * n) // 4]
    iqr   = q3 - q1
    lo    = q1 - _IQR_K * iqr
    hi    = q3 + _IQR_K * iqr
    return [v for v in values if lo <= v <= hi]


def _parse_time(s: str) -> dtime:
    """'HH:MM' → time object."""
    h, m = map(int, s.split(":"))
    return dtime(h, m)


# ══════════════════════════════════════════════════════════════════════
# ThermalZone
# ══════════════════════════════════════════════════════════════════════

class ThermalZone:
    """
    Zona de confort con modelo térmico de primer orden RC.

    Atributos públicos:
      id, name, climate_entity, _temp_primary, _temp_secondary,
      demand_weight, schedule_start, schedule_end, enabled, tau_hours
    """

    def __init__(
        self,
        hass:             "HomeAssistant",
        zone_id:          str,
        name:             str,
        climate_entity:   str,
        temp_primary:     str,
        temp_secondary:   list[str]  | None = None,
        sensor_weights:   list[float]| None = None,
        demand_weight:    float              = DEFAULT_DEMAND_WEIGHT,
        schedule_start:   str               = DEFAULT_SCHEDULE_START,
        schedule_end:     str               = DEFAULT_SCHEDULE_END,
        enabled:          bool              = True,
        initial_tau_h:    float             = 2.0,
    ) -> None:
        self._hass            = hass
        self.id               = zone_id
        self.name             = name
        self.climate_entity   = climate_entity
        self._temp_primary    = temp_primary
        self._temp_secondary  = temp_secondary or []
        self._sensor_weights  = sensor_weights or []
        self.demand_weight    = demand_weight
        self.schedule_start   = schedule_start
        self.schedule_end     = schedule_end
        self.enabled          = enabled

        # τ — constante de tiempo RC [h]
        self._tau_samples: list[float] = [initial_tau_h]
        self._tau_h: float             = initial_tau_h

        # Setpoint offset temporal
        self._setpoint_offset: float   = 0.0

    # ── Constructor desde config_entry ───────────────────────────────

    @classmethod
    def from_config(
        cls, hass: "HomeAssistant", cfg: dict
    ) -> "ThermalZone":
        from ..const import (
            CONF_ZONE_CLIMATE, CONF_ZONE_DEMAND_WEIGHT, CONF_ZONE_ENABLED,
            CONF_ZONE_ID, CONF_ZONE_NAME, CONF_ZONE_SCHEDULE_END,
            CONF_ZONE_SCHEDULE_START, CONF_ZONE_SENSOR_WEIGHTS,
            CONF_ZONE_TEMP_PRIMARY, CONF_ZONE_TEMP_SECONDARY,
        )
        return cls(
            hass           = hass,
            zone_id        = cfg.get(CONF_ZONE_ID,             "zone"),
            name           = cfg.get(CONF_ZONE_NAME,           "Zona"),
            climate_entity = cfg.get(CONF_ZONE_CLIMATE,        ""),
            temp_primary   = cfg.get(CONF_ZONE_TEMP_PRIMARY,   ""),
            temp_secondary = cfg.get(CONF_ZONE_TEMP_SECONDARY, []),
            sensor_weights = cfg.get(CONF_ZONE_SENSOR_WEIGHTS, []),
            demand_weight  = float(cfg.get(CONF_ZONE_DEMAND_WEIGHT, DEFAULT_DEMAND_WEIGHT)),
            schedule_start = cfg.get(CONF_ZONE_SCHEDULE_START, DEFAULT_SCHEDULE_START),
            schedule_end   = cfg.get(CONF_ZONE_SCHEDULE_END,   DEFAULT_SCHEDULE_END),
            enabled        = bool(cfg.get(CONF_ZONE_ENABLED,   True)),
        )

    # ── τ (constante tiempo) ─────────────────────────────────────────

    @property
    def tau_hours(self) -> float:
        return self._tau_h

    def add_tau_sample(self, tau_h: float) -> None:
        """
        Añade muestra de τ y recalcula la mediana IQR-filtrada.
        Mantiene ventana deslizante de 20 muestras.
        """
        if not (0.5 <= tau_h <= 24.0):
            return
        self._tau_samples.append(tau_h)
        if len(self._tau_samples) > 20:
            self._tau_samples = self._tau_samples[-20:]
        filtered = _iqr_filter(self._tau_samples)
        if filtered:
            sv           = sorted(filtered)
            self._tau_h  = sv[len(sv) // 2]

    # ── Temperatura operativa ─────────────────────────────────────────

    def get_operative_temperature(self) -> float | None:
        """
        Temperatura operativa = promedio ponderado de sensores.
        Si sólo hay sensor primario → retorna su valor directamente.
        """
        readings: list[tuple[float, float]] = []  # (valor, peso)

        val_primary = _safe_float(self._read(self._temp_primary))
        if val_primary is not None:
            readings.append((val_primary, 1.0))

        for i, eid in enumerate(self._temp_secondary):
            v = _safe_float(self._read(eid))
            if v is not None:
                w = (self._sensor_weights[i]
                     if i < len(self._sensor_weights) else 1.0)
                readings.append((v, w))

        if not readings:
            return None

        total_w = sum(w for _, w in readings)
        if total_w <= 0:
            return None
        return round(sum(v * w for v, w in readings) / total_w, 2)

    # ── Setpoint ──────────────────────────────────────────────────────

    def get_setpoint(self) -> float | None:
        """
        Lee setpoint del climate entity y aplica offset temporal.
        Retorna None si la entidad no existe o está indisponible.
        """
        s = self._hass.states.get(self.climate_entity)
        if s is None or s.state in ("unknown", "unavailable", "off", ""):
            return None
        sp = _safe_float(s.attributes.get("temperature"))
        if sp is None:
            return None
        return round(sp + self._setpoint_offset, 1)

    def apply_setpoint_offset(self, offset: float) -> None:
        """Aplica offset acumulable al setpoint. Llamar con signo opuesto para retirar."""
        self._setpoint_offset = round(self._setpoint_offset + offset, 1)

    # ── HVAC mode ────────────────────────────────────────────────────

    def get_hvac_mode(self) -> str:
        """Lee modo HVAC actual del climate entity."""
        s = self._hass.states.get(self.climate_entity)
        if s is None:
            return "unknown"
        return s.state

    # ── Horario ───────────────────────────────────────────────────────

    def is_in_schedule(self) -> bool:
        """
        True si la hora actual está dentro del horario de confort.
        Soporta horarios que cruzan medianoche (ej. 22:00 – 08:00).
        """
        if not self.enabled:
            return False
        now   = dt_util.now().time()
        start = _parse_time(self.schedule_start)
        end   = _parse_time(self.schedule_end)

        if start <= end:                        # Normal: 07:00 – 23:00
            return start <= now <= end
        else:                                   # Cruce medianoche: 22:00 – 08:00
            return now >= start or now <= end

    def get_schedule_factor(self) -> float:
        """
        Fracción de horas del día que la zona está en horario activo.
        Ejemplo: 07:00–23:00 → 16/24 = 0.667
        """
        start = _parse_time(self.schedule_start)
        end   = _parse_time(self.schedule_end)
        start_min = start.hour * 60 + start.minute
        end_min   = end.hour   * 60 + end.minute

        if start_min <= end_min:
            active_min = end_min - start_min
        else:                                   # cruce medianoche
            active_min = (24 * 60 - start_min) + end_min

        return round(active_min / (24 * 60), 4)

    # ── Pre-calentamiento ─────────────────────────────────────────────

    def get_preheat_minutes(
        self, setpoint: float, t_current: float | None = None
    ) -> float:
        """
        Calcula minutos necesarios para alcanzar el setpoint desde la
        temperatura actual usando modelo RC de primer orden.

        T(t) = T_inf − (T_inf − T_0) · exp(−t/τ)
        Despejando t: t = τ · ln[(T_inf − T_0) / (T_inf − T_sp)]

        T_inf estimada como setpoint + 2°C (efecto overshooting HVAC).
        """
        if t_current is None:
            t_current = self.get_operative_temperature()
        if t_current is None:
            return self._tau_h * 60.0 * 0.5   # heurístico si no hay lectura

        t_inf   = setpoint + 2.0   # temperatura de equilibrio HVAC ON
        delta_0 = t_inf - t_current
        delta_sp = t_inf - setpoint

        if delta_sp <= 0 or delta_0 <= 0 or delta_sp >= delta_0:
            return 0.0

        try:
            ratio  = delta_0 / delta_sp
            if ratio <= 1.0:
                return 0.0
            t_hours = self._tau_h * math.log(ratio)
            return round(max(0.0, t_hours * 60.0), 1)
        except (ValueError, ZeroDivisionError):
            return 0.0

    # ── Grados día por zona ───────────────────────────────────────────

    def get_zone_dd(self, t_base: float) -> float:
        """Grados Día instantáneos de la zona (HDD basado en T_operativa)."""
        t = self.get_operative_temperature()
        if t is None:
            return 0.0
        return round(max(0.0, t_base - t), 3)

    # ── Punto de rocío ────────────────────────────────────────────────

    def get_dew_point(self) -> float | None:
        """
        Punto de rocío de la zona usando la fórmula de Magnus.
        Lee la humedad del climate entity (si la expone).
        """
        s = self._hass.states.get(self.climate_entity)
        if s is None:
            return None
        rh = _safe_float(s.attributes.get("current_humidity"))
        t  = self.get_operative_temperature()
        if rh is None or t is None or rh <= 0:
            return None
        return round(self.dew_point(t, rh), 1)

    @staticmethod
    def dew_point(t: float, rh: float) -> float:
        """Magnus formula para punto de rocío."""
        α = 17.625
        β = 243.04
        γ = math.log(rh / 100.0) + (α * t) / (β + t)
        return (β * γ) / (α - γ)

    # ── PMV simplificado ─────────────────────────────────────────────

    def get_pmv_simplified(
        self,
        met: float = 1.1,
        clo: float = 1.0,
    ) -> float | None:
        """
        PMV simplificado ISO 7730 con T_operativa de la zona.
        met: actividad metabólica (1.0 = sedentario, 1.2 = de pie)
        clo: aislamiento ropa (0.5 verano, 1.0 invierno)
        """
        t = self.get_operative_temperature()
        s = self._hass.states.get(self.climate_entity)
        rh = _safe_float(
            s.attributes.get("current_humidity") if s else None,
            50.0
        )
        if t is None:
            return None

        met_w     = met * 58.15     # W/m²
        l_neutral = 0.303 * math.exp(-0.036 * met_w) + 0.028
        t_neutral = 21.0 + (clo - 0.5) * 2.0 - (met - 1.0) * 1.5
        pmv       = l_neutral * (met_w - (t - t_neutral) * 3.9 - (rh - 50) * 0.1)
        return round(max(-3.0, min(3.0, pmv)), 2)

    # ── Helper ────────────────────────────────────────────────────────

    def _read(self, entity_id: str):
        if not entity_id:
            return None
        s = self._hass.states.get(entity_id)
        if s is None or s.state in ("unknown", "unavailable", "none", ""):
            return None
        return s.state

    def __repr__(self) -> str:
        return (
            f"ThermalZone(id={self.id!r}, τ={self._tau_h:.2f}h, "
            f"w={self.demand_weight:.3f}, enabled={self.enabled})"
        )
