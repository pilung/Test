"""Config Flow — EMHASS HVAC Optimizer v0.4.0.

Wizard 5 pasos:
  Paso 1 — Basic        : URLs EMHASS, sensor global, T_base, sim mode
  Paso 2 — ATH Sensors  : ESPAltherma R1T, R4T, caudal, COP (pre-filled)
  Paso 3 — Global Sensors: SOC, PV, grid, house load (pre-filled)
  Paso 4 — Zones        : 4 zonas Daikin Altherma (4 sub-pasos, pre-filled)
  Paso 5 — Companion+Adv: Companion App URL, depósito inercia

Todas las entidades conocidas del sistema están pre-cargadas.
El usuario sólo necesita confirmar o ajustar.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    CONF_ZONES_CONFIG, DOMAIN, LOGGER_NAME, MAX_ZONES,
    ZONE_IDS,
)
from .flow_helpers import (
    KNOWN_ZONES,
    flatten_config, zones_from_flow_data,
    schema_step_ath, schema_step_basic,
    schema_step_companion_advanced,
    schema_step_global_sensors, schema_step_zone,
)

_LOGGER = logging.getLogger(LOGGER_NAME)

# Nombre de los pasos (para description_placeholders y strings)
_ZONE_NAMES = ["Salón", "Habitación Álvaro", "Habitación Invitados", "Habitación Matrimonio"]


class EMHASSHVACConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config Flow wizard 5 pasos para EMHASS HVAC Optimizer."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._zone_index: int = 0

    # ── Paso 1: Basic ────────────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validar que la URL de EMHASS sea alcanzable (best-effort)
            ok = await self._test_url(user_input.get("emhass_url", ""))
            if not ok:
                errors["emhass_url"] = "cannot_connect"
            else:
                self._data.update(user_input)
                return await self.async_step_ath()

        return self.async_show_form(
            step_id="user",
            data_schema=schema_step_basic(self._data),
            errors=errors,
            description_placeholders={
                "step_num": "1/5",
                "step_name": "Configuración básica",
            },
        )

    # ── Paso 2: Sensores ESPAltherma ────────────────────────────────

    async def async_step_ath(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_global_sensors()

        return self.async_show_form(
            step_id="ath",
            data_schema=schema_step_ath(self._data),
            description_placeholders={
                "step_num": "2/5",
                "step_name": "Sensores ESPAltherma (Daikin Altherma)",
            },
        )

    # ── Paso 3: Sensores globales ────────────────────────────────────

    async def async_step_global_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            self._data.update(user_input)
            self._zone_index = 0
            return await self.async_step_zone()

        return self.async_show_form(
            step_id="global_sensors",
            data_schema=schema_step_global_sensors(self._data),
            description_placeholders={
                "step_num": "3/5",
                "step_name": "Sensores globales PV / batería / red",
            },
        )

    # ── Paso 4: Zonas (1 sub-paso por zona) ─────────────────────────

    async def async_step_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """
        Sub-paso dinámico: muestra el formulario de la zona _zone_index.
        Al completar todas (índice == MAX_ZONES) avanza al paso 5.
        """
        idx = self._zone_index

        if user_input is not None:
            self._data[f"_zone_{idx}_data"] = user_input
            self._zone_index += 1
            if self._zone_index >= MAX_ZONES:
                return await self.async_step_companion_advanced()
            return await self.async_step_zone()

        zone_name = _ZONE_NAMES[idx] if idx < len(_ZONE_NAMES) else f"Zona {idx + 1}"
        defaults  = KNOWN_ZONES[idx] if idx < len(KNOWN_ZONES) else {}

        return self.async_show_form(
            step_id="zone",
            data_schema=schema_step_zone(idx, defaults),
            description_placeholders={
                "step_num":   f"4/5 — zona {idx + 1}/{MAX_ZONES}",
                "step_name":  zone_name,
                "zone_num":   str(idx + 1),
                "zone_name":  zone_name,
            },
        )

    # ── Paso 5: Companion App + Advanced ────────────────────────────

    async def async_step_companion_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            self._data.update(user_input)
            zones  = zones_from_flow_data(self._data)
            config = flatten_config(self._data, zones)
            _LOGGER.info(
                "Config Flow completado | zonas=%d | sim=%s | companion=%s",
                len(zones),
                config.get("simulation_mode"),
                config.get("companion_app_enabled"),
            )
            return self.async_create_entry(
                title="EMHASS HVAC Optimizer",
                data=config,
            )

        return self.async_show_form(
            step_id="companion_advanced",
            data_schema=schema_step_companion_advanced(self._data),
            description_placeholders={
                "step_num":  "5/5",
                "step_name": "Companion App y opciones avanzadas",
            },
        )

    # ── Options Flow ──────────────────────────────────────────────────

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "EMHASSHVACOptionsFlow":
        return EMHASSHVACOptionsFlow(config_entry)

    # ── Helpers ───────────────────────────────────────────────────────

    async def _test_url(self, url: str) -> bool:
        """Verifica que la URL de EMHASS responde (best-effort, no bloquea)."""
        if not url.startswith(("http://", "https://")):
            return False
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    return resp.status < 500
        except Exception:
            return False


# ══════════════════════════════════════════════════════════════════════
# Options Flow (reconfigura opciones post-instalación)
# ══════════════════════════════════════════════════════════════════════

class EMHASSHVACOptionsFlow(config_entries.OptionsFlow):
    """
    Options Flow: permite modificar cualquier parámetro post-instalación
    sin necesidad de reinstalar la integración.
    Los mismos 5 pasos que el Config Flow.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry   = config_entry
        self._data: dict[str, Any] = dict(config_entry.data)
        self._data.update(config_entry.options)
        # Recuperar zonas actuales como sub-pasos
        zones = self._data.get(CONF_ZONES_CONFIG, [])
        for i, z in enumerate(zones):
            self._data[f"_zone_{i}_data"] = z
        self._zone_index = 0

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        return await self.async_step_user(user_input)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_ath()
        return self.async_show_form(
            step_id="user",
            data_schema=schema_step_basic(self._data),
            description_placeholders={
                "step_num": "1/5",
                "step_name": "Configuración básica",
            },
        )

    async def async_step_ath(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_global_sensors()
        return self.async_show_form(
            step_id="ath",
            data_schema=schema_step_ath(self._data),
            description_placeholders={
                "step_num": "2/5",
                "step_name": "Sensores ESPAltherma",
            },
        )

    async def async_step_global_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            self._data.update(user_input)
            self._zone_index = 0
            return await self.async_step_zone()
        return self.async_show_form(
            step_id="global_sensors",
            data_schema=schema_step_global_sensors(self._data),
            description_placeholders={
                "step_num": "3/5",
                "step_name": "Sensores globales",
            },
        )

    async def async_step_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        idx = self._zone_index

        if user_input is not None:
            self._data[f"_zone_{idx}_data"] = user_input
            self._zone_index += 1
            if self._zone_index >= MAX_ZONES:
                return await self.async_step_companion_advanced()
            return await self.async_step_zone()

        zone_name = _ZONE_NAMES[idx] if idx < len(_ZONE_NAMES) else f"Zona {idx + 1}"
        # Defaults: datos actuales de la zona o KNOWN_ZONES
        current_zones = self._data.get(CONF_ZONES_CONFIG, [])
        defaults      = (
            self._data.get(f"_zone_{idx}_data")
            or (current_zones[idx] if idx < len(current_zones) else None)
            or (KNOWN_ZONES[idx] if idx < len(KNOWN_ZONES) else {})
        )

        return self.async_show_form(
            step_id="zone",
            data_schema=schema_step_zone(idx, defaults),
            description_placeholders={
                "step_num":  f"4/5 — zona {idx + 1}/{MAX_ZONES}",
                "step_name": zone_name,
                "zone_num":  str(idx + 1),
                "zone_name": zone_name,
            },
        )

    async def async_step_companion_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            self._data.update(user_input)
            zones  = zones_from_flow_data(self._data)
            config = flatten_config(self._data, zones)
            return self.async_create_entry(title="", data=config)

        return self.async_show_form(
            step_id="companion_advanced",
            data_schema=schema_step_companion_advanced(self._data),
            description_placeholders={
                "step_num":  "5/5",
                "step_name": "Companion App y opciones avanzadas",
            },
        )
