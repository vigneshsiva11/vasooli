"""Vasooli — FastAPI application entrypoint.

Wires up configuration, the MongoDB connection lifecycle, the health-check route,
and every stage's router. The pipeline stages (ingestion → diagnosis → decision →
policy → execution → verification → promise-to-pay) live in their own packages
under `app/`, with the read-only metrics and audit layer mounted last.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.config import get_settings
from app.db import close_mongo_connection, connect_to_mongo, ping
from app.decision import ensure_indexes as ensure_decision_indexes
from app.diagnosis import check_reachable as check_gemini_reachable
from app.diagnosis import ensure_indexes as ensure_diagnosis_indexes
from app.execution import ensure_indexes as ensure_execution_indexes
from app.ingestion import ensure_indexes as ensure_event_indexes
from app.policy import ensure_indexes as ensure_policy_indexes
from app.ptp import ensure_extraction_indexes
from app.ptp import ensure_indexes as ensure_promise_indexes
from app.routes import (
    decisions,
    diagnoses,
    events,
    executions,
    metrics,
    policy,
    promises,
    webhooks,
)
from app.webhooks import ensure_indexes as ensure_verification_indexes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the MongoDB connection on startup, close it on shutdown.

    A failed connection is logged rather than fatal, so the health endpoint stays
    reachable and can report the database as `disconnected`.
    """
    try:
        await connect_to_mongo()
    except Exception:  # noqa: BLE001 - startup should surface, not crash silently
        logger.exception("MongoDB connection failed during startup")
    else:
        try:
            await ensure_event_indexes()
            await ensure_diagnosis_indexes()
            await ensure_decision_indexes()
            await ensure_policy_indexes()
            await ensure_execution_indexes()
            await ensure_verification_indexes()
            await ensure_promise_indexes()
            # Stage 10. Its own collection, and none of its indexes is unique — see
            # `app/ptp/extraction_store.py` for why an extraction attempt is not a
            # fact that can only be true once.
            await ensure_extraction_indexes()
        except Exception:  # noqa: BLE001 - reported separately from connection failure
            logger.exception(
                "Failed to ensure MongoDB indexes; uniqueness is NOT enforced"
            )

    try:
        yield
    finally:
        await close_mongo_connection()


from fastapi.middleware.cors import CORSMiddleware

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    description="AI revenue recovery agent — the LLM recommends, policy authorizes.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(events.router)
app.include_router(diagnoses.router)
app.include_router(decisions.router)
app.include_router(policy.router)
app.include_router(executions.router)
# Two routers from Stage 6. The webhook receiver has no `database_ready` dependency
# so that signature rejection is unconditional — see `app/routes/webhooks.py`.
app.include_router(webhooks.webhook_router)
app.include_router(webhooks.router)
app.include_router(promises.router)
# Stage 7. Read-only: this router declares GET routes and nothing else, and calls
# no index-ensuring function above because it creates no collection of its own.
app.include_router(metrics.router)


@app.get("/", tags=["health"])
async def health() -> dict[str, str]:
    """Report that the service is up, and whether its dependencies are usable.

    The database and Gemini are checked concurrently so the endpoint costs one
    round trip rather than two, and neither dependency being down makes this
    endpoint fail — a degraded service still reports, it just reports honestly.
    """
    database_connected, (gemini_reachable, gemini_detail) = await asyncio.gather(
        ping(), check_gemini_reachable()
    )

    if not gemini_reachable:
        logger.warning("Gemini reported unreachable by health check: %s", gemini_detail)

    return {
        "status": "ok",
        "service": settings.app_name,
        "version": app.version,
        "environment": settings.environment,
        "database": "connected" if database_connected else "disconnected",
        "gemini": "reachable" if gemini_reachable else "unreachable",
    }
