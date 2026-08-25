"""Stage 4 correction — re-check every stored fingerprint label against today's registry.

`scripts/s4_fingerprint_backfill.py` labelled each pre-fingerprinting verdict by
replaying it under every rulebook the build knew *at the time it ran*:

* exactly one rulebook reproduced it  -> `source="reconstructed"`
* several reproduced it               -> `source="backfilled"`, newest of them stored

That first label is a claim about uniqueness, and uniqueness is not a property of
the verdict alone — it is a property of the verdict *and the registry it was
compared against*. Adding a rulebook to `app/policy/rulebook.py` can therefore
falsify a `reconstructed` label that was accurate when it was written, without
anything about the verdict changing. `rb1_aba19a5e5ee8124e` (the cooldown anchor
moving to `execution.executed_at`) did exactly that: verdicts on events with no
executions re-derive identically under it and under its predecessor, so six
verdicts that were uniquely identified became ambiguous.

`scripts/s4_audit.py` already detects this and reports it as `weakened` — not a
re-derivation failure, since each still re-derives exactly under the rulebook it
names, but an overstated provenance claim. This script is the correction.

It does not restate the labelling rule. It imports `decide_fingerprint` from the
backfill and asks it what it would say today, so the corrected labels are derived
by the same function that derived every other label in the collection rather than
by a second implementation that could drift from it.

What it will and will not write
-------------------------------

The only write is `rulebook_fingerprint_source`, and only ever in the direction
`reconstructed` -> `backfilled`, and only when the fingerprint already on the
record still reproduces the verdict exactly. That last condition is the guard,
rather than "the fingerprint did not move" — weakening a label is safe while the
stored fingerprint still fits, and unsafe if it does not, whatever a fresh
tiebreak would nominate instead.

Everything else it finds is reported and refused:

* **`fingerprint_moved`** — the label is already `backfilled` and right; only the
  rulebook the ambiguous-case tiebreak would nominate has changed, because the
  registry grew after the migration ran. Every stored value is still an exact
  match, so no record is wrong. It does falsify one inference the migration
  documents — that a `backfilled` verdict naming a superseded rulebook is one
  today's policy could not have produced — and restoring that signal would mean
  overwriting values that were correct when they were chosen. Reported for
  ratification, never written here.
* **`source_strengthened`** — recomputation says `reconstructed` where the record
  says `backfilled`. Refused unconditionally. A shrinking registry can make an
  identification look unique again; writing that would manufacture evidence out
  of the archive being incomplete, which is the exact inversion of what this
  field is for.
* **an unfit fingerprint** — the recorded fingerprint is no longer among the exact
  matches. Refused: that is a re-derivation problem for the audit to report, and
  weakening the label would trade an overstated claim for a false one.
* **unexplained** — no rulebook reproduces it. Refused; that is an audit finding,
  not a labelling error.

Verdicts labelled `evaluated` are never recomputed. That label records the
rulebook that actually judged the verdict, at the moment it ran; it is evidence
rather than identification, and `decide_fingerprint` has neither the vocabulary
to express it nor any standing to contradict it.

Dry-run by default, like the migration it corrects.

Run:  python scripts/s4_fingerprint_reconcile.py           # reports, writes nothing
      python scripts/s4_fingerprint_reconcile.py --apply    # writes
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.db import close_mongo_connection, connect_to_mongo, get_database
from app.models import DecisionRecord
from app.models.policy import PolicyVerdictRecord
from app.policy import current_fingerprint, rulebook_registry

import s4_fingerprint_backfill as backfill
from s4_replay import group_by_event, load_everything, replay

APPLY = "--apply" in sys.argv[1:]

#: The one transition this script is allowed to write.
WEAKEN = ("reconstructed", "backfilled")

#: Labels the migration produces, and the only ones this script reasons about.
#: `decide_fingerprint` chooses between exactly these two.
MIGRATED_SOURCES = frozenset(WEAKEN)

#: A verdict judged live, with the fingerprint of the rulebook that actually ran
#: recorded at the moment it ran. This is direct evidence, not an identification, and
#: it is out of this script's reach entirely — `decide_fingerprint` cannot return
#: `evaluated` and has no standing to contradict one. Recomputing these would replace
#: a record of what happened with an inference about what could have happened, which
#: is precisely the confusion the three sources exist to prevent.
SOURCE_EVALUATED = "evaluated"

problems: list[str] = []


def section(title: str) -> None:
    print(f"\n{title}")
    print("=" * len(title))


class Divergence:
    """One verdict whose stored label disagrees with today's recomputation."""

    def __init__(
        self,
        *,
        document: dict[str, Any],
        label: str,
        stored_source: str,
        stored_fingerprint: str,
        fresh_source: str,
        fresh_fingerprint: str,
        rationale: str,
        exact: list[str],
    ) -> None:
        self.document = document
        self.label = label
        self.stored_source = stored_source
        self.stored_fingerprint = stored_fingerprint
        self.fresh_source = fresh_source
        self.fresh_fingerprint = fresh_fingerprint
        self.rationale = rationale
        self.exact = exact

    @property
    def kind(self) -> str:
        if (self.stored_source, self.fresh_source) == WEAKEN:
            return "source_weakened"
        if (self.stored_source, self.fresh_source) == WEAKEN[::-1]:
            return "source_strengthened"
        if self.stored_fingerprint != self.fresh_fingerprint:
            return "fingerprint_moved"
        return "unclassified"

    @property
    def writable(self) -> bool:
        """A weakening whose recorded fingerprint still reproduces the verdict.

        The guard is deliberately *not* "the fingerprint did not move". For an
        ambiguous verdict `decide_fingerprint` takes the newest rulebook that fits, so
        a registry that has grown since the migration ran will often nominate a
        different one — that drift affects verdicts already labelled `backfilled` too
        and has nothing to do with this correction.

        What must hold is that the fingerprint on the record is still one of the exact
        matches. Weakening the label while leaving behind a fingerprint the verdict no
        longer re-derives under would trade an overstated claim for a false one.
        """
        return self.kind == "source_weakened" and self.stored_fingerprint in self.exact


