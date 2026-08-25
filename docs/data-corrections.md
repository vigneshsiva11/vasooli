# Data corrections

`policy_verdicts` is an append-only authorization log. Nothing in the running
system deletes from it or edits a stored verdict, and no code path exists that
can. Every correction is therefore a manual, out-of-band act, and every one of
them is recorded here in full — what changed, why it could not be left alone,
when, and what replaced it. A verdict log with an unexplained hole in it is worth
less than one with no hole and no explanation, because the hole is the part you
cannot check.

Two kinds of correction appear below, and they are not equally serious. Deleting
a verdict destroys a record. Correcting a *provenance label* leaves the verdict
untouched and revises a claim the project made about how well it knows where that
verdict came from. The second is nearly routine — a provenance claim can go stale
without anything being wrong — but it is recorded to the same standard, because a
field that quietly self-corrects is not evidence of anything.

---

## 2026-08-25 — `pol_S4_MULTI` v6, v7, v8, v9 removed

Four verdict documents deleted from `policy_verdicts`. All four were fixture
residue: their trails describe a sequence of contacts that never happened.

### What they were

| version | `_id` | verdict | decision | `evaluated_at` | fingerprint |
|---|---|---|---|---|---|
| 6 | `6a8c6f69474e2d8946197893` | `authorized` / `ok` | `6a8c6f68f81f8f6f93d0e6c5` v3 | 2026-08-22T04:20:57.289Z | `rb1_aba19a5e5ee8124e` `evaluated` |
| 7 | `6a8c6f69474e2d8946197894` | `authorized` / `ok` | `6a8c6f68f81f8f6f93d0e6c5` v3 | 2026-08-23T10:20:57.289Z | `rb1_aba19a5e5ee8124e` `evaluated` |
| 8 | `6a8c6f69474e2d8946197895` | `authorized` / `ok` | `6a8c6f68f81f8f6f93d0e6c5` v3 | 2026-08-24T14:20:57.289Z | `rb1_aba19a5e5ee8124e` `evaluated` |
| 9 | `6a8c6f6bf81f8f6f93d0e6c8` | `blocked` / `customer_opted_out` | `6a8c6f6af81f8f6f93d0e6c7` v4 | 2026-08-24T16:20:59.230Z | `rb1_aba19a5e5ee8124e` `evaluated` |

All four named event `pol_S4_MULTI`, customer `cust_pol_S4_multi`, intervention
`manual_escalation`. **No execution referenced any of them** — checked against
`executions.policy_verdict_id` before deleting, and the deletion would have
aborted if one had.

The four documents are preserved verbatim, exactly as stored, in
[`docs/data-corrections/2026-08-25-pol_S4_MULTI-v6-v9.json`](data-corrections/2026-08-25-pol_S4_MULTI-v6-v9.json).
The summary below is a reading of them, not a substitute for them.

v6, v7 and v8 each carried these two trail entries:

```
contact_cap:      PASS (0 of 3 contacts used for event pol_S4_MULTI)      # v6
contact_cap:      PASS (1 of 3 contacts used for event pol_S4_MULTI)      # v7
contact_cap:      PASS (2 of 3 contacts used for event pol_S4_MULTI)      # v8
customer_opt_out: PASS (customer cust_pol_S4_multi has not opted out)     # all three
```

Both statements were false when they were written. By that point the event
already carried three authorized contacts (v1, v2, v3) and the customer had
already withdrawn consent — v4 and v5 both blocked on `customer_opted_out`,
and the opt-out record is dated 2026-08-24T04:18:36.154Z, hours before v6.
So these three documents recorded an empty contact history and a live consent
on an event that had neither.

### Why

Not a policy-engine bug. The engine judged exactly what it was given; it was
given fiction. The cause was in the fixture, `scripts/s4_multi.py`.

That script demonstrates the maximal simultaneous-failure case: a receivable
chased three times over several days, then a withdrawn consent, then a balance
grown past the never-auto ceiling, so that four protections fail at once. To lay
a three-day timeline out in a few seconds it builds a `PolicyContext` by hand
with a back-dated `now`, rather than gathering context from the database.

That is sound on an event with no history — the injected counts and anchors are
chosen to equal exactly what `gather_context` would have returned from an empty
event, so the verdicts it writes still re-derive from the record. It stopped
being sound the moment the script ran a second time against the same event id,
which it did because `EVENT_ID` and `CUSTOMER` were fixed constants. The second
run injected `prior_authorized_contacts=0` and `customer_opted_out=False` over
an event that already had three contacts and a withdrawn consent, and wrote the
result into an append-only log, permanently.

