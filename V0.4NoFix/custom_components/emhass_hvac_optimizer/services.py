"""Services — EMHASS HVAC Optimizer v0.4.0.

Registra y gestiona los 9 servicios HA de la integración.
Llamado desde __init__.py en async_setup_entry / async_unload_entry.

Servicios disponibles:
  force_emhass_mpc        — Ciclo MPC inmediato
  run_autotuner           — Auto-calibración manual
  set_simulation_mode     — Activar/desactivar simulación
  set_zone_schedule       — Modificar horario zona
  set_zone_demand_weight  — Modificar peso demanda zona
  force_seasonal_mode     — Forzar MSC/TOU/auto
  calibrate_ac_limit      — Calibrar límite AC batería
  send_companion_training — Enviar datos Companion App
  set_setpoint_offset     — Offset temporal setpoint zona
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN, LOGGER_NAME,
    SEASONAL_MODE_MSC, SEASONAL_MODE_TOU, ZONE_IDS,
)

if TYPE_CHECKING:
    from .coordinator import EMHASSHVACCoordinator

_LOGGER = logging.getLogger(LOGGER_NAME)

# ── Nombres de servicio ───────────────────────────────────────────────
SVC_FORCE_EMHASS          = "force_emhass_mpc"
SVC_RUN_AUTOTUNER         = "run_autotuner"
SVC_SET_SIM               = "set_simulation_mode"
SVC_SET_ZONE_SCHEDULE     = "set_zone_schedule"
SVC_SET_ZONE_WEIGHT       = "set_zone_demand_weight"
SVC_FORCE_SEASONAL        = "force_seasonal_mode"
SVC_CALIBRATE_AC          = "calibrate_ac_limit"
SVC_COMPANION_TRAIN       = "send_companion_training"
SVC_SETPOINT_OFFSET       = "set_setpoint_offset"

_ZONE_ID_SCHEMA = vol.In(ZONE_IDS)

# ── Schemas de validación ─────────────────────────────────────────────
_SCHEMA_FORCE_EMHASS    = vol.Schema({})
_SCHEMA_AUTOTUNER       = vol.Schema({
    vol.Optional("target", default="all"): vol.In(
        ["all", "thermal", "cop", "zones", "battery"]
    ),
})
_SCHEMA_SIM             = vol.Schema({
    vol.Required("enabled"): cv.boolean,
})
_SCHEMA_ZONE_SCHEDULE   = vol.Schema({
    vol.Required("zone_id"):       _ZONE_ID_SCHEMA,
    vol.Required("schedule_start"): cv.time,
    vol.Required("schedule_end"):   cv.time,
})
_SCHEMA_ZONE_WEIGHT     = vol.Schema({
    vol.Required("zone_id"): _ZONE_ID_SCHEMA,
    vol.Required("weight"):  vol.All(vol.Coerce(float), vol.Range(min=0.05, max=1.0)),
})
_SCHEMA_SEASONAL        = vol.Schema({
    vol.Required("mode"): vol.In(["msc", "tou", "auto"]),
})
_SCHEMA_AC_LIMIT        = vol.Schema({})
_SCHEMA_COMPANION       = vol.Schema({})
_SCHEMA_OFFSET          = vol.Schema({
    vol.Required("zone_id"):         _ZONE_ID_SCHEMA,
    vol.Required("offset_celsius"):  vol.All(
        vol.Coerce(float), vol.Range(min=-5.0, max=5.0)
    ),
    vol.Optional("duration_minutes", default=60): vol.All(
        vol.Coerce(int), vol.Range(min=15, max=480)
    ),
})


# ══════════════════════════════════════════════════════════════════════
# Registro
# ══════════════════════════════════════════════════════════════════════

def async_register_services(hass: HomeAssistant) -> None:
    """
    Registra los 9 servicios. Llamar en async_setup_entry.
    Idempotente: si ya están registrados no hace nada.
    """
    if hass.services.has_service(DOMAIN, SVC_FORCE_EMHASS):
        return

    _register(hass, SVC_FORCE_EMHASS,          _SCHEMA_FORCE_EMHASS,  _handle_force_emhass)
    _register(hass, SVC_RUN_AUTOTUNER,          _SCHEMA_AUTOTUNER,     _handle_autotuner)
    _register(hass, SVC_SET_SIM,                _SCHEMA_SIM,           _handle_sim_mode)
    _register(hass, SVC_SET_ZONE_SCHEDULE,      _SCHEMA_ZONE_SCHEDULE, _handle_zone_schedule)
    _register(hass, SVC_SET_ZONE_WEIGHT,        _SCHEMA_ZONE_WEIGHT,   _handle_zone_weight)
    _register(hass, SVC_FORCE_SEASONAL,         _SCHEMA_SEASONAL,      _handle_seasonal)
    _register(hass, SVC_CALIBRATE_AC,           _SCHEMA_AC_LIMIT,      _handle_ac_limit)
    _register(hass, SVC_COMPANION_TRAIN,        _SCHEMA_COMPANION,     _handle_companion)
    _register(hass, SVC_SETPOINT_OFFSET,        _SCHEMA_OFFSET,        _handle_setpoint_offset)

    _LOGGER.info("EMHASS HVAC: %d servicios registrados", 9)


def async_unregister_services(hass: HomeAssistant) -> None:
    """Elimina los servicios al desinstalar la integración."""
    for svc in (
        SVC_FORCE_EMHASS, SVC_RUN_AUTOTUNER, SVC_SET_SIM,
        SVC_SET_ZONE_SCHEDULE, SVC_SET_ZONE_WEIGHT, SVC_FORCE_SEASONAL,
        SVC_CALIBRATE_AC, SVC_COMPANION_TRAIN, SVC_SETPOINT_OFFSET,
    ):
        if hass.services.has_service(DOMAIN, svc):
            hass.services.async_remove(DOMAIN, svc)


@callback
def _register(hass, name, schema, handler):
    hass.services.async_register(DOMAIN, name, handler, schema=schema)


# ══════════════════════════════════════════════════════════════════════
# Handlers
# ══════════════════════════════════════════════════════════════════════

def _get_coordinator(hass: HomeAssistant) -> "EMHASSHVACCoordinator | None":
    """Retorna el coordinator activo (primer entry)."""
    entries = hass.data.get(DOMAIN, {})
    for coord in entries.values():
        return coord
    return None


async def _handle_force_emhass(call: ServiceCall) -> None:
    """Fuerza ciclo MPC inmediato."""
    hass  = call.hass
    coord = _get_coordinator(hass)
    if not coord:
        _LOGGER.warning("force_emhass_mpc: coordinator no disponible")
        return
    ok = await coord.async_trigger_emhass_mpc()
    _LOGGER.info("force_emhass_mpc: %s", "✓ OK" if ok else "✗ ERROR")


async def _handle_autotuner(call: ServiceCall) -> None:
    """Ejecuta ciclo de auto-calibración."""
    hass   = call.hass
    target = call.data.get("target", "all")
    coord  = _get_coordinator(hass)
    if not coord:
        return

    if target == "all":
        results = await coord.autotuner.async_run_daily_cycle()
        _LOGGER.info(
            "run_autotuner (all): T_base=%.1f | TF=%.4f | errores=%d",
            results.t_base_optimal or coord.dd_model.t_base,
            results.thermal_factor_new or coord.dd_model.thermal_factor,
            len(results.errors),
        )
    elif target == "thermal":
        await coord.autotuner._tune_t_base(
            coord.autotuner.last_results or __import__("dataclasses").dataclass
        )
        await coord.autotuner._tune_thermal_factor(coord.autotuner.last_results)
    elif target == "cop":
        await coord.autotuner._tune_cop_curve(coord.autotuner.last_results)
    elif target == "zones":
        r = coord.autotuner.last_results
        if r:
            for zone in coord.zones:
                await coord.autotuner._tune_zone_tau(r, zone)
                await coord.autotuner._tune_preheat_accuracy(r, zone)
            await coord.autotuner._tune_demand_weights(r)
    elif target == "battery":
        r = coord.autotuner.last_results
        if r:
            await coord.autotuner._tune_battery_efficiency(r)
            await coord.autotuner._tune_emhass_socfinal(r)

    # Refrescar sensores
    await coord.async_request_refresh()


async def _handle_sim_mode(call: ServiceCall) -> None:
    """Activa/desactiva modo simulación en tiempo de ejecución."""
    hass    = call.hass
    enabled = call.data["enabled"]
    coord   = _get_coordinator(hass)
    if not coord:
        return

    coord._sim                       = enabled
    coord.hvac_ctrl._simulation_mode = enabled
    coord.seasonal_mgr._simulation_mode = enabled
    coord.autotuner._sim             = enabled
    _LOGGER.info("Simulation mode → %s", enabled)


async def _handle_zone_schedule(call: ServiceCall) -> None:
    """Modifica horario de confort de una zona."""
    hass   = call.hass
    zid    = call.data["zone_id"]
    start  = call.data["schedule_start"]
    end    = call.data["schedule_end"]
    coord  = _get_coordinator(hass)
    if not coord:
        return

    for zone in coord.zones:
        if zone.id == zid:
            zone.schedule_start = start.strftime("%H:%M")
            zone.schedule_end   = end.strftime("%H:%M")
            _LOGGER.info(
                "Zona %s: horario actualizado %s – %s",
                zid, zone.schedule_start, zone.schedule_end,
            )
            await coord.async_request_refresh()
            return

    _LOGGER.warning("set_zone_schedule: zona '%s' no encontrada", zid)


async def _handle_zone_weight(call: ServiceCall) -> None:
    """Modifica peso de demanda de una zona."""
    hass   = call.hass
    zid    = call.data["zone_id"]
    weight = call.data["weight"]
    coord  = _get_coordinator(hass)
    if not coord:
        return

    for zone in coord.zones:
        if zone.id == zid:
            zone.demand_weight = round(weight, 4)
            _LOGGER.info("Zona %s: demand_weight → %.4f", zid, weight)
            await coord.async_request_refresh()
            return

    _LOGGER.warning("set_zone_demand_weight: zona '%s' no encontrada", zid)


async def _handle_seasonal(call: ServiceCall) -> None:
    """Fuerza modo estacional o devuelve control a evaluación automática."""
    hass  = call.hass
    mode  = call.data["mode"]
    coord = _get_coordinator(hass)
    if not coord:
        return

    if mode == "auto":
        _LOGGER.info("Modo estacional: devuelto a evaluación automática")
        await coord.seasonal_mgr.async_evaluate_mode()
    elif mode == "msc":
        await coord.seasonal_mgr.async_apply_mode(SEASONAL_MODE_MSC)
        _LOGGER.info("Modo estacional forzado: MSC")
    elif mode == "tou":
        await coord.seasonal_mgr.async_apply_mode(SEASONAL_MODE_TOU)
        _LOGGER.info("Modo estacional forzado: TOU")

    await coord.async_request_refresh()


async def _handle_ac_limit(call: ServiceCall) -> None:
    """Fuerza calibración del límite AC de la batería."""
    hass  = call.hass
    coord = _get_coordinator(hass)
    if not coord:
        return
    ok = await coord.ac_dc_limits.async_autocalibrate_ac_limit()
    _LOGGER.info("calibrate_ac_limit: %s | límite=%.0f W",
                 "calibrado" if ok else "sin cambio (datos insuficientes)",
                 coord.ac_dc_limits.ac_limit_w)
    await coord.async_request_refresh()


async def _handle_companion(call: ServiceCall) -> None:
    """Envía datos de entrenamiento a la Companion App."""
    hass  = call.hass
    coord = _get_coordinator(hass)
    if not coord:
        return
    if not coord.companion:
        _LOGGER.warning("send_companion_training: Companion App no habilitada")
        return
    await coord._send_companion_training()


async def _handle_setpoint_offset(call: ServiceCall) -> None:
    """
    Aplica offset temporal al setpoint de una zona.
    El offset expira automáticamente transcurridos duration_minutes.
    """
    hass     = call.hass
    zid      = call.data["zone_id"]
    offset   = call.data["offset_celsius"]
    duration = call.data.get("duration_minutes", 60)
    coord    = _get_coordinator(hass)
    if not coord:
        return

    target_zone = None
    for zone in coord.zones:
        if zone.id == zid:
            target_zone = zone
            break

    if not target_zone:
        _LOGGER.warning("set_setpoint_offset: zona '%s' no encontrada", zid)
        return

    original_sp = target_zone.get_setpoint()
    target_zone.apply_setpoint_offset(offset)
    _LOGGER.info(
        "Zona %s: setpoint offset=%.1f°C | SP %.1f → %.1f°C | expira en %d min",
        zid, offset,
        original_sp or 0,
        (original_sp or 0) + offset,
        duration,
    )

    # Programar retirada del offset
    async def _remove_offset():
        import asyncio as _asyncio
        await _asyncio.sleep(duration * 60)
        target_zone.apply_setpoint_offset(-offset)
        await coord.async_request_refresh()
        _LOGGER.info("Zona %s: offset setpoint retirado (duración %d min)", zid, duration)

    asyncio.ensure_future(_remove_offset())
    await coord.async_request_refresh()