async def recompute() -> list[Divergence]:
    database = get_database()
    verdicts, decisions, opt_outs, events, executions = await load_everything(database)

    registry = rulebook_registry()
    current = current_fingerprint()

    section("1. What is stored, and what this build now knows")
    print(f"  {len(verdicts)} verdicts, {len(decisions)} decisions, {len(events)} events")
    print(f"\n  {len(registry)} known rulebook(s):")
    for fingerprint, rulebook in sorted(
        registry.items(), key=lambda item: item[1].note != "in force"
    ):
        marker = "in force  " if fingerprint == current else "superseded"
        print(f"    {marker} {fingerprint}  contacts={len(rulebook.contact_interventions)}")
        print(f"               {rulebook.note}")

    stored_sources: dict[str, int] = {}
    for verdict in verdicts:
        source = verdict.get("rulebook_fingerprint_source", "<absent>")
        stored_sources[source] = stored_sources.get(source, 0) + 1
    print("\n  stored labels:")
    for source, count in sorted(stored_sources.items()):
        print(f"    {count:>3}  {source}")

    # Same candidate order as the migration, and it matters: `decide_fingerprint`
    # documents that its input must be oldest-first with the rulebook in force last,
    # because the ambiguous branch takes `exact[-1]`. Reproducing the order is part of
    # reproducing the decision.
    candidates = sorted(
        registry.values(), key=lambda rulebook: rulebook.fingerprint == current
    )

    section("2. Ask the migration's own rule what it would say today")
    print(
        f"  replaying {len(verdicts)} verdicts x {len(candidates)} rulebooks through "
        "decide_fingerprint()\n"
    )

    by_event = group_by_event(verdicts)
    divergences: list[Divergence] = []
    agreed = 0
    attested = 0

    for event_id, event_verdicts in sorted(by_event.items()):
        event = events.get(event_id)
        if event is None:
            problems.append(f"{event_id}: has verdicts but no event document")
            continue

        for verdict in sorted(event_verdicts, key=lambda v: v["version"]):
            label = f"{event_id} v{verdict['version']}"
            stored_source = verdict.get("rulebook_fingerprint_source")
            stored_fingerprint = verdict.get("rulebook_fingerprint")
            if not stored_source or not stored_fingerprint:
                problems.append(
                    f"{label}: carries no fingerprint label, so there is nothing to "
                    "reconcile — run s4_fingerprint_backfill.py first"
                )
                continue

            if stored_source == SOURCE_EVALUATED:
                attested += 1
                continue

            if stored_source not in MIGRATED_SOURCES:
                problems.append(
                    f"{label}: carries source {stored_source!r}, which is neither "
                    f"attested nor one the migration produces {sorted(MIGRATED_SOURCES)}"
                )
                continue

            decision_document = decisions.get(verdict["decision_id"])
            if decision_document is None:
                problems.append(
                    f"{label}: references decision {verdict['decision_id']} which no "
                    "longer exists, so it cannot be replayed"
                )
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

            before = len(backfill.problems)
            fresh_fingerprint, fresh_source, rationale, matches = (
                backfill.decide_fingerprint(
                    label=label, replays=replays, current=current
                )
            )
            # `decide_fingerprint` records its own finding for a verdict no rulebook
            # explains. Carry it across rather than letting it accumulate silently in
            # the imported module.
            for entry in backfill.problems[before:]:
                problems.append(entry)

            if fresh_source == stored_source and fresh_fingerprint == stored_fingerprint:
                agreed += 1
                continue

            divergences.append(
                Divergence(
                    document=verdict,
                    label=label,
                    stored_source=stored_source,
                    stored_fingerprint=stored_fingerprint,
                    fresh_source=fresh_source,
                    fresh_fingerprint=fresh_fingerprint,
                    rationale=rationale,
                    exact=[result.rulebook.fingerprint for result in replays if result.exact],
                )
            )

    print(
        f"  {attested} verdict(s) carry an attested `{SOURCE_EVALUATED}` fingerprint and "
        "were not recomputed;\n    the rulebook that judged them is recorded, so there is "
        "nothing to identify"
    )
    print(f"  {agreed} migrated verdict(s) recompute to exactly the label they carry")
    print(f"  {len(divergences)} diverge\n")

    for divergence in divergences:
        print(f"  {divergence.kind:<21} {divergence.label}")
        print(
            f"                        stored {divergence.stored_fingerprint} "
            f"({divergence.stored_source})"
        )
        print(
            f"                        today  {divergence.fresh_fingerprint} "
            f"({divergence.fresh_source})"
        )
        print(f"                        {len(divergence.exact)} exact match(es): {divergence.exact}")

    return divergences


