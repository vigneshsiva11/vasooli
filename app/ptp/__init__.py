"""Promise-to-pay tracking (Stage 6 Part B).

Records commitments a customer has made to pay by a given date, and resolves each
against what actually happened.

The public surface, in the order a promise moves through it:

    create_promise      record a commitment; the event becomes `awaiting_promise`
    check_promise       re-check payment, then resolve against the deadline
    confirm_still_unpaid  THE MANDATORY RE-CHECK — the only source of the token
    send_follow_up      chase a broken promise, through the existing policy gate

`confirm_still_unpaid` is exported because it is the thing a future scheduler would
have to call, and `send_follow_up` is exported because its signature is the
enforcement mechanism: it takes an `UnpaidConfirmation` positionally, and nothing
but `confirm_still_unpaid` can produce one. Read `app/ptp/safety.py` before
changing either.

`_MINTED_BY_THE_CHECK` is deliberately NOT re-exported. It is the capability that
makes the token unforgeable, and a module that can import it from here can forge
one without reaching into a private name.
"""

from app.models.promise import (
    ALLOWED_PROMISE_STATES,
    ALLOWED_PROMISE_TRANSITIONS,
    INITIAL_PROMISE_STATE,
    OPEN_PROMISE_STATE,
    REQUIRES_FOLLOW_UP_SENT,
    TERMINAL_PROMISE_STATES,
    FollowUpReport,
    PromiseCheck,
    PromiseRequest,
    PromiseState,
    PromiseToPay,
    PromiseToPayDocument,
    deadline_passed,
    promise_transition_allowed,
    states_that_may_become,
    today_utc,
)
from app.ptp.safety import (
    MAX_CONFIRMATION_AGE_SECONDS,
    AlreadyRecovered,
    MismatchedConfirmation,
    StaleConfirmation,
    UnmintedConfirmation,
    UnpaidConfirmation,
    confirm_still_unpaid,
)
from app.ptp.service import (
    AWAITING_PROMISE_STATUS,
    as_record,
    check_promise,
    create_promise,
    send_follow_up,
)
from app.ptp.store import (
    COLLECTION_NAME,
    NON_PROMISABLE_STATUSES,
    DuplicatePromise,
    EventNotFound,
    EventSettled,
    PromiseError,
    PromiseNotFound,
    PromiseTransition,
    apply_transition,
    count_open_overdue,
    decode_date,
    ensure_indexes,
    find_latest,
    list_promises,
)

__all__ = [
    "ALLOWED_PROMISE_STATES",
    "ALLOWED_PROMISE_TRANSITIONS",
    "AWAITING_PROMISE_STATUS",
    "COLLECTION_NAME",
    "INITIAL_PROMISE_STATE",
    "MAX_CONFIRMATION_AGE_SECONDS",
    "NON_PROMISABLE_STATUSES",
    "OPEN_PROMISE_STATE",
    "REQUIRES_FOLLOW_UP_SENT",
    "TERMINAL_PROMISE_STATES",
    "AlreadyRecovered",
    "DuplicatePromise",
    "EventNotFound",
    "EventSettled",
    "FollowUpReport",
    "MismatchedConfirmation",
    "PromiseCheck",
    "PromiseError",
    "PromiseNotFound",
    "PromiseRequest",
    "PromiseState",
    "PromiseToPay",
    "PromiseToPayDocument",
    "PromiseTransition",
    "StaleConfirmation",
    "UnmintedConfirmation",
    "UnpaidConfirmation",
    "apply_transition",
    "as_record",
    "check_promise",
    "confirm_still_unpaid",
    "count_open_overdue",
    "create_promise",
    "deadline_passed",
    "decode_date",
    "ensure_indexes",
    "find_latest",
    "list_promises",
    "promise_transition_allowed",
    "send_follow_up",
    "states_that_may_become",
    "today_utc",
]
