"""Stage 8 Part B.5 — execute the authorized subset through the real endpoint.

Nothing here decides anything. Every action taken is one the policy layer already
authorized in Part B.4, carried out by `POST /execute/{event_id}`, which reads the
event's own latest verdict and refuses anything else. This script chooses only
*which* authorized events to act on, and that choice is deterministic and stated
below rather than sampled at random.

THE SELECTION RULE lives in `s8_rescope.select()`, imported below rather than restated
here. It is defined once on purpose: the first attempt at this checkpoint had the rule
written twice — a forecast copy in `s8_dryrun.py` and a live copy here — and the two
disagreed (56 executions vs 62), because the forecast copy was computed against a pool
that did not yet contain the 16 Gemini-diagnosed events. Read `s8_rescope` for the rule
and its justification. In outline:

1. Start from the events whose **live** latest verdict is `authorized`. Not the
   forecast from checkpoint 0 — any divergence is reported, not absorbed.
2. Drop the three `_hold_from_authorize` receivables. They have no verdict at all, so
   they are unexecutable by construction; listed anyway so their absence is explicit.
3. Take **every eligible contact-type action**, unbudgeted. Contacts write a templated
   record and call no external API, so they consume nothing scarce.
4. **Ration link-type actions against `--link-budget`**, apportioned across
   (surface, root cause) buckets in proportion to pool size with a floor of one.

WHY THERE IS A BUDGET AT ALL. The first run of this script selected 62 events needing
59 real payment links. Razorpay test mode allows **30 payment links per account for
the lifetime of the account** — a capacity ceiling, not a rate limit — so 54 of them
were refused with a masked `Too many requests` whose real body reads
`RATE_LIMIT_EXCEEDED — "test mode limit of 30 reached for payment_link"`. Cancelling a
link does not return its slot and waiting does not clear it. A refused create is
therefore not retryable into success, which is why the ceiling is enforced here,
before spending, instead of being discovered at call 6 of 59.

Nothing about the pipeline changed to accommodate this. The policy layer authorized
exactly what it authorized before; what changed is how many of those authorizations
the operator elects to spend on, which was always this script's only decision.

The bucket key uses each event's **stored current root cause**, not the dataset's
`_intended_root_cause`. Checkpoint 0 used the intended cause because it had no other
option — the 16 Gemini diagnoses did not exist yet, and their intended cause is
`None`. Now they do exist, so the bucketing reads the same field the decision engine
read, and every event including the model-diagnosed ones falls in a real bucket.

Four events carry a `ptp_honored` role and need a real paid link before a promise can
reach `honored`. Each is pinned to the front of its own bucket's order, so the floor of
one seat per bucket makes taking it structural. Whether that actually held is still
CHECKED rather than trusted, as a hard pass/fail below.

Link actions make real Razorpay test-mode API calls. Contact actions call nothing —
they write a templated record, deterministic by design, with no model in the loop.

Usage:
    .venv/Scripts/python.exe scripts/s8_execute.py --dry-run
    .venv/Scripts/python.exe scripts/s8_execute.py
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import s8_dataset as ds  # noqa: E402
import s8_rescope as rescope  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.models.execution import (  # noqa: E402
    ACTION_FOR_INTERVENTION,
    CONTACT_ACTION_TYPES,
    LINK_ACTION_TYPES,
)

#: What checkpoint 0 forecast, quoted so a divergence is visible rather than implied.
FORECAST_EXECUTIONS = 56
FORECAST_LINKS = 53

#: What this script's FIRST run selected, before the Razorpay test-mode link ceiling
#: was known. Quoted for the same reason: the re-scope is a visible delta, not a
#: quiet replacement of an inconvenient number.
FIRST_RUN_SELECTED = 62
FIRST_RUN_LINKS_NEEDED = 59

#: Stop the run after this many consecutive failures. The first run wrote 54 dead
#: records because nothing was watching; each one pins a verdict that then replays as
#: 200 instead of retrying, so the cost of not noticing is another supersede.
CIRCUIT_BREAKER = 3

PASSED = 0
FAILED = 0


def heading(text: str) -> None:
    print()
    print("=" * 98)
    print(text)
    print("=" * 98)


def check(label: str, condition: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {label}" + (f" — {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))
    return condition


def money(value: float) -> str:
    return f"{value:,.2f}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8123")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="derive and print the selection, execute nothing",
    )
    parser.add_argument(
        "--link-budget",
        type=int,
        default=25,
        help=(
            "real payment links this run may create. Razorpay test mode allows 30 per "
            "account for the account's LIFETIME, and a create that succeeds spends a "
            "slot permanently — cancelling does not return it. A create REFUSED by the "
            "separate burst rate limit costs no slot and can be retried; see --gap."
        ),
    )
    parser.add_argument(
        "--allocation",
        choices=sorted(rescope.ALLOCATIONS),
        default="proportional",
        help="how the link budget is spread across (surface, cause) buckets",
    )
    parser.add_argument(
        "--gap",
        type=float,
        default=20.0,
        help=(
            "seconds to wait before each link-creating call. Razorpay test mode has a "
            "burst limit distinct from the 30-link lifetime cap: measured at 5 creates "
            "accepted in ~16s, the 6th through 8th refused with 25 slots still free, "
            "and a create accepted again 87s later. Contact-type actions call no "
            "external API and are not paced."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "skip events that already hold a COMPLETED execution record instead of "
            "replaying them as 200s, and abort if any FAILED record is still present. "
            "For continuing a batch the burst limit interrupted."
        ),
    )
    parser.add_argument(
        "--allow-existing",
        action="store_true",
        help=(
            "proceed even if demo executions already exist. Off by default: an event "
            "whose verdict already has an execution record replays it as HTTP 200 "
            "instead of acting, so re-running over stale records silently produces a "
            "batch of no-ops rather than the clean run it looks like."
        ),
    )
    args = parser.parse_args()

    specs = {s["event_id"]: s for s in ds.generate()}
    spec_list = list(specs.values())
    held = set(ds.held_back_ids(spec_list))
    roles = ds.roles(spec_list)

    http = httpx.Client(base_url=args.base.rstrip("/"), timeout=180.0)
    health = http.get("/").json()
    print(f"server {args.base}   database={health.get('database')}")

    # =====================================================================
    heading("0. LIVE STATE — what the policy layer actually authorized")
    # =====================================================================
    verdicts = {
        v["event_id"]: v
        for v in http.get("/policy-verdicts").json()
        if v["event_id"] in specs
    }
    decisions = {
        d["event_id"]: d
        for d in http.get("/decisions").json()
        if d["event_id"] in specs
    }
    # Latest diagnosis per demo event, for the bucket key. One call, grouped here,
    # rather than 200 round trips to re-read a field the decision already rests on.
    diagnosis_cause: dict[str, str] = {}
    diagnosis_version: dict[str, int] = {}
    for d in http.get("/diagnoses").json():
        eid = d["event_id"]
        if eid in specs and d["version"] >= diagnosis_version.get(eid, 0):
            diagnosis_version[eid] = d["version"]
            diagnosis_cause[eid] = d["root_cause"]
    print(f"  demo events            : {len(specs)}")
    print(f"  demo verdicts (latest) : {len(verdicts)}")
    print(f"  demo decisions (latest): {len(decisions)}")
    print(f"  demo diagnoses (latest): {len(diagnosis_cause)}")
    by_verdict = Counter(v["verdict"] for v in verdicts.values())
    for verdict, n in by_verdict.most_common():
        print(f"    {verdict:<24} {n:>4}")

    check(
        "the three held-back events still have no verdict",
        not (held & set(verdicts)),
        f"{sorted(held)} unauthorized, as Part B.7 requires"
        if not (held & set(verdicts))
        else f"unexpectedly authorized: {sorted(held & set(verdicts))}",
    )

    already_records = {
        e["event_id"]: e
        for e in http.get("/executions", params={"history": True}).json()
        if e["event_id"] in specs
    }
    already = sorted(already_records)
    done = sorted(
        eid for eid, e in already_records.items() if e.get("status") == "completed"
    )
    stale = sorted(set(already) - set(done))
    check(
        "no demo event has been executed before this run",
        not already,
        f"pre-existing executions: {len(already)} ({len(done)} completed, "
        f"{len(stale)} failed)"
        if already
        else "0 prior demo executions, so every 201 below is a first side effect",
    )
    if already and not (args.dry_run or args.allow_existing or args.resume):
        print(
            f"\n  ABORTING. {len(already)} demo events already carry an execution "
            "record.\n  Their verdicts are already spent, so POST /execute would replay "
            "them as 200s\n  and this run would report success while doing nothing. Run "
            "scripts/s8_supersede.py\n  first to archive and clear them, or pass "
            "--allow-existing if that is genuinely\n  what you want."
        )
        http.close()
        return 1
    # --resume exists because the burst rate limit splits one logical batch across
    # several runs. A completed record is real work and is skipped, not replayed; a
    # failed one must be cleared first, because the unique index on policy_verdict_id
    # turns a retry into a 200 that changes nothing while reading as success.
    if args.resume and stale and not args.allow_existing:
        print(
            f"\n  ABORTING. {len(stale)} demo events carry a FAILED execution record: "
            f"{stale}\n  --resume skips completed work, but a failed record still holds "
            "that event's verdict,\n  so retrying it would replay as 200 rather than "
            "call the gateway. Clear them with\n  scripts/s8_supersede.py --only-failed "
            "--confirm, then re-run."
        )
        http.close()
        return 1

    # =====================================================================
    heading("1. SELECTION — deterministic, re-derived from the live verdicts")
    # =====================================================================
    authorized = sorted(
        eid for eid, v in verdicts.items() if v["verdict"] == "authorized"
    )
    # An authorized verdict cannot name a no-action intervention (policy blocks
    # those), so this filter should remove nothing. Checked rather than assumed: if
    # it ever removes something, the policy layer and the action catalogue disagree.
    eligible = [
        eid
        for eid in authorized
        if decisions[eid]["recommended_intervention"] in ACTION_FOR_INTERVENTION
    ]
    check(
        "every authorized event maps to an executable action",
        len(eligible) == len(authorized),
        f"{len(eligible)} of {len(authorized)}"
        + (
            f"; unexecutable: {sorted(set(authorized) - set(eligible))}"
            if len(eligible) != len(authorized)
            else " — no authorized verdict names a no-action intervention"
        ),
    )

    plan = rescope.select(
        specs=specs,
        decisions=decisions,
        diagnosis_cause=diagnosis_cause,
        eligible=eligible,
        honored=sorted(s["event_id"] for s in roles.get(ds.ROLE_PTP_HONORED, [])),
        budget=args.link_budget,
        allocation=args.allocation,
    )
    buckets = plan["buckets"]
    to_execute = plan["to_execute"]
    planned_links, planned_contacts = plan["link_ids"], plan["contact_ids"]

    print(f"  authorized and executable                        : {len(eligible)}")
    print(f"    link-consuming, rationed to {args.link_budget:>3}              "
          f": {len(planned_links)}")
    print(f"    contact-only, unbudgeted (no external call)    : {len(planned_contacts)}")
    print(f"    TOTAL TO EXECUTE                               : {len(to_execute)}")
    print(
        f"\n  checkpoint 0 forecast {FORECAST_EXECUTIONS} executions and "
        f"{FORECAST_LINKS} links, and the first run of this\n"
        f"  script selected {FIRST_RUN_SELECTED} needing {FIRST_RUN_LINKS_NEEDED} "
        "links. Both are superseded, for two\n"
        "  separate reasons that are worth keeping apart:\n"
        f"    - the forecast was computed against a predicted 106 authorizations "
        f"before the\n      16 Gemini diagnoses existed; the live figure is "
        f"{len(authorized)}. A pool difference.\n"
        "    - the first run then asked for 59 real payment links against a Razorpay\n"
        "      test account that allows 30 for its lifetime, and 54 were refused.\n"
        f"      A capacity limit. Hence the {args.link_budget}-link budget and the "
        f"'{args.allocation}' allocation.\n"
        "  Neither number is absorbed; both are reported."
    )

    print(f"\n  bucket detail — {len(buckets)} (surface, root cause) groups, "
          f"'{args.allocation}' allocation")
    print(f"    {'surface':<13} {'root cause':<26} {'pool':>5} {'taken':>6}")
    for key in sorted(buckets, key=lambda k: (-len(buckets[k]), k)):
        print(f"    {key[0]:<13} {key[1]:<26} {len(buckets[key]):>5} "
              f"{plan['alloc'][key]:>6}")
    print(f"    {'':<13} {'TOTAL':<26} {len(plan['link_pool']):>5} "
          f"{len(planned_links):>6}")

    per_intervention = Counter(
        decisions[e]["recommended_intervention"] for e in to_execute
    )
    link_ids, contact_ids = [], []
    print(f"\n  {'intervention':<32} {'action_type':<22} {'n':>4}  {'money':>15}")
    print("  " + "-" * 78)
    for intervention, n in per_intervention.most_common():
        action = ACTION_FOR_INTERVENTION[intervention]
        members = [
            e for e in to_execute
            if decisions[e]["recommended_intervention"] == intervention
        ]
        if action in LINK_ACTION_TYPES:
            link_ids.extend(members)
        else:
            contact_ids.extend(members)
        at_risk = sum(specs[e]["amount"] for e in members)
        print(f"  {intervention:<32} {action:<22} {n:>4}  {money(at_risk):>15}")
    print("  " + "-" * 78)
    print(
        f"  {'TOTAL':<32} {'':<22} {len(to_execute):>4}  "
        f"{money(sum(specs[e]['amount'] for e in to_execute)):>15}"
    )
    print(
        f"\n  real Razorpay test-mode links to be created : {len(link_ids)}"
        f"  (budget {args.link_budget})"
    )
    print(f"  templated contact records, no external call : {len(contact_ids)}")
    # link_ids/contact_ids are re-derived here from the intervention table, by a
    # different route than the plan built them. Agreement is asserted rather than
    # assumed: if these ever diverge, the number of links this script is about to
    # create is not the number the planner showed the operator, and that discrepancy
    # would be billed in irrecoverable test-account slots.
    check(
        "the printed link/contact split matches the plan the budget was checked against",
        sorted(link_ids) == planned_links and sorted(contact_ids) == planned_contacts,
        f"{len(link_ids)} links and {len(contact_ids)} contacts, re-derived from the "
        "intervention table, agree with the rationed plan"
        if sorted(link_ids) == planned_links and sorted(contact_ids) == planned_contacts
        else f"plan says {len(planned_links)} links/{len(planned_contacts)} contacts, "
        f"table says {len(link_ids)}/{len(contact_ids)}",
    )
    check(
        "the run cannot exceed the link budget",
        len(link_ids) <= args.link_budget,
        f"{len(link_ids)} links <= budget {args.link_budget} — Razorpay test mode "
        "allows 30 per account for its lifetime and a spent slot never comes back",
    )
    print(
        f"  modelled intervention spend                 : "
        f"{money(sum(decisions[e]['estimated_cost'] for e in to_execute))}"
    )

    print("\n  role-carrying events in this batch")
    for role in sorted(roles):
        members = [s["event_id"] for s in roles[role]]
        inside = [e for e in members if e in to_execute]
        print(f"    {role:<32} {len(inside)}/{len(members)} executed  "
              f"{sorted(inside) or 'none'}")
    honored = [s["event_id"] for s in roles.get(ds.ROLE_PTP_HONORED, [])]
    check(
        "all four ptp_honored events are in the set and will get a real link",
        all(e in link_ids for e in honored),
        f"NOT selected: {sorted(set(honored) - set(link_ids))} — a paid link is the "
        "only route to `honored`, so the rationing rule needs revisiting before this runs"
        if not all(e in link_ids for e in honored)
        else f"{sorted(honored)} — checked, not arranged",
    )
    suppressed = [s["event_id"] for s in roles.get(ds.ROLE_PTP_SUPPRESSED, [])]
    check(
        "both ptp_broken_followup_suppressed events will be executed",
        all(e in to_execute for e in suppressed),
        f"{sorted(suppressed)} — their execution starts the 24h cooldown that must "
        "suppress the follow-up",
    )
    still_promised = [s["event_id"] for s in roles.get(ds.ROLE_PTP_PROMISED, [])]
    check(
        "neither ptp_still_promised event is executed",
        not any(e in to_execute for e in still_promised),
        f"{sorted(still_promised)} sit in the review band, so policy never authorized "
        "them",
    )
    optouts = [e for e, s in specs.items() if s["_opted_out"]]
    check(
        "no opted-out event is in the execute set",
        not any(e in to_execute for e in optouts),
        f"{sorted(optouts)} were blocked at the policy layer and have no "
        "authorization to carry out",
    )

    if args.dry_run:
        print("\n  --dry-run: nothing executed.")
        heading(f"SELECTION ONLY — {PASSED} passed, {FAILED} failed")
        http.close()
        return 0 if FAILED == 0 else 1

    # =====================================================================
    heading(f"2. EXECUTE — POST /execute/{{event_id}} for {len(to_execute)} events")
    # =====================================================================
    records: dict[str, dict] = {}
    codes: Counter[int] = Counter()
    errors: list[str] = []
    started = time.monotonic()
    # A gateway refusal is not an HTTP error: POST /execute returns 201 and stores
    # status="failed" with the reason, which is how the first run reported {201: 62}
    # while 54 of them were dead. So the breaker reads the record, not the status code.
    # It exists because each failure writes a record keyed to that event's verdict, and
    # a verdict that already has a record replays as 200 instead of retrying — 28 blind
    # failures would mean a second supersede run, as it did last time.
    consecutive_failures = 0
    tripped = False
    skipped: list[str] = []
    for index, eid in enumerate(to_execute, start=1):
        if args.resume and eid in done:
            skipped.append(eid)
            records[eid] = already_records[eid]
            continue
        # Only link-creating actions touch the gateway, so only they need the gap.
        # Paced before the call rather than after, so an interrupted run that resumes
        # immediately still waits before its first create.
        creates_link = (
            ACTION_FOR_INTERVENTION[decisions[eid]["recommended_intervention"]]
            in LINK_ACTION_TYPES
        )
        if creates_link and args.gap:
            time.sleep(args.gap)
        response = http.post(f"/execute/{eid}")
        codes[response.status_code] += 1
        if response.status_code not in (200, 201):
            errors.append(f"{eid}: {response.status_code} {response.text[:200]}")
            print(f"  {index:>3}/{len(to_execute)}  {eid:<14} HTTP "
                  f"{response.status_code}  {response.text[:120]}")
            consecutive_failures += 1
        else:
            record = response.json()
            records[eid] = record
            if record.get("status") == "completed":
                consecutive_failures = 0
                print(f"  {index:>3}/{len(to_execute)}  {eid:<14} "
                      f"{record.get('action_type'):<24} "
                      f"{record.get('razorpay_payment_link_id') or 'contact'}  "
                      f"({time.monotonic() - started:.0f}s)")
            else:
                consecutive_failures += 1
                print(f"  {index:>3}/{len(to_execute)}  {eid:<14} recorded "
                      f"{record.get('status')} — {record.get('failure_reason')}")
        if consecutive_failures >= CIRCUIT_BREAKER:
            tripped = True
            print(
                f"\n  CIRCUIT BREAKER — {consecutive_failures} consecutive failures. "
                f"Stopping at {index} of {len(to_execute)}\n  rather than writing "
                f"{len(to_execute) - index} more dead records against unspent verdicts."
            )
            break
    if skipped:
        print(f"\n  --resume skipped {len(skipped)} events that were already completed: "
              f"{skipped}")
    check(
        "the run was not stopped early by consecutive failures",
        not tripped,
        f"stopped at {len(records) + len(errors)} of {len(to_execute)} — diagnose "
        "before re-running, then supersede the records this run wrote"
        if tripped
        else f"all {len(to_execute)} events attempted"
        + (f" ({len(skipped)} already complete, {len(to_execute) - len(skipped)} called)"
           if skipped else ""),
    )
    check(
        "every execution call succeeded",
        not errors,
        "\n           ".join(errors) if errors else f"{len(records)} records written",
    )
    check(
        "every call returned 201, not 200",
        codes.get(200, 0) == 0,
        f"status codes: {dict(codes)}"
        + ("" if codes.get(200, 0) == 0 else " — a 200 means nothing happened that call"),
    )

    if not records:
        heading(f"CHECKPOINT 4 — {PASSED} passed, {FAILED} failed")
        http.close()
        return 1

    # =====================================================================
    heading("3. WHAT WAS RECORDED")
    # =====================================================================
    wrong_action = [
        f"{eid}: {r['intervention']} recorded as {r['action_type']}"
        for eid, r in records.items()
        if r["action_type"] != ACTION_FOR_INTERVENTION[r["intervention"]]
    ]
    check(
        "every record's action_type is the one its intervention declares",
        not wrong_action,
        "\n           ".join(wrong_action) if wrong_action else
        f"{len(records)} records against ACTION_FOR_INTERVENTION",
    )

    mismatched_intervention = [
        f"{eid}: decision says {decisions[eid]['recommended_intervention']}, "
        f"execution says {r['intervention']}"
        for eid, r in records.items()
        if r["intervention"] != decisions[eid]["recommended_intervention"]
    ]
    check(
        "every execution carried out the intervention the decision recommended",
        not mismatched_intervention,
        "\n           ".join(mismatched_intervention)
        if mismatched_intervention
        else "no execution substituted a different action for the one authorized",
    )

    unpinned = [
        f"{eid}: execution pins verdict {r['policy_verdict_id']} v"
        f"{r['policy_verdict_version']}, latest is {verdicts[eid]['id']} v"
        f"{verdicts[eid]['version']}"
        for eid, r in records.items()
        if r["policy_verdict_id"] != verdicts[eid]["id"]
        or r["policy_verdict_version"] != verdicts[eid]["version"]
    ]
    check(
        "every execution pins the exact verdict that authorized it",
        not unpinned,
        "\n           ".join(unpinned) if unpinned else
        "the idempotency key is the verdict id, so no execution can be read as "
        "carrying out a later authorization",
    )

    failed = {eid: r for eid, r in records.items() if r["status"] != "completed"}
    check(
        "every attempt completed",
        not failed,
        "\n           ".join(
            f"{eid}: {r['status']} — {r['failure_reason']}" for eid, r in failed.items()
        ) if failed else f"{len(records)} completed, 0 failed",
    )

    bad_links = [
        f"{eid}: id={r['razorpay_payment_link_id']!r} url={r['razorpay_payment_link_url']!r}"
        for eid, r in records.items()
        if r["action_type"] in LINK_ACTION_TYPES
        and r["status"] == "completed"
        and not (
            r["razorpay_payment_link_id"]
            and str(r["razorpay_payment_link_url"] or "").startswith("https://")
        )
    ]
    completed_links = [
        eid for eid, r in records.items()
        if r["action_type"] in LINK_ACTION_TYPES and r["status"] == "completed"
    ]
    completed_contacts = [
        eid for eid, r in records.items()
        if r["action_type"] in CONTACT_ACTION_TYPES and r["status"] == "completed"
    ]
    check(
        "every completed link action carries a Razorpay id and an https URL",
        not bad_links,
        "\n           ".join(bad_links) if bad_links else
        f"{len(completed_links)} completed link records, each with an artifact Stage 6 "
        "can verify against",
    )
    bad_contacts = [
        f"{eid}: channel={r['contact_channel']!r} link_id={r['razorpay_payment_link_id']!r}"
        for eid, r in records.items()
        if r["action_type"] in CONTACT_ACTION_TYPES
        and (
            not r["contact_channel"]
            or not r["contact_message_summary"]
            or r["razorpay_payment_link_id"] is not None
            or r["razorpay_payment_link_url"] is not None
        )
    ]
    check(
        "every contact record carries a channel and summary and NO link artifact",
        not bad_contacts,
        "\n           ".join(bad_contacts) if bad_contacts else
        f"{sum(1 for r in records.values() if r['action_type'] in CONTACT_ACTION_TYPES)} "
        "contact records; a contact claiming a link would be storable only as a "
        "half-fact, and is rejected",
    )

    link_urls = [
        r["razorpay_payment_link_url"]
        for r in records.values()
        if r["razorpay_payment_link_url"]
    ]
    link_id_values = [
        r["razorpay_payment_link_id"]
        for r in records.values()
        if r["razorpay_payment_link_id"]
    ]
    check(
        "no two events share a Razorpay link",
        len(set(link_id_values)) == len(link_id_values),
        f"{len(set(link_id_values))} distinct ids across {len(link_id_values)} link "
        "records",
    )

    print("\n  action_type distribution")
    for action, n in Counter(r["action_type"] for r in records.values()).most_common():
        chased = sum(specs[e]["amount"] for e, r in records.items()
                     if r["action_type"] == action)
        print(f"    {action:<24} {n:>4}   {money(chased):>15} chased")
    total_chased = sum(specs[e]["amount"] for e in records)
    print(f"    {'TOTAL':<24} {len(records):>4}   {money(total_chased):>15}")

    print("\n  sample records, one per action type")
    for action in sorted({r["action_type"] for r in records.values()}):
        eid = next(e for e, r in records.items() if r["action_type"] == action)
        r = records[eid]
        print(f"    {eid}  {action}")
        print(f"      intervention : {r['intervention']}")
        print(f"      verdict      : {r['policy_verdict_id']} v{r['policy_verdict_version']}")
        if r["razorpay_payment_link_id"]:
            print(f"      link         : {r['razorpay_payment_link_id']}  "
                  f"{r['razorpay_payment_link_url']}")
        if r["contact_channel"]:
            print(f"      channel      : {r['contact_channel']}")
            print(f"      summary      : {r['contact_message_summary']}")
        print(f"      executed_at  : {r['executed_at']}   status={r['status']}")

    # =====================================================================
    heading("4. IDEMPOTENCY — a second call must do nothing")
    # =====================================================================
    # One link and one contact, so both paths are covered. A 201 here would mean a
    # second Razorpay link was created for the same authorization.
    probes = [
        next((e for e, r in records.items() if r["action_type"] in LINK_ACTION_TYPES), None),
        next((e for e, r in records.items() if r["action_type"] in CONTACT_ACTION_TYPES), None),
    ]
    idem_problems: list[str] = []
    for eid in [p for p in probes if p]:
        response = http.post(f"/execute/{eid}")
        repeat = response.json()
        if response.status_code != 200:
            idem_problems.append(f"{eid}: second call returned {response.status_code}")
        elif repeat != records[eid]:
            differing = sorted(
                k for k in set(repeat) | set(records[eid])
                if repeat.get(k) != records[eid].get(k)
            )
            idem_problems.append(f"{eid}: second call returned a different record "
                                 f"({differing})")
        print(f"  {eid:<14} {records[eid]['action_type']:<24} "
              f"second call -> HTTP {response.status_code}")
    check(
        "re-executing returns 200 and the identical record",
        not idem_problems,
        "\n           ".join(idem_problems) if idem_problems else
        "no second side effect: the unique index on policy_verdict_id makes a "
        "duplicate unstorable",
    )

    after = [
        e for e in http.get("/executions", params={"history": True}).json()
        if e["event_id"] in specs
    ]
    check(
        "the collection holds exactly one execution per selected event",
        len(after) == len(to_execute),
        f"{len(after)} stored for {len(to_execute)} selected",
    )

    # =====================================================================
    heading("5. THE UNEXECUTED — what was deliberately not touched")
    # =====================================================================
    not_executed = sorted(set(specs) - set(to_execute))
    reasons: Counter[str] = Counter()
    for eid in not_executed:
        v = verdicts.get(eid)
        if v is None:
            reasons["held back from authorize (no verdict)"] += 1
        elif v["verdict"] != "authorized":
            reasons[f"{v['verdict']} / {v['reason']}"] += 1
        else:
            reasons["authorized but not sampled (1-in-2)"] += 1
    print(f"  {len(not_executed)} of {len(specs)} demo events were not executed")
    for reason, n in reasons.most_common():
        print(f"    {reason:<48} {n:>4}")
    print(
        "\n  The 'authorized but not sampled' group is the honest part of the demo: the\n"
        "  agent had permission and the operator chose not to spend. Those events stay\n"
        "  in the at-risk denominator with no recovery against them."
    )

    heading(f"CHECKPOINT 4 — {PASSED} passed, {FAILED} failed")
    http.close()
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