def triage(divergences: list[Divergence]) -> list[Divergence]:
    section("3. What is in scope, and what is refused")

    writable = [d for d in divergences if d.writable]
    drift = [d for d in divergences if d.kind == "fingerprint_moved"]
    strengthened = [d for d in divergences if d.kind == "source_strengthened"]
    unfit = [d for d in divergences if d.kind == "source_weakened" and not d.writable]
    unclassified = [d for d in divergences if d.kind == "unclassified"]

    print(
        f"  {len(writable)} in scope — a `reconstructed` label whose uniqueness claim no "
        "longer holds,\n     with a recorded fingerprint that still reproduces the "
        "verdict exactly:"
    )
    for divergence in writable:
        print(
            f"    - {divergence.label}: reconstructed -> backfilled, keeps "
            f"{divergence.stored_fingerprint}"
        )
        print(
            f"      {len(divergence.exact)} rulebook(s) now reproduce it: "
            f"{divergence.exact}"
        )

    if unfit:
        print(
            f"\n  {len(unfit)} weakening(s) REFUSED — the recorded fingerprint is no "
            "longer among the exact\n  matches, so weakening the label would leave "
            "behind a fingerprint the record refutes:"
        )
        for divergence in unfit:
            print(
                f"    - {divergence.label}: stored {divergence.stored_fingerprint}, "
                f"exact matches {divergence.exact}"
            )
        problems.append(
            f"{len(unfit)} verdict(s) name a fingerprint that no longer reproduces them; "
            "that is a re-derivation problem for the audit, not a labelling one"
        )

    if drift:
        print(
            f"\n  {len(drift)} verdict(s) show TIEBREAK DRIFT and are left alone. Their "
            "label is already\n  `backfilled` and correct; only the rulebook the tiebreak "
            "would nominate has moved:"
        )
        for divergence in drift[:6]:
            print(
                f"    - {divergence.label}: stored {divergence.stored_fingerprint}, "
                f"today's newest fit {divergence.fresh_fingerprint}"
            )
        if len(drift) > 6:
            print(f"    ... and {len(drift) - 6} more, all the same shape")
        print(
            "\n      `decide_fingerprint` breaks a tie toward the newest rulebook that\n"
            "      fits, and the registry has grown since the migration ran, so the\n"
            "      newest fit is no longer the one stored. Every stored value here is\n"
            "      still an exact match, so nothing about these records is wrong and the\n"
            "      audit re-derives all of them.\n"
            "\n      It does falsify one inference the migration documents: that a\n"
            "      `backfilled` verdict naming a superseded rulebook is one today's\n"
            "      policy could not have produced. Today's rulebook reproduces these\n"
            "      exactly. Re-running the tiebreak would restore that signal, and would\n"
            "      also overwrite values chosen when they were correct for no gain in\n"
            "      re-derivability. That is a decision to ratify, not a correction to\n"
            "      apply, so this script only reports it."
        )

    if strengthened:
        print(
            f"\n  {len(strengthened)} strengthening(s) REFUSED outright — recomputation "
            "claims a unique\n  identification the record does not:"
        )
        for divergence in strengthened:
            print(f"    - {divergence.label}: {divergence.rationale}")
        problems.append(
            f"{len(strengthened)} verdict(s) recompute to `reconstructed` from a stored "
            "`backfilled`; the registry has lost a rulebook that used to fit, which is "
            "an archive problem, not a labelling one"
        )

    if unclassified:
        print(f"\n  {len(unclassified)} divergence(s) this script cannot classify:")
        for divergence in unclassified:
            print(
                f"    - {divergence.label}: {divergence.stored_source}/"
                f"{divergence.stored_fingerprint} -> {divergence.fresh_source}/"
                f"{divergence.fresh_fingerprint}"
            )
        problems.append(f"{len(unclassified)} divergence(s) fit no known transition")

    return writable


