"""Stage 8 diagnostic — measure Razorpay's rate limit rather than guess it.

Checkpoint 4 created 5 payment links in about 5 seconds and was then refused with
HTTP 429 "Too many requests" for the remaining 54 attempts. Razorpay's public rate
limit page 404s and this session has no web search, so the limit is measured here
instead of assumed.

Two questions, in order, cheapest first:

1. **Is the limiter still engaged?** A GET against `/v1/payment_links` costs nothing
   to the demo data and tells us whether the block was a short window that has since
   expired or something longer-lived.
2. **What does the 429 actually say?** Every response header is printed, because a
   `Retry-After` or an `X-RateLimit-*` header would give the real pacing instead of a
   pace inferred from the failure pattern. If none is returned, that absence is
   itself the finding, and the pacing has to come from measurement.

This script talks to Razorpay directly, on purpose: it is diagnosing the gateway, not
exercising the pipeline, and routing through `POST /execute` would consume a policy
verdict per probe. It writes nothing to MongoDB and creates no payment link unless
`--create` is passed.

Credentials come from `app.execution.razorpay.credentials_from_settings()` and are
never printed. `_redact` is applied to every response body for the same reason.

Usage:
    .venv/Scripts/python.exe scripts/s8_ratelimit_probe.py
    .venv/Scripts/python.exe scripts/s8_ratelimit_probe.py --create
    .venv/Scripts/python.exe scripts/s8_ratelimit_probe.py --create --burst 8 --gap 2.0
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.execution.razorpay import (  # noqa: E402
    PAYMENT_LINKS_URL,
    _redact,
    credentials_from_settings,
    to_minor_units,
)

#: Headers worth calling out if present. Everything is printed regardless; these are
#: the ones that would answer the pacing question outright.
INTERESTING = (
    "retry-after",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "ratelimit-limit",
    "ratelimit-remaining",
    "ratelimit-reset",
)


def heading(text: str) -> None:
    print()
    print("=" * 92)
    print(text)
    print("=" * 92)


def report(response: httpx.Response, credentials) -> None:
    print(f"    HTTP {response.status_code}   {response.elapsed.total_seconds():.2f}s")
    flagged = {
        name: value
        for name, value in response.headers.items()
        if name.lower() in INTERESTING
    }
    print(f"    rate-limit headers: {flagged or 'NONE RETURNED'}")
    print(f"    all headers: {dict(response.headers)}")
    body = _redact(response.text, credentials)
    print(f"    body: {body[:400]}")


async def create_one(client: httpx.AsyncClient, auth, label: str) -> httpx.Response:
    """Create one 1.00 INR probe link. Real, and labelled as a probe."""
    payload = {
        "amount": to_minor_units(1.00),
        "currency": "INR",
        "accept_partial": False,
        "description": "Vasooli rate-limit probe",
        "reference_id": f"vslprobe_{label}",
        "notes": {"purpose": "rate limit measurement, not demo data"},
    }
    return await client.post(PAYMENT_LINKS_URL, json=payload, auth=auth)


async def recover(credentials, auth, *, gap: float) -> int:
    """Measure how long the limiter stays engaged, then test a sustained pace.

    Two numbers come out of this, and the batch retry needs both: how long to wait
    after a refusal before trying again, and what steady gap does not trip it. Single
    attempts with exponential backoff, so a refusal costs one call and one wait rather
    than extending the block further.
    """
    heading("RECOVERY — how long does the block last?")
    cleared_after: float | None = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        for delay in (60, 120, 240, 480):
            print(f"\n  idling {delay}s, then one attempt...")
            await asyncio.sleep(delay)
            response = await create_one(client, auth, f"rec{delay:04d}")
            print(f"  after {delay}s idle:")
            report(response, credentials)
            if response.status_code in (200, 201):
                cleared_after = delay
                break

        if cleared_after is None:
            print("\n  still refused after 480s of idling. The block is longer than "
                  "this probe measures.")
            return 1

        heading(f"SUSTAINED PACE — {gap}s gap, 6 attempts, starting from a clear bucket")
        accepted, refused_at = 1, None
        for index in range(2, 8):
            await asyncio.sleep(gap)
            response = await create_one(client, auth, f"pace{index:02d}")
            print(f"\n  attempt {index} ({gap}s gap)")
            report(response, credentials)
            if response.status_code in (200, 201):
                accepted += 1
            else:
                refused_at = index
                break

    heading("WHAT THAT MEANS FOR THE BATCH")
    print(f"  block cleared after   : {cleared_after}s of idling")
    print(f"  accepted at {gap}s gap : {accepted} consecutive")
    if refused_at:
        print(f"  refused at attempt    : {refused_at} — {gap}s is NOT sustainable")
    else:
        print(f"  no refusal in 7 attempts — {gap}s looks sustainable")
        print(f"  59 links at {gap}s would take about {59 * gap / 60:.0f} minutes")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--create",
        action="store_true",
        help="also attempt real link creations, to find the pace that survives",
    )
    parser.add_argument("--burst", type=int, default=6, help="creations to attempt")
    parser.add_argument(
        "--gap", type=float, default=0.0, help="seconds to wait between creations"
    )
    parser.add_argument(
        "--recover",
        action="store_true",
        help=(
            "find how long the limiter stays engaged, then whether --gap sustains. "
            "Backs off between single attempts rather than hammering, because a "
            "refused request may itself extend the block — which is the most likely "
            "reason the checkpoint 4 run never recovered."
        ),
    )
    args = parser.parse_args()

    credentials = credentials_from_settings()
    auth = (credentials.key_id, credentials.key_secret)

    if args.recover:
        return await recover(credentials, auth, gap=args.gap or 10.0)

    heading("1. READ — is the limiter still engaged?")
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            PAYMENT_LINKS_URL, params={"count": 1}, auth=auth
        )
        report(response, credentials)

    if not args.create:
        print("\n  --create not passed: no link was created.")
        return 0

    heading(f"2. WRITE — {args.burst} creations, {args.gap}s apart")
    print(
        "  Each is a real test-mode link. The point is where the refusals start, so\n"
        "  the run continues through a 429 rather than aborting on it."
    )
    outcomes: list[tuple[int, int, float]] = []
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        for index in range(1, args.burst + 1):
            if index > 1 and args.gap:
                await asyncio.sleep(args.gap)
            elapsed = time.monotonic() - started
            payload = {
                "amount": to_minor_units(1.00),
                "currency": "INR",
                "accept_partial": False,
                "description": "Vasooli rate-limit probe",
                "reference_id": f"vslprobe_{int(elapsed * 1000):09d}_{index:02d}",
                "notes": {"purpose": "rate limit measurement, not demo data"},
            }
            response = await client.post(PAYMENT_LINKS_URL, json=payload, auth=auth)
            outcomes.append((index, response.status_code, elapsed))
            print(f"\n  attempt {index} at t+{elapsed:.2f}s")
            report(response, credentials)

    heading("3. WHAT THAT MEANS")
    ok = [i for i, code, _t in outcomes if code in (200, 201)]
    refused = [(i, t) for i, code, t in outcomes if code == 429]
    print(f"  accepted: {len(ok)} of {len(outcomes)}")
    if refused:
        print(f"  first refusal at attempt {refused[0][0]}, t+{refused[0][1]:.2f}s")
    else:
        print(f"  no refusals at a {args.gap}s gap across {args.burst} attempts")
    print(
        "\n  Probe links are real and will appear in the test dashboard with a\n"
        "  `vslprobe_` reference id, distinct from the demo batch's `vsl_` prefix."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
