from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from prometheus_client import make_asgi_app
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .jev_gateway import JevGateway, build_client
from .judge import Judge
from .metrics import PrometheusMetrics
from .resilience import CircuitBreaker, SingleFlightCache
from .service import EvaluationService

MAX_EVENTS = 2000
MAX_BATCH = 50


class TraceIn(BaseModel):
    model_config = ConfigDict(extra="allow")  # keep goal/agent/final_message/stats etc.
    session_id: str
    events: list[dict[str, Any]] = Field(max_length=MAX_EVENTS)


class BatchIn(BaseModel):
    traces: list[TraceIn] = Field(min_length=1, max_length=MAX_BATCH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    s = Settings.from_env()  # fail fast on missing config
    breaker = CircuitBreaker(s.breaker_failures, s.breaker_cooldown_s)
    metrics = PrometheusMetrics()
    async with build_client(s) as client:  # AsyncTypeSafeClient closes cleanly on shutdown
        gateway = JevGateway(client, breaker=breaker, max_concurrency=s.max_concurrency, metrics=metrics)
        app.state.settings, app.state.breaker = s, breaker
        app.state.service = EvaluationService(
            judge=Judge(gateway, s.thresholds, s.max_questions_per_request),
            thresholds=s.thresholds, tools=s.tools,
            cache=SingleFlightCache(s.cache_ttl_s, s.cache_max_items),
            responder_suffixes=s.responder_suffixes, metrics=metrics)
        yield


app = FastAPI(title="Incident evaluation service (TypeSafe Jev)", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())


async def require_key(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    expected = request.app.state.settings.api_key
    if expected and not (x_api_key and secrets.compare_digest(x_api_key, expected)):
        raise HTTPException(status_code=401, detail="invalid api key")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}  # liveness only: must not depend on Jev


@app.get("/readyz")
async def readyz(request: Request) -> dict[str, str]:
    # Jev being down must NOT make us unready: we degrade to rules-only instead.
    return {"status": "ready", "jev_circuit": request.app.state.breaker.state}


@app.post("/v1/evaluations", dependencies=[Depends(require_key)])
async def evaluate(body: TraceIn, request: Request) -> dict[str, Any]:
    """200 for any well-formed trace. Check `status`: ok | partial | degraded."""
    return await request.app.state.service.evaluate(body.model_dump())


@app.post("/v1/evaluations/batch", dependencies=[Depends(require_key)])
async def evaluate_batch(body: BatchIn, request: Request) -> dict[str, Any]:
    results = await request.app.state.service.evaluate_many([t.model_dump() for t in body.traces])
    return {"results": results}
