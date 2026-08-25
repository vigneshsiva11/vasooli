"""The one gate a promise-to-pay follow-up must pass: is the money already in?

THE RULE THIS MODULE EXISTS TO ENFORCE
--------------------------------------
A follow-up must never be sent to somebody who has already paid. Not "should not",
and not "we remember to check first" — the send path must be unable to run without
the check having just run.

This is enforced as a type, the same construction Stage 5 uses for
`AuthorizedVerdict`:

* `app/ptp/service.py:send_follow_up` takes an `UnpaidConfirmation` as a required
  positional argument. There is no default and no overload without it, so the
  sender cannot be *called* without one;
* an `UnpaidConfirmation` can only be minted by `confirm_still_unpaid`, which is
  the only holder of the `_MINTED_BY_THE_CHECK` sentinel. Constructing one
  directly raises `UnmintedConfirmation`. A caller who wants to send without
  checking has to reach for a private module attribute, which is a deliberate act
  and one a grep can find — `scripts/s6_adversarial.py` performs exactly that grep;
* `confirm_still_unpaid` RAISES when it finds a payment rather than returning a
  confirmation with a False flag. There is no `still_unpaid=False` value to
  mishandle, because a boolean that can be False is a boolean somebody can forget
  to branch on. `still_unpaid` is `Literal[True]`, and the only instances that
  exist are ones where it is true;
* the confirmation expires. `assert_fresh` refuses one older than
  `MAX_CONFIRMATION_AGE_SECONDS`, so a confirmation obtained at the top of a long
  job cannot be carried into a send minutes later, by which time the customer may
  have paid;
* the confirmation is bound to an event. `assert_matches` refuses one minted for a
  different event, so a confirmation for a customer who genuinely has not paid
  cannot be used to authorise a message to one who has.

WHAT THIS DOES NOT BUY
----------------------
It does not stop somebody writing to the `executions` collection directly, and it
does not stop a future contributor importing `_MINTED_BY_THE_CHECK`. Those are the
same limits `AuthorizedVerdict` has, and they are covered the same way: the policy
gate still runs on the send path regardless of how it was reached, and the
adversarial script asserts mechanically that nothing outside this module mints one.

"HAS THIS BEEN PAID" HAS ONE DEFINITION
--------------------------------------
The truth here comes from `app.webhooks.has_recovered` — a stored
`VerificationRecord` with outcome `recovered` — and this module does not
reimplement that predicate. That matters more than it looks: a second, slightly
different notion of "paid" living in the promise code is precisely the bug this
whole module is built to prevent.
"""

from __future__ import annotations

import logging
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from typing import Any, Final, Literal

from app.webhooks import has_recovered
from app.webhooks import store as verification_store

logger = logging.getLogger(__name__)

#: How old a confirmation may be at the moment it is used to send, in seconds.
#:
#: Short on purpose. The window between checking and sending is the window in
#: which a customer's payment can land unnoticed, and the only way to make that
#: window small is to refuse to accept a large one. Sixty seconds is far more than
#: the two awaits that separate the check from the send, and far less than any
#: batch job's runtime — so a confirmation reused across a sweep is refused.
MAX_CONFIRMATION_AGE_SECONDS: Final[float] = 60.0

#: The capability `confirm_still_unpaid` holds and nothing else does. Module-private
#: and never re-exported: `app/ptp/__init__.py` exports the class and the function,
#: not this.
_MINTED_BY_THE_CHECK: Final = object()


class AlreadyRecovered(Exception):
    """Raised by `confirm_still_unpaid` when the money is already in.

    Raised rather than returned. A return value carrying `recovered=True` would
    have to be inspected by the caller, and a caller that forgets to inspect it
    sends a payment reminder to somebody who has paid. An exception cannot be
    forgotten — the follow-up code after it simply does not run.
    """

    def __init__(
        self, event_id: str, verification: dict[str, Any], *, examined: int
    ) -> None:
        self.event_id = event_id
        self.verification = verification
        self.verifications_examined = examined
        self.checked_at = datetime.now(timezone.utc)
        super().__init__(
            f"event {event_id!r} was already recovered by verification "
            f"{str(verification.get('_id'))} "
            f"(amount {verification.get('amount_recovered')!r}); no follow-up is "
            "permitted and none will be attempted"
        )


class UnmintedConfirmation(RuntimeError):
    """Raised when an `UnpaidConfirmation` is constructed outside the check.

    The whole value of the token is that holding one proves the check ran. A
    hand-built instance would be a proof of nothing wearing the same type.
    """


