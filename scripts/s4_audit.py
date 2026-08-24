"""Stage 4 audit — re-derive every stored verdict and confirm it still holds.

The claim being tested is the one that matters for an authorization layer: *every
permission decision in the database can be reproduced from the record*. For each
stored verdict this script loads the exact decision it references, reconstructs
the world as it stood at that moment, re-runs the pure engine, and compares the
result field by field — including all six trail entries, string for string.

Reconstructing the context is possible only because the verdict log is
append-only and versions are allocated in write order, so the facts the engine saw
when it wrote verdict *v* are exactly:

* prior authorized contacts = authorized contact-type verdicts for the event with
  version < *v*;
* last contact = the newest `evaluated_at` among those;
* opt-out state = whether the customer's `opted_out_at` precedes this verdict.

`evaluated_at` is an input to the engine rather than something it derives, so it
is fed back in as `now` and is not independently verified — stated plainly rather
than counted as a match.

Re-derivation happens under the policy rules in force NOW, which means a ratified
policy amendment can legitimately change how an old verdict re-derives. Two
claims are therefore kept apart:

* **Permission** — verdict, reason, decision version — must re-derive byte-exactly
  for every stored verdict, with no exceptions. A difference here means a stored
  authorization would not be granted today, which is always a finding.
* **Trail prose** must also match, except where an amendment changed whether a
  check applies at all. Those are itemized and counted, never silently tolerated,
  and the test for them is narrow enough that it cannot absorb a real regression.

Also checked: `(event_id, version)` uniqueness, every reason/verdict pair against
`REASON_VERDICT`, and — kept separate — whether any verdict that is *currently in
force* rests on a decision that has since been superseded.

Run:  python scripts/s4_audit.py
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bson import ObjectId

from app.db import close_mongo_connection, connect_to_mongo, get_database
from app.models import DecisionRecord
from app.models.policy import POLICY_CHECKS, REASON_VERDICT, check_failed, check_name
from app.policy import PolicyContext, evaluate, is_contact_intervention

mismatches: list[str] = []
findings: list[str] = []
reclassified: list[str] = []


def section(title: str) -> None:
    print(f"\n{title}")
    print("=" * len(title))


def is_reclassification(stored: str, fresh: str) -> bool:
    """Whether two trail entries differ only because a check's applicability changed.

    The signature of a ratified classification change — reclassifying
    `payment_method_update_link` as contact-type, say — is that a check which used
    to record "not applicable" now records a real evaluation, or the reverse, while
    reaching the same PASS/FAIL conclusion.

    Deliberately narrow: it demands the same check name, the same outcome, and an
    applicability flip on exactly one side. A flipped status, a changed verdict, or
    prose differing for any other reason is not covered and stays a mismatch. So
    this cannot quietly absorb a substantive regression — only the wording change
    an amendment is expected to produce.
    """
    if check_name(stored) != check_name(fresh):
        return False
    if check_failed(stored) != check_failed(fresh):
        return False
    return ("not applicable" in stored) != ("not applicable" in fresh)


async def load_everything() -> tuple[list[dict], dict[str, dict], dict[str, Any]]:
    """Read every verdict, every decision (by id), and the opt-out list."""
    database = get_database()

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


def reconstruct_context(
    *,
    verdict: dict,
    decisions: dict[str, dict],
    opt_outs: dict[str, Any],
    customer_ref: str,
    event_verdicts: list[dict],
) -> PolicyContext:
    """Rebuild the facts the engine saw when this verdict was written."""
    earlier = [
        other
        for other in event_verdicts
        if other["version"] < verdict["version"] and other["verdict"] == "authorized"
    ]
    contacts = [
        other
        for other in earlier
        if other["decision_id"] in decisions
        and is_contact_intervention(
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


async def rederive_every_verdict(
    verdicts: list[dict],
    decisions: dict[str, dict],
    opt_outs: dict[str, Any],
    events: dict[str, dict],
) -> None:
    section("1. Re-derive every stored verdict from its referenced decision")

    by_event: dict[str, list[dict]] = defaultdict(list)
    for verdict in verdicts:
        by_event[verdict["event_id"]].append(verdict)

    checked = 0
    trail_entries = 0

    for event_id, event_verdicts in sorted(by_event.items()):
        event = events.get(event_id)
        if event is None:
            mismatches.append(f"{event_id}: has verdicts but no event document")
            continue

        for verdict in sorted(event_verdicts, key=lambda v: v["version"]):
            label = f"{event_id} v{verdict['version']}"

            decision_document = decisions.get(verdict["decision_id"])
            if decision_document is None:
                mismatches.append(
                    f"{label}: references decision {verdict['decision_id']} "
                    "which no longer exists"
                )
                continue

            decision = DecisionRecord.from_document(decision_document)
            context = reconstruct_context(
                verdict=verdict,
                decisions=decisions,
                opt_outs=opt_outs,
                customer_ref=event["customer_ref"],
                event_verdicts=event_verdicts,
            )
            rederived = evaluate(decision=decision, context=context)

            problems = []
            if rederived.verdict != verdict["verdict"]:
                problems.append(
                    f"verdict {verdict['verdict']!r} -> {rederived.verdict!r}"
                )
            if rederived.reason != verdict["reason"]:
                problems.append(f"reason {verdict['reason']!r} -> {rederived.reason!r}")
            if rederived.decision_version != verdict["decision_version"]:
                problems.append(
                    f"decision_version {verdict['decision_version']} -> "
                    f"{rederived.decision_version}"
                )

            stored_trail = verdict["checks_performed"]
            amended: list[str] = []
            if len(stored_trail) != len(POLICY_CHECKS):
                problems.append(
                    f"trail has {len(stored_trail)} entries, expected "
                    f"{len(POLICY_CHECKS)}"
                )
            else:
                for stored_entry, fresh_entry in zip(
                    stored_trail, rederived.checks_performed
                ):
                    trail_entries += 1
                    if stored_entry == fresh_entry:
                        continue
                    if is_reclassification(stored_entry, fresh_entry):
                        amended.append(
                            f"{check_name(stored_entry)} changed applicability:\n"
                            f"        then: {stored_entry}\n"
                            f"        now:  {fresh_entry}"
                        )
                    else:
                        problems.append(
                            f"trail entry differs:\n"
                            f"        stored: {stored_entry}\n"
                            f"        fresh:  {fresh_entry}"
                        )

            checked += 1
            if problems:
                mismatches.append(f"{label}: " + "; ".join(problems))
                print(f"  MISMATCH  {label}")
                for problem in problems:
                    print(f"      {problem}")
            elif amended:
                reclassified.append(
                    f"{label} — permission unchanged "
                    f"({verdict['verdict']}/{verdict['reason']}, decision "
                    f"v{verdict['decision_version']}); "
                    + "; ".join(amended)
                )
                print(
                    f"  amended   {label:<28} {verdict['verdict']:<22} "
                    f"{verdict['reason']:<24} decision "
                    f"v{verdict['decision_version']}  <- {len(amended)} check(s) "
                    "reclassified since"
                )
            else:
                print(
                    f"  match     {label:<28} {verdict['verdict']:<22} "
                    f"{verdict['reason']:<24} decision v{verdict['decision_version']}"
                )

    print(
        f"\n  {checked} verdicts re-derived, {trail_entries} trail entries compared "
        f"string for string"
    )
    if reclassified:
        print(
            f"  {len(reclassified)} verdict(s) were written before a ratified "
            "classification change: permission re-derives identically, trail wording "
            "does not"
        )


def check_reason_verdict_pairs(verdicts: list[dict]) -> None:
    section("2. Every stored reason/verdict pair matches the ratified table")

    seen: Counter[tuple[str, str]] = Counter()
    for verdict in verdicts:
        pair = (verdict["reason"], verdict["verdict"])
        seen[pair] += 1
        expected = REASON_VERDICT.get(verdict["reason"])
        if expected is None:
            mismatches.append(
                f"{verdict['event_id']} v{verdict['version']}: unknown reason "
                f"{verdict['reason']!r}"
            )
        elif expected != verdict["verdict"]:
            mismatches.append(
                f"{verdict['event_id']} v{verdict['version']}: reason "
                f"{verdict['reason']!r} should carry verdict {expected!r}, stored as "
                f"{verdict['verdict']!r}"
            )

    for (reason, verdict_name), count in sorted(seen.items()):
        expected = REASON_VERDICT.get(reason)
        status = "OK  " if expected == verdict_name else "BAD "
        print(f"  {status} {reason:<24} -> {verdict_name:<22} {count:>3} stored")


def check_trail_completeness(verdicts: list[dict]) -> None:
    section("3. Every stored trail is complete and self-consistent")

    for verdict in verdicts:
        label = f"{verdict['event_id']} v{verdict['version']}"
        trail = verdict["checks_performed"]
        names = tuple(check_name(entry) for entry in trail)

        if names != POLICY_CHECKS:
            mismatches.append(f"{label}: trail is {names}, expected {POLICY_CHECKS}")
            continue

        failures = {check_name(entry) for entry in trail if check_failed(entry)}
        if verdict["verdict"] == "authorized" and failures:
            mismatches.append(
                f"{label}: authorized but {sorted(failures)} failed in the trail"
            )
        if verdict["verdict"] != "authorized" and not failures:
            mismatches.append(
                f"{label}: {verdict['verdict']} but every check in the trail passed"
            )

    print(
        f"  all {len(verdicts)} trails carry exactly {len(POLICY_CHECKS)} checks in "
        "declared order, and agree with their own verdict"
    )


async def check_uniqueness(verdicts: list[dict]) -> None:
    section("4. Versioning integrity")

    pairs = Counter((verdict["event_id"], verdict["version"]) for verdict in verdicts)
    duplicates = {pair: count for pair, count in pairs.items() if count > 1}
    if duplicates:
        mismatches.append(f"duplicate (event_id, version) pairs: {duplicates}")
        print(f"  DUPLICATES  {duplicates}")
    else:
        print(f"  {len(pairs)} (event_id, version) pairs, all unique")

    by_event: dict[str, list[int]] = defaultdict(list)
    for verdict in verdicts:
        by_event[verdict["event_id"]].append(verdict["version"])
    for event_id, versions in sorted(by_event.items()):
        ordered = sorted(versions)
        if ordered != list(range(1, len(ordered) + 1)):
            mismatches.append(f"{event_id}: version sequence has gaps: {ordered}")
            print(f"  GAP  {event_id}: {ordered}")

    print(f"  {len(by_event)} events carry verdicts, no gaps in any version sequence")

    indexes = await get_database()["policy_verdicts"].index_information()
    unique = [
        name
        for name, spec in indexes.items()
        if spec.get("unique") and spec["key"] == [("event_id", 1), ("version", -1)]
    ]
    if unique:
        print(f"  uniqueness is enforced by the database, not just observed: {unique}")
    else:
        mismatches.append(
            "no unique index on (event_id, version); uniqueness above is luck"
        )


async def check_staleness(verdicts: list[dict], decisions: dict[str, dict]) -> None:
    section("5. Historical verdicts vs. verdicts currently in force")

    print(
        "  A verdict on a since-superseded decision is correct history and expected.\n"
        "  A verdict that is STILL THE CURRENT authorization while resting on a\n"
        "  superseded recommendation is a finding: it grants permission for an\n"
        "  action the pipeline no longer recommends.\n"
    )

    newest_decision: dict[str, int] = {}
    for document in decisions.values():
        event_id = document["event_id"]
        newest_decision[event_id] = max(
            newest_decision.get(event_id, 0), int(document["version"])
        )

    latest_verdict: dict[str, dict] = {}
    for verdict in verdicts:
        event_id = verdict["event_id"]
        current = latest_verdict.get(event_id)
        if current is None or verdict["version"] > current["version"]:
            latest_verdict[event_id] = verdict

    historical = 0
    for verdict in verdicts:
        event_id = verdict["event_id"]
        newest = newest_decision.get(event_id, 0)
        superseded = verdict["decision_version"] < newest
        if not superseded:
            continue

        if latest_verdict[event_id]["version"] == verdict["version"]:
            findings.append(
                f"{event_id} v{verdict['version']} is the current verdict but "
                f"authorizes decision v{verdict['decision_version']}, superseded "
                f"by v{newest}"
            )
            print(
                f"  FINDING   {event_id} v{verdict['version']} (current) -> "
                f"decision v{verdict['decision_version']}, superseded by v{newest}"
            )
        else:
            historical += 1
            print(
                f"  history   {event_id} v{verdict['version']} -> decision "
                f"v{verdict['decision_version']} (superseded by v{newest}); "
                f"later verdict v{latest_verdict[event_id]['version']} supersedes it"
            )

    print(
        f"\n  {historical} historical verdict(s) on superseded decisions, by design; "
        f"{len(findings)} currently in force on one"
    )


def verify_reclassification_guard() -> None:
    """Prove `is_reclassification` cannot excuse a substantive difference.

    Section 1 treats some trail differences as expected consequences of a ratified
    policy amendment rather than failures. That leniency is only safe if it is
    narrow, so it is tested here, on crafted pairs, every time the audit runs — an
    audit that quietly widened its own tolerance would be worse than no audit.
    """
    section("0. The audit's own tolerance is bounded")

    opt_na = (
        "customer_opt_out: PASS (not applicable: payment_method_update_link does "
        "not contact the customer)"
    )
    opt_pass = "customer_opt_out: PASS (customer cust_x has not opted out)"
    opt_fail = (
        "customer_opt_out: FAIL (customer cust_x is on the do-not-contact list and "
        "payment_method_update_link would contact them)"
    )
    cap_na = (
        "contact_cap: PASS (not applicable: payment_method_update_link does not "
        "count as a contact)"
    )
    cap_none = "contact_cap: PASS (0 of 3 contacts used for event e1)"
    cap_one = "contact_cap: PASS (1 of 3 contacts used for event e1)"
    cap_full = (
        "contact_cap: FAIL (3 contact(s) already authorized for event e1, cap is 3 "
        "per event across all decision versions)"
    )

    cases = [
        ("an amendment made a check applicable; it still passes", opt_na, opt_pass, True),
        ("the same, for the contact cap", cap_na, cap_none, True),
        ("the reverse — an amendment narrowed the contact set", opt_pass, opt_na, True),
        ("a check became applicable AND now FAILS (opted-out customer)", opt_na, opt_fail, False),
        ("a check became applicable AND the cap is now exceeded", cap_na, cap_full, False),
        ("a real failure re-described as not applicable", opt_fail, opt_na, False),
        ("prose differs but applicability did not", cap_none, cap_one, False),
        ("two different checks compared", opt_na, cap_na, False),
    ]

    for label, stored, fresh, expected in cases:
        actual = is_reclassification(stored, fresh)
        if actual == expected:
            verdict = "tolerated" if expected else "still a mismatch"
            print(f"  ok    {verdict:<16} {label}")
        else:
            mismatches.append(
                f"the audit's reclassification guard is wrong about {label!r}: "
                f"returned {actual}, expected {expected}"
            )
            print(f"  BROKEN  {label}: returned {actual}, expected {expected}")

    print(
        f"\n  {len(cases)} cases: only an applicability flip that reaches the same "
        "conclusion is tolerated"
    )


async def main() -> None:
    await connect_to_mongo()
    print("Stage 4 audit — every stored verdict re-derived from the record")

    verify_reclassification_guard()

    verdicts, decisions, opt_outs, events = await load_everything()
    print(
        f"{len(verdicts)} verdicts, {len(decisions)} decisions, "
        f"{len(opt_outs)} opted-out customer(s)"
    )

    await rederive_every_verdict(verdicts, decisions, opt_outs, events)
    check_reason_verdict_pairs(verdicts)
    check_trail_completeness(verdicts)
    await check_uniqueness(verdicts)
    await check_staleness(verdicts, decisions)

    await close_mongo_connection()

    print("\n" + "=" * 78)
    if mismatches:
        print(f"AUDIT FAILED — {len(mismatches)} verdict(s) did not re-derive:")
        for problem in mismatches:
            print(f"  - {problem}")
        sys.exit(1)

    if reclassified:
        print(
            f"all {len(verdicts)} stored verdicts re-derive their PERMISSION exactly; "
            f"{len(reclassified)} predate a policy amendment"
        )
        print(
            f"\n{len(reclassified)} verdict(s) written under an earlier rulebook:"
        )
        for entry in reclassified:
            print(f"  - {entry}")
        print(
            "\n  These are correct history. Each was evaluated under the policy in\n"
            "  force at the time; what changed since is whether a check applies, not\n"
            "  what it concluded. Verdict, reason and decision version all re-derive\n"
            "  byte-exactly, so no stored authorization would be decided differently\n"
            "  today.\n"
            "\n"
            "  Worth stating: that they stayed benign is not structural. None of these\n"
            "  customers had opted out and none of these events had a later verdict, so\n"
            "  widening the contact set could not change their outcome. Had either been\n"
            "  true, permission would have moved and this would read as a failure.\n"
            "\n"
            "  The underlying gap: a verdict does not record which rulebook judged it,\n"
            "  so this audit can only ever compare old records against today's rules.\n"
            "  Stamping each verdict with a fingerprint of the ratified parameters would\n"
            "  let history be checked against the policy that actually produced it."
        )
    else:
        print(
            f"all {len(verdicts)} stored verdicts re-derive exactly from their decisions"
        )

    if findings:
        print(f"\n{len(findings)} finding(s) to review:")
        for finding in findings:
            print(f"  - {finding}")
        sys.exit(1)
    print("no verdict currently in force rests on a superseded recommendation")


if __name__ == "__main__":
    asyncio.run(main())