### Why v9 as well

v9 was a genuine verdict — produced through `POST /authorize/{event_id}` from
real gathered context, not injected — and it re-derived correctly *while the
fiction was present*. But its trail quotes that fiction:

```
contact_cap:      FAIL (6 contact(s) already authorized for event pol_S4_MULTI, ...)
contact_cooldown: FAIL (last authorized contact was 2.0h ago at 2026-08-24T14:20:57.289000+00:00, ...)
```

The `6` is v1–v3 plus v6–v8. The cooldown anchor `14:20:57.289` is v8's
back-dated `evaluated_at`. With v6–v8 gone the true count is 3 and the true
anchor is v3's timestamp, so v9's stored prose becomes unre-derivable — and v9
carries an `evaluated` fingerprint, which is a claim of direct evidence and is
held to exact reproduction with no trail tolerance. v9 could not be kept
truthfully once its premises were removed. It is residue of the same second run
by the same mechanism, one step removed.

Deleting v6–v9 rather than v6–v8 also leaves the version sequence contiguous at
`[1, 2, 3, 4, 5]`. That was not the reason for the decision, but it means
`check_uniqueness()` in `scripts/s4_audit.py` needed no exemption for a
documented gap — the gap check still means what it always meant, that a hole in
an append-only sequence is a lost record.

### What was not deleted

v1–v5 stayed. v1, v2 and v3 are the first run's three chases: injected context,
but on a genuinely empty event, so what they assert was true and they re-derive.
v4 and v5 are real blocks on the withdrawn consent. v5 in particular was written
by the second run *before* the injection, so it never quoted the fiction — its
trail reads `3 contact(s) already authorized` with v3's anchor, which is correct
both before and after this deletion.

### What replaced it

Deleting v9 left v5 as the event's current verdict while the pipeline's current
recommendation had moved on to decision v4 — a live authorization resting on a
superseded recommendation, which `scripts/s4_audit.py` §6 reports as a finding
and should. Rather than widen that check, the event was re-authorized through
the ordinary route:

```
POST /authorize/pol_S4_MULTI  ->  v6  blocked/customer_opted_out  on decision v4
```

evaluated live against the real record, fingerprint `rb1_aba19a5e5ee8124e`,
source `evaluated`. Its trail:

```
decision_is_actionable: PASS (manual_escalation is a real intervention)
customer_opt_out:       FAIL (customer cust_pol_S4_multi is on the do-not-contact list ...)
contact_cap:            FAIL (3 contact(s) already authorized for event pol_S4_MULTI, cap is 3 ...)
contact_cooldown:       PASS (last authorized contact was 26.4h ago, outside the 24h cooldown)
erv_minimum:            PASS (ERV 32,950.00 clears the 25.00 minimum at a cost of 50.00)
amount_tier:            FAIL (60,000.00 is at or above the 25,000.00 never-auto ceiling ...)
```

