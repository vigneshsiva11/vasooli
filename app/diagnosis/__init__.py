"""Stage 2 — Diagnosis.

Determines *why* revenue is at risk: root cause, confidence score, supporting
evidence, and whether the amount is recoverable at all.

This is the only stage where the LLM reasons freely. Its output is a
*recommendation* — never an executed action.
"""

from app.diagnosis.gemini import check_reachable, reset_reachability_cache
from app.diagnosis.service import diagnose, normalise_root_cause
from app.diagnosis.store import (
    COLLECTION_NAME,
    VERSION_INDEX,
    append,
    ensure_indexes,
    latest_diagnosis,
    latest_version,
    list_diagnoses,
)

__all__ = [
    "COLLECTION_NAME",
    "VERSION_INDEX",
    "append",
    "check_reachable",
    "diagnose",
    "ensure_indexes",
    "latest_diagnosis",
    "latest_version",
    "list_diagnoses",
    "normalise_root_cause",
    "reset_reachability_cache",
]
