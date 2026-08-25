"""Stage 4 migration — stamp every stored verdict with the rulebook that judged it.

**This is the only script in the project that modifies existing documents.**
Everything else is append-only: ingestion upserts events, diagnoses and decisions
and verdicts are versioned rather than overwritten. This one adds two fields to
documents already written, which is why it is dry-run by default, refuses to touch
a verdict that already carries a fingerprint, and prints exactly what it would do
before `--apply` lets it do anything.

`PolicyVerdict.rulebook_fingerprint` is required, so until this has run, reading an
old verdict back through the model — `GET /policy-verdicts`, or the audit — fails
validation. Run it before anything else reads the verdict log.

How each verdict's fingerprint is chosen
----------------------------------------

Not uniformly. A uniform stamp would make the field inert: if all 47 verdicts
claimed today's rulebook, "re-derive under the recorded rulebook" would be
byte-identical to "re-derive under today's rules" for every record, and the
machinery would ship with nothing to exercise it.

So each verdict is replayed under every rulebook this build knows — the one in force
plus the archive in `app/policy/rulebook.py` — and the outcome decides the label:

* **exactly one rulebook reproduces it exactly** (verdict, reason, decision version,
  and all six trail entries string for string) → that fingerprint,
  `source="reconstructed"`. Identification by reproduction: circumstantial evidence,
  but evidence.
* **several rulebooks reproduce it** → the newest of them, `source="backfilled"`.
  Common and expected: a `delayed_retry` verdict never touches the contact set, so
  every rulebook agrees about it. The record does not pick one, so the label does not
  claim it did.
* **none reproduces it** → today's fingerprint, `source="backfilled"`, and reported
  loudly, because a verdict that no known rulebook explains is a finding.

Taking the newest *consistent* rulebook rather than always taking today's is what
keeps the field from contradicting its own record. Some verdicts re-derive exactly
under both archived rulebooks and not at all under the current one; stamping today's
fingerprint on those would store a value their own stored trail refutes.

What a backfilled fingerprint therefore records is the newest rulebook that fit the
verdict among those this build knew *when the label was written* — the narrowest
consistent account available at that moment. It is not a claim that no other
rulebook fits, and specifically not a claim that today's policy could not have
produced the verdict. Both readings would be stronger than the evidence: the
registry grows, and a rulebook added later can fit a verdict just as exactly as the
one recorded, without anything about that verdict changing. Read a superseded
fingerprint as "this fit, and was the tightest fit then", not as "only this fits".

`scripts/s4_fingerprint_reconcile.py` re-checks these labels against the registry as
it currently stands, and reports where a stored value is no longer the newest fit.
It deliberately does not rewrite them: the stored value still reproduces the verdict
exactly, so the record stays sound and the drift is a matter of prose precision
rather than of re-derivability. See `docs/data-corrections.md`.

`backfilled` means exactly what the brief says it means: the verdict predates
fingerprinting and its true rulebook is unrecoverable. Choosing the newest of several
equally consistent candidates is a tiebreak, not a finding, and the audit weakens its
assertions about these records accordingly — byte-exact reproduction under a
backfilled fingerprint is partly by construction, since the fingerprint was chosen
for reproducing.

Both departures from the brief's literal instruction — identifying by reproduction
rather than stamping today's fingerprint uniformly, and breaking ties toward the
newest consistent rulebook rather than the current one — are ratified.

Not attempted: dating the amendments from the uniquely-identified verdicts and
assigning the ambiguous ones by timestamp. It would resolve most of the ambiguity
and it would also be a second layer of inference presented in the same field as the
first, turning "unrecoverable" into "guessed" without saying so. The ambiguous ones
stay ambiguous.

Run:  python scripts/s4_fingerprint_backfill.py           # dry run, writes nothing
      python scripts/s4_fingerprint_backfill.py --apply    # writes
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bson import ObjectId

from app.db import close_mongo_connection, connect_to_mongo, get_database
from app.models import DecisionRecord
from app.models.policy import PolicyVerdictRecord
from app.policy import current_fingerprint, current_rulebook, rulebook_registry
from s4_replay import Replay, group_by_event, load_everything, replay

APPLY = "--apply" in sys.argv[1:]

problems: list[str] = []


def section(title: str) -> None:
    print(f"\n{title}")
    print("=" * len(title))


class Plan:
    """What this script intends to write to one verdict document, and why."""

    def __init__(
        self,
        *,
        object_id: ObjectId,
        label: str,
        fingerprint: str,
        source: str,
        rationale: str,
        matches: list[Replay],
    ) -> None:
        self.object_id = object_id
        self.label = label
        self.fingerprint = fingerprint
        self.source = source
        self.rationale = rationale
        self.matches = matches


def decide_fingerprint(
    *, label: str, replays: list[Replay], current: str
) -> tuple[str, str, str, list[Replay]]:
    """Pick the fingerprint and source for one verdict from its replay results.

    `replays` must be in candidate order — archive oldest-first, then the rulebook
    in force — because the ambiguous case takes the last exact match.

    Returns:
        (fingerprint, source, rationale, exact matches)
    """
    exact = [result for result in replays if result.exact]

    if len(exact) == 1:
        only = exact[0]
        return (
            only.rulebook.fingerprint,
            "reconstructed",
            f"re-derives exactly under this rulebook and no other ({only.rulebook.note})",
            exact,
        )

    if len(exact) > 1:
        # Several rulebooks reproduce it, so the record does not pick one. Take the
        # newest that does — the tightest consistent account available right now. The
        # label stays `backfilled` precisely because this is a choice and not a
        # deduction.
        #
        # The one property that matters: the stored fingerprint is never one the
        # record contradicts. Stamping today's on a verdict that demonstrably does
        # not re-derive under today's rules would put a value in the field that its
        # own trail refutes.
        #
        # What does NOT follow — and an earlier version of this comment claimed it
        # did — is that a backfilled verdict naming a superseded rulebook is one
        # today's policy could not have produced. "Newest that fits" is evaluated
        # against the registry as it stands when this runs, and the registry grows.
        # A rulebook added afterwards can fit the same verdict just as exactly, which
        # makes the stored value stale as a *tiebreak* while remaining perfectly valid
        # as a fit. `scripts/s4_fingerprint_reconcile.py` reports that drift and
        # leaves it alone.
        chosen = exact[-1]
        others = len(exact) - 1
        return (
            chosen.rulebook.fingerprint,
            "backfilled",
            f"{len(exact)} rulebooks reproduce it identically so the record does not "
            f"distinguish them; the newest is stored ({chosen.rulebook.note}), "
            f"{others} older one(s) fit equally well",
            exact,
        )

    # Nothing reproduced it. Report which rulebook came closest, since "permission
    # holds everywhere, only the prose moved" and "no rulebook reaches this verdict
    # at all" are very different findings. Today's fingerprint is the only remaining
    # default, and here it genuinely is one — there is no consistent alternative.
    permission_ok = [result for result in replays if result.permission_holds]
    if permission_ok:
        detail = (
            f"no rulebook reproduces its trail exactly; permission re-derives under "
            f"{len(permission_ok)} of {len(replays)}, so today's fingerprint is "
            "stored as a default rather than as a fit"
        )
    else:
        detail = (
            "no known rulebook reproduces even its verdict/reason — this verdict is "
            "unexplained by any recorded policy"
        )
        problems.append(f"{label}: {detail}")
    return current, "backfilled", detail, []


async def build_plans() -> list[Plan]:
    database = get_database()
    verdicts, decisions, opt_outs, events, executions = await load_everything(database)

    registry = rulebook_registry()
    current = current_fingerprint()

    section("1. What is in the database, and what rulebooks this build knows")
    print(f"  {len(verdicts)} verdicts, {len(decisions)} decisions, "
          f"{len(opt_outs)} opted-out customer(s), {len(events)} events")
    print(f"\n  {len(registry)} known rulebook(s):")
    for fingerprint, rulebook in sorted(
        registry.items(), key=lambda item: item[1].note != "in force"
    ):
        marker = "in force  " if fingerprint == current else "superseded"
        print(f"    {marker} {fingerprint}  contacts={len(rulebook.contact_interventions)}")
        print(f"               {rulebook.note}")

    already = [v for v in verdicts if v.get("rulebook_fingerprint")]
    if already:
        print(
            f"\n  {len(already)} verdict(s) already carry a fingerprint and will be "
            "left alone; this script never overwrites one"
        )

    # Order the candidates so the current rulebook is tried last, purely so the
    # printed replay order reads oldest-first. Identification does not depend on it:
    # a unique exact match is unique whatever order it was found in.
    candidates = sorted(
        registry.values(), key=lambda rulebook: rulebook.fingerprint == current
    )

    section("2. Replay every verdict under every known rulebook")
    print(
        f"  {len(verdicts)} verdicts x {len(candidates)} rulebooks = "
        f"{len(verdicts) * len(candidates)} replays\n"
    )

    by_event = group_by_event(verdicts)
    plans: list[Plan] = []

    for event_id, event_verdicts in sorted(by_event.items()):
        event = events.get(event_id)
        if event is None:
            problems.append(f"{event_id}: has verdicts but no event document")
            continue

        for verdict in sorted(event_verdicts, key=lambda v: v["version"]):
            label = f"{event_id} v{verdict['version']}"

            if verdict.get("rulebook_fingerprint"):
                print(
                    f"  skip      {label:<34} already {verdict['rulebook_fingerprint']}"
                    f" ({verdict.get('rulebook_fingerprint_source', '?')})"
                )
                continue

            decision_document = decisions.get(verdict["decision_id"])
            if decision_document is None:
                problems.append(
                    f"{label}: references decision {verdict['decision_id']} which no "
                    "longer exists, so it cannot be replayed under any rulebook"
                )
                print(f"  UNKNOWN   {label:<34} decision missing")
                continue

            decision = DecisionRecord.from_document(decision_document)
            replays = [
                replay(
                    verdict=verdict,
                    decision=decision,
                    decisions=decisions,
                    opt_outs=opt_outs,
                    customer_ref=event["customer_ref"],
                    event_verdicts=event_verdicts,
                    executions=executions,
                    rulebook=rulebook,
                )
                for rulebook in candidates
            ]

            fingerprint, source, rationale, matches = decide_fingerprint(
                label=label, replays=replays, current=current
            )
            plans.append(
                Plan(
                    object_id=verdict["_id"],
                    label=label,
                    fingerprint=fingerprint,
                    source=source,
                    rationale=rationale,
                    matches=matches,
                )
            )

            grid = " ".join(
                ("=" if result.exact else "~" if result.permission_holds else "x")
                for result in replays
            )
            marker = "IDENTIFIED" if source == "reconstructed" else "backfill  "
            print(
                f"  {marker} {label:<34} [{grid}] -> {fingerprint} ({source})"
            )
            if source == "reconstructed":
                print(f"                {rationale}")
                print(
                    f"                {decision.recommended_intervention}, stored as "
                    f"{verdict['verdict']}/{verdict['reason']}"
                )

    print(
        "\n  legend, one cell per rulebook in archive order then current: "
        "= exact, ~ permission only, x differs"
    )
    return plans


def summarise(plans: list[Plan]) -> None:
    section("3. What the migration would write")

    if not plans:
        print(
            "  nothing — every stored verdict already carries a fingerprint. This "
            "migration has already run."
        )
        return

    current = current_fingerprint()
    registry = rulebook_registry()

    by_target: Counter[tuple[str, str]] = Counter(
        (plan.fingerprint, plan.source) for plan in plans
    )
    for (fingerprint, source), count in sorted(by_target.items()):
        where = "in force" if fingerprint == current else "superseded"
        print(f"  {count:>3} verdict(s) -> {fingerprint} ({where})  source={source}")

    reconstructed = [plan for plan in plans if plan.source == "reconstructed"]
    if reconstructed:
        print(
            f"\n  {len(reconstructed)} verdict(s) positively identified — each "
            "re-derives exactly under one known rulebook and no other:"
        )
        for plan in reconstructed:
            where = (
                "the rulebook in force"
                if plan.fingerprint == current
                else f"superseded: {registry[plan.fingerprint].note}"
            )
            print(f"    - {plan.label}: {plan.fingerprint} — {where}")
    else:
        print(
            "\n  no verdict was uniquely identified, so the fingerprint field would "
            "ship with no historical test data — worth investigating before applying"
        )

    ambiguous = [
        plan for plan in plans if plan.source == "backfilled" and len(plan.matches) > 1
    ]
    unexplained = [
        plan for plan in plans if plan.source == "backfilled" and not plan.matches
    ]

    print(
        f"\n  {len(ambiguous)} backfilled because several rulebooks fit equally well; "
        "the newest that fits is stored"
    )
    excludes_today = [plan for plan in ambiguous if plan.fingerprint != current]
    if excludes_today:
        print(
            f"    of those, {len(excludes_today)} name a SUPERSEDED rulebook — today's "
            "rules do not\n    reproduce them, so the tightest fit is an archived one:"
        )
        for plan in excludes_today:
            print(f"      - {plan.label}: {plan.fingerprint}")

    print(f"\n  {len(unexplained)} backfilled with no rulebook fitting exactly")
    for plan in unexplained:
        print(f"    - {plan.label}: {plan.rationale}")


async def apply(plans: list[Plan]) -> None:
    section("4. Applying")

    if not APPLY:
        print("  DRY RUN — nothing written. Re-run with --apply to write.")
        return

    collection = get_database()["policy_verdicts"]
    written = 0
    for plan in plans:
        result = await collection.update_one(
            # The filter re-asserts the precondition at write time: if a concurrent
            # run stamped this document since the plan was built, this matches
            # nothing rather than overwriting it.
            {
                "_id": plan.object_id,
                "rulebook_fingerprint": {"$exists": False},
            },
            {
                "$set": {
                    "rulebook_fingerprint": plan.fingerprint,
                    "rulebook_fingerprint_source": plan.source,
                }
            },
        )
        if result.modified_count == 1:
            written += 1
        else:
            problems.append(
                f"{plan.label}: update matched {result.matched_count} document(s) and "
                f"modified {result.modified_count}; expected exactly 1"
            )
    print(f"  {written} of {len(plans)} verdict(s) updated")


async def verify() -> None:
    section("5. Every stored verdict now validates through the model")

    documents = await get_database()["policy_verdicts"].find().to_list(length=None)
    sources: Counter[str] = Counter()
    fingerprints: Counter[str] = Counter()

    for document in documents:
        label = f"{document['event_id']} v{document.get('version')}"
        try:
            record = PolicyVerdictRecord.from_document(document)
        except Exception as exc:  # noqa: BLE001 - the point is to report any failure
            problems.append(f"{label}: does not validate — {exc}")
            print(f"  INVALID  {label}: {type(exc).__name__}")
            continue
        sources[record.rulebook_fingerprint_source] += 1
        fingerprints[record.rulebook_fingerprint] += 1

    print(f"  {len(documents)} verdict(s) read back, {len(documents) - len(problems)} valid")
    print("\n  by source:")
    for source, count in sorted(sources.items()):
        print(f"    {count:>3}  {source}")
    print("\n  by fingerprint:")
    registry = rulebook_registry()
    current = current_fingerprint()
    for fingerprint, count in sorted(fingerprints.items()):
        known = registry.get(fingerprint)
        if known is None:
            note = "UNKNOWN to this build"
            problems.append(
                f"{count} verdict(s) carry fingerprint {fingerprint}, which no "
                "rulebook in this build matches"
            )
        elif fingerprint == current:
            note = "in force"
        else:
            note = f"superseded — {known.note}"
        print(f"    {count:>3}  {fingerprint}  {note}")


async def main() -> None:
    await connect_to_mongo()
    print("Stage 4 migration — rulebook fingerprints for existing verdicts")
    print(f"mode: {'APPLY (will write)' if APPLY else 'DRY RUN (writes nothing)'}")
    print(
        "\nThis is the only script that modifies documents already written. It adds\n"
        "two fields and never changes an existing value; a verdict that already has a\n"
        "fingerprint is skipped."
    )

    plans = await build_plans()
    summarise(plans)
    await apply(plans)
    if APPLY:
        await verify()

    await close_mongo_connection()

    print("\n" + "=" * 78)
    if problems:
        print(f"PROBLEMS ({len(problems)}):")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)

    if not APPLY:
        print(
            f"dry run clean: {len(plans)} verdict(s) planned, nothing written. "
            "Re-run with --apply."
        )
    else:
        print(
            f"{len(plans)} verdict(s) stamped; every stored verdict now names the "
            "rulebook it is checked against"
        )


if __name__ == "__main__":
    asyncio.run(main())
