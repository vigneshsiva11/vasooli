"""Stage 5 audit — re-check every stored execution against the verdict it names.

The store's referential guard runs once, at write time. This runs afterwards, over
everything already in the collection, and asks the same questions from scratch. That
redundancy is the point: a guard proves the row that went through it was sound, and
an audit proves the rows that are *there* are sound. They differ whenever something
wrote around the guard — an older build, a migration, a mongo shell, a bug.

Six claims, each re-derived from the database rather than trusted:

1. **The contract still holds.** Every stored document re-validates as an
   `ExecutionRecord`, carries no key outside the declared surface, and that surface
   contains no field capable of claiming the money came back. Stage 5 records what
   was attempted; whether it worked is Stage 6's subject and needs its own evidence.
2. **Nothing executed without permission.** For each record: the verdict exists,
   belongs to the same event, is the claimed version, says `authorized`, and
   authorized the intervention that was actually carried out.
3. **The order of events is consistent.** A verdict was evaluated before its
   execution ran, and any verdict that superseded it was evaluated after — because
   the write-time guard refuses a superseded permission, so a later verdict with an
   *earlier* timestamp would mean that refusal was bypassed.
4. **Idempotency is enforced, not merely observed.** One record per verdict, and a
   unique index that makes a second one impossible rather than unlikely.
5. **The cap-and-cooldown arithmetic is right.** Recomputed here from raw documents,
   independently of `app.policy.store`, and compared with what that module returns.
   A disagreement is not cosmetic: whichever is wrong, the effect is a customer
   contacted more often than the ratified cap allows.
6. **Failure costs nothing.** Every `failed` record explains itself, claims no
   artifact, and is excluded from the counts in claim 5.

A verdict superseded *after* its execution is correct history, not a finding — it is
what happens whenever an event is re-authorized. Kept separate from the findings,
the same way the Stage 4 audit separates historical verdicts from ones still in force.

Nothing here writes. Run:  python scripts/s5_audit.py
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pydantic import ValidationError

from app.db import close_mongo_connection, connect_to_mongo, get_database
from app.decision.store import COLLECTION_NAME as DECISION_COLLECTION
from app.execution.store import COLLECTION_NAME as EXECUTION_COLLECTION
from app.execution.store import VERDICT_INDEX
from app.models.execution import (
    ACTION_FOR_INTERVENTION,
    CONTACT_ACTION_TYPES,
    LINK_ACTION_TYPES,
    ExecutionRecord,
)
from app.policy.rulebook import COOLDOWN_FROM_VERDICT
from app.policy.rules import current_rulebook
from app.policy.store import COLLECTION_NAME as VERDICT_COLLECTION
from app.policy.store import prior_authorized_contacts

#: Anything that would mean an execution is recorded as something it was not.
problems: list[str] = []
#: Correct history, reported so it is visible rather than silently tolerated.
history: list[str] = []
#: Authorized permissions that have not been spent. Not a defect.
pending: list[str] = []

#: Any of these in a field name would let the record answer a question this stage
#: cannot answer. Same list the adversarial suite uses; re-stated here so the claim
#: is checked against the live collection and not only against the model.
OUTCOME_WORDS = re.compile(
    r"recover|receiv|collect|settl|refund|\bpaid\b|success|outcome|verifi"
    r"|confirm|reconcil|opened|clicked|responded",
    re.IGNORECASE,
)


def section(title: str) -> None:
    print(f"\n{title}")
    print("=" * len(title))


async def load_everything() -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, list[dict[str, Any]]],
]:
    """Read the executions, the verdicts they name, the decisions those authorized.

    Verdicts are keyed by string id because that is how an execution references one.
    They are also grouped by event, since claim 3 needs every verdict for an event to
    ask which one superseded which.
    """
    database = get_database()

    executions = (
        await database[EXECUTION_COLLECTION]
        .find({})
        .sort([("executed_at", 1)])
        .to_list(length=None)
    )
    verdict_documents = await database[VERDICT_COLLECTION].find({}).to_list(length=None)
    decision_documents = (
        await database[DECISION_COLLECTION].find({}).to_list(length=None)
    )

    verdicts = {str(document["_id"]): document for document in verdict_documents}
    decisions = {str(document["_id"]): document for document in decision_documents}

    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for document in verdict_documents:
        by_event[document["event_id"]].append(document)

    return executions, verdicts, decisions, by_event


# ---------------------------------------------------------------------------
# 1. The contract
# ---------------------------------------------------------------------------


def check_the_contract(executions: list[dict[str, Any]]) -> None:
    section("1. The data contract, re-checked against what is actually stored")

    declared = set(ExecutionRecord.model_fields)
    offenders = sorted(name for name in declared if OUTCOME_WORDS.search(name))
    if offenders:
        problems.append(
            f"ExecutionRecord declares {offenders}, which could claim an outcome this "
            "stage cannot know"
        )
        print(f"  FINDING   declared fields claim an outcome: {offenders}")
    else:
        print(
            f"  ok        the {len(declared)}-field surface contains nothing that "
            "could claim the money came back"
        )
    print(f"            {sorted(declared)}")

    allowed = declared | {"_id"}
    revalidated = 0
    for document in executions:
        label = f"{document['event_id']} ({document.get('action_type')})"
        extra = sorted(set(document) - allowed)
        if extra:
            problems.append(f"{label}: stored document carries undeclared keys {extra}")
            print(f"  FINDING   {label}: undeclared keys {extra}")
            continue
        smuggled = sorted(name for name in document if OUTCOME_WORDS.search(name))
        if smuggled:
            problems.append(f"{label}: stored keys claim an outcome: {smuggled}")
            print(f"  FINDING   {label}: outcome-claiming keys {smuggled}")
            continue

        payload = {key: value for key, value in document.items() if key != "_id"}
        try:
            ExecutionRecord(**payload)
        except ValidationError as exc:
            first = str(exc).strip().splitlines()
            problems.append(
                f"{label}: stored document no longer validates: "
                f"{next((line.strip() for line in first if 'Value error' in line), first[0])}"
            )
            print(f"  FINDING   {label}: does not re-validate")
            continue
        revalidated += 1

    print(
        f"  ok        all {revalidated} of {len(executions)} stored document(s) "
        "re-validate against the model as written today"
    )


# ---------------------------------------------------------------------------
# 2. Permission
# ---------------------------------------------------------------------------


def check_permission(
    executions: list[dict[str, Any]],
    verdicts: dict[str, dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
) -> None:
    section("2. Every execution names a verdict that authorized exactly it")
    print(
        "  The five questions the write-time guard asks, asked again from scratch. A\n"
        "  guard vouches for the rows that went through it; this vouches for the rows\n"
        "  that are there.\n"
    )

    confirmed = 0
    for document in executions:
        label = f"{document['event_id']} v{document['policy_verdict_version']}"
        verdict = verdicts.get(document["policy_verdict_id"])

        if verdict is None:
            problems.append(
                f"{label}: names verdict {document['policy_verdict_id']} which does "
                "not exist — an execution nothing authorized"
            )
            print(f"  FINDING   {label}: dangling verdict reference")
            continue
        if verdict["event_id"] != document["event_id"]:
            problems.append(
                f"{label}: names a verdict belonging to event {verdict['event_id']!r}"
            )
            print(f"  FINDING   {label}: the verdict belongs to another event")
            continue
        if int(verdict["version"]) != int(document["policy_verdict_version"]):
            problems.append(
                f"{label}: names version {document['policy_verdict_version']} of a "
                f"verdict that is version {verdict['version']}"
            )
            print(f"  FINDING   {label}: version mismatch")
            continue
        if verdict["verdict"] != "authorized":
            problems.append(
                f"{label}: EXECUTED A {verdict['verdict'].upper()} VERDICT "
                f"({verdict['reason']}) — the one thing this system must never do"
            )
            print(f"  FINDING   {label}: executed a {verdict['verdict']} verdict")
            continue

        decision = decisions.get(verdict["decision_id"])
        if decision is None:
            problems.append(
                f"{label}: the authorizing verdict names decision "
                f"{verdict['decision_id']}, which no longer exists"
            )
            print(f"  FINDING   {label}: the authorized decision is gone")
            continue
        authorized_intervention = decision["recommended_intervention"]
        if authorized_intervention != document["intervention"]:
            problems.append(
                f"{label}: executed {document['intervention']!r} on a verdict that "
                f"authorized {authorized_intervention!r}"
            )
            print(f"  FINDING   {label}: intervention mismatch")
            continue

        expected_action = ACTION_FOR_INTERVENTION.get(document["intervention"])
        if expected_action != document["action_type"]:
            problems.append(
                f"{label}: {document['intervention']!r} produces "
                f"{expected_action!r}, recorded as {document['action_type']!r}"
            )
            print(f"  FINDING   {label}: action type does not follow the intervention")
            continue

        confirmed += 1
        print(
            f"  ok        {document['event_id']:<38} v{verdict['version']} "
            f"{document['intervention']:<28} {document['action_type']:<22} "
            f"{document['status']}"
        )

    print(
        f"\n  {confirmed} of {len(executions)} execution(s) trace to an authorized "
        "verdict for the same event, version, and intervention"
    )


# ---------------------------------------------------------------------------
# 3. Ordering
# ---------------------------------------------------------------------------


def check_ordering(
    executions: list[dict[str, Any]],
    verdicts: dict[str, dict[str, Any]],
    by_event: dict[str, list[dict[str, Any]]],
) -> None:
    section("3. Permission came first, and nothing superseded it before it ran")
    print(
        "  A verdict superseded AFTER its execution is ordinary history — that is what\n"
        "  re-authorizing an event produces. A superseding verdict with an EARLIER\n"
        "  timestamp is a finding: the write-time guard refuses a stale permission, so\n"
        "  that ordering means the refusal was bypassed.\n"
    )

    for document in executions:
        verdict = verdicts.get(document["policy_verdict_id"])
        if verdict is None:
            continue  # already a finding in claim 2
        label = f"{document['event_id']} v{verdict['version']}"

        if verdict["evaluated_at"] > document["executed_at"]:
            problems.append(
                f"{label}: executed at {document['executed_at']} but the verdict "
                f"authorizing it was evaluated later, at {verdict['evaluated_at']}"
            )
            print(f"  FINDING   {label}: executed before it was authorized")
            continue

        later = [
            other
            for other in by_event[document["event_id"]]
            if int(other["version"]) > int(verdict["version"])
        ]
        if not later:
            continue

        premature = [
            other for other in later if other["evaluated_at"] < document["executed_at"]
        ]
        if premature:
            names = ", ".join(f"v{other['version']}" for other in premature)
            problems.append(
                f"{label}: superseded by {names}, which was evaluated BEFORE this "
                "execution ran; the staleness guard should have refused it"
            )
            print(f"  FINDING   {label}: ran on a permission already superseded")
            continue

        newest = max(int(other["version"]) for other in later)
        history.append(
            f"{label} was superseded by v{newest} after it ran — the execution is "
            "correct history and the later verdict is the current authorization state"
        )
        print(f"  history   {label} -> superseded by v{newest} after execution")

    ordered = len(executions) - len(
        [entry for entry in problems if "executed before" in entry]
    )
    print(
        f"\n  {ordered} execution(s) ran after the verdict that permitted them; "
        f"{len(history)} were superseded afterwards, by design"
    )


# ---------------------------------------------------------------------------
# 4. Idempotency
# ---------------------------------------------------------------------------


async def check_idempotency(executions: list[dict[str, Any]]) -> None:
    section("4. One execution per permission, enforced rather than observed")

    counts = Counter(document["policy_verdict_id"] for document in executions)
    repeated = {
        verdict_id: count for verdict_id, count in counts.items() if count > 1
    }
    if repeated:
        for verdict_id, count in repeated.items():
            problems.append(
                f"verdict {verdict_id} has {count} execution records; a permission "
                "was spent more than once"
            )
        print(f"  FINDING   verdicts executed more than once: {repeated}")
    else:
        print(f"  ok        {len(counts)} verdict(s) hold exactly one record each")

    indexes = await get_database()[EXECUTION_COLLECTION].index_information()
    enforced = [
        name
        for name, spec in indexes.items()
        if spec.get("unique") and spec["key"] == [("policy_verdict_id", 1)]
    ]
    if enforced:
        print(
            f"  ok        uniqueness is a database constraint, not luck: {enforced} "
            f"(expected {VERDICT_INDEX!r})"
        )
    else:
        problems.append(
            "no unique index on policy_verdict_id; single execution above is "
            "observation, not a guarantee"
        )
        print("  FINDING   no unique index on policy_verdict_id")

    # A payment link recorded twice would mean one artifact answering to two
    # executions, which the verdict index cannot catch on its own.
    links = Counter(
        document["razorpay_payment_link_id"]
        for document in executions
        if document.get("razorpay_payment_link_id")
    )
    shared = {link: count for link, count in links.items() if count > 1}
    if shared:
        for link, count in shared.items():
            problems.append(f"payment link {link} appears on {count} execution records")
        print(f"  FINDING   payment links recorded more than once: {shared}")
    else:
        print(f"  ok        {len(links)} Razorpay artifact(s), each named once")


# ---------------------------------------------------------------------------
# 5. What a completed execution costs, and 6. what a failed one does not
# ---------------------------------------------------------------------------


async def check_cap_and_cooldown_arithmetic(
    executions: list[dict[str, Any]],
    verdicts: dict[str, dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
    by_event: dict[str, list[dict[str, Any]]],
) -> None:
    section("5. The cap-and-cooldown arithmetic, recomputed from raw documents")

    rulebook = current_rulebook()
    print(
        f"  rulebook {rulebook.fingerprint}, cooldown measured from "
        f"{rulebook.cooldown_measured_from!r},\n"
        f"  cap {rulebook.max_contacts_per_event} per event, "
        f"{rulebook.cooldown_hours}h cooldown.\n"
    )
    print(
        "  Recomputed here without calling app.policy.store, then compared with what\n"
        "  that module returns. Two implementations of one rule that disagree mean a\n"
        "  customer is contacted more often than was ratified, whichever is wrong.\n"
    )

    by_verdict_id = {
        document["policy_verdict_id"]: document for document in executions
    }

    events = sorted({document["event_id"] for document in executions})
    for event_id in events:
        contacts = [
            verdict
            for verdict in by_event[event_id]
            if verdict["verdict"] == "authorized"
            and rulebook.is_contact(
                decisions.get(verdict["decision_id"], {}).get(
                    "recommended_intervention", ""
                )
            )
        ]

        anchors: list[Any] = []
        notes: list[str] = []
        for verdict in sorted(contacts, key=lambda v: int(v["version"])):
            execution = by_verdict_id.get(str(verdict["_id"]))
            if rulebook.cooldown_measured_from == COOLDOWN_FROM_VERDICT:
                anchors.append(verdict["evaluated_at"])
                notes.append(f"v{verdict['version']} evaluated")
            elif execution is None:
                anchors.append(verdict["evaluated_at"])
                notes.append(f"v{verdict['version']} reserved (not executed)")
            elif execution["status"] == "completed":
                anchors.append(execution["executed_at"])
                notes.append(f"v{verdict['version']} sent")
            else:
                notes.append(f"v{verdict['version']} FAILED (released)")

        expected_count = len(anchors)
        expected_last = max(anchors) if anchors else None

        actual_count, actual_last = await prior_authorized_contacts(event_id)
        agree = actual_count == expected_count and actual_last == expected_last

        if not agree:
            problems.append(
                f"{event_id}: recomputed ({expected_count}, {expected_last}) but "
                f"app.policy.store returns ({actual_count}, {actual_last})"
            )
            print(f"  FINDING   {event_id}: {expected_count}/{expected_last} vs "
                  f"{actual_count}/{actual_last}")
            continue

        if expected_count > rulebook.max_contacts_per_event:
            problems.append(
                f"{event_id}: {expected_count} effective contacts against a cap of "
                f"{rulebook.max_contacts_per_event}"
            )
            print(f"  FINDING   {event_id}: cap of "
                  f"{rulebook.max_contacts_per_event} exceeded at {expected_count}")
            continue

        print(
            f"  ok        {event_id:<38} {expected_count}/"
            f"{rulebook.max_contacts_per_event}  {'; '.join(notes) or 'no contacts'}"
        )

    print(
        f"\n  {len(events)} event(s) with executions; both implementations of the cap "
        "and cooldown agree on every one"
    )

    section("6. A failed execution explains itself and costs the customer nothing")

    failures = [
        document for document in executions if document["status"] == "failed"
    ]
    if not failures:
        print("  no failed executions stored")
    for document in failures:
        label = f"{document['event_id']} v{document['policy_verdict_version']}"
        if not document.get("failure_reason"):
            problems.append(f"{label}: failed with no reason recorded")
            print(f"  FINDING   {label}: unexplained failure")
            continue
        claimed = [
            field
            for field in (
                "razorpay_payment_link_id",
                "razorpay_payment_link_url",
                "contact_channel",
                "contact_message_summary",
            )
            if document.get(field)
        ]
        if claimed:
            problems.append(f"{label}: failed but claims {claimed}")
            print(f"  FINDING   {label}: a failure claiming artifacts {claimed}")
            continue

        counted = False
        if rulebook.cooldown_measured_from != COOLDOWN_FROM_VERDICT:
            count, _ = await prior_authorized_contacts(document["event_id"])
            live_contacts = [
                verdict
                for verdict in by_event[document["event_id"]]
                if verdict["verdict"] == "authorized"
                and rulebook.is_contact(
                    decisions.get(verdict["decision_id"], {}).get(
                        "recommended_intervention", ""
                    )
                )
                and by_verdict_id.get(str(verdict["_id"]), {}).get("status")
                != "failed"
            ]
            counted = count > len(live_contacts)
        if counted:
            problems.append(
                f"{label}: the failed attempt is still consuming a contact-cap slot"
            )
            print(f"  FINDING   {label}: failure consumed a cap slot")
            continue

        print(
            f"  ok        {label:<42} released; reason: "
            f"{document['failure_reason'][:60]}"
        )

    print(
        f"\n  {len(failures)} failed execution(s), none consuming a contact slot or "
        "anchoring a cooldown"
    )


# ---------------------------------------------------------------------------
# What has not been executed
# ---------------------------------------------------------------------------


def check_unspent_permissions(
    executions: list[dict[str, Any]],
    by_event: dict[str, list[dict[str, Any]]],
    decisions: dict[str, dict[str, Any]],
) -> None:
    section("7. Authorized permissions that have not been spent")
    print(
        "  Not a defect — an authorization is permission to act, not an obligation.\n"
        "  Reported because an unexecuted contact-type authorization still counts\n"
        "  against the cap as a reservation, so it has an effect either way.\n"
    )

    spent = {document["policy_verdict_id"] for document in executions}
    rulebook = current_rulebook()

    for event_id, event_verdicts in sorted(by_event.items()):
        latest = max(event_verdicts, key=lambda verdict: int(verdict["version"]))
        if latest["verdict"] != "authorized" or str(latest["_id"]) in spent:
            continue
        intervention = decisions.get(latest["decision_id"], {}).get(
            "recommended_intervention", "?"
        )
        reserving = rulebook.is_contact(intervention)
        pending.append(
            f"{event_id} v{latest['version']} authorizes {intervention!r} and has not "
            f"been executed ({'reserves a cap slot' if reserving else 'reserves nothing'})"
        )
        print(
            f"  pending   {event_id:<38} v{latest['version']} {intervention:<28} "
            f"{'reserves a slot' if reserving else 'not contact-type'}"
        )

    print(f"\n  {len(pending)} authorized verdict(s) awaiting execution")


def report(executions: list[dict[str, Any]]) -> None:
    print("\n" + "=" * 78)

    statuses = Counter(document["status"] for document in executions)
    actions = Counter(document["action_type"] for document in executions)
    print(
        f"{len(executions)} execution(s): "
        + ", ".join(f"{count} {status}" for status, count in sorted(statuses.items()))
    )
    print(
        "  by action: "
        + ", ".join(f"{count} {action}" for action, count in sorted(actions.items()))
    )
    links = sum(actions[action] for action in LINK_ACTION_TYPES)
    contacts = sum(actions[action] for action in CONTACT_ACTION_TYPES)
    print(f"  {links} Razorpay artifact(s), {contacts} logged contact(s)")

    if history:
        print(f"\n{len(history)} execution(s) on since-superseded permissions:")
        for entry in history:
            print(f"  - {entry}")

    if problems:
        print(f"\nAUDIT FAILED — {len(problems)} finding(s):")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)

    print(
        "\nno stored execution names a verdict that did not authorize it, no record\n"
        "claims an outcome this stage cannot know, no permission was spent twice, and\n"
        "the cap-and-cooldown arithmetic agrees with an independent recomputation"
    )


async def main() -> None:
    await connect_to_mongo()
    print("Stage 5 audit — every stored execution re-checked against its permission")

    executions, verdicts, decisions, by_event = await load_everything()
    print(
        f"\n{len(executions)} execution(s), {len(verdicts)} verdict(s), "
        f"{len(decisions)} decision(s) across {len(by_event)} event(s)"
    )
    if not executions:
        print("\nNothing to audit. Run scripts/s5_verify.py first.")
        await close_mongo_connection()
        sys.exit(1)

    check_the_contract(executions)
    check_permission(executions, verdicts, decisions)
    check_ordering(executions, verdicts, by_event)
    await check_idempotency(executions)
    await check_cap_and_cooldown_arithmetic(executions, verdicts, decisions, by_event)
    check_unspent_permissions(executions, by_event, decisions)

    await close_mongo_connection()
    report(executions)


if __name__ == "__main__":
    asyncio.run(main())
