"""Stage 3 — Decision.

Scores each candidate intervention by Expected Recovery Value:

    ERV = (amount at risk) x (probability of recovery) - (cost of action)

and selects the highest-ERV option from a FIXED, bounded list of allowed
interventions. No LLM participates in this stage, so no new action type can be
introduced at runtime: the catalogue is a `Literal`, and adding to it is a
reviewable change to a type.

This stage RECOMMENDS. It cannot authorize and it cannot execute — there is no
field on `Decision` capable of expressing either, and nothing in this package
imports the policy gate, the execution layer, or a Razorpay client.
"""

from app.decision.engine import COST_CURRENCY, decide, evaluate
from app.decision.matrix import (
    INTERVENTION_MATRIX,
    INTERVENTIONS,
    Candidate,
    InterventionSpec,
    candidates_for,
    cost_of,
)
from app.decision.store import (
    COLLECTION_NAME,
    VERSION_INDEX,
    DanglingDiagnosisReference,
    append,
    ensure_indexes,
    latest_decision,
    latest_version,
    list_decisions,
)

__all__ = [
    "COLLECTION_NAME",
    "COST_CURRENCY",
    "INTERVENTIONS",
    "INTERVENTION_MATRIX",
    "VERSION_INDEX",
    "Candidate",
    "DanglingDiagnosisReference",
    "InterventionSpec",
    "append",
    "candidates_for",
    "cost_of",
    "decide",
    "ensure_indexes",
    "evaluate",
    "latest_decision",
    "latest_version",
    "list_decisions",
]
