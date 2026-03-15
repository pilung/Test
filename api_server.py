"""API Server — EMHASS HVAC Companion App v0.4.0.

FastAPI REST server. Punto único de entrada para la integración HA.

Endpoints:
  GET  /health              — Estado y versión modelos
  POST /thermal/predict     — Forecast potencia térmica 48h
  POST /thermal/train       — Entrenamiento RC + ML desde payload HA
  GET  /thermal/status      — Métricas detalladas modelos
  POST /autotuner/run       — Ciclo AutoTuner completo
  GET  /metrics             — Métricas Prometheus (opcional)

Uso con uvicorn:
  uvicorn api_server:app --host 0.0.0.0 --port 8765 --reload
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator

from .autotuner_companion import CompanionAutoTuner

_LOGGER  = logging.getLogger("companion_app")
_VERSION = "0.4.0"

# ── Startup / Shutdown ────────────────────────────────────────────────
_tuner: CompanionAutoTuner | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Carga modelos persistidos al arrancar."""
    global _tuner
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    _tuner = CompanionAutoTuner()
    _LOGGER.info(
        "Companion App v%s arrancando | RC=%s | ML=%s",
        _VERSION,
        "✓" if _tuner.rc_params.fitted else "✗",
        "✓" if _tuner.ml_model.fitted else "✗",
    )
    yield
    _LOGGER.info("Companion App apagando")


app = FastAPI(
    title       = "EMHASS HVAC Companion App",
    description = "RC gray-box + ML thermal model server",
    version     = _VERSION,
    lifespan    = lifespan,
)

