"""EMHASS HVAC Optimizer — __init__.py v0.4.0.

Punto de entrada de la integración HA.
Orquesta: coordinator, plataformas y 9 servicios.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    DOMAIN, LOGGER_NAME, PLATFORMS,
    UPDATE_INTERVAL_SECONDS,
)
from .coordinator import EMHASSHVACCoordinator
from .services import async_register_services, async_unregister_services

_LOGGER = logging.getLogger(LOGGER_NAME)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Configura la integración desde una config entry."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = EMHASSHVACCoordinator(
        hass            = hass,
        entry           = entry,
        update_interval = timedelta(seconds=UPDATE_INTERVAL_SECONDS),
    )

    # Primera actualización — si falla es error crítico de configuración
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as exc:
        raise ConfigEntryNotReady(
            f"EMHASS HVAC Optimizer: primera actualización falló: {exc}"
        ) from exc

    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Registrar plataformas (sensor)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Registrar servicios (idempotente)
    async_register_services(hass)

    # Listener para recargar cuando cambien opciones
    entry.async_on_unload(
        entry.add_update_listener(_async_update_listener)
    )

    _LOGGER.info(
        "EMHASS HVAC Optimizer iniciado ✓ | entry=%s | sim=%s | zonas=%d | "
        "companion=%s",
        entry.entry_id[:8],
        coordinator.simulation_mode,
        len(coordinator.zones),
        "activa" if coordinator.companion else "inactiva",
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Desinstala la integración."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    # Eliminar servicios sólo si no quedan más entries
    if not hass.data[DOMAIN]:
        async_unregister_services(hass)

    _LOGGER.info("EMHASS HVAC Optimizer descargado | entry=%s", entry.entry_id[:8])
    return unload_ok


async def _async_update_listener(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Recarga la integración cuando cambian las opciones."""
    _LOGGER.info(
        "EMHASS HVAC Optimizer: opciones actualizadas, recargando entry=%s",
        entry.entry_id[:8],
    )
    await hass.config_entries.async_reload(entry.entry_id)
