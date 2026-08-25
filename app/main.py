"""Vasooli — FastAPI application entrypoint.

Wires up configuration, the MongoDB connection lifecycle, and the health-check
route. Pipeline stages (ingestion → diagnosis → decision → policy → execution →
verification) live in their own packages under `app/` and are not mounted yet.
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
from app.routes import decisions, diagnoses, events, executions, policy

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
        except Exception:  # noqa: BLE001 - reported separately from connection failure
            logger.exception(
                "Failed to ensure MongoDB indexes; uniqueness is NOT enforced"
            )

    try:
        yield
    finally:
        await close_mongo_connection()


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    description="AI revenue recovery agent — the LLM recommends, policy authorizes.",
    version="0.1.0",
    lifespan=lifespan,
)


app.include_router(events.router)
app.include_router(diagnoses.router)
app.include_router(decisions.router)
app.include_router(policy.router)
app.include_router(executions.router)


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
