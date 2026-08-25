"""Stage 4 audit — re-derive every stored verdict and confirm it still holds.

The claim being tested is the one that matters for an authorization layer: *every
permission decision in the database can be reproduced from the record*. For each
stored verdict this script loads the exact decision it references, reconstructs the
world as it stood at that moment, re-runs the pure engine **under the rulebook the
verdict itself names**, and compares the result field by field — including all six
trail entries, string for string.

That last point is what changed. Re-derivation used to run against whatever
parameters were current, which quietly assumed the rulebook had never changed; it has
changed twice. Now each verdict carries a fingerprint of the ratified parameters in
force when it was judged, the superseded parameter sets are archived in
`app/policy/rulebook.py`, and the engine takes the rulebook as an argument — so an old
verdict is checked against the policy that actually produced it.

How strong a claim that supports depends on where the fingerprint came from, and the
audit does not flatten the difference:

* `evaluated` — stamped by the engine as it produced the verdict. Byte-exact
  reproduction is demanded, with no tolerance whatsoever.
* `reconstructed` — identified by the migration as re-deriving exactly under one
  archived rulebook and no other. Byte-exact reproduction is demanded, and the
  uniqueness that justified the label is re-checked, since a later amendment could
  make a second rulebook fit and weaken it.
* `backfilled` — the verdict predates fingerprinting and its true rulebook is
  unrecoverable. The migration stored the newest rulebook consistent with the record,
  so byte-exact reproduction here is partly *by construction* and is not independent
  evidence. Reported as such, and a trail difference is tolerated only where an
  amendment changed whether a check applies at all.

Reconstructing the context is possible only because the verdict log is append-only
and versions are allocated in write order — see `s4_replay.py`, which also does the
replaying. Prior contacts are classified under the rulebook being replayed, not
today's, so the engine is fed the history that actually happened.

`evaluated_at` is an input to the engine rather than something it derives, so it is
fed back in as `now` and is not independently verified — stated plainly rather than
counted as a match.

Two claims are kept apart throughout:

* **Permission** — verdict, reason, decision version — must re-derive byte-exactly
  for every stored verdict, with no exceptions and under every source. A difference
  here means a stored authorization would not be granted again, which is always a
  finding.
* **Trail prose** must also match, with the single narrow exception above.

Also checked: fingerprint coverage and whether every recorded fingerprint is one this
build can identify, `(event_id, version)` uniqueness, every reason/verdict pair
against `REASON_VERDICT`, and — kept separate — whether any verdict that is
*currently in force* rests on a decision that has since been superseded.

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
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.db import close_mongo_connection, connect_to_mongo, get_database
from app.models import DecisionRecord
from app.models.policy import (
    POLICY_CHECKS,
    REASON_VERDICT,
    UNATTESTED_FINGERPRINT_SOURCES,
    check_failed,
    check_name,
)
from app.policy import current_fingerprint, current_rulebook, rulebook_registry
from s4_replay import group_by_event, load_everything, replay

mismatches: list[str] = []
findings: list[str] = []
reclassified: list[str] = []
#: Verdicts whose recorded fingerprint names a rulebook this build has no record of.
unidentified: list[str] = []
#: Verdicts whose recorded rulebook exists but cannot be installed by this build.
unreproducible: list[str] = []
#: `reconstructed` verdicts whose identification no longer holds uniquely.
weakened: list[str] = []


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

    Since verdicts carry a rulebook fingerprint, this is no longer the audit's main
    line of defence but its fallback: it applies only to `backfilled` verdicts, whose
    recorded rulebook is a stand-in rather than a record. A verdict with an attested
    fingerprint gets no tolerance at all.
    """
    if check_name(stored) != check_name(fresh):
        return False
    if check_failed(stored) != check_failed(fresh):
        return False
    return ("not applicable" in stored) != ("not applicable" in fresh)


