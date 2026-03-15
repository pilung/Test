"""Price Manager — EMHASS HVAC Optimizer v0.4.0.

Abstracción multi-fuente de precios de electricidad con
auto-detección y fallback cascade.

Fuentes soportadas (por prioridad):
  1. PVPC      — sensor.pvpc_precio_actual  (HACS integration)
  2. Nordpool  — sensor.nordpool_kwh_es_eur_3_10_0_25
  3. Tibber    — sensor.electricity_price_home
  4. TOU fijo  — sensor.preciokwh  (template sistema actual)

Pure Python. Zero external dependencies.
"""
from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from ..const import (
    LOGGER_NAME,
    PRICE_CHEAP, PRICE_EXPENSIVE, PRICE_NEGATIVE,
    PRICE_NORMAL, PRICE_VERY_CHEAP,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(LOGGER_NAME)

# ── Definición de fuentes ────────────────────────────────────────────
_SOURCES: dict[str, dict] = {
    "pvpc": {
        "entities":    ["sensor.pvpc_precio_actual", "sensor.esios_pvpc"],
        "type":        "dynamic",
        "priority":    1,
        "today_attr":  "today",
        "tomorrow_attr": "tomorrow",
    },
    "nordpool": {
        "entities":    ["sensor.nordpool_kwh_es_eur_3_10_0_25",
                        "sensor.nordpool_kwh_es_eur"],
        "type":        "dynamic",
        "priority":    1,
        "today_attr":  "today",
        "tomorrow_attr": "tomorrow",
    },
    "tibber": {
        "entities":    ["sensor.electricity_price_home",
                        "sensor.tibber_price_home"],
        "type":        "dynamic",
        "priority":    1,
        "today_attr":  "today",
        "tomorrow_attr": "tomorrow",
    },
    "tou_fixed": {
        "entities":    ["sensor.preciokwh"],
        "type":        "static",
        "priority":    3,
        "today_attr":  "today",
        "tomorrow_attr": "tomorrow",
    },
}
_EXPORT_PRICE_DEFAULT = 0.06   # €/kWh flat (actual del sistema)


def _safe_float(v, default=None):
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


class PriceManager:
    """
    Gestor de precios de electricidad multi-fuente.

    Uso desde coordinator:
        pm = PriceManager(hass)
        source = pm.get_active_source()
        forecast = pm.get_forecast(horizon_hours=48)
        status   = pm.get_price_grid_status()
    """

    def __init__(self, hass: "HomeAssistant") -> None:
        self._hass           = hass
        self._active_source  = "tou_fixed"
        self._entity_cache: dict[str, str | None] = {}   # source → entity_id

    # ── Fuente activa ────────────────────────────────────────────────

    def get_active_source(self) -> str:
        """
        Auto-detecta la mejor fuente de precios disponible.
        Prioridad: dinámicas (1) > TOU fijo (3).
        Fallback final: tou_fixed.
        """
        best: tuple[int, str] | None = None

        for src_name, cfg in _SOURCES.items():
            entity = self._find_entity(src_name)
            if entity is None:
                continue
            prio = cfg["priority"]
            if best is None or prio < best[0]:
                best = (prio, src_name)

        self._active_source = best[1] if best else "tou_fixed"
        return self._active_source

    def _find_entity(self, source_name: str) -> str | None:
        """Encuentra el primer entity_id válido para una fuente."""
        if source_name in self._entity_cache:
            return self._entity_cache[source_name]

        for eid in _SOURCES[source_name]["entities"]:
            state = self._hass.states.get(eid)
            if state and state.state not in ("unknown", "unavailable", "none", ""):
                self._entity_cache[source_name] = eid
                return eid

        self._entity_cache[source_name] = None
        return None

    # ── Precio actual ────────────────────────────────────────────────

    def get_current_price(self) -> float | None:
        """Precio de importación actual en €/kWh."""
        source = self.get_active_source()
        entity = self._find_entity(source)
        if not entity:
            return None
        state = self._hass.states.get(entity)
        if not state:
            return None
        # Intenta leer el estado directamente (sensor.preciokwh lo expone así)
        val = _safe_float(state.state)
        if val is not None:
            return val
        # Intenta atributo 'price' (Tibber, Nordpool)
        val = _safe_float(state.attributes.get("price"))
        return val

    def get_export_price(self) -> float:
        """Precio de exportación actual en €/kWh."""
        state = self._hass.states.get("sensor.preciokwhexport")
        if state and state.state not in ("unknown", "unavailable", "none"):
            v = _safe_float(state.state)
            if v is not None:
                return v
        return _EXPORT_PRICE_DEFAULT

    # ── Forecast 48h ────────────────────────────────────────────────

    def get_forecast(self, horizon_hours: int = 48) -> dict:
        """
        Retorna vectores de precios import/export para las próximas
        horizon_hours horas.

        Retorna:
          {
            "import":     list[float],   # €/kWh por hora
            "export":     list[float],   # €/kWh por hora
            "source":     str,
            "confidence": int,           # 100 dinámico / 80 estático
          }
        """
        source = self.get_active_source()
        entity = self._find_entity(source)
        cfg    = _SOURCES[source]

        imp = self._extract_vector(entity, cfg["today_attr"], cfg["tomorrow_attr"])
        exp = [_EXPORT_PRICE_DEFAULT] * 48

        if len(imp) < horizon_hours:
            # Rellenar repitiendo patrón de 24h
            while len(imp) < horizon_hours:
                imp.extend(imp[:24])
        imp = imp[:horizon_hours]
        exp = exp[:horizon_hours]

        return {
            "import":     imp,
            "export":     exp,
            "source":     source,
            "confidence": 100 if cfg["type"] == "dynamic" else 80,
        }

    def _extract_vector(
        self, entity_id: str | None,
        today_attr: str, tomorrow_attr: str
    ) -> list[float]:
        """
        Extrae vector de precios de los atributos today/tomorrow.
        Fallback: repite el precio actual 48 veces.
        """
        fallback_price = self.get_current_price() or 0.18
        fallback       = [fallback_price] * 48

        if not entity_id:
            return fallback

        state = self._hass.states.get(entity_id)
        if not state:
            return fallback

        today    = state.attributes.get(today_attr, []) or []
        tomorrow = state.attributes.get(tomorrow_attr, []) or []

        def _to_floats(lst) -> list[float]:
            result: list[float] = []
            for item in (lst if isinstance(lst, list) else []):
                if isinstance(item, dict):
                    v = _safe_float(item.get("price") or item.get("value"))
                else:
                    v = _safe_float(item)
                if v is not None:
                    result.append(v)
            return result

        prices = _to_floats(today) + _to_floats(tomorrow)
        return prices if prices else fallback

    # ── Estado del precio ────────────────────────────────────────────

    def get_price_grid_status(self) -> str:
        """
        Clasifica el precio actual según umbrales de const.py.

        Estados:
          NEGATIVE | VERY_CHEAP | CHEAP | NORMAL | EXPENSIVE | VERY_EXPENSIVE
        """
        price = self.get_current_price()
        if price is None:
            return "UNKNOWN"
        if price <= PRICE_NEGATIVE:
            return "NEGATIVE"
        if price <= PRICE_VERY_CHEAP:
            return "VERY_CHEAP"
        if price <= PRICE_CHEAP:
            return "CHEAP"
        if price <= PRICE_NORMAL:
            return "NORMAL"
        if price <= PRICE_EXPENSIVE:
            return "EXPENSIVE"
        return "VERY_EXPENSIVE"

    def detect_negative_price_slots(
        self, forecast: list[float]
    ) -> list[int]:
        """
        Detecta slots con precio negativo u oportunidades de carga masiva.
        Retorna índices de slots donde price ≤ PRICE_NEGATIVE.
        """
        return [i for i, p in enumerate(forecast) if p <= PRICE_NEGATIVE]

    # ── Info pública ──────────────────────────────────────────────────

    def get_available_sources(self) -> list[str]:
        """Lista de fuentes con entidad disponible en HA."""
        return [
            src for src in _SOURCES
            if self._find_entity(src) is not None
        ]

    def invalidate_cache(self) -> None:
        """Limpia caché de entidades (llamar si cambia configuración)."""
        self._entity_cache.clear()
