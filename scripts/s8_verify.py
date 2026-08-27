"""Stage 8 checkpoint 5 — verification at demo scale.

Runs the 25 link-carrying demo executions through the EXISTING Stage 6 webhook path
(`POST /webhooks/razorpay`), producing one verification record each, and reports
exactly how real each one is.

CREATES NO PAYMENT LINK. This is enforced twice, not asserted once:

1. `razorpay_call()` refuses any POST to the payment-links collection URL — the one
   call shape that creates a link. The only such call in the codebase is
   `app/execution/razorpay.py`, reachable solely through `POST /execute/{event_id}`,
   which this script never calls.
2. The account's link count is read before and after and must be identical. A
   create that somehow slipped past (1) would move that number and fail the run.

THREE TIERS OF REALITY, and the run labels every record with its tier. This matters
more than the counts: a dashboard claiming money came back should not be able to
hide how it learned that.

  * `real_state_simulated_delivery` — the link was genuinely cancelled through
    Razorpay's cancel API. The state change is real and the webhook entity is the
    real post-cancel object, fetched back. Only the DELIVERY is ours, because the
    tunnel Razorpay would deliver to is not running.
  * `simulated` — Razorpay will not move a test link to paid or expired on its own,
    so the real entity is fetched and the payment or expiry fields are overridden.
    Everything else in the payload is Razorpay's own.
  * `genuine` — a link genuinely paid end to end. Attempted, capped, and reported
    honestly whether or not it succeeded. If it did not, every `recovered` outcome
    in this dataset is simulated, and that is stated rather than glossed.

OUTCOME ASSIGNMENT. Not an invented rate. Each event's own stored decision carries
the `recovery_probability` the decision engine used to justify the action; a draw
seeded by event id decides that event's fate against it. Sum of probabilities over
the 25 links is the expected recovery count, and both expected and realised are
printed so the gap is visible.

  Three events are FORCED to recovered regardless of their draw: the `ptp_honored`
  events the seeded draw stranded. `app/models/promise.py` allows
  `promised -> honored` only when a verification with outcome `recovered` exists, so
  without this checkpoint 6 cannot create the ratified four honored promises. This
  is a disclosed override of 3 of 25 outcomes, ratified after being flagged, and it
  raises THIS COHORT's recovery rate to 13/28 rather than the 10/28 the pure draw
  gives. Both are printed, so the delta is visible.

  13/28 = 46.4% is the demo cohort alone and is NOT the reported headline for it.
  Ratified 2026-08-27: the executed-cohort figure reported across the whole dataset
  is 39.53% — 17 of 43 link-producing executions, which excludes contact-type
  interventions that no webhook can ever verify from the denominator. 46.4% may be
  quoted only as "the ratified demo cohort". See docs/data-corrections.md.

Usage:
    .venv/Scripts/python.exe scripts/s8_verify.py --dry-run
    .venv/Scripts/python.exe scripts/s8_verify.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import s8_dataset as ds  # noqa: E402
from app.execution.razorpay import (  # noqa: E402
    PAYMENT_LINKS_URL,
    _redact,
    credentials_from_settings,
)
from app.webhooks.signature import (  # noqa: E402
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    expected_signature,
    webhook_secret,
)

API = "http://127.0.0.1:8123"
RAZORPAY = "https://api.razorpay.com/v1"

#: Genuine cancels, as ratified. Real state changes on Razorpay, drawn from the
#: not-recovered set — a recovered link must not be cancelled.
CANCEL_COUNT = 8

#: The `ptp_honored` events the seeded draw stranded as not-recovered. Forced to
#: recovered so checkpoint 6 can reach the `honored` state at all. Listed explicitly
#: rather than computed, so the override is reviewable as data.
FORCED_RECOVERED = ("demo_002_pay", "demo_019_pay", "demo_150_sub")

#: What the pure seeded draw produces, kept for the delta report.
PURE_DRAW_RECOVERED = 10

TIER_GENUINE = "genuine"
TIER_REAL_STATE = "real_state_simulated_delivery"
TIER_SIMULATED = "simulated"

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


class LinkCreationRefused(AssertionError):
    """Raised if this script ever tries to create a payment link."""


def razorpay_call(
    client: httpx.Client, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    """Every Razorpay call in this script goes through here.

    The guard is structural rather than advisory: creating a payment link is a POST
    to the collection URL, and that shape is refused before the socket is touched.
    Reads (`GET .../{id}`) and cancels (`POST .../{id}/cancel`) do not match it, so
    the checkpoint's real work is unaffected.
    """
    if method.upper() == "POST" and url.rstrip("/") == PAYMENT_LINKS_URL.rstrip("/"):
        raise LinkCreationRefused(
            f"Refusing {method} {url}: that is the payment-link CREATE call. "
            "Checkpoint 5 pays and cancels existing links only, and this account has "
            "4 of 30 lifetime slots left."
        )
    creds = credentials_from_settings()
    return client.request(
        method, url, auth=(creds.key_id, creds.key_secret), timeout=30, **kwargs
    )


def account_link_count(client: httpx.Client) -> int:
    """How many payment links exist on the account. One GET, creates nothing."""
    response = razorpay_call(client, "GET", PAYMENT_LINKS_URL, params={"count": 100})
    response.raise_for_status()
    return len(response.json().get("payment_links", []))


def draw(event_id: str) -> float:
    """A stable pseudo-random number in [0, 1) for one event.

    Seeded by event id so the whole assignment is reproducible from the dataset
    alone — re-running this script cannot quietly produce a different story.
    """
    digest = hashlib.sha256(f"s8_verify:{event_id}".encode("utf-8")).hexdigest()
    return int(digest[:12], 16) / float(16**12)


def webhook_body(*, event: str, entity: dict[str, Any], created_at: int) -> bytes:
    """The exact bytes that get signed AND sent.

    Serialising once is the point: `json.dumps` gives no guarantee of byte-identical
    output across calls, and the signature is over bytes, so signing one
    serialisation and sending another is a silent forgery of your own request.

    `contains` lists only what `payload` actually holds. A real `payment_link.paid`
    also carries `order` and `payment` entities; they are omitted rather than
    invented, and the reconciler reads neither.
    """
    body = {
        "account_id": "acc_test",
        "contains": ["payment_link"],
        "created_at": created_at,
        "entity": "event",
        "event": event,
        "payload": {"payment_link": {"entity": entity}},
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def post_webhook(
    client: httpx.Client, *, body: bytes, razorpay_event_id: str
) -> httpx.Response:
    """Deliver a signed webhook to the running API."""
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: expected_signature(body=body, secret=webhook_secret()),
        EVENT_ID_HEADER: razorpay_event_id,
    }
    return client.post(
        f"{API}/webhooks/razorpay", content=body, headers=headers, timeout=30
    )


def load_state() -> tuple[dict[str, dict], dict[str, dict], dict[str, list]]:
    """Executions with a link, latest decision per event, dataset roles.

    Read with raw pymongo. `.env` is parsed directly rather than through
    `get_settings()` so this stays a check on the system rather than a use of it.
    """
    env = Path(".env").read_text(encoding="utf-8")
    uri = re.search(r"MONGODB_URI=(.+)", env).group(1).strip()
    name = re.search(r"MONGODB_DB_NAME=(.+)", env).group(1).strip()
    db = MongoClient(uri)[name]

    executions = {
        e["event_id"]: e
        for e in db["executions"].find({})
        if str(e.get("event_id", "")).startswith("demo_")
        and e.get("razorpay_payment_link_id")
    }
    decisions: dict[str, dict] = {}
    for d in db["decisions"].find({}):
        eid = d["event_id"]
        if eid.startswith("demo_") and d.get("version", 0) >= decisions.get(
            eid, {}
        ).get("version", 0):
            decisions[eid] = d
    roles = ds.roles(list(ds.generate()))
    return executions, decisions, roles


def assign(
    executions: dict[str, dict], decisions: dict[str, dict]
) -> tuple[list[str], list[str], list[str], dict[str, float]]:
    """Split the 25 links into recovered / cancelled / expired.

    Returns the three id lists plus the probability used per event, so the caller can
    print the arithmetic instead of asserting it.
    """
    probability = {
        eid: decisions[eid]["recovery_probability"] for eid in executions
    }
    drawn = sorted(eid for eid in executions if draw(eid) < probability[eid])
    forced = [eid for eid in FORCED_RECOVERED if eid in executions]
    recovered = sorted(set(drawn) | set(forced))
    remainder = sorted(set(executions) - set(recovered))
    cancelled = remainder[:CANCEL_COUNT]
    expired = remainder[CANCEL_COUNT:]
    return recovered, cancelled, expired, probability


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the assignment and the guarantees, touch nothing",
    )
    parser.add_argument(
        "--pay-attempts",
        type=int,
        default=1,
        help=(
            "how many links to attempt a genuine end-to-end test payment on. Capped "
            "deliberately: the attempt costs no link slot but it is a real request "
            "against a real gateway."
        ),
    )
    args = parser.parse_args()

    executions, decisions, roles = load_state()
    recovered, cancelled, expired, probability = assign(executions, decisions)
    honored = sorted(s["event_id"] for s in roles.get(ds.ROLE_PTP_HONORED, []))
    revenue = {
        eid: decisions[eid]["revenue_at_risk"] for eid in executions
    }

    client = httpx.Client()

    # =====================================================================
    heading("0. THE GUARANTEE — this checkpoint creates no payment link")
    # =====================================================================
    before = account_link_count(client)
    print(f"  payment links on the account now : {before} of 30")
    print(f"  lifetime slots remaining         : {30 - before}")
    try:
        razorpay_call(client, "POST", PAYMENT_LINKS_URL, json={"amount": 100})
    except LinkCreationRefused as exc:
        check(
            "the guard refuses a link-creating call before it reaches the network",
            True,
            f"{type(exc).__name__} raised on POST {PAYMENT_LINKS_URL}",
        )
    else:
        check(
            "the guard refuses a link-creating call before it reaches the network",
            False,
            "THE GUARD DID NOT FIRE — a create was just attempted. Stop.",
        )
        client.close()
        return 1
    print("  the only calls below are GET /payment_links/{id} and "
          "POST /payment_links/{id}/cancel")

    # =====================================================================
    heading("1. OUTCOME ASSIGNMENT — from each decision's own recovery_probability")
    # =====================================================================
    expectation = sum(probability.values())
    print(f"  link-carrying executions        : {len(executions)}")
    print(f"  sum of recovery_probability     : {expectation:.2f}  "
          "(the matrix's expected recovery count)")
    print(f"  seeded draw alone would recover : {PURE_DRAW_RECOVERED}")
    print(f"  recovered after the override    : {len(recovered)}")
    print(f"  cancelled (genuine)             : {len(cancelled)}")
    print(f"  expired (simulated)             : {len(expired)}")
    print()
    print("  THE OVERRIDE, stated plainly. These 3 were drawn not-recovered and are")
    print("  forced to recovered, because app/models/promise.py allows")
    print("  promised -> honored only when a recovered verification exists:")
    for eid in FORCED_RECOVERED:
        print(f"    {eid:<14} p={probability[eid]:.2f}  u={draw(eid):.3f}  "
              f"{revenue[eid]:>10,.2f}   ptp_honored")
    print(f"  This moves THIS COHORT's recovery rate from "
          f"{PURE_DRAW_RECOVERED}/28 = {PURE_DRAW_RECOVERED / 28:.1%} to "
          f"{len(recovered)}/28 = {len(recovered) / 28:.1%}.")
    print("  That is the demo cohort only, and it is NOT the reported headline.")
    print("  Ratified 2026-08-27: the executed-cohort figure reported across the")
    print("  whole dataset is 39.53% (17 of 43 link-producing executions). 46.4%")
    print("  may be quoted only as 'the ratified demo cohort'. The ~35% figure from")
    print("  checkpoint 0 is superseded by both and should not be quoted at all.")

    check(
        "every ptp_honored event with a link is assigned recovered",
        all(eid in recovered for eid in honored if eid in executions),
        f"{[e for e in honored if e in executions]} — checkpoint 6 can reach honored "
        "for each"
        if all(eid in recovered for eid in honored if eid in executions)
        else f"stranded: {[e for e in honored if e in executions and e not in recovered]}",
    )
    check(
        "no link is both recovered and cancelled",
        not (set(recovered) & set(cancelled)),
        "the three sets are disjoint, so no genuinely cancelled link is also "
        "reported as paid",
    )
    check(
        "the three outcome sets cover every link exactly once",
        sorted(set(recovered) | set(cancelled) | set(expired)) == sorted(executions)
        and len(recovered) + len(cancelled) + len(expired) == len(executions),
        f"{len(recovered)} + {len(cancelled)} + {len(expired)} = {len(executions)}",
    )
    check(
        "the genuine-cancel count is the ratified 8",
        len(cancelled) == CANCEL_COUNT,
        f"{len(cancelled)} links will be cancelled through Razorpay's cancel API",
    )

    print()
    print(f"  {'event':<14} {'outcome':<10} {'p':>5} {'u':>6} {'money':>11}  tier")
    for eid in sorted(executions):
        if eid in recovered:
            outcome, tier = "recovered", TIER_SIMULATED
        elif eid in cancelled:
            outcome, tier = "cancelled", TIER_REAL_STATE
        else:
            outcome, tier = "expired", TIER_SIMULATED
        forced = "  <- forced" if eid in FORCED_RECOVERED else ""
        print(f"  {eid:<14} {outcome:<10} {probability[eid]:>5.2f} {draw(eid):>6.3f} "
              f"{revenue[eid]:>11,.2f}  {tier}{forced}")

    if args.dry_run:
        heading(f"DRY RUN — {PASSED} passed, {FAILED} failed")
        print("  Nothing was cancelled, no webhook was delivered, no link was created.")
        client.close()
        return 1 if FAILED else 0

    # =====================================================================
    heading(f"2. GENUINE PAYMENT — attempted on {args.pay_attempts} link, capped")
    # =====================================================================
    genuinely_paid: set[str] = set()
    for eid in sorted(recovered)[: max(0, args.pay_attempts)]:
        link_id = executions[eid]["razorpay_payment_link_id"]
        link = razorpay_call(client, "GET", f"{RAZORPAY}/payment_links/{link_id}").json()
        short_url = link.get("short_url")
        print(f"  {eid} -> {link_id}, status={link.get('status')!r}")
        print(f"  hosted checkout: {short_url}")
        page = client.get(str(short_url), follow_redirects=True, timeout=30)
        print(f"  GET short_url -> HTTP {page.status_code}, "
              f"content-type={page.headers.get('content-type', '?')!r}, "
              f"{len(page.content):,} bytes")
        after_attempt = razorpay_call(
            client, "GET", f"{RAZORPAY}/payment_links/{link_id}"
        ).json()
        if after_attempt.get("status") == "paid":
            genuinely_paid.add(eid)
        print(f"  link status after the attempt: {after_attempt.get('status')!r}")
    check(
        "the genuine-payment result is reported as measured, not assumed",
        True,
        f"{len(genuinely_paid)} of {args.pay_attempts} attempted links reached "
        f"status='paid' on Razorpay"
        if genuinely_paid
        else "0 links reached status='paid'. Razorpay exposes no server-side API to "
        "pay a payment link; the hosted checkout is a browser page requiring "
        "interactive card entry. EVERY 'recovered' OUTCOME BELOW IS SIMULATED.",
    )

    # =====================================================================
    heading(f"3. GENUINE CANCELS — {len(cancelled)} real state changes on Razorpay")
    # =====================================================================
    delivered: dict[str, dict] = {}
    for eid in cancelled:
        link_id = executions[eid]["razorpay_payment_link_id"]
        cancel = razorpay_call(
            client, "POST", f"{RAZORPAY}/payment_links/{link_id}/cancel"
        )
        if cancel.status_code != 200:
            check(
                f"cancel {eid}",
                False,
                f"HTTP {cancel.status_code} "
                f"{_redact(cancel.text, credentials_from_settings())[:180]}",
            )
            continue
        entity = cancel.json()
        # The entity is Razorpay's own post-cancel object. Nothing is overridden.
        body = webhook_body(
            event="payment_link.cancelled",
            entity=entity,
            created_at=int(entity.get("cancelled_at") or entity["created_at"]),
        )
        response = post_webhook(
            client, body=body, razorpay_event_id=f"evt_s8cancel_{eid}"
        )
        delivered[eid] = {
            "http": response.status_code,
            "ack": response.json() if response.status_code == 200 else response.text,
            "tier": TIER_REAL_STATE,
            "status": entity.get("status"),
        }
        print(f"  {eid:<14} cancelled on Razorpay (status={entity.get('status')!r}) "
              f"-> webhook HTTP {response.status_code}")
    check(
        "every genuine cancel returned status='cancelled' from Razorpay",
        all(d["status"] == "cancelled" for d in delivered.values()),
        f"{len(delivered)} links, all reporting cancelled — the state change is real, "
        "only the delivery is ours",
    )

    # =====================================================================
    heading(f"4. SIMULATED PAID — {len(recovered)} links, real entity, "
            "payment fields overridden")
    # =====================================================================
    for eid in recovered:
        link_id = executions[eid]["razorpay_payment_link_id"]
        entity = razorpay_call(
            client, "GET", f"{RAZORPAY}/payment_links/{link_id}"
        ).json()
        tier = TIER_GENUINE if eid in genuinely_paid else TIER_SIMULATED
        if tier is TIER_SIMULATED:
            # Two fields, both named here so the override is auditable. amount_paid is
            # set to the link's own amount: a full payment, so the reconciler's
            # mismatch check compares like with like instead of flagging a shortfall
            # this script invented.
            entity = {**entity, "status": "paid", "amount_paid": int(entity["amount"])}
        body = webhook_body(
            event="payment_link.paid",
            entity=entity,
            created_at=int(entity["created_at"]),
        )
        response = post_webhook(client, body=body, razorpay_event_id=f"evt_s8paid_{eid}")
        delivered[eid] = {
            "http": response.status_code,
            "ack": response.json() if response.status_code == 200 else response.text,
            "tier": tier,
            "status": entity.get("status"),
        }
    print(f"  {len(recovered)} paid webhooks delivered, "
          f"{sum(1 for e in recovered if delivered[e]['http'] == 200)} answered 200")

    # =====================================================================
    heading(f"5. SIMULATED EXPIRED — {len(expired)} links")
    # =====================================================================
    stamp = int(time.time())
    for eid in expired:
        link_id = executions[eid]["razorpay_payment_link_id"]
        entity = razorpay_call(
            client, "GET", f"{RAZORPAY}/payment_links/{link_id}"
        ).json()
        entity = {**entity, "status": "expired", "expired_at": stamp}
        body = webhook_body(
            event="payment_link.expired", entity=entity, created_at=stamp
        )
        response = post_webhook(
            client, body=body, razorpay_event_id=f"evt_s8expired_{eid}"
        )
        delivered[eid] = {
            "http": response.status_code,
            "ack": response.json() if response.status_code == 200 else response.text,
            "tier": TIER_SIMULATED,
            "status": "expired",
        }
    print(f"  {len(expired)} expired webhooks delivered, "
          f"{sum(1 for e in expired if delivered[e]['http'] == 200)} answered 200")

    check(
        "every webhook was accepted as authentic",
        all(d["http"] == 200 for d in delivered.values()),
        f"{len(delivered)} of {len(executions)} answered 200 — the signature was "
        "computed from .env and the server verified it with the secret it booted "
        "with, so both hold the same value"
        if all(d["http"] == 200 for d in delivered.values())
        else f"non-200: {[(e, d['http']) for e, d in delivered.items() if d['http'] != 200]}",
    )
    check(
        "every accepted webhook was actually processed into a record",
        all(
            isinstance(d["ack"], dict) and d["ack"].get("processed") is True
            for d in delivered.values()
        ),
        "processed=true on all of them; a 200 alone would also cover a duplicate or "
        "an unmatched link, so this reads the field rather than the status code",
    )

    # =====================================================================
    heading("6. IDEMPOTENCY — a replayed delivery must not double-count")
    # =====================================================================
    replay_id = sorted(expired or recovered)[0]
    link_id = executions[replay_id]["razorpay_payment_link_id"]
    entity = razorpay_call(client, "GET", f"{RAZORPAY}/payment_links/{link_id}").json()
    if replay_id in expired:
        entity = {**entity, "status": "expired", "expired_at": stamp}
        body = webhook_body(
            event="payment_link.expired", entity=entity, created_at=stamp
        )
        event_id = f"evt_s8expired_{replay_id}"
    else:
        entity = {**entity, "status": "paid", "amount_paid": int(entity["amount"])}
        body = webhook_body(
            event="payment_link.paid", entity=entity, created_at=int(entity["created_at"])
        )
        event_id = f"evt_s8paid_{replay_id}"
    replay = post_webhook(client, body=body, razorpay_event_id=event_id)
    ack = replay.json()
    check(
        "replaying the same razorpay_event_id is acknowledged but not reprocessed",
        replay.status_code == 200 and ack.get("processed") is False,
        f"{replay_id}: HTTP {replay.status_code}, processed={ack.get('processed')}, "
        f"detail={ack.get('detail')!r}",
    )

    # =====================================================================
    heading("7. READ BACK — from the API, not from what this script believes")
    # =====================================================================
    stored = client.get(
        f"{API}/verifications", params={"history": True}, timeout=30
    ).json()
    demo = [v for v in stored if v["event_id"] in executions]
    by_outcome = Counter(v["outcome"] for v in demo)
    check(
        "one verification record exists per link-carrying execution",
        len({v["event_id"] for v in demo}) == len(executions),
        f"{len({v['event_id'] for v in demo})} distinct events across {len(demo)} "
        f"records for {len(executions)} executions",
    )
    check(
        "the stored outcomes match the assignment exactly",
        by_outcome.get("recovered", 0) == len(recovered)
        and by_outcome.get("cancelled", 0) == len(cancelled)
        and by_outcome.get("expired", 0) == len(expired),
        f"stored {dict(by_outcome)} against assigned "
        f"recovered={len(recovered)} cancelled={len(cancelled)} "
        f"expired={len(expired)}",
    )
    mismatches = [v for v in demo if v.get("amount_mismatch")]
    check(
        "no verification reports an amount mismatch",
        not mismatches,
        "every paid webhook carried amount_paid equal to the link's own amount, so "
        "the reconciler's comparison against the decision's revenue_at_risk agrees"
        if not mismatches
        else f"{[(v['event_id'], v['amount_recovered'], v['amount_expected']) for v in mismatches]}",
    )
    money = sum(v["amount_recovered"] for v in demo)
    expected_money = sum(revenue[eid] for eid in recovered)
    check(
        "recovered money equals the sum of the recovered events' revenue_at_risk",
        abs(money - expected_money) < 0.01,
        f"{money:,.2f} stored against {expected_money:,.2f} expected",
    )

    # =====================================================================
    heading("8. THE GUARANTEE, RE-MEASURED")
    # =====================================================================
    after = account_link_count(client)
    print(f"  payment links before this run : {before}")
    print(f"  payment links after this run  : {after}")
    check(
        "this checkpoint created no payment link",
        after == before,
        f"{after} == {before}, measured against the account rather than assumed. "
        f"{30 - after} lifetime slots remain",
    )

    # =====================================================================
    heading("9. WHAT IS REAL, AND WHAT IS NOT")
    # =====================================================================
    tiers = Counter(d["tier"] for d in delivered.values())
    for tier, n in sorted(tiers.items()):
        print(f"    {tier:<32} {n:>3}")
    print()
    print(f"  money reported recovered : {money:,.2f}")
    print(f"  of which genuinely paid  : "
          f"{sum(revenue[e] for e in genuinely_paid):,.2f}")
    print(f"  of which simulated       : "
          f"{money - sum(revenue[e] for e in genuinely_paid):,.2f}")
    print()
    print("  The 8 cancellations are real state changes on Razorpay. Their webhook")
    print("  delivery is ours because the tunnel Razorpay would post to is not")
    print("  running; if it is restarted, Razorpay's queued retries may add a second")
    print("  record per cancelled link — the same outcome, a duplicate row.")

    heading(f"CHECKPOINT 5 — {PASSED} passed, {FAILED} failed")
    client.close()
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
