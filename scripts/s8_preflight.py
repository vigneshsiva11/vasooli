"""Stage 8 preflight — prove new Razorpay test credentials have capacity before spending.

The first checkpoint 4 run discovered the 30-link lifetime ceiling at call 6 of 59, by
spending. This checks the same thing for free, before the re-scoped run touches
anything, so a mistake costs a GET instead of a demo.

WHAT IT CHECKS:

1. Credentials are present and answer. Never printed — `_redact` is applied to every
   response body, and only a non-reversible fingerprint of the key id is shown, so a
   run's log can prove *which* account was used without disclosing the key.
2. How many payment links already exist on the account, and therefore how many of the
   30 remain. A fresh test account reads 0. The exhausted account reads 30. If the
   .env swap silently did not take effect, this is where that shows up — the old
   account cannot pretend to be empty.
3. That the remaining capacity covers the planned budget with headroom.
4. That `razorpay_webhook_secret` is set, because checkpoint 5 signs webhooks with it
   and a new test account issues a new one. Presence only; the value is never read out.

WHY THE KEY FINGERPRINT. `get_settings()` is `@lru_cache`d, so a running server holds
the credentials it started with. Swapping .env without restarting uvicorn leaves the
server on the OLD account while this script reads the NEW one. Printing a fingerprint
here and comparing it to the server's own reported fingerprint is what catches that;
the script cannot see inside the server, so it prints the fingerprint and says plainly
that the server must be restarted.

This script CREATES NOTHING. It issues one GET.

Usage:
    .venv/Scripts/python.exe scripts/s8_preflight.py
    .venv/Scripts/python.exe scripts/s8_preflight.py --link-budget 25
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import get_settings  # noqa: E402
from app.execution.razorpay import (  # noqa: E402
    PAYMENT_LINKS_URL,
    _redact,
    credentials_from_settings,
)

#: Measured, not documented: Razorpay test mode allows this many payment links per
#: account for the account's lifetime. Cancelling one does not return its slot.
TEST_MODE_LINK_CEILING = 30

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


def fingerprint(secret: str) -> str:
    """A stable, non-reversible 8-hex tag for a credential.

    Enough to tell two accounts apart in a log and to prove a swap took effect;
    not enough to reconstruct the key.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--link-budget", type=int, default=25)
    parser.add_argument(
        "--headroom",
        type=int,
        default=3,
        help="slots that must remain free after the budget is spent",
    )
    args = parser.parse_args()

    settings = get_settings()
    credentials = credentials_from_settings()
    auth = (credentials.key_id, credentials.key_secret)

    heading("0. CREDENTIALS — identified, never disclosed")
    print(f"  key id fingerprint     : {fingerprint(credentials.key_id)}")
    print(f"  key secret fingerprint : {fingerprint(credentials.key_secret)}")
    print("  If these match the previous run's fingerprints, .env did not change.")
    check(
        "a Razorpay key id and secret are configured",
        bool(credentials.key_id and credentials.key_secret),
        "both present",
    )
    check(
        "a webhook secret is configured for checkpoint 5",
        bool(settings.razorpay_webhook_secret),
        f"present, fingerprint {fingerprint(settings.razorpay_webhook_secret)} — a new "
        "test account issues a new secret, so this must be the new one"
        if settings.razorpay_webhook_secret
        else "razorpay_webhook_secret is empty; checkpoint 5 cannot sign a webhook",
    )
    if FAILED:
        heading(f"PREFLIGHT — {PASSED} passed, {FAILED} failed")
        return 1

    heading("1. CAPACITY — how many of the 30 lifetime links are already gone")
    existing: list[dict] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            PAYMENT_LINKS_URL, params={"count": 100}, auth=auth
        )
        print(f"  HTTP {response.status_code}")
        if response.status_code != 200:
            print(f"  body: {_redact(response.text, credentials)[:400]}")
            check("the account answers a read", False, f"HTTP {response.status_code}")
            heading(f"PREFLIGHT — {PASSED} passed, {FAILED} failed")
            return 1
        existing = response.json().get("payment_links", [])

    check("the account answers a read", True, "GET /v1/payment_links returned 200")

    remaining = TEST_MODE_LINK_CEILING - len(existing)
    print(f"  payment links already on this account : {len(existing)}")
    print(f"  lifetime ceiling                      : {TEST_MODE_LINK_CEILING}")
    print(f"  remaining                             : {remaining}")
    if existing:
        references = [
            link.get("reference_id") or "(none)" for link in existing
        ]
        vsl = sum(1 for r in references if r.startswith("vsl_"))
        probe = sum(1 for r in references if r.startswith("vslprobe_"))
        print(f"    of those, vsl_ (demo/fixture): {vsl}, "
              f"vslprobe_ (throwaway): {probe}, other: "
              f"{len(references) - vsl - probe}")

    check(
        "this is not the exhausted account",
        len(existing) < TEST_MODE_LINK_CEILING,
        f"{len(existing)} of {TEST_MODE_LINK_CEILING} used"
        if len(existing) < TEST_MODE_LINK_CEILING
        else "30 of 30 used — this is the same account that refused 54 creates; the "
        ".env swap has not taken effect or the new keys were not saved",
    )
    check(
        "remaining capacity covers the planned budget with headroom",
        remaining >= args.link_budget + args.headroom,
        f"{remaining} remaining >= {args.link_budget} budget + {args.headroom} headroom"
        if remaining >= args.link_budget + args.headroom
        else f"only {remaining} remaining, but the run wants {args.link_budget} plus "
        f"{args.headroom} headroom — lower --link-budget or use a fresher account",
    )

    heading(f"PREFLIGHT — {PASSED} passed, {FAILED} failed")
    print("  NOTHING WAS CREATED. One GET, no writes.")
    if not FAILED:
        print("\n  `get_settings()` is @lru_cache'd, so a server started before the "
              ".env change\n  is still holding the old credentials. RESTART uvicorn "
              "before executing, or the\n  run will spend against the exhausted "
              "account and fail exactly as before.")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
