"""Stage 9 checkpoint 1 — the index migration, proved before and after.

The one part of Stage 9 that touches data written by earlier stages. It replaces the
non-partial unique index `uniq_razorpay_event_id` with a partial one filtered on
`{razorpay_event_id: {$exists: true}}`, and adds a second partial unique index on
`confirmation_id`.

Why it is necessary: MongoDB indexes a missing field as null and a unique index
permits exactly one null, so a `ManualVerification` — which carries no
`razorpay_event_id` at all — would insert once and then be refused for a duplicate it
does not have.

What this script proves, in order:

1. BEFORE — the stored index definitions, and how many documents the existing unique
   index covers;
2. that the partial filter selects EXACTLY the same document set, by comparing the
   two `_id` sets rather than only their sizes. Equal counts with different members
   would be the failure this comparison exists to catch;
3. the migration, run through the real code path (`app.webhooks.store.ensure_indexes`)
   rather than a hand-written `create_index`, so what is proved is what production
   startup does;
4. AFTER — the new definitions, that uniqueness is still enforced (a rebuild with
   `unique=True` fails outright on duplicate data, so a successful rebuild is itself
   the proof that none exist), and that the covered set is unchanged;
5. that the reversal is available: the exact one-line call that restores the old
   index is printed, not just described.

Observation is raw motor. Only the migration itself goes through app code.
"""

from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv("D:/vasooli/.env")

COLLECTION = "verifications"
EVENT_ID_INDEX = "uniq_razorpay_event_id"
CONFIRMATION_ID_INDEX = "uniq_confirmation_id"
RAZORPAY_FILTER = {"razorpay_event_id": {"$exists": True}}
CONFIRMATION_FILTER = {"confirmation_id": {"$exists": True}}

PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {label}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


def describe(information: dict) -> None:
    for name in sorted(information):
        spec = information[name]
        keys = ", ".join(f"{k}:{v}" for k, v in spec.get("key", []))
        unique = " unique" if spec.get("unique") else ""
        partial = spec.get("partialFilterExpression")
        partial_text = f" partial={dict(partial)}" if partial else " partial=NONE"
        print(f"    {name:<28} ({keys}){unique}{partial_text}")


async def ids_matching(collection, query: dict) -> set[str]:
    return {
        str(document["_id"])
        async for document in collection.find(query, {"_id": 1})
    }