class StaleConfirmation(RuntimeError):
    """Raised when a confirmation is too old to be acted on."""


class MismatchedConfirmation(RuntimeError):
    """Raised when a confirmation was minted for a different event."""


@dataclass(frozen=True)
class UnpaidConfirmation:
    """Evidence, freshly obtained, that one event's money has NOT arrived.

    Frozen, so the event it names and the time it was taken cannot be edited after
    the fact. Not a Pydantic model on purpose: Pydantic types come with
    `model_validate`, which would give this class a second constructor that takes a
    dictionary — exactly the hole the sentinel closes.
    """

    event_id: str
    checked_at: datetime
    verifications_examined: int
    still_unpaid: Literal[True] = True
    _mint: InitVar[object | None] = None

    def __post_init__(self, _mint: object | None) -> None:
        if _mint is not _MINTED_BY_THE_CHECK:
            raise UnmintedConfirmation(
                "UnpaidConfirmation cannot be constructed directly; the only way "
                "to obtain one is app.ptp.safety.confirm_still_unpaid(), which "
                "reads the verification record first. Holding one of these is "
                "supposed to prove that check happened."
            )
        if self.still_unpaid is not True:
            raise UnmintedConfirmation(
                "still_unpaid is Literal[True]; a confirmation that something IS "
                "paid is not a thing this type can represent — that case raises "
                "AlreadyRecovered instead"
            )
        if self.checked_at.tzinfo is None:
            raise UnmintedConfirmation("checked_at must be timezone-aware")

    @property
    def age_seconds(self) -> float:
        """How long ago this confirmation was taken."""
        return (datetime.now(timezone.utc) - self.checked_at).total_seconds()

    def assert_fresh(self, max_age_seconds: float = MAX_CONFIRMATION_AGE_SECONDS) -> None:
        """Refuse a confirmation that is no longer good enough to act on.

        Raises:
            StaleConfirmation: if more than `max_age_seconds` have passed.
        """
        age = self.age_seconds
        if age > max_age_seconds:
            raise StaleConfirmation(
                f"the payment re-check for event {self.event_id!r} was {age:.1f}s "
                f"ago, past the {max_age_seconds:.0f}s limit; the customer may have "
                "paid in the meantime, so this confirmation no longer permits a "
                "follow-up. Re-check."
            )

    def assert_matches(self, event_id: str) -> None:
        """Refuse a confirmation minted for some other event.

        Raises:
            MismatchedConfirmation: if the event ids differ.
        """
        if self.event_id != event_id:
            raise MismatchedConfirmation(
                f"confirmation is for event {self.event_id!r} but the follow-up "
                f"targets {event_id!r}; a re-check of one customer does not "
                "authorise a message to another"
            )


async def confirm_still_unpaid(event_id: str) -> UnpaidConfirmation:
    """THE MANDATORY RE-CHECK. Prove this event is unpaid, or refuse.

    The only constructor of `UnpaidConfirmation`, and therefore the only way to
    reach a follow-up.

    Returns:
        UnpaidConfirmation: freshly minted, valid for
        `MAX_CONFIRMATION_AGE_SECONDS`.

    Raises:
        AlreadyRecovered: the money is in. The caller's follow-up code does not run.
    """
    # Two reads, one definition. `has_recovered` is the single implementation of
    # "has this been paid" and lives in `app/webhooks/service.py`; the list read
    # here supplies only the count for the audit trail. Re-deriving the predicate
    # from the list to save a round trip is exactly the duplication this module's
    # docstring warns about, so it is not done. Both reads happen on both paths, so
    # `verifications_examined` is a real count whichever way this call goes.
    examined = len(await verification_store.list_for_event(event_id))

    recovered = await has_recovered(event_id)
    if recovered is not None:
        logger.info(
            "PTP re-check: event %r is already recovered by verification %s — "
            "refusing to send a follow-up",
            event_id,
            str(recovered.get("_id")),
        )
        raise AlreadyRecovered(event_id, recovered, examined=examined)

    confirmation = UnpaidConfirmation(
        event_id=event_id,
        checked_at=datetime.now(timezone.utc),
        verifications_examined=examined,
        _mint=_MINTED_BY_THE_CHECK,
    )
    logger.info(
        "PTP re-check: event %r still unpaid after examining %d verification(s)",
        event_id,
        examined,
    )
    return confirmation
