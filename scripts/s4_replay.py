"""Replaying a stored verdict under a chosen rulebook.

Shared by `s4_audit.py` and `s4_fingerprint_backfill.py`, which need the same
operation for different reasons: the audit replays a verdict under the rulebook it
recorded, and the migration replays it under every rulebook this build knows in
order to work out which one it must have been.

Reconstructing the context is possible only because the verdict log is append-only
and versions are allocated in write order, so the facts the engine saw when it wrote
verdict *v* are exactly:

* prior authorized contacts = authorized contact-type verdicts for the event with
  version < *v*, classified **under the rulebook being replayed** rather than
  today's contact set;
* last contact = the newest `evaluated_at` among those;
* opt-out state = whether the customer's `opted_out_at` precedes this verdict.

That the contact classification follows the rulebook is the whole reason this is
parameterised. Under the launch rulebook a payment link was not a contact, so it
neither consumed one nor started a cooldown — replaying an old verdict while
counting prior contacts by today's rules would feed the engine a history that never
happened.

`evaluated_at` is an input to the engine rather than something it derives, so it is
fed back in as `now` and is not independently verified.

Nothing here writes to the database.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, NamedTuple

from app.models import DecisionRecord
from app.models.policy import PolicyVerdict
from app.policy import PolicyContext, Rulebook, UnreproducibleRulebook, evaluate


async def load_everything(database: Any) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, Any],
    dict[str, dict[str, Any]],
]:
    """Read every verdict, every decision (by id), the opt-out list and the events."""
    verdicts = (
        await database["policy_verdicts"]
        .find()
        .sort([("event_id", 1), ("version", 1)])
        .to_list(length=None)
    )
    decisions = {
        str(document["_id"]): document
        for document in await database["decisions"].find().to_list(length=None)
    }
    opt_outs = {
        document["customer_ref"]: document["opted_out_at"]
        for document in await database["customer_opt_outs"].find().to_list(length=None)
    }
    events = {
        document["event_id"]: document
        for document in await database["events"].find().to_list(length=None)
    }
    return verdicts, decisions, opt_outs, events


def group_by_event(verdicts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Verdicts bucketed by event, so version history is available per event."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for verdict in verdicts:
        grouped[verdict["event_id"]].append(verdict)
    return grouped


def reconstruct_context(
    *,
    verdict: dict[str, Any],
    decisions: dict[str, dict[str, Any]],
    opt_outs: dict[str, Any],
    customer_ref: str,
    event_verdicts: list[dict[str, Any]],
    rulebook: Rulebook,
) -> PolicyContext:
    """Rebuild the facts the engine saw when this verdict was written.

    `rulebook` decides which earlier verdicts counted as contacts, which is what
    makes this a reconstruction of the past rather than a reinterpretation of it.
    """
    earlier = [
        other
        for other in event_verdicts
        if other["version"] < verdict["version"] and other["verdict"] == "authorized"
    ]
    contacts = [
        other
        for other in earlier
        if other["decision_id"] in decisions
        and rulebook.is_contact(
            decisions[other["decision_id"]]["recommended_intervention"]
        )
    ]

    opted_out_at = opt_outs.get(customer_ref)
    opted_out = opted_out_at is not None and opted_out_at <= verdict["evaluated_at"]

    return PolicyContext(
        customer_ref=customer_ref,
        customer_opted_out=opted_out,
        prior_authorized_contacts=len(contacts),
        last_authorized_contact_at=(
            max(other["evaluated_at"] for other in contacts) if contacts else None
        ),
        # Fed back in: the engine stamps the verdict with whatever clock it was
        # given, so this is an input being replayed, not a derivation being checked.
        now=verdict["evaluated_at"],
    )


class Replay(NamedTuple):
    """The result of re-deriving one stored verdict under one rulebook."""

    rulebook: Rulebook
    #: What the engine produces now, or None if the rulebook could not be applied.
    rederived: PolicyVerdict | None
    #: Differences in verdict / reason / decision version. Any entry here means a
    #: stored *authorization* would not be granted under this rulebook.
    permission_diffs: list[str]
    #: (stored, fresh) pairs for trail entries that differ.
    trail_diffs: list[tuple[str, str]]
    #: Why the rulebook could not be applied at all, if it could not.
    error: str | None = None

    @property
    def applied(self) -> bool:
        """Whether the rulebook could be applied by this build."""
        return self.error is None

    @property
    def permission_holds(self) -> bool:
        """Whether verdict, reason and decision version all re-derive."""
        return self.error is None and not self.permission_diffs

    @property
    def exact(self) -> bool:
        """Whether everything re-derives, trail entries string for string.

        The migration's identification test: only an exact replay is evidence that
        a verdict was judged under a particular rulebook.
        """
        return self.permission_holds and not self.trail_diffs


def replay(
    *,
    verdict: dict[str, Any],
    decision: DecisionRecord,
    decisions: dict[str, dict[str, Any]],
    opt_outs: dict[str, Any],
    customer_ref: str,
    event_verdicts: list[dict[str, Any]],
    rulebook: Rulebook,
) -> Replay:
    """Re-derive one stored verdict under one rulebook and compare, field by field."""
    context = reconstruct_context(
        verdict=verdict,
        decisions=decisions,
        opt_outs=opt_outs,
        customer_ref=customer_ref,
        event_verdicts=event_verdicts,
        rulebook=rulebook,
    )

    try:
        rederived = evaluate(decision=decision, context=context, rulebook=rulebook)
    except UnreproducibleRulebook as exc:
        # The rulebook differs from this build in a way the engine cannot install.
        # Reported rather than approximated: a partial replay would look like a
        # faithful one.
        return Replay(
            rulebook=rulebook,
            rederived=None,
            permission_diffs=[],
            trail_diffs=[],
            error=str(exc),
        )

    permission_diffs: list[str] = []
    if rederived.verdict != verdict["verdict"]:
        permission_diffs.append(
            f"verdict {verdict['verdict']!r} -> {rederived.verdict!r}"
        )
    if rederived.reason != verdict["reason"]:
        permission_diffs.append(f"reason {verdict['reason']!r} -> {rederived.reason!r}")
    if rederived.decision_version != verdict["decision_version"]:
        permission_diffs.append(
            f"decision_version {verdict['decision_version']} -> "
            f"{rederived.decision_version}"
        )

    stored_trail: list[str] = verdict["checks_performed"]
    trail_diffs: list[tuple[str, str]] = []
    if len(stored_trail) != len(rederived.checks_performed):
        permission_diffs.append(
            f"trail has {len(stored_trail)} entries, expected "
            f"{len(rederived.checks_performed)}"
        )
    else:
        trail_diffs = [
            (stored_entry, fresh_entry)
            for stored_entry, fresh_entry in zip(
                stored_trail, rederived.checks_performed
            )
            if stored_entry != fresh_entry
        ]

    return Replay(
        rulebook=rulebook,
        rederived=rederived,
        permission_diffs=permission_diffs,
        trail_diffs=trail_diffs,
    )