async def apply(writable: list[Divergence]) -> None:
    section("4. Applying")

    if not writable:
        print("  nothing in scope; no label needs weakening")
        return

    if not APPLY:
        print(
            f"  DRY RUN — {len(writable)} label(s) would be weakened, nothing written. "
            "Re-run with --apply."
        )
        return

    collection = get_database()["policy_verdicts"]
    written = 0
    for divergence in writable:
        result = await collection.update_one(
            # Re-assert every precondition at write time. If anything stamped this
            # document since the plan was built — including another run of this script —
            # the filter matches nothing rather than overwriting a value it did not read.
            {
                "_id": divergence.document["_id"],
                "rulebook_fingerprint": divergence.stored_fingerprint,
                "rulebook_fingerprint_source": "reconstructed",
            },
            {"$set": {"rulebook_fingerprint_source": "backfilled"}},
        )
        if result.modified_count == 1:
            written += 1
            print(f"  weakened  {divergence.label}")
        else:
            problems.append(
                f"{divergence.label}: update matched {result.matched_count} document(s) "
                f"and modified {result.modified_count}; expected exactly 1"
            )
    print(f"\n  {written} of {len(writable)} label(s) weakened")


async def verify() -> None:
    section("5. Read every verdict back through the model")

    documents = await get_database()["policy_verdicts"].find().to_list(length=None)
    sources: dict[str, int] = {}
    invalid = 0

    for document in documents:
        label = f"{document['event_id']} v{document.get('version')}"
        try:
            record = PolicyVerdictRecord.from_document(document)
        except Exception as exc:  # noqa: BLE001 - the point is to report any failure
            problems.append(f"{label}: does not validate — {exc}")
            print(f"  INVALID  {label}: {type(exc).__name__}")
            invalid += 1
            continue
        sources[record.rulebook_fingerprint_source] = (
            sources.get(record.rulebook_fingerprint_source, 0) + 1
        )

    print(f"  {len(documents)} verdict(s) read back, {len(documents) - invalid} valid")
    print("\n  by source:")
    for source, count in sorted(sources.items()):
        print(f"    {count:>3}  {source}")

    remaining = sources.get("reconstructed", 0)
    print(
        f"\n  {remaining} verdict(s) still claim a unique identification; each was "
        "re-checked\n  above against the full registry as it stands now"
    )


async def main() -> None:
    await connect_to_mongo()
    print("Stage 4 correction — fingerprint labels re-checked against today's registry")
    print(f"mode: {'APPLY (will write)' if APPLY else 'DRY RUN (writes nothing)'}")
    print(
        "\nA `reconstructed` label claims one rulebook and no other reproduces the\n"
        "verdict. That claim is relative to the registry it was tested against, so\n"
        "adding a rulebook can falsify it without the verdict changing. This weakens\n"
        "such labels to `backfilled`. It writes nothing else."
    )

    divergences = await recompute()
    writable = triage(divergences)
    await apply(writable)
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
            f"dry run clean: {len(writable)} label(s) to weaken, "
            f"{len(divergences) - len(writable)} divergence(s) refused, nothing written."
        )
    else:
        print(
            f"{len(writable)} label(s) weakened to `backfilled`; no fingerprint value "
            "was changed and no label was strengthened"
        )


if __name__ == "__main__":
    asyncio.run(main())