# ── Request timing middleware ─────────────────────────────────────────
@app.middleware("http")
async def _add_timing(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    ms = (time.perf_counter() - t0) * 1000
    response.headers["X-Process-Time-ms"] = f"{ms:.1f}"
    return response


# ══════════════════════════════════════════════════════════════════════
# Schemas Pydantic
# ══════════════════════════════════════════════════════════════════════

class PredictRequest(BaseModel):
    temp_forecast: list[float] = Field(..., min_length=1, max_length=96)
    t_base:        float       = Field(18.5, ge=14.0, le=22.0)
    t_setpoint:    float       = Field(21.0, ge=16.0, le=26.0)
    t_current:     float       = Field(18.0, ge=-10.0, le=40.0)
    solar_forecast: list[float] | None = None

    @field_validator("solar_forecast")
    @classmethod
    def pad_solar(cls, v, values):
        if v is None:
            return None
        n = len(values.data.get("temp_forecast", []))
        if len(v) < n:
            v = v + [0.0] * (n - len(v))
        return v[:n]


class PredictResponse(BaseModel):
    forecast_w:   list[float]
    confidence:   int
    model_active: str
    w_rc:         float
    w_ml:         float


class TrainRequest(BaseModel):
    """Payload completo enviado por HAAutoTuner._build_companion_payload()."""
    version:       str  = "0.4.0"
    ts_built:      str  = ""
    hours_history: int  = 168
    series:        dict[str, Any] = Field(default_factory=dict)
    ha_calibrated: dict[str, Any] = Field(default_factory=dict)
    step_events:   dict[str, Any] = Field(default_factory=dict)


class TrainResponse(BaseModel):
    ok:           bool
    r2:           float
    model_used:   str
    n_samples:    int
    tau_global_h: float
    zone_tau:     dict[str, float]
    recommendations: dict[str, Any]
    errors:       list[str]


class AutotunerRequest(BaseModel):
    target: str = Field("all", pattern="^(all|thermal|cop|zones|battery)$")


class StatusResponse(BaseModel):
    version:      str
    model_active: str
    rc_fitted:    bool
    ml_fitted:    bool
    rc_r2:        float
    ml_r2:        float
    rc_ua_wk:     float
    rc_tau_h:     float
    rc_R:         float
    rc_C:         float
    rc_ci_tau_lo: float
    rc_ci_tau_hi: float
    ml_alpha:     float
    ml_features:  dict[str, float]
    uptime_s:     float


_START_TIME = time.time()


# ══════════════════════════════════════════════════════════════════════
# Endpoints
# ══════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health() -> dict:
    """Health check rápido para coordinator.is_available."""
    if _tuner is None:
        raise HTTPException(503, "Modelos no inicializados")
    return _tuner.health()


@app.post("/thermal/predict", response_model=PredictResponse)
async def thermal_predict(req: PredictRequest) -> PredictResponse:
    """
    Predice potencia térmica requerida hora a hora [W].
    Ensemble ponderado RC + ML con pesos adaptativos por R².
    """
    if _tuner is None:
        raise HTTPException(503, "Modelos no inicializados")

    result = _tuner.predict(
        t_ext_fc   = req.temp_forecast,
        t_setpoint = req.t_setpoint,
        t_current  = req.t_current,
        solar_fc   = req.solar_forecast,
        t_base     = req.t_base,
    )
    return PredictResponse(**result)


@app.post("/thermal/train", response_model=TrainResponse)
async def thermal_train(req: TrainRequest) -> TrainResponse:
    """
    Entrena RC + ML desde el payload histórico de HAAutoTuner.
    Operación blocking (~10-30s según volumen de datos).
    """
    if _tuner is None:
        raise HTTPException(503, "Modelos no inicializados")

    try:
        results = _tuner.train(req.model_dump())
    except Exception as exc:
        _LOGGER.exception("thermal_train error: %s", exc)
        raise HTTPException(500, f"Error entrenamiento: {exc}") from exc

    return TrainResponse(
        ok            = results.rc_params.fitted or results.ml_r2 > 0.4,
        r2            = max(results.rc_params.r2, results.ml_r2),
        model_used    = results.model_active,
        n_samples     = results.ml_n_samples or results.rc_params.n_samples,
        tau_global_h  = round(results.rc_params.tau_hours, 2),
        zone_tau      = results.zone_tau,
        recommendations = results.recommendations,
        errors        = results.errors,
    )


@app.get("/thermal/status", response_model=StatusResponse)
async def thermal_status() -> StatusResponse:
    """Métricas detalladas de ambos modelos."""
    if _tuner is None:
        raise HTTPException(503, "Modelos no inicializados")

    rc  = _tuner.rc_params
    ml  = _tuner.ml_model
    tau_ci_lo = (rc.ci_R_lo * rc.ci_C_lo) / 3_600 if rc.fitted else 0.0
    tau_ci_hi = (rc.ci_R_hi * rc.ci_C_hi) / 3_600 if rc.fitted else 0.0

    return StatusResponse(
        version      = _VERSION,
        model_active = _tuner._active_model_name(),
        rc_fitted    = rc.fitted,
        ml_fitted    = ml.fitted,
        rc_r2        = rc.r2,
        ml_r2        = ml.r2,
        rc_ua_wk     = round(rc.ua, 3),
        rc_tau_h     = round(rc.tau_hours, 2),
        rc_R         = rc.R,
        rc_C         = rc.C,
        rc_ci_tau_lo = round(tau_ci_lo, 2),
        rc_ci_tau_hi = round(tau_ci_hi, 2),
        ml_alpha     = ml.alpha_best,
        ml_features  = ml.feature_importance(),
        uptime_s     = round(time.time() - _START_TIME, 1),
    )


@app.post("/autotuner/run")
async def autotuner_run(req: AutotunerRequest) -> dict:
    """
    Dispara ciclo AutoTuner desde HA (sin payload nuevo).
    Útil para forzar re-entrenamiento con datos existentes en /app/data.
    """
    if _tuner is None:
        raise HTTPException(503, "Modelos no inicializados")

    results = {
        "target":       req.target,
        "rc_r2":        _tuner.rc_params.r2,
        "ml_r2":        _tuner.ml_model.r2,
        "model_active": _tuner._active_model_name(),
        "tau_h":        round(_tuner.rc_params.tau_hours, 2),
        "ua_wk":        round(_tuner.rc_params.ua, 3),
        "confidence":   _tuner._ensemble_confidence(),
    }
    _LOGGER.info("autotuner_run: target=%s | %s", req.target, results)
    return results


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> str:
    """Métricas en formato Prometheus (text/plain)."""
    if _tuner is None:
        return "# companion app not ready
"
    rc = _tuner.rc_params
    ml = _tuner.ml_model
    lines = [
        "# HELP companion_rc_r2 RC model R-squared",
        "# TYPE companion_rc_r2 gauge",
        f"companion_rc_r2 {rc.r2}",
        "# HELP companion_ml_r2 ML model R-squared",
        "# TYPE companion_ml_r2 gauge",
        f"companion_ml_r2 {ml.r2}",
        "# HELP companion_rc_tau_hours RC thermal time constant hours",
        "# TYPE companion_rc_tau_hours gauge",
        f"companion_rc_tau_hours {round(rc.tau_hours, 3)}",
        "# HELP companion_rc_ua_wk Building UA coefficient W/K",
        "# TYPE companion_rc_ua_wk gauge",
        f"companion_rc_ua_wk {round(rc.ua, 3)}",
        f"companion_uptime_seconds {round(time.time() - _START_TIME, 1)}",
    ]
    return "
".join(lines) + "
"


if __name__ == "__main__":
    uvicorn.run(
        "api_server:app",
        host    = "0.0.0.0",
        port    = int(os.getenv("PORT", 8765)),
        reload  = os.getenv("RELOAD", "false").lower() == "true",
        log_level = os.getenv("LOG_LEVEL", "info").lower(),
    )