async def rederive_every_verdict(
    verdicts: list[dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
    opt_outs: dict[str, Any],
    events: dict[str, dict[str, Any]],
    executions: dict[str, dict[str, Any]],
) -> None:
    section("1. Re-derive every stored verdict under the rulebook it names")

    registry = rulebook_registry()
    current = current_rulebook()
    by_event = group_by_event(verdicts)

    checked = 0
    trail_entries = 0
    attested_count = 0

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

            fingerprint = verdict.get("rulebook_fingerprint")
            source = verdict.get("rulebook_fingerprint_source")
            if not fingerprint:
                mismatches.append(
                    f"{label}: carries no rulebook fingerprint, so it can only be "
                    "checked against the present; run "
                    "scripts/s4_fingerprint_backfill.py --apply"
                )
                print(f"  NO STAMP  {label}")
                continue

            rulebook = registry.get(fingerprint)
            if rulebook is None:
                unidentified.append(
                    f"{label} names rulebook {fingerprint}, which no rulebook in "
                    "this build matches; falling back to the rules in force, so its "
                    "comparison below is against the present and not against history"
                )
                print(f"  UNKNOWN   {label:<30} rulebook {fingerprint} not in archive")
                rulebook = current

            #: An attested fingerprint is direct or reproduction-based evidence of
            #: the rulebook that judged this verdict, so no leniency is owed.
            attested = (
                source not in UNATTESTED_FINGERPRINT_SOURCES
                and fingerprint in registry
            )
            attested_count += 1 if attested else 0

            result = replay(
                verdict=verdict,
                decision=decision,
                decisions=decisions,
                opt_outs=opt_outs,
                customer_ref=event["customer_ref"],
                event_verdicts=event_verdicts,
                executions=executions,
                rulebook=rulebook,
            )
            if not result.applied:
                unreproducible.append(f"{label}: {result.error}")
                print(f"  CANNOT    {label:<30} recorded rulebook not installable")
                continue

            problems = list(result.permission_diffs)
            amended: list[str] = []
            trail_entries += len(verdict["checks_performed"])

            for stored_entry, fresh_entry in result.trail_diffs:
                if not attested and is_reclassification(stored_entry, fresh_entry):
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

            # The label `reconstructed` asserts that exactly one known rulebook
            # reproduces this verdict. Re-checked rather than trusted: a later
            # amendment can make a second rulebook fit, at which point the
            # identification is no longer unique and the claim has to be weakened.
            if source == "reconstructed" and fingerprint in registry:
                fits = [
                    candidate.fingerprint
                    for candidate in registry.values()
                    if replay(
                        verdict=verdict,
                        decision=decision,
                        decisions=decisions,
                        opt_outs=opt_outs,
                        customer_ref=event["customer_ref"],
                        event_verdicts=event_verdicts,
                        executions=executions,
                        rulebook=candidate,
                    ).exact
                ]
                if len(fits) != 1 or fits[0] != fingerprint:
                    weakened.append(
                        f"{label} is recorded as reconstructed to {fingerprint}, but "
                        f"{len(fits)} rulebook(s) now reproduce it exactly "
                        f"({sorted(fits)}); the identification is no longer unique"
                    )

            checked += 1
            marker = "attested" if attested else "stand-in"
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
                    f"  amended   {label:<30} {verdict['verdict']:<22} "
                    f"{verdict['reason']:<24} {len(amended)} check(s) reclassified"
                )
            else:
                print(
                    f"  match     {label:<30} {verdict['verdict']:<22} "
                    f"{verdict['reason']:<24} {fingerprint} {marker}"
                )

    print(
        f"\n  {checked} verdicts re-derived under their own recorded rulebook, "
        f"{trail_entries} trail entries compared string for string"
    )
    print(
        f"  {attested_count} carried an attested fingerprint and were held to exact "
        f"reproduction with no tolerance; {checked - attested_count} carried a "
        "stand-in"
    )
    if reclassified:
        print(
            f"  {len(reclassified)} verdict(s) differ in trail wording from their "
            "stand-in rulebook"
        )
    if weakened:
        print(f"  {len(weakened)} reconstruction(s) are no longer unique")


