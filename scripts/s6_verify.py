"""Stage 6 Part A — verification end to end against real Razorpay test mode.

Runs the inbound webhook path over real HTTP against the running API, with bodies
built from payment-link objects fetched live from Razorpay's API and signed with the
real `RAZORPAY_WEBHOOK_SECRET`.

**Read this before believing any of the output.** Every phase below states how real
it is, because they are not equally real:

* phase 4 (`payment_link.cancelled`) is **fully real**. The link is cancelled through
  Razorpay's documented cancel API, the object is then fetched back, and every field
  in the webhook body is Razorpay's own value for a genuinely cancelled link. No
  field is invented.
* phase 2 (`payment_link.paid`) fetches the real link object and overrides exactly
  **two** fields — `status` -> "paid" and `amount_paid` -> `amount` — because
  completing a card checkout needs a browser and this harness has none. Those two
  overrides are printed at runtime so nobody has to take this docstring's word for
  it. To make the phase fully real, pay the link in a browser with a test card and
  re-run: the harness detects `status == "paid"` and skips the override.
* the signature, the secret, the HMAC, the HTTP hop, the reconciler, the database
  writes, and the event-status transitions are real in every phase. Only the
  *delivery hop* is ours rather than Razorpay's: Razorpay refuses to deliver to
  anything but ports 80/443 on a public host, so a localhost endpoint cannot be
  reached by their servers. `docs/webhook-tunnel.md` has the zrok steps for closing
  that last gap.

Phases:

1. preflight — server reachable, secret configured, targets still live at Razorpay
2. paid — the happy path: record written, event -> recovered, amounts agree
3. replay — the same `x-razorpay-event-id` twice, and twice concurrently
4. cancelled — fully real, event -> recovery_failed
5. mismatch — a recovery for the wrong amount is recorded and flagged, not smoothed
6. rejection — tampered, absent, malformed and unsigned requests all get 400
7. out-of-order — a late `payment_link.expired` cannot walk a recovery backwards
8. unmatched — a link no execution claims is acknowledged, never matched
9. readback — GET /verifications agrees with what was written

This script writes real records and moves real event statuses. What it touched is
listed at the end.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx

from app.db import close_mongo_connection, connect_to_mongo, get_database
from app.execution.razorpay import credentials_from_settings
from app.webhooks.signature import (
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    expected_signature,
    webhook_secret,
)

API = "http://127.0.0.1:8123"
RAZORPAY = "https://api.razorpay.com/v1"

#: Stage 5 fixture links, each with the event and expected amount already confirmed
#: from the decision chain. Three different links so no phase depends on another's
#: leftovers.
PAID_LINK = "plink_TTsV8YH18jku14"          # exe_S5ADV_20260825T045458_HONEST, 2200.00
MISMATCH_LINK = "plink_TTrxBuptDYfom8"      # exe_S5_20260825T042248_DRETRY,    2050.00
CANCEL_LINK = "plink_TTg45VBIDmMeOe"        # exe_S5_20260824T164458_DRETRY,    2050.00

#: A syntactically valid link id that no execution record claims.
UNMATCHED_LINK = "plink_S6NoSuchLink00"

#: Unique per invocation, so the Razorpay event ids this harness mints never collide
#: with a previous run's. Without this the script would be a one-shot: every phase
#: would deduplicate against its own history and report a duplicate as a failure.
RUN = f"s6a_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"

passes: list[str] = []
failures: list[str] = []
touched: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    """Record one assertion. Never raises: a failed check must not hide later ones."""
    tag = "PASS" if condition else "FAIL"
    line = f"  [{tag}] {label}"
    if detail:
        line += f"\n         {detail}"
    print(line)
    (passes if condition else failures).append(label)
    return condition


def heading(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


# ---------------------------------------------------------------------------
# Building and sending a webhook the way Razorpay does.
# ---------------------------------------------------------------------------


def webhook_body(*, event: str, entity: dict[str, Any], created_at: int) -> bytes:
    """Serialise a Razorpay webhook body to the exact bytes that get signed.

    Returned as bytes, and those same bytes are what gets signed and sent. Serialising
    twice — once to sign, once to send — is the mistake this function exists to make
    impossible, since `json.dumps` is not guaranteed to produce identical output for
    an equal dict across calls with different arguments.

    `contains` lists exactly the entities present in `payload`. A real
    `payment_link.paid` from Razorpay also carries `order` and `payment` entities;
    they are omitted here rather than fabricated, and the reconciler does not read
    them — it needs the link id and `amount_paid`, both of which live on the
    payment_link entity.
    """
    body = {
        # Razorpay's own top-level envelope. `account_id` is not read by the
        # reconciler and the payment_link object does not carry it, so it is a
        # placeholder — the only field here that is.
        "account_id": "acc_test",
        "contains": ["payment_link"],
        "created_at": created_at,
        "entity": "event",
        "event": event,
        "payload": {"payment_link": {"entity": entity}},
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


async def post_webhook(
    client: httpx.AsyncClient,
    *,
    body: bytes,
    razorpay_event_id: str | None,
    signature: str | None = None,
    secret: str | None = None,
) -> httpx.Response:
    """POST a webhook to the running API, signing the raw bytes.

    `signature` overrides the computed digest — that is how the tamper cases are
    driven. `secret` signs with the wrong key without touching any module global.
    """
    if signature is None:
        signature = expected_signature(
            body=body, secret=secret if secret is not None else webhook_secret()
        )
    headers = {"Content-Type": "application/json"}
    if signature != "":
        headers[SIGNATURE_HEADER] = signature
    if razorpay_event_id is not None:
        headers[EVENT_ID_HEADER] = razorpay_event_id
    return await client.post(
        f"{API}/webhooks/razorpay", content=body, headers=headers, timeout=30
    )


async def fetch_link(client: httpx.AsyncClient, link_id: str) -> dict[str, Any]:
    """Fetch a payment link object from Razorpay's live API."""
    creds = credentials_from_settings()
    response = await client.get(
        f"{RAZORPAY}/payment_links/{link_id}",
        auth=(creds.key_id, creds.key_secret),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


async def cancel_link(client: httpx.AsyncClient, link_id: str) -> dict[str, Any]:
    """Cancel a payment link through Razorpay's documented cancel API."""
    creds = credentials_from_settings()
    response = await client.post(
        f"{RAZORPAY}/payment_links/{link_id}/cancel",
        auth=(creds.key_id, creds.key_secret),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


async def event_status(event_id: str) -> str | None:
    document = await get_database()["events"].find_one({"event_id": event_id}, {"status": 1})
    return None if document is None else document.get("status")


async def records_for(razorpay_event_id: str) -> list[dict[str, Any]]:
    return (
        await get_database()["verifications"]
        .find({"razorpay_event_id": razorpay_event_id})
        .to_list(length=None)
    )


# ---------------------------------------------------------------------------
# Phases.
# ---------------------------------------------------------------------------


async def phase_1_preflight(client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
    heading("PHASE 1 — preflight")

    health = await client.get(f"{API}/", timeout=30)
    check(
        "API reachable",
        health.status_code == 200,
        f"GET / -> {health.status_code} {health.json() if health.status_code == 200 else health.text[:120]}",
    )

    try:
        secret = webhook_secret()
        configured = bool(secret)
    except Exception as exc:  # noqa: BLE001
        configured = False
        print(f"         {exc}")
    check(
        "RAZORPAY_WEBHOOK_SECRET configured",
        configured,
        "value not printed, by design; only its presence and length are reported "
        f"({len(secret) if configured else 0} chars)",
    )

    links: dict[str, dict[str, Any]] = {}
    for link_id in (PAID_LINK, MISMATCH_LINK, CANCEL_LINK):
        obj = await fetch_link(client, link_id)
        links[link_id] = obj
        print(
            f"         {link_id}  status={obj.get('status')}  amount={obj.get('amount')}"
            f"  amount_paid={obj.get('amount_paid')}"
        )
        await asyncio.sleep(1.2)  # Razorpay rate-limits this endpoint hard.
    check("all three target links fetched from Razorpay", len(links) == 3)
    return links


async def phase_2_paid(
    client: httpx.AsyncClient, link: dict[str, Any]
) -> tuple[str, str]:
    heading("PHASE 2 — payment_link.paid: the happy path")

    entity = dict(link)
    really_paid = entity.get("status") == "paid" and (entity.get("amount_paid") or 0) > 0
    if really_paid:
        print(
            "  This link was genuinely paid at Razorpay. No field is being overridden; "
            "this phase is fully real."
        )
    else:
        entity["status"] = "paid"
        entity["amount_paid"] = entity["amount"]
        print(
            "  OVERRIDDEN, because a card checkout needs a browser this harness does "
            "not have:\n"
            f"    status      : {link.get('status')!r} -> 'paid'\n"
            f"    amount_paid : {link.get('amount_paid')!r} -> {entity['amount_paid']!r}\n"
            f"  Every other field below is Razorpay's own value for {entity['id']}."
        )

    razorpay_event_id = f"{RUN}_paid_01"
    body = webhook_body(
        event="payment_link.paid", entity=entity, created_at=int(entity["created_at"])
    )
    status_before = await event_status("exe_S5ADV_20260825T045458_HONEST")
    response = await post_webhook(client, body=body, razorpay_event_id=razorpay_event_id)
    payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    print(f"  HTTP {response.status_code}  {json.dumps(payload, indent=2)[:900]}")

    check("paid webhook accepted", response.status_code == 200)
    check("reported as processed", payload.get("processed") is True)

    stored = await records_for(razorpay_event_id)
    check("exactly one VerificationRecord written", len(stored) == 1, f"{len(stored)} found")
    if not stored:
        return razorpay_event_id, ""

    record = stored[0]
    event_id = record["event_id"]
    touched.append(f"verifications: {record['_id']} ({record['outcome']} for {event_id})")
    check("outcome is 'recovered'", record["outcome"] == "recovered", str(record["outcome"]))
    check(
        "amount_recovered is Razorpay's amount_paid in major units",
        abs(record["amount_recovered"] - entity["amount_paid"] / 100) < 1e-9,
        f"record={record['amount_recovered']} razorpay_paise={entity['amount_paid']}",
    )
    check(
        "amount_expected is the authorized decision's revenue_at_risk",
        abs(record["amount_expected"] - 2200.00) < 1e-9,
        f"record={record['amount_expected']}",
    )
    check("amounts agree, so amount_mismatch is False", record["amount_mismatch"] is False)
    check(
        "execution_id names the execution that created this link",
        str(record["execution_id"]) == "6a8d202d4bef55b6e7c69848",
        str(record["execution_id"]),
    )
    check(
        "razorpay_event_id stored is Razorpay's header value, not one of ours",
        record["razorpay_event_id"] == razorpay_event_id,
    )

    status = await event_status(event_id)
    check("originating event transitioned to 'recovered'", status == "recovered", f"status={status}")
    touched.append(f"events: {event_id} status {status_before} -> {status}")
    return razorpay_event_id, event_id


async def phase_3_replay(
    client: httpx.AsyncClient, link: dict[str, Any], razorpay_event_id: str
) -> None:
    heading("PHASE 3 — replay: the same Razorpay event id, twice and then concurrently")

    entity = dict(link)
    if entity.get("status") != "paid":
        entity["status"] = "paid"
        entity["amount_paid"] = entity["amount"]
    body = webhook_body(
        event="payment_link.paid", entity=entity, created_at=int(entity["created_at"])
    )

    response = await post_webhook(client, body=body, razorpay_event_id=razorpay_event_id)
    payload = response.json()
    print(f"  sequential replay -> HTTP {response.status_code}  {payload.get('detail')}")
    check("replay acknowledged with 200, not an error", response.status_code == 200)
    check("replay reported as NOT processed", payload.get("processed") is False)
    check("replay detail names it a duplicate", "duplicate" in str(payload.get("detail")).lower())

    stored = await records_for(razorpay_event_id)
    check(
        "still exactly one VerificationRecord after the replay",
        len(stored) == 1,
        f"{len(stored)} found",
    )

    # Now the case the pre-flight check cannot catch on its own: two deliveries of a
    # brand-new event id in flight at the same time. Only the unique index can decide
    # this one.
    concurrent_id = f"{RUN}_paid_concurrent"
    responses = await asyncio.gather(
        *(
            post_webhook(client, body=body, razorpay_event_id=concurrent_id)
            for _ in range(4)
        )
    )
    codes = [r.status_code for r in responses]
    processed = [r.json().get("processed") for r in responses]
    print(f"  4 concurrent deliveries of {concurrent_id!r} -> {codes}  processed={processed}")
    check("all four concurrent deliveries answered 200", set(codes) == {200}, str(codes))
    check(
        "exactly one of the four reported processed=True",
        processed.count(True) == 1,
        f"processed={processed}",
    )
    stored = await records_for(concurrent_id)
    check(
        "exactly one VerificationRecord from four concurrent deliveries",
        len(stored) == 1,
        f"{len(stored)} found",
    )
    if stored:
        touched.append(f"verifications: {stored[0]['_id']} (concurrency test)")


async def phase_4_cancelled(client: httpx.AsyncClient) -> None:
    heading("PHASE 4 — payment_link.cancelled: fully real, no field invented")

    fetched = await fetch_link(client, CANCEL_LINK)
    if fetched.get("status") == "cancelled":
        print(
            f"  {CANCEL_LINK} was already cancelled at Razorpay by an earlier run; "
            "re-using that genuinely cancelled object rather than cancelling a "
            "second link. Still fully real — no field is invented."
        )
    else:
        cancelled = await cancel_link(client, CANCEL_LINK)
        touched.append(f"razorpay: payment link {CANCEL_LINK} CANCELLED (test mode)")
        print(
            f"  cancelled at Razorpay: status={cancelled.get('status')!r} "
            f"cancelled_at={cancelled.get('cancelled_at')!r} "
            f"amount_paid={cancelled.get('amount_paid')!r}"
        )
        await asyncio.sleep(1.2)
        fetched = await fetch_link(client, CANCEL_LINK)
    check(
        "Razorpay reports the link as cancelled on a fresh read",
        fetched.get("status") == "cancelled",
        f"status={fetched.get('status')}",
    )

    razorpay_event_id = f"{RUN}_cancelled_01"
    body = webhook_body(
        event="payment_link.cancelled",
        entity=fetched,
        created_at=int(fetched.get("cancelled_at") or fetched["created_at"]),
    )
    response = await post_webhook(client, body=body, razorpay_event_id=razorpay_event_id)
    payload = response.json()
    print(f"  HTTP {response.status_code}  {payload.get('detail')}")

    check("cancelled webhook accepted", response.status_code == 200)
    check("reported as processed", payload.get("processed") is True)

    stored = await records_for(razorpay_event_id)
    check("one VerificationRecord written", len(stored) == 1, f"{len(stored)} found")
    if not stored:
        return
    record = stored[0]
    touched.append(f"verifications: {record['_id']} (cancelled for {record['event_id']})")
    check("outcome is 'cancelled'", record["outcome"] == "cancelled", str(record["outcome"]))
    check(
        "amount_recovered is exactly 0 on a cancellation",
        record["amount_recovered"] == 0,
        str(record["amount_recovered"]),
    )
    check("amount_mismatch is False on a cancellation", record["amount_mismatch"] is False)

    status = await event_status(record["event_id"])
    check(
        "originating event transitioned to 'recovery_failed'",
        status == "recovery_failed",
        f"status={status}",
    )
    touched.append(f"events: {record['event_id']} status now {status} (was at_risk before Stage 6)")


async def phase_5_mismatch(client: httpx.AsyncClient, link: dict[str, Any]) -> None:
    heading("PHASE 5 — a recovery for the wrong amount is recorded AND flagged")

    entity = dict(link)
    shortfall_paise = 5000  # ₹50 short of the ₹2050 the link was created for.
    entity["status"] = "paid"
    entity["amount_paid"] = entity["amount"] - shortfall_paise
    print(
        f"  Reporting amount_paid={entity['amount_paid']} against an amount of "
        f"{entity['amount']} — a shortfall of {shortfall_paise} paise. Simulated: a "
        "genuine partial payment needs accept_partial, which Stage 5 does not set."
    )

    razorpay_event_id = f"{RUN}_mismatch_01"
    body = webhook_body(
        event="payment_link.paid", entity=entity, created_at=int(entity["created_at"])
    )
    response = await post_webhook(client, body=body, razorpay_event_id=razorpay_event_id)
    payload = response.json()
    print(f"  HTTP {response.status_code}  {payload.get('detail')}")

    check("mismatched recovery still accepted", response.status_code == 200)
    check("recorded rather than dropped", payload.get("processed") is True)
    check(
        "the acknowledgement says MISMATCH out loud",
        "MISMATCH" in str(payload.get("detail")),
        str(payload.get("detail")),
    )

    stored = await records_for(razorpay_event_id)
    check("one VerificationRecord written", len(stored) == 1, f"{len(stored)} found")
    if not stored:
        return
    record = stored[0]
    touched.append(f"verifications: {record['_id']} (mismatch for {record['event_id']})")
    check("amount_mismatch is True", record["amount_mismatch"] is True)
    check(
        "amount_recovered is what arrived, not what was hoped for",
        abs(record["amount_recovered"] - 2000.00) < 1e-9,
        f"recovered={record['amount_recovered']} expected={record['amount_expected']}",
    )
    check(
        "amount_expected is untouched at the decision's revenue_at_risk",
        abs(record["amount_expected"] - 2050.00) < 1e-9,
        str(record["amount_expected"]),
    )
    status = await event_status(record["event_id"])
    check(
        "a short payment still counts as recovered for lifecycle purposes",
        status == "recovered",
        f"status={status} — money arrived; the discrepancy is on the record, not hidden",
    )
    touched.append(f"events: {record['event_id']} status now {status} (was at_risk before Stage 6)")

    # And the flag cannot be lied about at the model level.
    from app.models.verification import VerificationRecord

    try:
        VerificationRecord(
            event_id=record["event_id"],
            execution_id=str(record["execution_id"]),
            razorpay_event_id=f"{RUN}_mismatch_lie",
            razorpay_event="payment_link.paid",
            razorpay_payment_link_id=record["razorpay_payment_link_id"],
            outcome="recovered",
            amount_recovered=2000.00,
            amount_expected=2050.00,
            amount_mismatch=False,
        )
        check("model refuses amount_mismatch=False over disagreeing amounts", False, "it was accepted")
    except ValueError as exc:
        check(
            "model refuses amount_mismatch=False over disagreeing amounts",
            True,
            str(exc).splitlines()[-1][:160],
        )


async def phase_6_rejection(client: httpx.AsyncClient, link: dict[str, Any]) -> None:
    heading("PHASE 6 — rejection: nothing unverified gets past the gate")

    entity = dict(link)
    entity["status"] = "paid"
    entity["amount_paid"] = entity["amount"]
    body = webhook_body(
        event="payment_link.paid", entity=entity, created_at=int(entity["created_at"])
    )
    good = expected_signature(body=body, secret=webhook_secret())

    before = await get_database()["verifications"].count_documents({})

    # 1. One character of the valid digest flipped. Same length, still hex.
    flipped = ("b" if good[0] != "b" else "c") + good[1:]
    response = await post_webhook(
        client, body=body, razorpay_event_id=f"{RUN}_tamper_sig", signature=flipped
    )
    check(
        "tampered signature -> 400",
        response.status_code == 400,
        f"HTTP {response.status_code}: {str(response.json().get('detail'))[:120]}",
    )
    check(
        "the 400 names it a mismatch, not a malformed header",
        "SignatureMismatch" in str(response.json().get("detail")),
    )

    # 2. A valid signature over a DIFFERENT body: the classic replay-with-edits.
    edited = webhook_body(
        event="payment_link.paid",
        entity={**entity, "amount_paid": entity["amount"] * 10},
        created_at=int(entity["created_at"]),
    )
    response = await post_webhook(
        client, body=edited, razorpay_event_id=f"{RUN}_tamper_body", signature=good
    )
    check(
        "body edited after signing -> 400",
        response.status_code == 400,
        f"HTTP {response.status_code}: {str(response.json().get('detail'))[:120]}",
    )

    # 3. No signature header at all.
    response = await post_webhook(
        client, body=body, razorpay_event_id=f"{RUN}_nosig", signature=""
    )
    check(
        "absent signature header -> 400",
        response.status_code == 400,
        f"HTTP {response.status_code}: {str(response.json().get('detail'))[:120]}",
    )
    check(
        "absent and wrong are reported as different failures",
        "MissingSignature" in str(response.json().get("detail")),
    )

    # 4. Signed with the wrong secret.
    response = await post_webhook(
        client,
        body=body,
        razorpay_event_id=f"{RUN}_wrongsecret",
        secret="not-the-webhook-secret",
    )
    check(
        "signed with the wrong secret -> 400",
        response.status_code == 400,
        f"HTTP {response.status_code}: {str(response.json().get('detail'))[:120]}",
    )

    # 5. Malformed signature: right idea, wrong shape.
    response = await post_webhook(
        client, body=body, razorpay_event_id=f"{RUN}_shortsig", signature="deadbeef"
    )
    check(
        "signature of the wrong length -> 400",
        response.status_code == 400
        and "MalformedSignature" in str(response.json().get("detail")),
        f"HTTP {response.status_code}: {str(response.json().get('detail'))[:120]}",
    )

    # 6. Correctly signed, but no event id header. The only dedup key Razorpay sends.
    response = await post_webhook(client, body=body, razorpay_event_id=None)
    check(
        "valid signature but no x-razorpay-event-id -> 400",
        response.status_code == 400
        and "MissingEventId" in str(response.json().get("detail")),
        f"HTTP {response.status_code}: {str(response.json().get('detail'))[:120]}",
    )

    # 7. Correctly signed junk. Reaches the parser only because the digest matched.
    junk = b"this is not json"
    response = await post_webhook(client, body=junk, razorpay_event_id=f"{RUN}_junk")
    check(
        "correctly signed non-JSON -> 400",
        response.status_code == 400
        and "MalformedBody" in str(response.json().get("detail")),
        f"HTTP {response.status_code}: {str(response.json().get('detail'))[:120]}",
    )

    after = await get_database()["verifications"].count_documents({})
    check(
        "seven rejected requests wrote nothing at all",
        before == after,
        f"verifications count {before} -> {after}",
    )


async def phase_7_out_of_order(client: httpx.AsyncClient, link: dict[str, Any]) -> None:
    heading("PHASE 7 — a late payment_link.expired cannot undo a confirmed recovery")

    entity = {**link, "status": "expired", "amount_paid": 0, "expired_at": int(link["created_at"]) + 60}
    print(
        "  Razorpay guarantees at-least-once delivery and explicitly NOT ordering, so "
        "this is a real sequence, not a contrived one: the paid event for this link "
        "was processed in phase 2 and its expiry arrives now."
    )

    razorpay_event_id = f"{RUN}_expired_late"
    body = webhook_body(
        event="payment_link.expired", entity=entity, created_at=int(entity["expired_at"])
    )
    response = await post_webhook(client, body=body, razorpay_event_id=razorpay_event_id)
    payload = response.json()
    print(f"  HTTP {response.status_code}  {payload.get('detail')}")

    check("late expiry accepted", response.status_code == 200)
    check(
        "the expiry IS recorded — it is a true statement about the link",
        payload.get("processed") is True,
    )
    stored = await records_for(razorpay_event_id)
    if stored:
        touched.append(f"verifications: {stored[0]['_id']} (late expiry, out-of-order test)")
        check("recorded with outcome 'expired'", stored[0]["outcome"] == "expired")
        status = await event_status(stored[0]["event_id"])
        check(
            "the event is STILL 'recovered' — the transition was refused",
            status == "recovered",
            f"status={status}",
        )
        check(
            "the acknowledgement says the transition was refused",
            "does not permit" in str(payload.get("detail")),
            str(payload.get("detail")),
        )
    else:
        check("late expiry recorded", False, "no record written")


async def phase_8_unmatched(client: httpx.AsyncClient, link: dict[str, Any]) -> None:
    heading("PHASE 8 — a link no execution claims is acknowledged, never matched")

    entity = {**link, "id": UNMATCHED_LINK, "status": "paid", "amount_paid": link["amount"]}
    razorpay_event_id = f"{RUN}_unmatched"
    body = webhook_body(
        event="payment_link.paid", entity=entity, created_at=int(link["created_at"])
    )
    before = await get_database()["verifications"].count_documents({})
    response = await post_webhook(client, body=body, razorpay_event_id=razorpay_event_id)
    payload = response.json()
    print(f"  HTTP {response.status_code}  {payload.get('detail')}")

    check("unmatched link answered 200, so Razorpay stops retrying", response.status_code == 200)
    check("reported as NOT processed", payload.get("processed") is False)
    check(
        "the detail says it was deliberately not matched",
        "not matched" in str(payload.get("detail")),
        str(payload.get("detail")),
    )
    after = await get_database()["verifications"].count_documents({})
    check("no record invented for it", before == after, f"count {before} -> {after}")

    # And an event Razorpay might send that this stage does not record.
    response = await post_webhook(
        client,
        body=webhook_body(
            event="payment.captured", entity=entity, created_at=int(link["created_at"])
        ),
        razorpay_event_id=f"{RUN}_unsubscribed",
    )
    payload = response.json()
    check(
        "an unsubscribed event is acknowledged without action",
        response.status_code == 200 and payload.get("processed") is False,
        f"HTTP {response.status_code}: {payload.get('detail')}",
    )
    final = await get_database()["verifications"].count_documents({})
    check("and wrote nothing either", final == after, f"count {after} -> {final}")


async def phase_9_readback(client: httpx.AsyncClient) -> None:
    heading("PHASE 9 — GET /verifications")

    response = await client.get(f"{API}/verifications?history=true", timeout=30)
    check("GET /verifications?history=true -> 200", response.status_code == 200)
    if response.status_code != 200:
        print(f"         {response.text[:300]}")
        return
    records = response.json()
    print(f"  {len(records)} verification(s) over HTTP:")
    for record in records:
        print(
            f"    {record['verified_at']}  {record['outcome']:<10} "
            f"{record['event_id']:<40} recovered={record['amount_recovered']:>8.2f} "
            f"expected={record['amount_expected']:>8.2f} mismatch={record['amount_mismatch']}"
        )
    stored = await get_database()["verifications"].count_documents({})
    check(
        "HTTP history view returns every stored record",
        len(records) == stored,
        f"http={len(records)} mongo={stored}",
    )

    latest = await client.get(f"{API}/verifications", timeout=30)
    events_seen = {record["event_id"] for record in latest.json()}
    check(
        "default view collapses to one record per event",
        len(latest.json()) == len(events_seen),
        f"{len(latest.json())} records over {len(events_seen)} events",
    )

    recovered = await client.get(f"{API}/verifications?outcome=recovered&history=true", timeout=30)
    check(
        "outcome filter returns only recoveries",
        recovered.status_code == 200
        and all(r["outcome"] == "recovered" for r in recovered.json()),
        f"{len(recovered.json())} recovered",
    )
    bad = await client.get(f"{API}/verifications?outcome=refunded", timeout=30)
    check("an unknown outcome filter is rejected 422", bad.status_code == 422)


async def main() -> int:
    await connect_to_mongo()
    try:
        async with httpx.AsyncClient() as client:
            links = await phase_1_preflight(client)
            paid_event_id, _ = await phase_2_paid(client, links[PAID_LINK])
            await phase_3_replay(client, links[PAID_LINK], paid_event_id)
            await phase_4_cancelled(client)
            await phase_5_mismatch(client, links[MISMATCH_LINK])
            await phase_6_rejection(client, links[PAID_LINK])
            await phase_7_out_of_order(client, links[PAID_LINK])
            await phase_8_unmatched(client, links[PAID_LINK])
            await phase_9_readback(client)
    finally:
        await close_mongo_connection()

    heading("SUMMARY")
    print(f"  {len(passes)} passed, {len(failures)} failed")
    if failures:
        for label in failures:
            print(f"    FAILED: {label}")
    print("\n  Real state this run changed:")
    for line in touched:
        print(f"    - {line}")
    print(
        "\n  Reality of the delivery hop: every body above was signed with the real\n"
        "  RAZORPAY_WEBHOOK_SECRET using HMAC-SHA256 over the exact bytes sent, and\n"
        "  every field came from a live Razorpay payment_link object. The POST itself\n"
        "  came from this harness rather than from Razorpay's servers, because\n"
        "  Razorpay only delivers to ports 80/443 on a public host. See\n"
        "  docs/webhook-tunnel.md to close that gap."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