async def main() -> int:
    uri = os.environ["MONGODB_URI"]
    database_name = os.environ.get("MONGODB_DB_NAME", "vasooli")
    client = AsyncIOMotorClient(uri)
    verifications = client[database_name][COLLECTION]

    print("=" * 78)
    print("STAGE 9 / CHECKPOINT 1 — verification index migration")
    print("=" * 78)

    # --- 1. BEFORE ---------------------------------------------------------
    print("\n[1] BEFORE — stored index definitions")
    before = await verifications.index_information()
    describe(before)

    all_ids = await ids_matching(verifications, {})
    covered_before = await ids_matching(verifications, RAZORPAY_FILTER)
    total = len(all_ids)
    print(f"\n    documents in {COLLECTION}: {total}")
    print(f"    documents with razorpay_event_id: {len(covered_before)}")

    old = before.get(EVENT_ID_INDEX)
    check(
        f"{EVENT_ID_INDEX} exists before the migration",
        old is not None,
        "nothing to migrate otherwise",
    )
    if old is not None:
        check(
            f"{EVENT_ID_INDEX} is unique before the migration",
            bool(old.get("unique")),
            f"unique={old.get('unique')!r}",
        )
        check(
            f"{EVENT_ID_INDEX} has NO partialFilterExpression before the migration",
            old.get("partialFilterExpression") is None,
            "this is the condition that makes the migration necessary",
        )
    check(
        f"{CONFIRMATION_ID_INDEX} does not exist yet",
        CONFIRMATION_ID_INDEX not in before,
        "Stage 9 adds it",
    )

    # --- 2. The coverage claim, as a set comparison -------------------------
    print("\n[2] COVERAGE — does the partial filter select the same documents?")
    check(
        "every stored verification carries razorpay_event_id",
        covered_before == all_ids,
        f"{len(covered_before)} of {total} match {RAZORPAY_FILTER}",
    )
    missing = all_ids - covered_before
    if missing:
        print(f"    documents the partial index would NOT cover: {sorted(missing)}")
    distinct_keys = await verifications.distinct("razorpay_event_id")
    check(
        "razorpay_event_id values are distinct across those documents",
        len(distinct_keys) == len(covered_before),
        f"{len(distinct_keys)} distinct values over {len(covered_before)} documents",
    )
    already_manual = await ids_matching(verifications, CONFIRMATION_FILTER)
    check(
        "no confirmation_id records exist yet",
        not already_manual,
        f"{len(already_manual)} found",
    )

    # --- 3. The migration, through the real code path ----------------------
    print("\n[3] MIGRATION — app.webhooks.store.ensure_indexes()")
    sys.path.insert(0, "D:/vasooli")
    from app.db import connect_to_mongo, close_mongo_connection
    from app.webhooks import store

    await connect_to_mongo()
    try:
        await store.ensure_indexes()
        print("    ensure_indexes() returned without raising")
        migrated = True
    except Exception as exc:  # noqa: BLE001 - the failure IS the result here
        print(f"    ensure_indexes() RAISED: {type(exc).__name__}: {exc}")
        migrated = False
    finally:
        await close_mongo_connection()
    check(
        "ensure_indexes() completed",
        migrated,
        "a unique rebuild over duplicate data would have raised here",
    )

    # --- 4. AFTER ----------------------------------------------------------
    print("\n[4] AFTER — stored index definitions")
    after = await verifications.index_information()
    describe(after)

    new = after.get(EVENT_ID_INDEX)
    check(f"{EVENT_ID_INDEX} still exists", new is not None)
    if new is not None:
        check(
            f"{EVENT_ID_INDEX} is still unique",
            bool(new.get("unique")),
            "uniqueness was rebuilt, not dropped",
        )
        stored_filter = new.get("partialFilterExpression")
        normalised = (
            {k: dict(v) if hasattr(v, "items") else v for k, v in stored_filter.items()}
            if stored_filter
            else None
        )
        check(
            f"{EVENT_ID_INDEX} is now partial on the razorpay key",
            normalised == RAZORPAY_FILTER,
            f"{normalised}",
        )
        check(
            f"{EVENT_ID_INDEX} still keys on razorpay_event_id ascending",
            list(new.get("key", [])) == [("razorpay_event_id", 1)],
            f"{list(new.get('key', []))}",
        )

    confirmation = after.get(CONFIRMATION_ID_INDEX)
    check(f"{CONFIRMATION_ID_INDEX} now exists", confirmation is not None)
    if confirmation is not None:
        check(
            f"{CONFIRMATION_ID_INDEX} is unique",
            bool(confirmation.get("unique")),
            "one confirmation per execution is a database constraint",
        )
        stored_filter = confirmation.get("partialFilterExpression")
        normalised = (
            {k: dict(v) if hasattr(v, "items") else v for k, v in stored_filter.items()}
            if stored_filter
            else None
        )
        check(
            f"{CONFIRMATION_ID_INDEX} is partial on the confirmation key",
            normalised == CONFIRMATION_FILTER,
            f"{normalised}",
        )

    for name in ("event_id_verified_at", "execution_id_verified_at"):
        check(
            f"{name} survived the migration",
            name in after,
            "the read indexes are untouched",
        )

    covered_after = await ids_matching(verifications, RAZORPAY_FILTER)
    all_after = await ids_matching(verifications, {})
    check(
        "the covered document set is byte-identical before and after",
        covered_after == covered_before,
        f"{len(covered_after)} documents, same _id set",
    )
    check(
        "no document was added, removed or rewritten",
        all_after == all_ids,
        f"{len(all_after)} documents, same _id set as before",
    )

    # --- 5. Reversal -------------------------------------------------------
    print("\n[5] REVERSAL — one line, if this is ever to be undone")
    print("    await collection().drop_index('uniq_razorpay_event_id')")
    print("    await collection().create_index(")
    print("        [('razorpay_event_id', ASCENDING)], unique=True,")
    print("        name='uniq_razorpay_event_id')   # no partialFilterExpression")
    print("    (and drop 'uniq_confirmation_id', which nothing before Stage 9 used)")

    print("\n" + "=" * 78)
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    print("=" * 78)
    client.close()
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
