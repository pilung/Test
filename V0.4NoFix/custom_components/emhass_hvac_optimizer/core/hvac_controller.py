"""HVAC Controller — EMHASS HVAC Optimizer v0.4.0.

Estrategias de control HVAC coordinadas con PV y batería:
  • Solar-Assisted Heating  : exceso PV > 1 kW → boost setpoint +2 °C
  • Battery-Assisted Cooling: precio caro + SOC alto → descarga batería
  • Control por zonas con demand_weight y factor horario
  • Simulation mode: registra decisiones sin llamar a servicios HA

Pure Python. Zero external dependencies.
"""
from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from ..const import (
    AC_SOLAR_THRESHOLD, LOGGER_NAME,
    PRICE_EXPENSIVE, SEASONAL_MODE_MSC,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from ..models.thermal_zone import ThermalZone

_LOGGER = logging.getLogger(LOGGER_NAME)

# Umbrales de control
_SOLAR_EXCESS_W        = 1_000.0   # W exceso PV mínimo para solar heating
_SOLAR_SETPOINT_BOOST  = 2.0       # °C boost setpoint con solar excess
_BATT_SOC_MIN_COOLING  = 60.0      # % SOC mínimo para battery-assisted cooling
_TEMP_MARGIN_PREHEAT   = 1.5       # °C margen bajo setpoint para activar preheat
_EXPORT_PRICE_LOW      = 0.08      # €/kWh — precio export bajo → mejor usar en edificio
_MAX_SETPOINT_HEAT     = 24.0      # °C — setpoint máximo calefacción
_MIN_SETPOINT_COOL     = 20.0      # °C — setpoint mínimo refrigeración

# Etiquetas modo HVAC
MODE_AUTO         = "auto_optimal"
MODE_SOLAR        = "solar_assisted"
MODE_BATTERY      = "battery_assisted"
MODE_MANUAL       = "manual_override"
MODE_STANDBY      = "standby"


class HVACController:
    """
    Controlador HVAC coordinado con PV y batería.

    Modo simulación (simulation_mode=True):
      • Calcula y registra todas las decisiones
      • NO llama a ningún servicio de HA
      • Útil para los primeros 7-14 días de observación
    """

    def __init__(
        self,
        hass: "HomeAssistant",
        simulation_mode: bool = True,
        seasonal_mode: str = SEASONAL_MODE_MSC,
    ) -> None:
        self._hass            = hass
        self._simulation_mode = simulation_mode
        self._seasonal_mode   = seasonal_mode
        self._current_mode    = MODE_STANDBY
        self._solar_opp_kwh   = 0.0
        self._last_boost: dict[str, float] = {}  # zone_id → boost °C

    # ── Propiedades ───────────────────────────────────────────────────

    @property
    def current_mode(self) -> str:
        return self._current_mode

    @property
    def solar_opportunity_kwh(self) -> float:
        return self._solar_opp_kwh

    @property
    def simulation_mode(self) -> bool:
        return self._simulation_mode

    @simulation_mode.setter
    def simulation_mode(self, value: bool) -> None:
        self._simulation_mode = value

    # ── Estrategia 1: Solar-Assisted Heating ─────────────────────────

    async def async_solar_assisted_heating(
        self,
        zones: list["ThermalZone"],
        pv_power_w: float,
        base_load_w: float,
        export_price: float,
    ) -> dict[str, float]:
        """
        Aprovecha exceso solar para pre-calentar el edificio.

        Condiciones de activación:
          • Exceso PV = pv_power_w − base_load_w > _SOLAR_EXCESS_W
          • Precio export < _EXPORT_PRICE_LOW (mejor almacenar en calor)
          • Al menos una zona activa con T < setpoint − _TEMP_MARGIN_PREHEAT

        Retorna: dict {zone_id: setpoint_nuevo} para las zonas boosteadas.
        """
        boosts: dict[str, float] = {}
        excess = pv_power_w - base_load_w

        if excess < _SOLAR_EXCESS_W:
            return boosts
        if export_price > _EXPORT_PRICE_LOW:
            return boosts

        self._solar_opp_kwh = round(excess * (30 / 60) / 1000, 3)  # kWh en 30 min

        for zone in zones:
            if not zone.enabled or not zone.is_in_schedule():
                continue
            t_op      = zone.get_operative_temperature()
            setpoint  = zone.get_setpoint()
            if t_op is None or setpoint is None:
                continue
            if t_op < setpoint - _TEMP_MARGIN_PREHEAT:
                new_sp = min(setpoint + _SOLAR_SETPOINT_BOOST, _MAX_SETPOINT_HEAT)
                boosts[zone.id] = new_sp
                _LOGGER.info(
                    "[%s] Solar-Assisted Heating | zona=%s | "
                    "T_op=%.1f°C | SP=%.1f→%.1f°C | exceso=%.0fW",
                    "SIM" if self._simulation_mode else "ACT",
                    zone.name, t_op, setpoint, new_sp, excess,
                )
                if not self._simulation_mode and zone.climate_entity:
                    await self._set_temperature(zone.climate_entity, new_sp)
                self._last_boost[zone.id] = _SOLAR_SETPOINT_BOOST

        if boosts:
            self._current_mode = MODE_SOLAR
        return boosts

    # ── Estrategia 2: Battery-Assisted Cooling ────────────────────────

    async def async_battery_assisted_cooling(
        self,
        zones: list["ThermalZone"],
        price_import: float,
        soc_battery: float,
    ) -> dict[str, float]:
        """
        Usa la batería para refrigeración en horas de precio alto.

        Condiciones de activación:
          • price_import > PRICE_EXPENSIVE (0.26 €/kWh)
          • soc_battery > _BATT_SOC_MIN_COOLING (60%)
          • Zona necesita refrigeración: T > setpoint + 1 °C

        Retorna: dict {zone_id: setpoint_nuevo} para las zonas ajustadas.
        """
        adjustments: dict[str, float] = {}

        if price_import <= PRICE_EXPENSIVE or soc_battery <= _BATT_SOC_MIN_COOLING:
            return adjustments

        for zone in zones:
            if not zone.enabled or not zone.is_in_schedule():
                continue
            if zone.get_hvac_mode() != "cool":
                continue
            t_op     = zone.get_operative_temperature()
            setpoint = zone.get_setpoint()
            if t_op is None or setpoint is None:
                continue
            if t_op > setpoint + 1.0:
                new_sp = max(setpoint - 1.0, _MIN_SETPOINT_COOL)
                adjustments[zone.id] = new_sp
                _LOGGER.info(
                    "[%s] Battery-Assisted Cooling | zona=%s | "
                    "precio=%.3f€/kWh | SOC=%.1f%% | T=%.1f°C | SP→%.1f°C",
                    "SIM" if self._simulation_mode else "ACT",
                    zone.name, price_import, soc_battery, t_op, new_sp,
                )
                if not self._simulation_mode and zone.climate_entity:
                    await self._set_temperature(zone.climate_entity, new_sp)

        if adjustments:
            self._current_mode = MODE_BATTERY
        return adjustments

    # ── Control de zonas por horario ──────────────────────────────────

    async def async_apply_zone_schedules(
        self, zones: list["ThermalZone"]
    ) -> dict[str, str]:
        """
        Aplica el horario de cada zona (ON/OFF según schedule).
        Respeta demand_weight para decidir prioridad de encendido.

        Retorna: dict {zone_id: "on"/"off"/"skip"} con decisiones.
        """
        decisions: dict[str, str] = {}
        for zone in sorted(zones, key=lambda z: -z.demand_weight):
            if not zone.enabled or not zone.climate_entity:
                decisions[zone.id] = "skip"
                continue

            should_on = zone.is_in_schedule()
            decisions[zone.id] = "on" if should_on else "off"

            _LOGGER.debug(
                "[%s] Zona %s | schedule=%s-%s | activa=%s",
                "SIM" if self._simulation_mode else "ACT",
                zone.name, zone.schedule_start, zone.schedule_end, should_on,
            )

            if not self._simulation_mode:
                hvac_mode = zone.get_hvac_mode()
                if should_on and hvac_mode == "off":
                    await self._set_hvac_mode(zone.climate_entity, "heat")
                elif not should_on and hvac_mode != "off":
                    await self._set_hvac_mode(zone.climate_entity, "off")

        if any(v == "on" for v in decisions.values()):
            if self._current_mode not in (MODE_SOLAR, MODE_BATTERY):
                self._current_mode = MODE_AUTO
        else:
            self._current_mode = MODE_STANDBY

        return decisions

    # ── Oportunidad solar ──────────────────────────────────────────────

    def calculate_solar_opportunity(
        self,
        pv_forecast: list[float],
        base_load_w: float,
        t_interior_avg: float | None,
        setpoint_avg: float | None,
        cthermal_kwh_c: float = 5.0,
    ) -> float:
        """
        Calcula energía térmica aprovechable de excedentes PV próximas 4h.
        Considera capacidad térmica del edificio (Cthermal).

        Retorna: kWh disponibles para pre-calentamiento.
        """
        if not pv_forecast:
            return 0.0

        # Suma de excedentes en las próximas 4h (slots de 1h)
        excess_kwh = sum(
            max(0.0, pv_w - base_load_w) / 1000.0
            for pv_w in pv_forecast[:4]
        )

        # Límite por capacidad térmica del edificio
        if (t_interior_avg is not None
                and setpoint_avg is not None
                and cthermal_kwh_c > 0):
            temp_margin   = max(0.0, setpoint_avg - t_interior_avg + 2.0)
            thermal_limit = cthermal_kwh_c * temp_margin
            excess_kwh    = min(excess_kwh, thermal_limit)

        self._solar_opp_kwh = round(excess_kwh, 3)
        return self._solar_opp_kwh

    # ── Helpers privados ──────────────────────────────────────────────

    async def _set_temperature(self, entity_id: str, temp: float) -> None:
        await self._hass.services.async_call(
            "climate", "set_temperature",
            {"entity_id": entity_id, "temperature": temp},
        )

    async def _set_hvac_mode(self, entity_id: str, mode: str) -> None:
        await self._hass.services.async_call(
            "climate", "set_hvac_mode",
            {"entity_id": entity_id, "hvac_mode": mode},
        )