def check_fingerprint_coverage(verdicts: list[dict[str, Any]]) -> None:
    section("2. Which rulebook each verdict says judged it")

    registry = rulebook_registry()
    current = current_fingerprint()

    print(
        "  A fingerprint is the difference between checking history against the rules\n"
        "  of its own time and checking it against today's. What the field can be\n"
        "  trusted to mean depends on its source, so both are reported.\n"
    )

    sources: Counter[str] = Counter()
    pairs: Counter[tuple[str, str]] = Counter()
    for verdict in verdicts:
        fingerprint = verdict.get("rulebook_fingerprint") or "<missing>"
        source = verdict.get("rulebook_fingerprint_source") or "<missing>"
        sources[source] += 1
        pairs[(fingerprint, source)] += 1

    print("  by source:")
    meaning = {
        "evaluated": "stamped by the engine — direct evidence",
        "reconstructed": "identified by unique exact re-derivation — circumstantial",
        "backfilled": "predates fingerprinting — a stand-in, not a claim",
    }
    for source, count in sorted(sources.items()):
        print(f"    {count:>3}  {source:<14} {meaning.get(source, 'UNKNOWN SOURCE')}")
        if source not in meaning:
            mismatches.append(f"{count} verdict(s) carry unknown source {source!r}")

    print("\n  by rulebook:")
    for (fingerprint, source), count in sorted(pairs.items()):
        known = registry.get(fingerprint)
        if known is None:
            note = "NOT IN THIS BUILD"
        elif fingerprint == current:
            note = "in force"
        else:
            note = f"superseded — {known.note[:58]}"
        print(f"    {count:>3}  {fingerprint}  {source:<14} {note}")

    missing = sources.get("<missing>", 0)
    if missing:
        mismatches.append(
            f"{missing} verdict(s) carry no fingerprint; run "
            "scripts/s4_fingerprint_backfill.py --apply"
        )
    else:
        print(f"\n  all {len(verdicts)} verdicts name a rulebook")

    superseded_named = sum(
        count
        for (fingerprint, _), count in pairs.items()
        if fingerprint in registry and fingerprint != current
    )
    print(
        f"  {superseded_named} verdict(s) name a SUPERSEDED rulebook, and are checked "
        "against it rather than against today's parameters"
    )


def check_reason_verdict_pairs(verdicts: list[dict[str, Any]]) -> None:
    section("3. Every stored reason/verdict pair matches the ratified table")

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


def check_trail_completeness(verdicts: list[dict[str, Any]]) -> None:
    section("4. Every stored trail is complete and self-consistent")

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


async def check_uniqueness(verdicts: list[dict[str, Any]]) -> None:
    section("5. Versioning integrity")

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


async def check_staleness(
    verdicts: list[dict[str, Any]], decisions: dict[str, dict[str, Any]]
) -> None:
    section("6. Historical verdicts vs. verdicts currently in force")

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

    latest_verdict: dict[str, dict[str, Any]] = {}
    for verdict in verdicts:
        event_id = verdict["event_id"]
        current = latest_verdict.get(event_id)
        if current is None or verdict["version"] > current["version"]:
            latest_verdict[event_id] = verdict

    historical = 0
    for verdict in verdicts:
        event_id = verdict["event_id"]
        newest = newest_decision.get(event_id, 0)
        if verdict["decision_version"] >= newest:
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


