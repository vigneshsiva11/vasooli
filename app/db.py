"""MongoDB connection lifecycle, backed by Motor's async driver.

A single `AsyncIOMotorClient` is created at application startup and reused for
the process lifetime; Motor maintains its own connection pool, so nothing else
in the codebase should construct a client.
"""

from __future__ import annotations

import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config import get_settings

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None
_database: AsyncIOMotorDatabase | None = None


async def connect_to_mongo() -> None:
    """Open the client and confirm the server is actually reachable.

    Motor connects lazily, so we issue an explicit `ping` here to fail loudly at
    startup rather than on the first real query.
    """
    global _client, _database

    if _client is not None:
        return

    settings = get_settings()
    client: AsyncIOMotorClient = AsyncIOMotorClient(
        settings.mongodb_uri,
        serverSelectionTimeoutMS=5_000,
        uuidRepresentation="standard",
        tz_aware=True,
    )

    await client.admin.command("ping")

    _client = client
    _database = client[settings.mongodb_db_name]
    logger.info("Connected to MongoDB database %r", settings.mongodb_db_name)


async def close_mongo_connection() -> None:
    """Close the client and drop the cached handles."""
    global _client, _database

    if _client is None:
        return

    _client.close()
    _client = None
    _database = None
    logger.info("Closed MongoDB connection")


def get_database() -> AsyncIOMotorDatabase:
    """Return the active database handle.

    Raises:
        RuntimeError: if called before `connect_to_mongo`.
    """
    if _database is None:
        raise RuntimeError(
            "MongoDB is not connected. `connect_to_mongo()` runs during "
            "application startup — call it first."
        )
    return _database


async def ping() -> bool:
    """Return whether the database currently answers a `ping`."""
    if _client is None:
        return False

    try:
        await _client.admin.command("ping")
    except Exception:  # noqa: BLE001 - health checks report, they don't raise
        logger.exception("MongoDB ping failed")
        return False

    return True
