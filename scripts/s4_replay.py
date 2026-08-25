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
  today's contact set, and — under a rulebook that measures from executions —
  filtered by what their execution outcome was **as at** verdict *v*;
* last contact = the newest anchor among those, which is an `executed_at` for a
  verdict already executed at the time and an `evaluated_at` for one not yet
  executed;
* opt-out state = whether the customer's `opted_out_at` precedes this verdict.

That the contact classification follows the rulebook is the whole reason this is
parameterised. Under the launch rulebook a payment link was not a contact, so it
neither consumed one nor started a cooldown — replaying an old verdict while
counting prior contacts by today's rules would feed the engine a history that never
happened.

Stage 5 gave that argument a second application. `cooldown_measured_from` moved from
`verdict.evaluated_at` to `execution.executed_at`, so a verdict judged during Stage 4
must still be re-derived with executions ignored entirely — they did not exist and
could not have influenced it. `reconstruct_context` therefore branches on the
rulebook's own anchor rather than on what the code does today. Re-deriving an old
verdict under the new anchor would be reinterpreting it, which is precisely what the
fingerprint exists to prevent.

**Historical visibility.** Under the execution anchor, an execution is only a fact
for verdict *v* if its `executed_at` is at or before *v*'s `evaluated_at`. A verdict
executed *later* was, at the moment *v* was judged, an unexecuted reservation — so it
is counted as one, anchored at its `evaluated_at`. Reading the outcome of an
execution that had not happened yet would let a replay know the future.

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
from app.policy.rulebook import COOLDOWN_FROM_VERDICT


async def load_everything(database: Any) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    """Read every verdict, decision, opt-out, event, and execution.

    Executions are keyed by `policy_verdict_id`, which is unique by index, so the
    mapping is total rather than a first-wins collapse.
    """
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
    executions = {
        document["policy_verdict_id"]: document
        for document in await database["executions"].find().to_list(length=None)
    }
    return verdicts, decisions, opt_outs, events, executions


def group_by_event(verdicts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Verdicts bucketed by event, so version history is available per event."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for verdict in verdicts:
        grouped[verdict["event_id"]].append(verdict)
    return grouped


def contact_anchors(
    *,
    contacts: list[dict[str, Any]],
    executions: dict[str, dict[str, Any]],
    as_at: Any,
    rulebook: Rulebook,
) -> list[Any]:
    """Return one cooldown anchor per contact that still counts, as at `as_at`.

    The list length is the effective contact count and its maximum is the last
    contact time, which is why both come from one function: a rulebook that
    discounts a contact must discount it in both places, and computing them
    separately is how they drift apart.

    Under `verdict.evaluated_at` every authorized contact counts, anchored at when
    permission was granted. Under `execution.executed_at`:

    * an execution not visible as at `as_at` — either none exists, or its
      `executed_at` is later than `as_at` — is an unexecuted reservation, anchored
      at `evaluated_at`. Permission has been issued and may yet be used;
    * a visible completed execution is anchored at its real send time;
    * a visible failed execution is dropped. Nothing reached the customer, so it
      consumes no cap slot and starts no cooldown.

    The visibility test is what keeps a replay from knowing the future. An execution
    that happened after the verdict being replayed was not a fact the engine could
    have seen, so it is counted as the reservation it was at the time — not as the
    outcome it later became.
    """
    if rulebook.cooldown_measured_from == COOLDOWN_FROM_VERDICT:
        return [contact["evaluated_at"] for contact in contacts]

    anchors: list[Any] = []
    for contact in contacts:
        execution = executions.get(str(contact["_id"]))
        if execution is None or execution["executed_at"] > as_at:
            anchors.append(contact["evaluated_at"])
        elif execution["status"] == "completed":
            anchors.append(execution["executed_at"])
    return anchors


def reconstruct_context(
    *,
    verdict: dict[str, Any],
    decisions: dict[str, dict[str, Any]],
    opt_outs: dict[str, Any],
    customer_ref: str,
    event_verdicts: list[dict[str, Any]],
    executions: dict[str, dict[str, Any]],
    rulebook: Rulebook,
) -> PolicyContext:
    """Rebuild the facts the engine saw when this verdict was written.

    `rulebook` decides which earlier verdicts counted as contacts and whether
    executions are consulted at all, which is what makes this a reconstruction of the
    past rather than a reinterpretation of it.

    `executions` is required rather than defaulted, deliberately. An omitted mapping
    would read as "no executions exist", which under the execution anchor silently
    demotes every completed send to a reservation and moves the cooldown earlier — a
    wrong answer that looks like a clean replay.
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
    anchors = contact_anchors(
        contacts=contacts,
        executions=executions,
        as_at=verdict["evaluated_at"],
        rulebook=rulebook,
    )

    opted_out_at = opt_outs.get(customer_ref)
    opted_out = opted_out_at is not None and opted_out_at <= verdict["evaluated_at"]

    return PolicyContext(
        customer_ref=customer_ref,
        customer_opted_out=opted_out,
        prior_authorized_contacts=len(anchors),
        last_authorized_contact_at=max(anchors) if anchors else None,
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
    executions: dict[str, dict[str, Any]],
    rulebook: Rulebook,
) -> Replay:
    """Re-derive one stored verdict under one rulebook and compare, field by field."""
    context = reconstruct_context(
        verdict=verdict,
        decisions=decisions,
        opt_outs=opt_outs,
        customer_ref=customer_ref,
        event_verdicts=event_verdicts,
        executions=executions,
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