def verify_audit_tolerances() -> None:
    """Prove the audit's own leniency is bounded, on crafted pairs, every run.

    Section 1 treats some trail differences as expected consequences of a ratified
    policy amendment rather than failures. That leniency is only safe if it is
    narrow, so it is tested here every time the audit runs — an audit that quietly
    widened its own tolerance would be worse than no audit.

    Two things are checked: that `is_reclassification` covers only an applicability
    flip reaching the same conclusion, and that the source-based gate which decides
    whether it is consulted at all lets nothing attested through.
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

    # The gate above the guard: tolerance is consulted only for sources that do not
    # attest to the rulebook in force at the time.
    print("\n  and it is only consulted at all for an unattested source:")
    for source, tolerated in (
        ("evaluated", False),
        ("reconstructed", False),
        ("backfilled", True),
    ):
        actual = source in UNATTESTED_FINGERPRINT_SOURCES
        if actual == tolerated:
            print(
                f"    ok    {source:<14} "
                f"{'tolerance applies' if tolerated else 'held to exact reproduction'}"
            )
        else:
            mismatches.append(
                f"source {source!r} tolerance gate is wrong: returned {actual}, "
                f"expected {tolerated}"
            )
            print(f"    BROKEN  {source}: returned {actual}, expected {tolerated}")


def report() -> None:
    print("\n" + "=" * 78)

    if unidentified:
        print(
            f"{len(unidentified)} verdict(s) name a rulebook this build cannot "
            "identify:"
        )
        for entry in unidentified:
            print(f"  - {entry}")
        print(
            "\n  Each was compared against the present instead. Either the archive in\n"
            "  app/policy/rulebook.py is missing an entry, or the canonical form the\n"
            "  fingerprint is computed from has changed without the scheme prefix\n"
            "  changing with it.\n"
        )

    if unreproducible:
        print(
            f"{len(unreproducible)} verdict(s) name a rulebook this build cannot "
            "apply, so they were not re-derived at all:"
        )
        for entry in unreproducible:
            print(f"  - {entry}")
        print()

    if mismatches:
        print(f"AUDIT FAILED — {len(mismatches)} verdict(s) did not re-derive:")
        for problem in mismatches:
            print(f"  - {problem}")
        sys.exit(1)

    if weakened:
        print(f"{len(weakened)} reconstruction(s) no longer identify uniquely:")
        for entry in weakened:
            print(f"  - {entry}")
        print(
            "\n  Not a re-derivation failure — each still re-derives exactly under the\n"
            "  rulebook it names. But the `reconstructed` label claims that rulebook is\n"
            "  the only one that fits, and that is no longer true, so the source should\n"
            "  be weakened to `backfilled`.\n"
        )

    if reclassified:
        print(
            f"{len(reclassified)} verdict(s) differ in trail wording from the "
            "stand-in rulebook recorded for them:"
        )
        for entry in reclassified:
            print(f"  - {entry}")
        print(
            "\n  These carry a `backfilled` fingerprint, which is not a record of what\n"
            "  judged them — the rulebook that did is unrecoverable. Permission\n"
            "  re-derives byte-exactly in every case; what differs is whether a check\n"
            "  applies, which is what a ratified classification change does.\n"
        )
    print("no verdict failed to re-derive under the rulebook it names")

    if findings:
        print(f"\n{len(findings)} finding(s) to review:")
        for finding in findings:
            print(f"  - {finding}")
        sys.exit(1)
    print("no verdict currently in force rests on a superseded recommendation")


async def main() -> None:
    await connect_to_mongo()
    print("Stage 4 audit — every stored verdict re-derived under its own rulebook")

    verify_audit_tolerances()

    verdicts, decisions, opt_outs, events, executions = await load_everything(
        get_database()
    )
    print(
        f"\n{len(verdicts)} verdicts, {len(decisions)} decisions, "
        f"{len(opt_outs)} opted-out customer(s), {len(executions)} execution(s), "
        f"{len(rulebook_registry())} known rulebook(s)"
    )

    await rederive_every_verdict(verdicts, decisions, opt_outs, events, executions)
    check_fingerprint_coverage(verdicts)
    check_reason_verdict_pairs(verdicts)
    check_trail_completeness(verdicts)
    await check_uniqueness(verdicts)
    await check_staleness(verdicts, decisions)

    await close_mongo_connection()
    report()


if __name__ == "__main__":
    asyncio.run(main())
