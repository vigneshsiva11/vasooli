"""Domain models for revenue-at-risk events (Stage 1 — Ingestion)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

#: Where in the merchant's revenue flow the money is at risk.
Surface = Literal["payment", "checkout", "subscription", "receivable"]


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


class RevenueEvent(BaseModel):
    """A single unit of revenue the merchant is at risk of losing.

    This is the system's normalised input shape: every downstream stage reads
    events in this form regardless of which surface produced them.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(
        ...,
        min_length=1,
        description="Upstream identifier for the event, e.g. a Razorpay payment id.",
    )
    surface: Surface = Field(
        ...,
        description="Which revenue surface the risk originated on.",
    )
    amount: float = Field(
        ...,
        gt=0,
        description="Amount at risk, in major currency units.",
    )
    currency: str = Field(
        default="INR",
        min_length=3,
        max_length=3,
        description="ISO 4217 currency code.",
    )
    raw_failure_reason: Optional[str] = Field(
        default=None,
        description="Verbatim upstream failure text, if the surface supplied one.",
    )
    customer_ref: str = Field(
        ...,
        min_length=1,
        description="Merchant-side reference for the customer who owes the money.",
    )
    created_at: datetime = Field(
        default_factory=_utc_now,
        description="When the event entered the system (UTC).",
    )
    status: str = Field(
        default="at_risk",
        description="Lifecycle state of the event.",
    )


class RevenueEventRecord(RevenueEvent):
    """A stored `RevenueEvent`, carrying its MongoDB document id."""

    id: str = Field(..., description="MongoDB document id, rendered as a string.")

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "RevenueEventRecord":
        """Build a record from a raw MongoDB document."""
        fields = {key: value for key, value in document.items() if key != "_id"}
        return cls(id=str(document["_id"]), **fields)


class EventCreatedResponse(BaseModel):
    """Acknowledgement returned once an event has been persisted."""

    id: str = Field(..., description="MongoDB document id of the inserted event.")
    event_id: str = Field(..., description="Echo of the upstream event identifier.")
