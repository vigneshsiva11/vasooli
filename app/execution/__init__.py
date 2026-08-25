"""Stage 5 — Execution.

Carries out actions the policy gate has already approved: a Razorpay test-mode
payment link, a simulated retry (also a link — see `service.py`), or a logged
contact. Records exactly what was done and when, and nothing about whether it worked.

Entry points here accept an *authorized* action only. `require_authorized` is the one
way in, and `AuthorizedVerdict` is what it produces: the executor's argument type
cannot represent a blocked or review-pending verdict. The store's referential guard
and the audit re-check the same thing from the other side, against the database
rather than against the call path.

The public surface, in the order a request travels it:

* `require_authorized` — narrow a stored verdict, or refuse loudly;
* `execute` — perform the action once, returning an `ExecutionOutcome`;
* `ensure_indexes`, `list_executions`, `find_for_verdict` — persistence;
* `render`, `summarise` — the deterministic contact templates;
* `create_payment_link`, `RazorpayCredentials` — the outbound call.

Nothing here decides *whether* to act. That was settled in Stage 4.
"""

from app.execution.razorpay import (
    PaymentLink,
    RazorpayCallFailed,
    RazorpayCredentials,
    RazorpayNotConfigured,
    create_payment_link,
    credentials_from_settings,
    reference_id_for,
)
from app.execution.service import (
    AuthorizedAction,
    DecisionNotFound,
    DiagnosisNotFound,
    EventNotFound,
    ExecutionError,
    ExecutionOutcome,
    execute,
)
from app.execution.store import (
    COLLECTION_NAME,
    DanglingVerdictReference,
    DuplicateExecution,
    InterventionMismatch,
    StaleVerdictReference,
    UnauthorizedVerdictReference,
    VerdictReferenceError,
    ensure_indexes,
    find_for_verdict,
    list_executions,
)
from app.execution.templates import (
    TEMPLATE_VERSION,
    ContactChannel,
    ContactMessage,
    NoTemplate,
    render,
    summarise,
)
from app.models.execution import (
    ACTION_FOR_INTERVENTION,
    ALLOWED_ACTION_TYPES,
    EXECUTABLE_INTERVENTIONS,
    AuthorizedVerdict,
    ExecutionRecord,
    ExecutionRecordDocument,
    NotAuthorized,
    require_authorized,
)

__all__ = [
    "ACTION_FOR_INTERVENTION",
    "ALLOWED_ACTION_TYPES",
    "COLLECTION_NAME",
    "EXECUTABLE_INTERVENTIONS",
    "TEMPLATE_VERSION",
    "AuthorizedAction",
    "AuthorizedVerdict",
    "ContactChannel",
    "ContactMessage",
    "DanglingVerdictReference",
    "DecisionNotFound",
    "DiagnosisNotFound",
    "DuplicateExecution",
    "EventNotFound",
    "ExecutionError",
    "ExecutionOutcome",
    "ExecutionRecord",
    "ExecutionRecordDocument",
    "InterventionMismatch",
    "NoTemplate",
    "NotAuthorized",
    "PaymentLink",
    "RazorpayCallFailed",
    "RazorpayCredentials",
    "RazorpayNotConfigured",
    "StaleVerdictReference",
    "UnauthorizedVerdictReference",
    "VerdictReferenceError",
    "create_payment_link",
    "credentials_from_settings",
    "ensure_indexes",
    "execute",
    "find_for_verdict",
    "list_executions",
    "reference_id_for",
    "render",
    "require_authorized",
    "summarise",
]
