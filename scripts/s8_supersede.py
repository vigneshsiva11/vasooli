"""Stage 8 data correction — archive and clear the superseded checkpoint 4 executions.

WHY. The first checkpoint 4 run wrote 62 execution records: 8 completed and 54 failed
with a masked `Too many requests` whose real body is
`RATE_LIMIT_EXCEEDED — "test mode limit of 30 reached for payment_link"`. Razorpay test
mode allows 30 payment links per account for the account's lifetime, and that account
is at 30/30. The 54 are therefore not retryable on those credentials, and the 5 links
the run did create live on an account the demo is moving off, so nothing downstream
can verify them.

WHAT THIS DELETES, AND WHAT IT REFUSES TO TOUCH. Only execution records whose
`event_id` belongs to the Stage 8 demo dataset. The 105 fixture events from Stages 5-6
and every execution against them are left exactly as they are — asserted before and
after, not merely intended.

WHY DELETE RATHER THAN APPEND A RETRY. The alternative is to leave the 62 in place and
re-authorize the failed events into fresh verdicts. That is policy-clean —
`app/policy/store.py` releases both the cap slot and the cooldown anchor for a failed
execution — but it leaves the dashboard reporting an 87% execution failure rate caused
entirely by an exhausted sandbox, plus two execution records per event for anything
retried, and it cannot fix the 5 completed-but-orphaned links at all: those events'
verdicts are spent, and re-authorizing them would be refused by the 24h cooldown
anchored at their real send time. Clearing and re-running produces one coherent batch
on one account.

ARCHIVE FIRST. Every deleted document is written verbatim to a JSON file before
anything is removed, so "superseded" means recoverable rather than destroyed. The
archive path is printed and the run aborts if it cannot be written.

DEFAULT IS A DRY RUN. Nothing is deleted without `--confirm`.

Usage:
    .venv/Scripts/python.exe scripts/s8_supersede.py
    .venv/Scripts/python.exe scripts/s8_supersede.py --confirm
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
import s8_dataset as ds  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import get_settings  # noqa: E402

EXECUTIONS = "executions"
VERIFICATIONS = "verifications"
PROMISES = "promises"

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="actually delete. Without this the script reports and changes nothing.",
    )
    parser.add_argument(
        "--archive-dir",
        default=".s8_archive",
        help="directory for the verbatim JSON archive of every deleted document",
    )
    parser.add_argument(
        "--only-failed",
        action="store_true",
        help=(
            "clear only demo executions whose status is not 'completed', leaving "
            "successful ones in place. For the paced re-run: a refused create costs "
            "no Razorpay link slot, so the completed records are real work worth "
            "keeping, while the failed ones must go or their verdicts replay as 200."
        ),
    )
    args = parser.parse_args()

    demo_ids = {s["event_id"] for s in ds.generate()}
    settings = get_settings()
    client = MongoClient(settings.mongodb_uri)
    db = client[settings.mongodb_db_name]

    # =====================================================================
    heading("0. WHAT IS THERE NOW")
    # =====================================================================
    all_executions = list(db[EXECUTIONS].find({}))
    demo_execs = [e for e in all_executions if e.get("event_id") in demo_ids]
    fixture_execs = [e for e in all_executions if e.get("event_id") not in demo_ids]
    print(f"  executions total   : {len(all_executions)}")
    print(f"    demo (in scope)  : {len(demo_execs)}")
    print(f"    fixture (frozen) : {len(fixture_execs)}")
    by_status = Counter(e.get("status") for e in demo_execs)
    for status, n in by_status.most_common():
        print(f"      demo {status:<10} {n:>4}")

    if not demo_execs:
        print("\n  Nothing to supersede — no demo execution records exist.")
        client.close()
        return 0

    # `target` is what gets archived and deleted; `keep_demo` is what survives inside
    # the demo scope. Under --only-failed the completed records stay, so the verify
    # step below has to prove they are still there by id, not merely that the failed
    # ones are gone.
    if args.only_failed:
        target = [e for e in demo_execs if e.get("status") != "completed"]
        keep_demo = [e for e in demo_execs if e.get("status") == "completed"]
        print(f"\n  --only-failed: {len(target)} to clear, {len(keep_demo)} completed "
              "records kept")
        for e in keep_demo:
            print(f"      keeping {e['event_id']:<14} {e.get('action_type'):<24} "
                  f"{e.get('razorpay_payment_link_id') or '(no link)'}")
        if not target:
            print("\n  Nothing to supersede — no failed demo execution records exist.")
            client.close()
            return 0
    else:
        target = demo_execs
        keep_demo = []

    # A verification or promise pointing at a record about to be deleted would be
    # orphaned. Checked explicitly: silently orphaning a foreign key is exactly the
    # kind of quiet damage a "data correction" is supposed not to do.
    demo_verifications = [
        v for v in db[VERIFICATIONS].find({}) if v.get("event_id") in demo_ids
    ]
    demo_promises = [p for p in db[PROMISES].find({}) if p.get("event_id") in demo_ids]
    check(
        "no verification record depends on a demo execution",
        not demo_verifications,
        f"{len(demo_verifications)} demo verifications exist and would be orphaned: "
        f"{sorted({v.get('event_id') for v in demo_verifications})}"
        if demo_verifications
        else "checkpoint 5 has not run, so there is nothing downstream to orphan",
    )
    check(
        "no promise record depends on a demo execution",
        not demo_promises,
        f"{len(demo_promises)} demo promises exist and would be orphaned: "
        f"{sorted({p.get('event_id') for p in demo_promises})}"
        if demo_promises
        else "checkpoint 6 has not run, so there is nothing downstream to orphan",
    )
    if FAILED:
        print(
            "\n  ABORTING. Deleting these executions would orphan live downstream "
            "records.\n  Clear those first, or re-scope this correction."
        )
        client.close()
        return 1

    # =====================================================================
    heading("1. ARCHIVE — written before anything is deleted")
    # =====================================================================
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_dir = Path(args.archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"executions_superseded_{stamp}.json"
    payload = {
        "archived_at": stamp,
        "reason": (
            "Refused by a Razorpay test-mode BURST rate limit, not the lifetime "
            "ceiling: the account had 25 of 30 slots free when these were refused, "
            "and a create succeeded again 87 seconds later. Superseded so the paced "
            "re-run can retry these verdicts, which a stored record would otherwise "
            "replay as 200."
            if args.only_failed
            else
            "Superseded by the re-scoped checkpoint 4. The first run needed 59 real "
            "Razorpay test-mode payment links; test mode allows 30 per account for "
            "the account's lifetime and the account was exhausted, so 54 executions "
            "failed and the 5 that succeeded are on credentials the demo no longer "
            "uses."
        ),
        "collection": EXECUTIONS,
        "count": len(target),
        "documents": target,
    }
    archive_path.write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    written = json.loads(archive_path.read_text(encoding="utf-8"))
    check(
        "the archive is on disk and holds every document about to be deleted",
        written.get("count") == len(target)
        and len(written.get("documents", [])) == len(target),
        f"{archive_path} — {len(written.get('documents', []))} documents, "
        f"{archive_path.stat().st_size:,} bytes",
    )
    if FAILED:
        print("\n  ABORTING. Nothing deleted, because the archive is not trustworthy.")
        client.close()
        return 1

    completed_links = [
        e for e in target
        if e.get("status") == "completed" and e.get("razorpay_payment_link_id")
    ]
    if completed_links:
        print(f"\n  the {len(completed_links)} real links being released are recorded "
              "in the archive and\n  remain on the old Razorpay test account as "
              "orphans. They are not deleted there:")
        for e in completed_links:
            print(f"    {e['event_id']:<14} {e['razorpay_payment_link_id']}")
    else:
        print("\n  no real Razorpay link is orphaned by this correction — every "
              "record being\n  cleared was refused by the gateway and never held a "
              "link id.")

    # =====================================================================
    heading("2. DELETE")
    # =====================================================================
    if not args.confirm:
        print(f"  DRY RUN — would delete {len(target)} demo execution records.")
        print("  Nothing was deleted. Re-run with --confirm to proceed.")
        heading(f"SUPERSEDE (DRY RUN) — {PASSED} passed, {FAILED} failed")
        client.close()
        return 0

    # Keyed on _id, not event_id: under --only-failed a single event could in principle
    # hold both a completed and a failed record, and an event_id filter would take both.
    target_ids = [e["_id"] for e in target]
    result = db[EXECUTIONS].delete_many({"_id": {"$in": target_ids}})
    print(f"  delete_many reported {result.deleted_count} deleted")
    check(
        "exactly the targeted executions were deleted",
        result.deleted_count == len(target),
        f"{result.deleted_count} deleted, {len(target)} were in scope",
    )

    # =====================================================================
    heading("3. VERIFY — re-read, do not trust the delete result")
    # =====================================================================
    after = list(db[EXECUTIONS].find({}))
    after_demo = [e for e in after if e.get("event_id") in demo_ids]
    after_fixture = [e for e in after if e.get("event_id") not in demo_ids]
    survivors = {str(e["_id"]) for e in after_demo}
    still_there = [e for e in target if str(e["_id"]) in survivors]
    check(
        "no targeted execution record remains",
        not still_there,
        f"{len(still_there)} still present: "
        f"{sorted({e.get('event_id') for e in still_there})}"
        if still_there
        else f"all {len(target)} are gone, so those verdicts are free to execute again",
    )
    keep_before = sorted(str(e["_id"]) for e in keep_demo)
    keep_after = sorted(str(e["_id"]) for e in after_demo)
    check(
        "every demo execution meant to survive did, by id",
        keep_before == keep_after,
        f"{len(keep_after)} completed demo records kept, same document ids"
        if keep_before == keep_after
        else f"expected {len(keep_before)} survivors, found {len(keep_after)}",
    )
    check(
        "every fixture execution survived untouched",
        len(after_fixture) == len(fixture_execs),
        f"{len(after_fixture)} before and after — Stages 5-6 data was not in scope "
        "and was not touched"
        if len(after_fixture) == len(fixture_execs)
        else f"{len(fixture_execs)} before, {len(after_fixture)} after",
    )
    fixture_ids_before = sorted(str(e["_id"]) for e in fixture_execs)
    fixture_ids_after = sorted(str(e["_id"]) for e in after_fixture)
    check(
        "the surviving fixture executions are the same documents, by id",
        fixture_ids_before == fixture_ids_after,
        f"{len(fixture_ids_after)} document ids match exactly — a count match alone "
        "would not rule out a swap",
    )

    heading(f"SUPERSEDE — {PASSED} passed, {FAILED} failed")
    print(f"  archive: {archive_path}")
    print("  next: scripts/s8_execute.py, once new Razorpay test credentials are in "
          ".env")
    client.close()
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
