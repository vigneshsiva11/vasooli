"""Stage 8 Part B.5 (revised) — re-scope the execute set to fit a real link budget.

WHY THIS EXISTS. The first checkpoint 4 run selected 62 events needing 59 real
payment links. 8 completed and 54 were refused by Razorpay with a masked
`BAD_REQUEST_ERROR: Too many requests`. The unmasked error is:

    RATE_LIMIT_EXCEEDED — "test mode limit of 30 reached for payment_link"

Razorpay test mode allows **30 payment links per account for the lifetime of the
account**. That is a capacity ceiling, not a rate limit: 15 creations at a 3s gap
were refused 15 times, the refusals persisted 11 hours, and cancelling a link does
not return its slot. So no pacing strategy and no amount of waiting recovers the
missing 54. The only fix is fewer links, on an account that has slots left.

WHAT CHANGED, AND WHAT DID NOT. The pipeline is untouched — no diagnosis, decision,
policy, execution or verification logic changes. What changes is *how many of the
authorized events the operator chooses to spend on*, which was always this script's
only job. The policy layer still authorizes exactly what it authorized before.

THE REVISED SELECTION RULE, re-derived from live data every run:

1. Start from the events whose **live** latest verdict is `authorized`, exactly as
   before. Nothing about authorization is re-litigated here.
2. Take **every eligible contact-type action**, unbudgeted. Contacts write a
   templated record and call no external API, so they consume none of the scarce
   resource. The old rule said this about receivables; the constraint turns out to be
   a *link* cap specifically, so the exemption is stated in terms of what is scarce.
3. Ration link-type actions against LINK_BUDGET, bucketed by (surface, live root
   cause). Two allocations are implemented and both are printed every run; one is
   selected by `--allocation`:
     - `proportional` (default): each bucket's share is `pool * budget / total`, by
       largest-remainder apportionment, with a **floor of one seat per bucket**.
     - `flat`: one per bucket per pass, round-robin, biggest pools first.

WHY PROPORTIONAL IS THE DEFAULT. Flat maximises cause diversity, but at a budget of 25
over 13 buckets it lands exactly on depth 2, so `insufficient_funds` (pool 25) gets the
same two links as `subscription/issuer_declined` (pool 2). Measured against the pool it
draws from, flat drifts 35.5pp in total absolute terms and proportional drifts 14.0pp —
so the executed cohort's cause mix mirrors the population it came from instead of
flattening it, which is what makes a per-cause recovery rate mean anything. Nothing is
hidden by this choice: all 14 causes remain in the at-risk and authorized populations
regardless, because rationing changes only what the operator spends on.

WHY THE FLOOR OF ONE IS LOAD-BEARING. It is not a fairness gesture. Four events carry a
`ptp_honored` role and need a real paid link before a promise can reach `honored`; each
is pinned to the front of its own bucket's order, so guaranteeing every bucket at least
one seat is exactly what guarantees those four are taken. Without the floor, a bucket
whose fair share rounded to zero would silently drop a role the promise scenarios
depend on. Both the floor and the outcome are asserted below rather than assumed.

This script WRITES NOTHING — not to MongoDB, not to Razorpay. It prints the plan.

Usage:
    .venv/Scripts/python.exe scripts/s8_rescope.py
    .venv/Scripts/python.exe scripts/s8_rescope.py --link-budget 25
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import s8_dataset as ds  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.models.execution import (  # noqa: E402
    ACTION_FOR_INTERVENTION,
    LINK_ACTION_TYPES,
)

#: Razorpay test-mode payment links per account, for the account's lifetime.
#: Measured, not documented — see the module docstring.
TEST_MODE_LINK_CEILING = 30

#: What the first checkpoint 4 run selected, quoted so the re-scope is a visible
#: delta rather than a quiet replacement.
PREVIOUS_SELECTED = 62
PREVIOUS_LINKS_NEEDED = 59

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


def ration_flat(
    buckets: dict[tuple[str, str], list[str]], budget: int
) -> tuple[list[str], dict[tuple[str, str], int]]:
    """Round-robin one per bucket per pass, biggest pools first, until budget spent.

    Maximises cause diversity in the executed cohort at the cost of its realism: at a
    budget of 25 over 13 buckets this lands exactly on depth 2, so every bucket gets
    the same 2 links whether its pool holds 25 events or 2. The pool-size ordering
    only breaks ties on a partial final pass, and does nothing at all when the budget
    divides evenly into the bucket count.
    """
    order = sorted(buckets, key=lambda k: (-len(buckets[k]), k))
    taken: list[str] = []
    per_bucket: dict[tuple[str, str], int] = {key: 0 for key in buckets}
    depth = 0
    while len(taken) < budget:
        progressed = False
        for key in order:
            if len(taken) >= budget:
                break
            group = buckets[key]
            if depth < len(group):
                taken.append(group[depth])
                per_bucket[key] += 1
                progressed = True
        if not progressed:
            break  # every bucket exhausted before the budget was
        depth += 1
    return taken, per_bucket


def ration_proportional(
    buckets: dict[tuple[str, str], list[str]], budget: int
) -> tuple[list[str], dict[tuple[str, str], int]]:
    """Apportion the budget in proportion to pool size, with a floor of one per bucket.

    Largest-remainder apportionment: each bucket's fair share is
    `pool * budget / total`, floored, then the leftover seats go to the buckets with
    the largest unmet fractions. The executed cohort's cause mix then mirrors the
    at-risk population instead of flattening it, which is what makes a per-cause
    recovery rate mean anything.

    THE FLOOR OF ONE IS LOAD-BEARING, not a fairness gesture. It is what keeps the
    `ptp_honored` pin a guarantee: those four events are first in their own bucket's
    order, so they are taken as long as their bucket gets at least one seat. Without
    the floor, a bucket whose fair share rounds to zero would silently drop a role
    the promise scenarios depend on. It also costs nothing here — every bucket's fair
    share is above zero anyway; the floor only makes the guarantee structural rather
    than incidental.
    """
    keys = sorted(buckets, key=lambda k: (-len(buckets[k]), k))
    total = sum(len(buckets[key]) for key in keys)
    quota = {key: len(buckets[key]) * budget / total for key in keys}
    alloc = {key: min(len(buckets[key]), max(1, int(quota[key]))) for key in keys}

    # Hand out or claw back seats one at a time, always to the bucket furthest from
    # its fair share, so the result is deterministic and re-derivable.
    while sum(alloc.values()) < budget:
        candidates = [k for k in keys if alloc[k] < len(buckets[k])]
        if not candidates:
            break
        best = min(candidates, key=lambda k: (-(quota[k] - alloc[k]), -len(buckets[k]), k))
        alloc[best] += 1
    while sum(alloc.values()) > budget:
        candidates = [k for k in keys if alloc[k] > 1]
        if not candidates:
            break
        worst = min(candidates, key=lambda k: (quota[k] - alloc[k], len(buckets[k]), k))
        alloc[worst] -= 1

    taken: list[str] = []
    for key in keys:
        taken.extend(buckets[key][: alloc[key]])
    return taken, alloc


ALLOCATIONS = {"flat": ration_flat, "proportional": ration_proportional}


def select(
    *,
    specs: dict,
    decisions: dict,
    diagnosis_cause: dict,
    eligible: list[str],
    honored: list[str],
    budget: int,
    allocation: str,
) -> dict:
    """THE single definition of the execute-set rule.

    Both the planner and `s8_execute.py` call this. The first checkpoint 4 run had the
    rule written out twice — once as a forecast in `s8_dryrun.py` and once live in
    `s8_execute.py` — and they disagreed (56 vs 62), because the forecast copy was
    computed against a pool that did not yet contain the Gemini-diagnosed events. A
    planner that promises 25 links and an executor that creates 30 is the same bug
    with a real bill attached, so there is one function and both callers use it.

    `eligible` must already be the authorized-and-executable set: this function
    rations, it does not authorize.
    """
    link_pool, contact_pool = [], []
    for eid in eligible:
        action = ACTION_FOR_INTERVENTION[decisions[eid]["recommended_intervention"]]
        (link_pool if action in LINK_ACTION_TYPES else contact_pool).append(eid)

    buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
    for eid in link_pool:
        buckets[(specs[eid]["surface"], diagnosis_cause[eid])].append(eid)
    # ptp_honored first within its own bucket, then event id. Pinning the order is
    # what makes the role guarantee hold without exempting the event from the rule.
    for key in buckets:
        buckets[key].sort(key=lambda e: (e not in honored, e))

    taken, alloc = ALLOCATIONS[allocation](dict(buckets), budget)
    return {
        "link_pool": sorted(link_pool),
        "buckets": dict(buckets),
        "alloc": alloc,
        "link_ids": sorted(taken),
        "contact_ids": sorted(contact_pool),
        "to_execute": sorted(set(taken) | set(contact_pool)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8123")
    parser.add_argument(
        "--link-budget",
        type=int,
        default=25,
        help=(
            "real payment links this run may create. Default 25, leaving 5 of a "
            "fresh account's 30 as headroom for checkpoint 5/6 and for mistakes."
        ),
    )
    parser.add_argument(
        "--allocation",
        choices=sorted(ALLOCATIONS),
        default="proportional",
        help=(
            "how to spread the link budget across (surface, cause) buckets. "
            "'proportional' mirrors the at-risk mix; 'flat' gives every bucket the "
            "same count. Both are printed for comparison; this picks the one used."
        ),
    )
    args = parser.parse_args()

    specs = {s["event_id"]: s for s in ds.generate()}
    spec_list = list(specs.values())
    held = set(ds.held_back_ids(spec_list))
    roles = ds.roles(spec_list)
    honored = sorted(s["event_id"] for s in roles.get(ds.ROLE_PTP_HONORED, []))

    http = httpx.Client(base_url=args.base.rstrip("/"), timeout=180.0)
    health = http.get("/").json()
    print(f"server {args.base}   database={health.get('database')}")
    print(f"link budget for this run : {args.link_budget}")

    # =====================================================================
    heading("0. THE DAMAGE — what the first checkpoint 4 run left behind")
    # =====================================================================
    executions = [
        e for e in http.get("/executions", params={"history": True}).json()
        if e["event_id"] in specs
    ]
    by_status = Counter(e["status"] for e in executions)
    completed_links = [
        e for e in executions
        if e["status"] == "completed" and e["action_type"] in LINK_ACTION_TYPES
    ]
    completed_contacts = [
        e for e in executions
        if e["status"] == "completed" and e["action_type"] not in LINK_ACTION_TYPES
    ]
    print(f"  demo execution records : {len(executions)}")
    for status, n in by_status.most_common():
        print(f"    {status:<12} {n:>4}")
    print(f"  completed WITH a real link (on the old account) : {len(completed_links)}")
    print(f"  completed contacts, no external call            : {len(completed_contacts)}")
    reasons = Counter(
        e.get("failure_reason") or "" for e in executions if e["status"] == "failed"
    )
    for reason, n in reasons.most_common():
        print(f"  failure reason x{n}: {reason}")

    # The artifact field names are asserted against a record that must carry them
    # first. Probing `.get()` on a misspelled key returns None and would make the
    # "no fabricated link" check below pass without testing anything.
    ID_FIELD, URL_FIELD = "razorpay_payment_link_id", "razorpay_payment_link_url"
    check(
        "the link artifact fields exist on records that must carry them",
        bool(completed_links)
        and all(e.get(ID_FIELD) and e.get(URL_FIELD) for e in completed_links),
        f"{len(completed_links)} completed link records all carry {ID_FIELD} and "
        f"{URL_FIELD}, so probing those fields below is a real test"
        if completed_links
        else "no completed link record to confirm the field names against",
    )
    check(
        "no failed execution smuggled in a link artifact",
        not [
            e for e in executions
            if e["status"] == "failed" and (e.get(ID_FIELD) or e.get(URL_FIELD))
        ],
        "a refused create wrote no id and no URL, so nothing fabricated a link",
    )

    # =====================================================================
    heading("1. LIVE AUTHORIZATION — unchanged, and not re-litigated here")
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
    diagnosis_cause: dict[str, str] = {}
    diagnosis_version: dict[str, int] = {}
    for d in http.get("/diagnoses").json():
        eid = d["event_id"]
        if eid in specs and d["version"] >= diagnosis_version.get(eid, 0):
            diagnosis_version[eid] = d["version"]
            diagnosis_cause[eid] = d["root_cause"]

    authorized = sorted(
        eid for eid, v in verdicts.items() if v["verdict"] == "authorized"
    )
    eligible = [
        eid for eid in authorized
        if decisions[eid]["recommended_intervention"] in ACTION_FOR_INTERVENTION
    ]
    print(f"  demo verdicts (latest)   : {len(verdicts)}")
    print(f"  authorized               : {len(authorized)}")
    print(f"  authorized and executable: {len(eligible)}")
    check(
        "the three held-back events still have no verdict",
        not (held & set(verdicts)),
        f"{sorted(held)} unauthorized, as Part B.7 requires",
    )

    common = dict(
        specs=specs,
        decisions=decisions,
        diagnosis_cause=diagnosis_cause,
        eligible=eligible,
        honored=honored,
        budget=args.link_budget,
    )
    plans = {name: select(allocation=name, **common) for name in sorted(ALLOCATIONS)}
    plan = plans[args.allocation]
    link_pool, contact_pool = plan["link_pool"], plan["contact_ids"]
    print(f"    link-consuming (rationed): {len(link_pool)}")
    print(f"    contact-only (unbudgeted): {len(contact_pool)}")

    # =====================================================================
    heading("2. THE RE-SCOPED SELECTION")
    # =====================================================================
    buckets = plan["buckets"]

    check(
        "the link budget is at least the bucket count, so every bucket gets a slot",
        args.link_budget >= len(buckets),
        f"budget {args.link_budget} >= {len(buckets)} buckets — this is what makes the "
        "ptp_honored pin a guarantee rather than a hope",
    )

    flat_links, flat_alloc = plans["flat"]["link_ids"], plans["flat"]["alloc"]
    prop_links = plans["proportional"]["link_ids"]
    prop_alloc = plans["proportional"]["alloc"]
    taken_links, per_bucket = plan["link_ids"], plan["alloc"]
    to_execute = sorted(set(taken_links) | set(contact_pool))

    print(f"\n  bucket detail — {len(buckets)} (surface, root cause) groups. Both "
          "allocations shown;")
    print(f"  '{args.allocation}' is the one selected. Buckets ordered by pool size.")
    print(f"    {'surface':<13} {'root cause':<26} {'pool':>5} {'prop':>5} "
          f"{'flat':>5}  pinned")
    for key in sorted(buckets, key=lambda k: (-len(buckets[k]), k)):
        pins = [e for e in buckets[key] if e in honored]
        print(f"    {key[0]:<13} {key[1]:<26} {len(buckets[key]):>5} "
              f"{prop_alloc[key]:>5} {flat_alloc[key]:>5}  {','.join(pins) or '-'}")
    print(f"    {'':<13} {'TOTAL':<26} {len(link_pool):>5} "
          f"{len(prop_links):>5} {len(flat_links):>5}")

    print("\n  contact-only actions, unbudgeted because they call no external API")
    for eid in sorted(contact_pool):
        print(f"    {eid}  {specs[eid]['surface']:<13} "
              f"{diagnosis_cause[eid]:<26} "
              f"{decisions[eid]['recommended_intervention']}")
    print("  This is where the 14th bucket went: dunning_exhausted is a "
          "manual_escalation,")
    print("  so it is executed as a free contact rather than rationed as a link.")

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

    print(f"\n  real Razorpay test-mode links to create : {len(link_ids)}")
    print(f"  templated contact records, no API call   : {len(contact_ids)}")
    print(f"  modelled intervention spend              : "
          f"{money(sum(decisions[e]['estimated_cost'] for e in to_execute))}")
    print(f"  money chased                             : "
          f"{money(sum(specs[e]['amount'] for e in to_execute))}")

    check(
        "every bucket holding a ptp_honored event got at least one seat",
        all(per_bucket[(specs[e]["surface"], diagnosis_cause[e])] >= 1 for e in honored),
        "the floor of one per bucket is what turns the pin into a guarantee",
    )

    # The reason to prefer one allocation over the other, stated as numbers rather
    # than asserted: how far each executed mix drifts from the pool it is drawn from.
    print("\n  executed cause mix vs the authorized link pool it is drawn from")
    print(f"    {'root cause':<28} {'pool':>5} {'pool %':>7} {'prop %':>7} {'flat %':>7}")
    pool_by_cause = Counter(diagnosis_cause[e] for e in link_pool)
    prop_by_cause = Counter(diagnosis_cause[e] for e in prop_links)
    flat_by_cause = Counter(diagnosis_cause[e] for e in flat_links)
    prop_drift = flat_drift = 0.0
    for cause, n in pool_by_cause.most_common():
        pool_pct = 100.0 * n / len(link_pool)
        prop_pct = 100.0 * prop_by_cause[cause] / len(prop_links)
        flat_pct = 100.0 * flat_by_cause[cause] / len(flat_links)
        prop_drift += abs(prop_pct - pool_pct)
        flat_drift += abs(flat_pct - pool_pct)
        print(f"    {cause:<28} {n:>5} {pool_pct:>6.1f}% {prop_pct:>6.1f}% "
              f"{flat_pct:>6.1f}%")
    print(f"\n    total absolute drift from the pool mix: "
          f"proportional {prop_drift:.1f}pp, flat {flat_drift:.1f}pp")

    check(
        "the plan fits the budget",
        len(link_ids) <= args.link_budget,
        f"{len(link_ids)} links <= budget {args.link_budget}",
    )
    check(
        "the plan fits a fresh account's lifetime ceiling with headroom",
        len(link_ids) < TEST_MODE_LINK_CEILING,
        f"{len(link_ids)} links against a {TEST_MODE_LINK_CEILING}-link account — "
        f"{TEST_MODE_LINK_CEILING - len(link_ids)} slots spare",
    )

    # =====================================================================
    heading("3. ROLE COVERAGE — checked, not arranged")
    # =====================================================================
    for role in sorted(roles):
        members = [s["event_id"] for s in roles[role]]
        inside = [e for e in members if e in to_execute]
        print(f"    {role:<32} {len(inside)}/{len(members)} executed  "
              f"{sorted(inside) or 'none'}")

    check(
        "all four ptp_honored events are in the set and will get a real link",
        all(e in link_ids for e in honored),
        f"{honored} — each pinned to the front of its own bucket's order, so it is "
        "taken on that bucket's first seat under either allocation"
        if all(e in link_ids for e in honored)
        else f"missing: {[e for e in honored if e not in link_ids]}",
    )
    suppressed = sorted(s["event_id"] for s in roles.get(ds.ROLE_PTP_SUPPRESSED, []))
    check(
        "both ptp_broken_followup_suppressed events are in the set",
        all(e in contact_ids for e in suppressed),
        f"{suppressed} — contact-type, so unbudgeted; their execution starts the 24h "
        "cooldown that must suppress the follow-up",
    )
    promised = sorted(s["event_id"] for s in roles.get(ds.ROLE_PTP_PROMISED, []))
    check(
        "neither ptp_still_promised event is executed",
        not [e for e in promised if e in to_execute],
        f"{promised} sit in the review band, so policy never authorized them",
    )
    optout = sorted(s["event_id"] for s in roles.get(ds.ROLE_OPT_OUT, []))
    check(
        "no opted-out event is in the execute set",
        not [e for e in optout if e in to_execute],
        f"{optout} were blocked at the policy layer and carry no authorization",
    )
    check(
        "no held-back event is in the execute set",
        not (held & set(to_execute)),
        f"{sorted(held)} have no verdict at all, so they are unexecutable",
    )

    # =====================================================================
    heading("4. DELTA — what the re-scope costs the demo")
    # =====================================================================
    print(f"  {'':<34} {'first run':>10} {'re-scoped':>10}")
    print(f"  {'events selected':<34} {PREVIOUS_SELECTED:>10} {len(to_execute):>10}")
    print(f"  {'links needed':<34} {PREVIOUS_LINKS_NEEDED:>10} {len(link_ids):>10}")
    print(f"  {'links obtainable':<34} {0:>10} {len(link_ids):>10}")
    print(f"  {'executions that can complete':<34} {8:>10} {len(to_execute):>10}")
    pct_of_authorized = 100.0 * len(to_execute) / len(eligible) if eligible else 0.0
    print(
        f"\n  the re-scoped set is {pct_of_authorized:.1f}% of the "
        f"{len(eligible)} authorized-and-executable events."
    )
    print(
        f"  the other {len(eligible) - len(to_execute)} stay in the at-risk "
        "denominator with no recovery\n  against them — the agent had permission and "
        "the operator chose not to spend."
    )

    heading(f"RE-SCOPE PLAN — {PASSED} passed, {FAILED} failed")
    print("  NOTHING WAS WRITTEN. No MongoDB write, no Razorpay call.")
    print(f"  events to execute ({len(to_execute)}):")
    for index in range(0, len(to_execute), 8):
        print("    " + " ".join(to_execute[index : index + 8]))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
