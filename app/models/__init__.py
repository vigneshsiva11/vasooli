"""Pydantic domain models and MongoDB document schemas shared across stages."""

from app.models.diagnosis import (
    ALLOWED_ROOT_CAUSES,
    MAX_EVIDENCE_ITEMS,
    NON_RECOVERABLE_ROOT_CAUSES,
    UNKNOWN_ROOT_CAUSE,
    CheckoutRootCause,
    Diagnosis,
    DiagnosisMethod,
    DiagnosisRecord,
    LLMDiagnosisProposal,
    PaymentRootCause,
    ReceivableRootCause,
    SubscriptionRootCause,
    is_recoverable,
)
from app.models.events import (
    EventCreatedResponse,
    RevenueEvent,
    RevenueEventRecord,
    Surface,
)

__all__ = [
    "ALLOWED_ROOT_CAUSES",
    "MAX_EVIDENCE_ITEMS",
    "NON_RECOVERABLE_ROOT_CAUSES",
    "UNKNOWN_ROOT_CAUSE",
    "CheckoutRootCause",
    "Diagnosis",
    "DiagnosisMethod",
    "DiagnosisRecord",
    "EventCreatedResponse",
    "LLMDiagnosisProposal",
    "PaymentRootCause",
    "ReceivableRootCause",
    "RevenueEvent",
    "RevenueEventRecord",
    "SubscriptionRootCause",
    "Surface",
    "is_recoverable",
]
