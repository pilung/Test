"""Seasonal Mode Manager — EMHASS HVAC Optimizer v0.4.0.

Cambia automáticamente el modo de la batería LUNA2000 entre:
  • MSC (Maximize Self-Consumption): verano / alta producción solar
  • TOU (Time of Use):               invierno / baja producción solar

Lógica de decisión:
  1. Calcula ratio PV/Load de los últimos 7 días
  2. Considera el mes del año
  3. Si el modo recomendado ≠ modo actual → aplica cambio

Entidades Huawei actuadas (todas conocidas del sistema):
  • select.bateriasmododefuncionamiento
  • number.bateriasfindedescargasoc

Pure Python. Zero external dependencies.
"""
from __future__ import annotations

import logging
import math
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

from ..const import (
    LOGGER_NAME, MONTHS_SUMMER, MONTHS_WINTER,
    PV_RATIO_MSC_MIN, RECORDER_HOURS_BACK,
    SEASONAL_MODE_MSC, SEASONAL_MODE_TOU,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(LOGGER_NAME)

# Entidades Huawei del sistema actual
_ENTITY_MODE_SELECT  = "select.bateriasmododefuncionamiento"
_ENTITY_SOC_MIN      = "number.bateriasfindedescargasoc"
_ENTITY_SOC_MAX_CHG  = "number.bateriascortedecargaderedsoc"
_ENTITY_PV_POWER     = "sensor.inverterinputpower"
_ENTITY_HOUSE_LOAD   = "sensor.powerhouseload"
_ENTITY_EXCESS_MODE  = "select.bateriasusodeenergiafvexcedenteentou"

# Configuración de modos
_MODE_CONFIG = {
    SEASONAL_MODE_MSC: {
        "battery_mode":  "maximiseselfconsumption",
        "soc_min":       5,     # descarga profunda OK en verano
        "reason_suffix": "alta producción solar → priorizar autoconsumo",
    },
    SEASONAL_MODE_TOU: {
        "battery_mode":  "timeofuseluna2000",
        "soc_min":       12,    # reserva emergencia
        "reason_suffix": "baja producción solar → aprovechar valles",
    },
}


def _safe_float(v, default=None):
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


class SeasonalModeManager:
    """
    Gestor de modos estacionales batería LUNA2000.

    Uso desde coordinator:
        sm = SeasonalModeManager(hass, simulation_mode=True)
        recommended, reason = await sm.async_evaluate_mode()
        # → ("TOU", "Diciembre | ratio PV/Load=0.45 < 1.2")
    """

    def __init__(
        self,
        hass: "HomeAssistant",
        simulation_mode: bool = True,
    ) -> None:
        self._hass            = hass
        self._simulation_mode = simulation_mode
        self._recommended     = SEASONAL_MODE_TOU
        self._reason          = "inicial"
        self._pv_ratio_7d     = 0.0
        self._applied         = False

    # ── Propiedades ───────────────────────────────────────────────────

    @property
    def recommended_mode(self) -> str:
        return self._recommended

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def pv_load_ratio_7d(self) -> float:
        return self._pv_ratio_7d

    @property
    def was_applied(self) -> bool:
        return self._applied

    # ── Evaluación automática ─────────────────────────────────────────

    async def async_evaluate_mode(self) -> tuple[str, str]:
        """
        Evalúa el modo óptimo y lo aplica si difiere del actual.

        Lógica:
          Verano (jun-ago) + ratio≥1.2 → MSC
          Verano (jun-ago) + ratio<1.2  → TOU
          Invierno (dic-feb)             → TOU
          Primavera/Otoño + ratio≥1.2   → MSC
          Primavera/Otoño + ratio<1.2   → TOU

        Retorna: (modo_recomendado, razón_texto)
        """
        pv_7d, load_7d = await self._get_pv_load_kwh_7d()
        ratio   = pv_7d / load_7d if load_7d > 0 else 0.0
        month   = dt_util.now().month
        self._pv_ratio_7d = round(ratio, 3)

        # Lógica de decisión estacional
        if month in MONTHS_SUMMER:
            season = "Verano"
            recommended = SEASONAL_MODE_MSC if ratio >= PV_RATIO_MSC_MIN else SEASONAL_MODE_TOU
        elif month in MONTHS_WINTER:
            season = "Invierno"
            recommended = SEASONAL_MODE_TOU
        else:
            season = "Primavera/Otoño"
            recommended = SEASONAL_MODE_MSC if ratio >= PV_RATIO_MSC_MIN else SEASONAL_MODE_TOU

        reason = (
            f"{season} | mes={month} | ratio PV/Load 7d={ratio:.2f} "
            f"| {_MODE_CONFIG[recommended]['reason_suffix']}"
        )

        self._recommended = recommended
        self._reason      = reason

        # Comparar con modo actual
        current = self._get_current_mode()
        target  = _MODE_CONFIG[recommended]["battery_mode"]

        if current != target:
            _LOGGER.info(
                "[%s] Modo estacional: %s → %s | %s",
                "SIM" if self._simulation_mode else "ACT",
                current, recommended, reason,
            )
            if not self._simulation_mode:
                await self.async_apply_mode(recommended)
                self._applied = True
        else:
            self._applied = False
            _LOGGER.debug("Modo estacional: %s ya activo. Sin cambio.", recommended)

        return recommended, reason

    async def async_apply_mode(self, mode: str) -> None:
        """
        Aplica un modo de batería llamando a servicios HA.
        En simulation_mode: solo registra.

        Acciones:
          1. Cambiar select.bateriasmododefuncionamiento
          2. Ajustar number.bateriasfindedescargasoc (SOC mínimo)
          3. En modo TOU: asegurar excess PV en modo charge
        """
        if mode not in _MODE_CONFIG:
            _LOGGER.error("Modo desconocido: %s", mode)
            return

        cfg = _MODE_CONFIG[mode]

        if self._simulation_mode:
            _LOGGER.info(
                "[SIM] apply_mode=%s | battery_mode=%s | soc_min=%d%%",
                mode, cfg["battery_mode"], cfg["soc_min"],
            )
            return

        # 1. Cambiar modo batería
        await self._hass.services.async_call(
            "select", "select_option",
            {
                "entity_id": _ENTITY_MODE_SELECT,
                "option":    cfg["battery_mode"],
            },
        )
        _LOGGER.info("Modo batería → %s", cfg["battery_mode"])

        # 2. Ajustar SOC mínimo
        await self._hass.services.async_call(
            "number", "set_value",
            {
                "entity_id": _ENTITY_SOC_MIN,
                "value":     cfg["soc_min"],
            },
        )
        _LOGGER.info("SOC mín → %d%%", cfg["soc_min"])

        # 3. En TOU: excedente PV → charge (para cargar desde PV en ventanas baratas)
        if mode == SEASONAL_MODE_TOU:
            state = self._hass.states.get(_ENTITY_EXCESS_MODE)
            if state and state.state != "charge":
                await self._hass.services.async_call(
                    "select", "select_option",
                    {
                        "entity_id": _ENTITY_EXCESS_MODE,
                        "option":    "charge",
                    },
                )

    # ── Métricas PV/Load ─────────────────────────────────────────────

    async def _get_pv_load_kwh_7d(self) -> tuple[float, float]:
        """
        Calcula PV producida y consumo total de los últimos 7 días
        usando el recorder.

        Retorna: (pv_kwh_7d, load_kwh_7d)
        """
        now   = dt_util.now()
        start = now - timedelta(hours=168)   # 7 días

        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import get_significant_states

            instance  = get_instance(self._hass)
            state_map = await instance.async_add_executor_job(
                get_significant_states,
                self._hass, start, now,
                [_ENTITY_PV_POWER, _ENTITY_HOUSE_LOAD],
                None, True,
            )
            pv_states   = state_map.get(_ENTITY_PV_POWER, [])
            load_states = state_map.get(_ENTITY_HOUSE_LOAD, [])

            def _integrate_kwh(states) -> float:
                """Integra potencia en kWh asumiendo 1 lectura/minuto aprox."""
                values = []
                for s in states:
                    if s.state in ("unknown", "unavailable", "none", ""):
                        continue
                    v = _safe_float(s.state)
                    if v is not None and v >= 0:
                        values.append(v)
                if not values:
                    return 0.0
                mean_w = sum(values) / len(values)
                return mean_w * 168 / 1000   # W_medio × 168h / 1000 = kWh

            pv_kwh   = _integrate_kwh(pv_states)
            load_kwh = _integrate_kwh(load_states)
            return pv_kwh, load_kwh

        except Exception as exc:
            _LOGGER.debug("SeasonalManager _get_pv_load_kwh_7d: %s", exc)

        # Fallback: sensores actuales × 168h
        pv_now   = _safe_float(self._read_entity(_ENTITY_PV_POWER))   or 0.0
        load_now = _safe_float(self._read_entity(_ENTITY_HOUSE_LOAD)) or 0.0
        return pv_now * 168 / 1000, load_now * 168 / 1000

    def _get_current_mode(self) -> str:
        """Lee el modo de batería actualmente configurado en Huawei."""
        s = self._hass.states.get(_ENTITY_MODE_SELECT)
        if s and s.state not in ("unknown", "unavailable", "none", ""):
            return s.state
        return ""

    def _read_entity(self, entity_id: str):
        s = self._hass.states.get(entity_id)
        if s is None or s.state in ("unknown", "unavailable", "none", ""):
            return None
        return s.state
