"""Companion App REST client — EMHASS HVAC Optimizer v0.4.0.

Gestiona la comunicación HTTP con la Companion App (Docker :8765)
que ejecuta los modelos pesados (RC gray-box, ML online, AutoTuner).

Degradación elegante:
  Si la Companion App no responde → retorna None sin excepción.
  El coordinator detecta is_available=False y usa modelos HA-side
  (Degree Days) como fallback automático.

Deps: aiohttp (incluido en HA core, sin instalación adicional).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from ..const import (
    COMP_ENDPOINT_AUTOTUNER, COMP_ENDPOINT_CTHERMAL,
    COMP_ENDPOINT_COP_PREDICT, COMP_ENDPOINT_COP_TRAIN,
    COMP_ENDPOINT_HEALTH, COMP_ENDPOINT_PREDICT,
    COMP_ENDPOINT_STATUS, COMP_ENDPOINT_TRAIN,
    COMP_RETRY, COMP_TIMEOUT,
    LOGGER_NAME, MODEL_DD,
)

_LOGGER = logging.getLogger(LOGGER_NAME)


class CompanionClient:
    """
    Cliente asíncrono para la Companion App.

    Uso desde coordinator:
        client = CompanionClient(url)
        if await client.async_health_check():
            result = await client.async_thermal_predict(temps, t_base)
            if result:
                forecast_w = result["forecast_w"]
    """

    def __init__(self, url: str) -> None:
        self._url        = url.rstrip("/")
        self._available  = False
        self._model_active: str = MODEL_DD
        self._last_error: str   = ""

    # ── Propiedades ───────────────────────────────────────────────────

    @property
    def is_available(self) -> bool:
        return self._available

    @property
    def model_active(self) -> str:
        return self._model_active

    @property
    def base_url(self) -> str:
        return self._url

    # ── Health check ──────────────────────────────────────────────────

    async def async_health_check(self) -> bool:
        """
        Comprueba disponibilidad de la Companion App.
        Actualiza is_available y model_active.
        """
        data = await self._get(COMP_ENDPOINT_HEALTH)
        if data and data.get("status") == "ok":
            self._available   = True
            self._model_active = data.get("model_active", MODEL_DD)
            _LOGGER.debug(
                "Companion App online | model=%s | days=%s",
                self._model_active,
                data.get("days_history", "?"),
            )
            return True
        self._available = False
        return False

    # ── Predicción térmica ────────────────────────────────────────────

    async def async_thermal_predict(
        self,
        temp_forecast: list[float],
        t_base: float,
        solar_forecast: list[float] | None = None,
    ) -> dict[str, Any] | None:
        """
        Solicita forecast térmico 48h al modelo RC/ML.

        Retorna:
          {
            "forecast_w": list[float],   # W por hora (48 valores)
            "confidence": int,           # 0-100
            "model_active": str          # "rc_model" | "ml_model" | "degree_days"
          }
        """
        payload = {
            "temp_forecast":  temp_forecast,
            "t_base":         t_base,
            "solar_forecast": solar_forecast or [],
        }
        data = await self._post(COMP_ENDPOINT_PREDICT, payload)
        if data:
            self._model_active = data.get("model_active", self._model_active)
        return data

    async def async_thermal_train(
        self,
        temp_hist: list[float],
        energy_hist: list[float],
        timestamps: list[str],
    ) -> dict[str, Any] | None:
        """
        Envía historial para entrenamiento del modelo térmico.

        Retorna:
          {"ok": bool, "r2": float, "thermal_factor": float, "model_used": str}
        """
        payload = {
            "temp_hist":   temp_hist,
            "energy_hist": energy_hist,
            "timestamps":  timestamps,
        }
        return await self._post(COMP_ENDPOINT_TRAIN, payload)

    async def async_thermal_status(self) -> dict[str, Any] | None:
        """
        Estado del modelo térmico activo.

        Retorna:
          {"model": str, "mae_24h": float, "cthermal": float, "rthermal": float}
        """
        return await self._get(COMP_ENDPOINT_STATUS)

    # ── COP ───────────────────────────────────────────────────────────

    async def async_cop_train(
        self,
        t_ext: list[float],
        cop_measured: list[float],
    ) -> dict[str, Any] | None:
        """
        Entrena curva COP con datos históricos.

        Retorna: {"ok": bool, "a": float, "b": float, "c": float, "r2": float}
        """
        return await self._post(COMP_ENDPOINT_COP_TRAIN, {
            "t_ext":        t_ext,
            "cop_measured": cop_measured,
        })

    async def async_cop_predict(
        self, temps: list[float]
    ) -> list[float] | None:
        """
        Predice COP para una lista de temperaturas.

        Retorna: list[float] con COP por temperatura, o None si falla.
        """
        data = await self._post(COMP_ENDPOINT_COP_PREDICT, {"temps": temps})
        if data and "cop_values" in data:
            return data["cop_values"]
        return None

    # ── AutoTuner ────────────────────────────────────────────────────

    async def async_autotuner_run(
        self, param: str = "all"
    ) -> dict[str, Any] | None:
        """
        Dispara ciclo de auto-tuning en la Companion App.
        param: "all" | "thermal" | "cop" | "ac_limit"

        Retorna:
          {"results": {"param": {"old": float, "new": float, "converged": bool}}}
        """
        return await self._post(COMP_ENDPOINT_AUTOTUNER, {"param": param})

    async def async_cthermal_learn(
        self, step_events: list[dict]
    ) -> dict[str, Any] | None:
        """
        Envía eventos step-response para aprender Cthermal del edificio.

        Retorna:
          {"cthermal_kwh_c": float, "n_events": int, "confidence": float}
        """
        return await self._post(COMP_ENDPOINT_CTHERMAL, {"step_events": step_events})

    # ── HTTP helpers ──────────────────────────────────────────────────

    async def _get(self, endpoint: str) -> dict[str, Any] | None:
        return await self._request("GET", endpoint, None)

    async def _post(
        self, endpoint: str, payload: dict
    ) -> dict[str, Any] | None:
        return await self._request("POST", endpoint, payload)

    async def _request(
        self,
        method: str,
        endpoint: str,
        payload: dict | None,
    ) -> dict[str, Any] | None:
        """
        Ejecuta petición HTTP con retry y timeout.
        Nunca lanza excepción: retorna None si algo falla.
        """
        url     = f"{self._url}{endpoint}"
        timeout = aiohttp.ClientTimeout(total=COMP_TIMEOUT)

        for attempt in range(COMP_RETRY + 1):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.request(
                        method, url, json=payload
                    ) as resp:
                        if resp.status == 200:
                            self._available = True
                            return await resp.json()
                        _LOGGER.warning(
                            "Companion App %s %s → HTTP %d",
                            method, endpoint, resp.status,
                        )
                        return None

            except asyncio.TimeoutError:
                self._last_error = f"timeout after {COMP_TIMEOUT}s"
            except aiohttp.ClientConnectorError as exc:
                self._last_error = f"connection refused: {exc}"
            except Exception as exc:
                self._last_error = str(exc)

            if attempt < COMP_RETRY:
                await asyncio.sleep(1.5 * (attempt + 1))

        self._available = False
        _LOGGER.debug(
            "Companion App no disponible | %s | %s",
            endpoint, self._last_error,
        )
        return None