Three failures where the deleted v9 claimed four. The difference is the
cooldown, and it is the honest number: the real last contact is v3's, 26.4 hours
before this verdict, so the cooldown has genuinely lapsed. The fiction's `2.0h
ago` came from v8's back-dated timestamp. The four-simultaneous-failure case the
fixture exists to demonstrate is still demonstrated, re-derivably, by the
run-tagged event `pol_S4_MULTI_20260825T042142`.

### How this is prevented from recurring

`scripts/s4_multi.py` was fixed before the deletion, in two ways:

* `EVENT_ID` and `CUSTOMER` are now run-tagged with a UTC timestamp, so each run
  gets a fresh event and a fresh customer and the injected context is always
  describing the empty event it claims to describe.
* `three_prior_chases()` now refuses structurally rather than trusting the tag.
  Before injecting anything it counts existing verdicts for the event and checks
  the opt-out list, and aborts if either says the event has a history. An
  injected context written over a real history is a stored verdict describing a
  world the database contradicts, and nothing downstream can tell the difference
  after the fact — so the check belongs before the write, not in the audit.

### Verification

`scripts/s4_audit.py` after the deletion and re-authorization: **exit 0**, 189
verdicts re-derived under the rulebook each one names, 0 mismatches, 0 findings,
85 events with no gaps in any version sequence. `pol_S4_MULTI` v1–v6 all report
`match`, with v6 `attested` against `rb1_aba19a5e5ee8124e`.

One advisory remains open and is unrelated to this correction: 6 verdicts are
labelled `reconstructed` but are now reproduced exactly by two rulebooks each,
so their source should be weakened to `backfilled`. They re-derive correctly;
the label overstates how uniquely it was identified.

*(Resolved the same day — see the entry below.)*

---

## 2026-08-25 — six `reconstructed` labels weakened to `backfilled`

Six verdicts had their `rulebook_fingerprint_source` changed from
`reconstructed` to `backfilled`. No verdict, trail, reason or fingerprint value
was altered. Ratified before the change.

| event | version | fingerprint (unchanged) |
|---|---|---|
| `pol_S4_20260824T045335_RPL_FRESH` | 1 | `rb1_3ecc9dde2839f090` |
| `pol_S4_20260824T045335_RPL_PRE` | 1 | `rb1_3ecc9dde2839f090` |
| `pol_S4_20260824T045335_RPL_PRE` | 2 | `rb1_3ecc9dde2839f090` |
| `pol_S4_20260824T060855_RPL_FRESH` | 1 | `rb1_3ecc9dde2839f090` |
| `pol_S4_20260824T060855_RPL_PRE` | 1 | `rb1_3ecc9dde2839f090` |
| `pol_S4_20260824T060855_RPL_PRE` | 2 | `rb1_3ecc9dde2839f090` |

### Why

`reconstructed` means one rulebook reproduces the verdict exactly and no other
does. That is not a property of the verdict alone — it is a property of the
verdict *and the registry it was compared against*. Adding a rulebook to
`app/policy/rulebook.py` can therefore falsify a `reconstructed` label that was
accurate when it was written, without anything about the verdict changing.

`rb1_aba19a5e5ee8124e` — the amendment that moved the cooldown anchor from
`verdict.evaluated_at` to `execution.executed_at` — did exactly that. On an event
with no executions the two anchors coincide, so verdicts on such events re-derive
identically under it and under its predecessor. Six verdicts that were uniquely
identified when `scripts/s4_fingerprint_backfill.py` ran became ambiguous the
moment that rulebook entered the registry, and their labels went on claiming a
uniqueness that no longer held.

Nothing was wrong with the verdicts. `scripts/s4_audit.py` had been reporting
this as `weakened` — explicitly not a re-derivation failure, since each still
re-derives byte-exactly under the rulebook it names — and it was the last open
advisory on the Stage 4 log.

### How

Via `scripts/s4_fingerprint_reconcile.py`, added for this correction.

It does not restate the labelling rule. `decide_fingerprint()` in
`scripts/s4_fingerprint_backfill.py` already encodes it — one exact match →
`reconstructed`, several → `backfilled` — so the reconcile script imports that
function and asks what it would say today. The corrected labels are therefore
produced by the same code that produced every other label in the collection,
rather than by a second implementation that could drift from it, or by hand.

The migration itself could not be reused: it skips any verdict that already
carries a fingerprint, and its write filter requires the field to be absent. That
refusal is deliberate and was left intact.

Bounds on what the reconcile script will write:

* the only field it writes is `rulebook_fingerprint_source`, and only in the
  direction `reconstructed` → `backfilled`;
* it writes only when the fingerprint already on the record is still among the
  exact matches. The guard is *not* "the fingerprint did not move" — see the
  tiebreak-drift finding below. Weakening a label is safe while the stored
  fingerprint still fits and unsafe if it does not, because that would trade an
  overstated claim for a false one;
* a *strengthening* — `backfilled` → `reconstructed` — is refused
  unconditionally. A shrinking registry can make an identification look unique
  again, and writing that would manufacture evidence out of the archive being
  incomplete, which is the inversion of what the field is for;
* verdicts labelled `evaluated` are never recomputed at all. That label records
  the rulebook that actually ran, at the moment it ran. It is evidence rather than
  identification, and `decide_fingerprint` has neither the vocabulary to express
  it nor any standing to contradict it. (The first draft of the script did
  recompute them, and every one of the 135 registered as a spurious divergence —
  which is what surfaced the distinction.)
* dry-run by default, and each write re-asserts `_id` + fingerprint + the
  expected prior source in its filter, so a concurrent change matches nothing
  rather than being overwritten.

### An unrelated finding this surfaced: tiebreak drift

Recomputing every migrated label turned up **44 further verdicts** that diverge,
all already labelled `backfilled` and all left alone. Their stored fingerprint is
`rb1_3ecc9dde2839f090`; today's recomputation would nominate
`rb1_aba19a5e5ee8124e` instead, because all four known rulebooks now reproduce
them exactly and the ambiguous-case tiebreak takes the newest that fits. The
registry has grown since the migration ran, so "the newest that fits" no longer
resolves to the value stored.

Every stored fingerprint here is still an exact match, so no record is wrong and
the audit re-derives all of them. But it does falsify one inference the migration
documents (`scripts/s4_fingerprint_backfill.py`, lines 40–41 and 147–148): that a
`backfilled` verdict naming a *superseded* rulebook is one today's policy could
not have produced. Today's rulebook reproduces these 44 exactly.

That inference was therefore already unreliable for 44 stored verdicts before
this correction touched anything — which is also why the six weakenings were not
blocked on it. The two issues are independent: one is a label overstating
uniqueness, the other is a tiebreak target going stale.

Restoring the signal would mean overwriting 44 (now 50) fingerprint values that
were each correct when chosen, for no gain in re-derivability. **Not done, not
applied, and reported rather than acted on.** The reconcile script prints the full
list on every run.

**Ratified 2026-08-25: leave all 44 untouched.** Every one still re-derives
correctly under the fingerprint it names, and rewriting 50 stored values to settle
a question of docstring precision is not worth a further data mutation. The
imprecise claim was in the prose, so the prose was corrected instead of the data.

What changed, in `scripts/s4_fingerprint_backfill.py` — comments and printed
output only, no behaviour:

* the docstring passage that read "a backfilled verdict naming a superseded
  rulebook is one that today's policy demonstrably could not have produced" now
  says what the field actually records: the newest rulebook that fit the verdict
  among those known *when the label was written* — the tightest consistent account
  available at that moment, not a claim that no other rulebook fits, and not a
  claim about what today's policy could have produced;
* the same claim appeared a second time as an inline comment inside
  `decide_fingerprint()`, the function that implements the tiebreak. Softened
  there too, since leaving it would have left the overclaim in the code that
  causes it. It now states explicitly that the earlier version claimed this and
  was wrong to;
* `summarise()` printed it as well — "name a SUPERSEDED rulebook, meaning today's
  policy cannot have produced them". Reworded to "today's rules do not reproduce
  them, so the tightest fit is an archived one".

One nuance the rewrite pins down, because it explains why the claim was ever
made. At the instant the migration runs the claim is *true by construction*: the
tiebreak takes the newest rulebook that fits, so if the stored fingerprint is a
superseded one, the current rulebook provably did not reproduce that verdict. What
the original prose missed is that this is a statement about the registry at write
time, and the registry grows. A rulebook added later can fit a verdict exactly
without anything about the verdict changing, which makes the stored value stale as
a *tiebreak* while leaving it perfectly valid as a *fit*. The claim did not become
wrong; it decayed, silently, and nothing recomputed it.

That decay is now detected rather than assumed away:
`scripts/s4_fingerprint_reconcile.py` re-checks every migrated label against the
current registry on each run and reports the drift, without writing. Both softened
passages point at it and at this file.

### Verification

* `scripts/s4_fingerprint_reconcile.py` — dry run showed exactly the 6 the audit
  had named, then `--apply` wrote 6 of 6; all 189 verdicts read back valid
  through `PolicyVerdictRecord`. Labels now: 135 `evaluated`, 51 `backfilled`
  (was 45), 3 `reconstructed` (was 9).
* re-run of the same script: **0 in scope**, so the correction is idempotent.
* `scripts/s4_fingerprint_backfill.py` dry run: still plans 0 writes, unaffected.
* `scripts/s4_audit.py` — **exit 0**, and the `weakened` advisory is gone
  entirely. The six now report `match ... stand-in`, and none of them appears
  under `reclassified`, so weakening the label did not start relying on the trail
  tolerance that `backfilled` grants: they still re-derive byte-exactly.
* `scripts/s4_adversarial.py` **exit 0** (137 refused), `scripts/s5_audit.py`
  **exit 0**, `scripts/s5_adversarial.py` **exit 0** (120 refused).

