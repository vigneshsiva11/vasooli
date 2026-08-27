"""Stage 8 — the demo dataset generator. Pure, deterministic, offline.

Produces 200 `RevenueEvent` bodies and nothing else. This module opens no database
handle, makes no HTTP call, and imports nothing from any stage's `store` module. It
is a data source, so the later phase scripts can import it without any risk that
importing the plan performs part of it.

Every event carries planning metadata under keys prefixed with `_`. Those keys are
this stage's own bookkeeping — which events are meant to reach Gemini, which belong
to opted-out customers, which are held back from the blanket authorize so a
promise-to-pay follow-up has something to authorize. `api_body()` strips them, so
what reaches `POST /events` is exactly the ingestion contract and nothing more.

Determinism comes from one seed. Re-running produces byte-identical events, which
is what lets checkpoint 0 make a claim about data that has not been ingested yet.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any

#: One seed for the whole batch. Changing it changes every amount, every customer
#: assignment, and every timestamp — so it is fixed, and the dry run reports it.
SEED = 20260826

#: Event id prefix, so the demo batch is identifiable in the raw database without
#: any metrics endpoint treating it differently. Nothing reads this prefix.
PREFIX = "demo"

SURFACE_SUFFIX = {
    "payment": "pay",
    "checkout": "chk",
    "subscription": "sub",
    "receivable": "rcv",
}

# ---------------------------------------------------------------------------
# Composition. Counts are exact, not probabilistic: a distribution the dry run
# can only estimate is a distribution nobody ratified.
# ---------------------------------------------------------------------------

#: (surface, root cause or None for an intended-LLM case) -> how many.
#:
#: `None` means the reason text is deliberately outside every rule, so
#: `rules.classify()` returns None and the diagnosis service escalates to Gemini.
#: Which root cause the model then picks is not ours to predict.
COMPOSITION: dict[str, dict[str | None, int]] = {
    "payment": {
        "insufficient_funds": 34,
        "issuer_declined": 22,
        "card_expired": 18,
        "temporary_processing_error": 14,
        "suspected_fraud": 5,
        None: 7,
    },
    "checkout": {
        "checkout_friction": 9,
        "technical_error": 8,
        "price_sensitivity": 8,
        "payment_method_unavailable": 7,
        "low_purchase_intent": 4,
        None: 4,
    },
    "subscription": {
        "insufficient_funds": 9,
        "card_expired": 6,
        "mandate_expired": 5,
        "issuer_declined": 3,
        "voluntary_churn": 3,
        "mandate_revoked": 1,
        "dunning_exhausted": 1,
        None: 2,
    },
    "receivable": {
        "genuine_delay": 17,
        "non_responsive": 7,
        "payment_dispute": 3,
        None: 3,
    },
}

#: How many events per surface sit below the ₹5,000 autonomous limit ("auto") and
#: how many above it ("review"). Exact counts again.
#:
#: The 6 sub-₹5,000 receivables are the ratified carve-out from the ₹5,000–150,000
#: B2B range. Without them no contact-type intervention in the whole batch would
#: ever execute — every larger invoice is review-tier by construction, and
#: review-tier verdicts have no execution path.
BANDS: dict[str, dict[str, tuple[int, float, float]]] = {
    #                     count,    lo,       hi
    "payment": {"auto": (70, 200.0, 4989.0), "review": (30, 5000.0, 14990.0)},
    "checkout": {"auto": (30, 150.0, 4989.0), "review": (10, 5000.0, 14990.0)},
    "subscription": {"auto": (24, 199.0, 4989.0), "review": (6, 5000.0, 14990.0)},
    "receivable": {"auto": (6, 800.0, 4890.0), "review": (24, 5000.0, 149000.0)},
}

#: Paise values that show up on real transaction amounts. Whole rupees are common
#: too, so a share of amounts get zero paise — the thing being avoided is round
#: HUNDREDS, not every amount ending in .00.
PAISE = (50, 25, 75, 99, 90, 40, 60, 80, 20, 10, 5, 35, 45, 55, 65, 85, 15, 95)
WHOLE_RUPEE_SHARE = 0.45

#: How far back events are dated, so the raw database reads as an operating
#: history rather than one burst. Everything downstream is stamped when the
#: pipeline actually runs, which is today — that limitation is reported, not hidden.
BACKDATE_DAYS = 14

# ---------------------------------------------------------------------------
# Failure reason pools.
#
# Every string below was checked against `app/diagnosis/rules.py` by hand and is
# checked again mechanically by `scripts/s8_dryrun.py`, which asserts that
# `rules.classify()` returns the intended root cause for each. A string that
# drifts out of its rule is a failed assertion, not a surprise at ingestion.
# ---------------------------------------------------------------------------

REASONS: dict[str, dict[str, tuple[str, ...]]] = {
    "payment": {
        "insufficient_funds": (
            "insufficient_funds",
            "Insufficient funds in the linked account",
            "Payment failed: low balance",
            "Card did not go through, not enough funds in account",
            "INSUFFICIENT_FUNDS",
            "Insufficient balance",
        ),
        "card_expired": (
            "card_expired",
            "expired_card",
            "The card on file has expired",
            "Card expiry date is in the past",
            "Expired card presented for the transaction",
        ),
        "issuer_declined": (
            "issuer_declined",
            "card_declined_by_issuer",
            "do_not_honour",
            "Transaction declined by the issuing bank",
            "Issuer refused the authorisation",
            "do not honour",
        ),
        "temporary_processing_error": (
            "gateway_error",
            "gateway_timeout",
            "network_error",
            "Request timed out at the acquirer",
            "Upstream gateway error during authorisation",
            "OTP verification failed on the bank page",
        ),
        "suspected_fraud": (
            "suspected_fraud",
            "stolen_card",
            "fraud_suspected",
            "Flagged as suspicious by the risk engine",
            "Blocked by risk checks, possible fraudulent card",
        ),
    },
    "checkout": {
        "checkout_friction": (
            "otp_failed",
            "Too many steps in the checkout form",
            "Customer found the address form confusing and dropped off",
            "OTP page kept redirecting back",
            "Page was too slow after the bank redirect",
        ),
        "technical_error": (
            "session_timeout",
            "gateway_error",
            "Checkout page failed to load",
            "Script error on the payment step",
            "Blank screen after clicking pay",
        ),
        "price_sensitivity": (
            "Customer said it was too expensive",
            "Abandoned after seeing the delivery fee",
            "Looking for a discount code before ordering",
            "Shipping charge pushed the total too high",
            "Compared pricing with another store",
        ),
        "payment_method_unavailable": (
            "payment_method_unavailable",
            "UPI was not available at checkout",
            "Cash on delivery had no option on the final page",
            "Netbanking for their bank was missing from the list",
            "EMI was not offered on this order value",
        ),
        "low_purchase_intent": (
            "Customer was just browsing",
            "Said they are not ready to buy yet",
            "Window shopping on mobile",
            "Comparing options across sites",
        ),
    },
    "subscription": {
        "insufficient_funds": (
            "insufficient_funds",
            "Insufficient funds on the mandate debit",
            "Low balance at the time of the renewal charge",
        ),
        "card_expired": (
            "card_expired",
            "Saved card has expired",
            "Expired card on the subscription",
        ),
        "mandate_expired": (
            "mandate_expired",
            "The e-mandate has expired",
            "UPI autopay mandate lapsed last cycle",
        ),
        # The subscription keyword table has no issuer pattern at all, so free
        # text here would fall through to Gemini. Recurring rails emit codes
        # anyway, which is why only the canonical code appears.
        "issuer_declined": ("issuer_declined",),
        "voluntary_churn": (
            "subscription_cancelled",
            "cancelled_by_customer",
            "Customer said they don't want to renew",
        ),
        "mandate_revoked": ("mandate_revoked",),
        "dunning_exhausted": ("retries_exhausted",),
    },
    "receivable": {
        "genuine_delay": (
            "Their finance team says payment will be released next week",
            "Invoice is processing internally, awaiting sign-off",
            "Cash flow tight this month, will pay at end of month",
            "Payment is late but confirmed for the next cycle",
            "Approval pending with their procurement head",
            "Slight delay, treasury runs payments fortnightly",
        ),
        "non_responsive": (
            "no_response",
            "unreachable",
            "Three emails sent, no reply from their AP desk",
            "Email bounced and the phone number is not in service",
            "Contact has been ignoring follow-ups for a month",
        ),
        "payment_dispute": (
            "invoice_disputed",
            "Client disputes the line items on the invoice",
            "They say the amount is wrong - billing error on our side",
        ),
    },
}

#: Reason text that deliberately matches no rule, so the case reaches Gemini.
#:
#: Each entry is `(text, cause_a_model_should_reach)`. Every one of them dodges every
#: `_EXACT_CODES` key and every `_KEYWORD_RULES` regex for its surface — that part is
#: asserted offline before ingestion. What differs is the second element:
#:
#:   * a root cause means the text is REGEX-PROOF BUT ANSWERABLE. A human analyst
#:     would reach that conclusion from the words alone, so a competent model should
#:     too. This is the case the LLM path exists for, and the one worth showing: the
#:     signal is there, it just is not expressible as a pattern.
#:   * `None` means the text is genuinely UNANSWERABLE. There is no defensible root
#:     cause in the surface's vocabulary, and the honest answer is `unknown` at low
#:     confidence, which the decision engine turns into `no_action_low_confidence`.
#:
#: RATIFIED after checkpoint 1: 13 answerable, 3 unanswerable. The first draft made
#: most of them unanswerable, and Gemini correctly returned `unknown` at 0.35 — a
#: right answer to a question with no answer, which demonstrated nothing about the
#: LLM's contribution. The expected cause is an EXPECTATION, never asserted: the
#: model's classification is the model's, and a disagreement is reported, not failed.
AMBIGUOUS: dict[str, tuple[tuple[str | None, str | None], ...]] = {
    "payment": (
        # Unanswerable, kept deliberately. "The bank messaged them but the money
        # never moved" fits insufficient_funds, issuer_declined and a plain hold
        # equally well. Measured at checkpoint 1: unknown @ 0.35.
        (
            "Customer says the bank sent them a message about the transaction but "
            "the money never left their account.",
            None,
        ),
        # insufficient_funds without the words. "low/insufficient funds" and "not
        # enough money" are both patterns; "balance was short" is neither.
        (
            "Their account balance was short of the transaction value when we "
            "attempted it, and they mentioned salary credits land on the 3rd.",
            "insufficient_funds",
        ),
        # card_expired without ever saying "card" — the regex needs `card` adjacent
        # to `expired`, so removing the noun removes the pattern entirely.
        (
            "The saved instrument on file reached the end of its validity last "
            "month and they have not added a new one.",
            "card_expired",
        ),
        # issuer_declined without `issuer` or `declined`. "issuing side" does not
        # match `\bissuer\b`, and no decline verb appears at all.
        (
            "The issuing side sent back a permanent instruction not to attempt this "
            "instrument again until the cardholder contacts them.",
            "issuer_declined",
        ),
        # temporary_processing_error without `temporar`, `timeout`, or a
        # gateway/network/server noun next to error/failure.
        (
            "Our processor was mid-deployment when this went through and the "
            "acknowledgement never came back; the next attempt on the same rail "
            "worked.",
            "temporary_processing_error",
        ),
        # Unanswerable, kept deliberately. No code, no symptom, no history beyond
        # "it happened again".
        (
            "Repeat failure on the same instrument, no code returned by the "
            "processor.",
            None,
        ),
        (
            "The amount was held against their balance and then released without "
            "settling; they said the account is close to its limit this week.",
            "insufficient_funds",
        ),
    ),
    "checkout": (
        # Unanswerable, kept deliberately. An abandoned cart with no failure text is
        # the commonest record in any checkout log and there is nothing to reason
        # from. `classify()` returns None on empty text before it looks at anything.
        (None, None),
        # checkout_friction without `form`, `too many steps`, `confusing`, `slow` or
        # `redirect`.
        (
            "They filled in delivery details three times because the page kept "
            "resetting the state field, then gave up.",
            "checkout_friction",
        ),
        # technical_error without `error`, `crash`, `broke`, `blank screen` or
        # `failed to load`. The desktop-worked contrast is the signal.
        (
            "On mobile the confirm button did nothing on two separate visits; the "
            "same cart completed fine on desktop.",
            "technical_error",
        ),
        # payment_method_unavailable without any of the method nouns the regex
        # watches for (upi, netbanking, wallet, emi, cod), so the pattern cannot fire
        # even though the meaning is unmistakable.
        (
            "They wanted to pay by bank transfer from their corporate account and we "
            "only offer cards at this checkout.",
            "payment_method_unavailable",
        ),
    ),
    "subscription": (
        # mandate_expired without `mandate`. The subscription table keys on that
        # noun, and "standing instruction" is the same thing in different words.
        (
            "The standing instruction we hold for this customer reached its end date "
            "before this cycle was raised.",
            "mandate_expired",
        ),
        (
            "Third cycle in a row where their account did not hold the amount on the "
            "debit date; each earlier cycle cleared a day or two afterwards.",
            "insufficient_funds",
        ),
    ),
    "receivable": (
        # Left exactly as first drafted. Measured directly against Gemini at
        # checkpoint 1: genuine_delay @ 0.90, which is what this entry is for.
        (
            "Their accounts team says the purchase order number on our side does not "
            "match what procurement raised, so it is sitting with someone for "
            "sign-off.",
            "genuine_delay",
        ),
        # non_responsive without `no response`, `not respond`, `no reply`,
        # `unreachable`, `ignoring` or `bounced`.
        (
            "Vendor portal shows the invoice as received but our contact changed "
            "roles, nobody has been assigned to it since, and four messages to the "
            "shared AP inbox have gone unanswered.",
            "non_responsive",
        ),
        # genuine_delay without `delay`, `late`, `next week`, `end of month`,
        # `will pay`, `cash flow` or `approval pending`.
        (
            "Their finance controller signed it off but the payment run only "
            "executes on the 25th, so it sits until then.",
            "genuine_delay",
        ),
    ),
}

# ---------------------------------------------------------------------------
# Customer references.
# ---------------------------------------------------------------------------

_GIVEN = (
    "aarav", "diya", "vihaan", "ananya", "arjun", "ishita", "kabir", "meera",
    "rohan", "saanvi", "aditya", "priya", "nikhil", "tara", "yash", "kavya",
    "rahul", "neha", "siddharth", "aisha", "manav", "ritika", "farhan", "sneha",
    "gaurav", "pooja", "imran", "divya", "karthik", "lakshmi", "varun", "shreya",
    "abhishek", "nandini", "harsh", "juhi", "omkar", "swati", "tanmay", "bhavna",
)
_SURNAME = (
    "sharma", "reddy", "iyer", "menon", "patel", "nair", "gupta", "bose",
    "kulkarni", "chatterjee", "desai", "rao", "singh", "joshi", "mehta", "das",
    "pillai", "shetty", "banerjee", "verma",
)
_COMPANY = (
    "northline", "quaystone", "meridianlabs", "tatvasoft", "bluepeak", "arcwell",
    "sunderlogistics", "vaayu", "krishaindustries", "orbitmedia", "finchpay",
    "greenfold", "castlerock", "novadesk", "trilokretail", "penumbra",
    "shaktifoods", "clearbridge", "urbannest", "zephyrtech", "amberworks",
    "coastalpack", "helixprint", "silverline", "brightmill", "junipercorp",
    "westbayfoods", "ironvale", "lumenhealth", "sarathitextiles",
)

BUSINESS_SURFACES = frozenset({"receivable"})


def _consumer_ref(rng: random.Random, taken: set[str]) -> str:
    """A distinct consumer-side customer reference."""
    while True:
        ref = (
            f"cust_{rng.choice(_GIVEN)}_{rng.choice(_SURNAME)}"
            f"_{rng.randint(100, 999)}"
        )
        if ref not in taken:
            taken.add(ref)
            return ref


def _business_ref(rng: random.Random, taken: set[str]) -> str:
    """A distinct business-side customer reference, for receivables."""
    while True:
        ref = f"acct_{rng.choice(_COMPANY)}_{rng.randint(10, 99)}"
        if ref not in taken:
            taken.add(ref)
            return ref


# ---------------------------------------------------------------------------
# Amounts.
# ---------------------------------------------------------------------------


def _amount(rng: random.Random, lo: float, hi: float) -> float:
    """Draw a log-uniform amount inside [lo, hi] and strip the roundness off it.

    Log-uniform rather than uniform because real transaction amounts cluster low
    and tail high — a uniform draw over ₹5,000–150,000 would put half the
    receivables above ₹77,500, which no invoice book looks like.

    The de-rounding forces a non-zero rupee unit digit, so nothing lands on a
    suspiciously round hundred. Paise are zero on a share of amounts, because
    whole-rupee totals are ordinary; what is not ordinary is every amount being a
    multiple of 500.
    """
    raw = math.exp(rng.uniform(math.log(lo), math.log(hi)))
    rupees = int(raw)
    if rupees % 10 == 0:
        rupees += rng.randint(1, 9)
    paise = 0 if rng.random() < WHOLE_RUPEE_SHARE else rng.choice(PAISE)
    return round(rupees + paise / 100.0, 2)


# ---------------------------------------------------------------------------
# Generation.
# ---------------------------------------------------------------------------

#: Roles assigned to specific events after amounts are known. These drive Parts
#: B.5–B.7 and are recorded here so the plan is inspectable before it is run.
ROLE_OPT_OUT = "optout_blocked"
ROLE_PTP_HONORED = "ptp_honored"
ROLE_PTP_SUPPRESSED = "ptp_broken_followup_suppressed"
ROLE_PTP_REEVALUATING = "ptp_broken_then_followup"
ROLE_PTP_PROMISED = "ptp_still_promised"


def generate() -> list[dict[str, Any]]:
    """Build all 200 event specs. Deterministic for a fixed `SEED`."""
    rng = random.Random(SEED)
    now = datetime.now(timezone.utc)
    taken: set[str] = set()
    specs: list[dict[str, Any]] = []
    index = 0

    for surface in ("payment", "checkout", "subscription", "receivable"):
        # Build the surface's root-cause slots, then its amount bands, then pair
        # them by shuffling. Pairing by shuffle rather than by construction is
        # what stops root cause and amount from being correlated by accident.
        causes: list[str | None] = []
        for cause, count in COMPOSITION[surface].items():
            causes.extend([cause] * count)

        bands: list[str] = []
        for band, (count, _lo, _hi) in BANDS[surface].items():
            bands.extend([band] * count)
        assert len(causes) == len(bands) == sum(COMPOSITION[surface].values())

        # Receivables are paired deliberately, not randomly: the 6 carve-out
        # invoices must land on genuine_delay and non_responsive, because those
        # are the only receivable causes the matrix maps to an actionable
        # intervention. A carve-out invoice that landed on payment_dispute would
        # be spent on a no-action decision.
        if surface == "receivable":
            small = ["genuine_delay"] * 4 + ["non_responsive"] * 2
            remaining = list(causes)
            for cause in small:
                remaining.remove(cause)
            rng.shuffle(remaining)
            causes = small + remaining
            bands = ["auto"] * 6 + ["review"] * 24
        else:
            rng.shuffle(causes)
            rng.shuffle(bands)

        ambiguous_pool = list(AMBIGUOUS[surface])
        used_ambiguous = 0

        for cause, band in zip(causes, bands):
            index += 1
            count, lo, hi = BANDS[surface][band]
            amount = _amount(rng, lo, hi)

            llm_expected: str | None = None
            if cause is None:
                reason, llm_expected = ambiguous_pool[used_ambiguous]
                used_ambiguous += 1
            else:
                reason = rng.choice(REASONS[surface][cause])

            if surface in BUSINESS_SURFACES:
                customer = _business_ref(rng, taken)
            else:
                customer = _consumer_ref(rng, taken)

            specs.append(
                {
                    "event_id": f"{PREFIX}_{index:03d}_{SURFACE_SUFFIX[surface]}",
                    "surface": surface,
                    "amount": amount,
                    "currency": "INR",
                    "raw_failure_reason": reason,
                    "customer_ref": customer,
                    "created_at": (
                        now
                        - timedelta(
                            days=rng.uniform(0.0, float(BACKDATE_DAYS)),
                        )
                    ).isoformat(),
                    "_intended_root_cause": cause,
                    "_expects_llm": cause is None,
                    # What a careful analyst would conclude from an ambiguous string,
                    # for LLM-path events only. An EXPECTATION for reporting, never
                    # asserted — the model's classification is the model's. `None`
                    # here on an LLM-path event means the text is deliberately
                    # unanswerable and `unknown` is the correct answer.
                    "_llm_expected_cause": llm_expected,
                    "_band": band,
                    "_opted_out": False,
                    "_hold_from_authorize": False,
                    "_role": None,
                }
            )

        assert used_ambiguous == COMPOSITION[surface][None]

    assert len(specs) == 200, len(specs)

    _force_erv_floor_case(specs)
    _assign_repeat_customers(specs, rng)
    _assign_roles(specs, rng, now)
    return specs


def _force_erv_floor_case(specs: list[dict[str, Any]]) -> None:
    """Place one cart just under the ERV floor, on purpose.

    checkout/low_purchase_intent carries a 0.05 recovery probability against a
    ₹5 link, so the floor is cleared only above ₹600. A ₹519.75 cart scores
    ₹20.99 and is refused with `erv_below_minimum` — the system declining to
    spend ₹5 chasing a browser. That is a story worth having in the data, and
    leaving it to chance would risk not having it.
    """
    for spec in specs:
        if (
            spec["surface"] == "checkout"
            and spec["_intended_root_cause"] == "low_purchase_intent"
            and spec["_band"] == "auto"
        ):
            spec["amount"] = 519.75
            spec["_note"] = "placed below the ERV floor deliberately"
            return
    raise AssertionError("no auto-band low_purchase_intent cart to place")


def _assign_repeat_customers(specs: list[dict[str, Any]], rng: random.Random) -> None:
    """Collapse 22 events onto 10 customers, leaving 188 distinct references.

    Repeat at-risk customers are real and they matter downstream: a second event
    for the same reference makes `count_prior_events` non-zero, which puts history
    evidence into a genuine diagnosis. Most of a merchant's book is still distinct
    customers, so this stays small.

    Consumer references are only reused across consumer surfaces and business
    references only across receivables — a person does not owe an invoice.
    """
    consumer = [s for s in specs if s["surface"] not in BUSINESS_SURFACES]
    business = [s for s in specs if s["surface"] in BUSINESS_SURFACES]
    rng.shuffle(consumer)
    rng.shuffle(business)

    # 8 customers with 2 events, 2 customers with 3 events.
    groups: list[tuple[list[dict[str, Any]], int]] = []
    cursor = 0
    for size in (3, 2, 2, 2, 2, 2, 2):  # 7 consumer groups: 3+2*6 = 15 events
        groups.append((consumer[cursor : cursor + size], size))
        cursor += size
    bcursor = 0
    for size in (3, 2, 2):  # 3 business groups: 3+2+2 = 7 events
        groups.append((business[bcursor : bcursor + size], size))
        bcursor += size

    for members, size in groups:
        assert len(members) == size
        anchor = members[0]["customer_ref"]
        for member in members[1:]:
            member["customer_ref"] = anchor
            member["_repeat_customer"] = True
        members[0]["_repeat_customer"] = True


def _assign_roles(
    specs: list[dict[str, Any]], rng: random.Random, now: datetime
) -> None:
    """Tag the specific events that carry Parts B.5–B.7.

    Chosen by first match in event-id order rather than at random, so the roster
    is reproducible and can be read off the dry run and checked by hand.
    """
    by_id = sorted(specs, key=lambda s: s["event_id"])

    def take(
        predicate, role: str, count: int, **extra: Any
    ) -> list[dict[str, Any]]:
        picked: list[dict[str, Any]] = []
        for spec in by_id:
            if len(picked) == count:
                break
            if spec["_role"] is not None or not predicate(spec):
                continue
            spec["_role"] = role
            spec.update(extra)
            picked.append(spec)
        if len(picked) != count:
            raise AssertionError(
                f"needed {count} events for role {role!r}, found {len(picked)}"
            )
        return picked

    def auto(surface: str, cause: str):
        return (
            lambda s: s["surface"] == surface
            and s["_intended_root_cause"] == cause
            and s["_band"] == "auto"
        )

    # --- Opt-out, 3 events on 3 surfaces with 3 different contact types -----
    # An opt-out only stops a CONTACT intervention, so each of these must be an
    # event whose decision contacts the customer and which would otherwise be
    # authorized. A retry would sail straight through and demonstrate nothing.
    optouts = [
        *take(auto("receivable", "genuine_delay"), ROLE_OPT_OUT, 1),
        *take(auto("checkout", "technical_error"), ROLE_OPT_OUT, 1),
        *take(auto("payment", "card_expired"), ROLE_OPT_OUT, 1),
    ]
    for spec in optouts:
        spec["_opted_out"] = True

    # --- Promise-to-pay: honored ------------------------------------------
    # An honored promise needs a recovered VerificationRecord, which needs a paid
    # payment link. Receivables never get one — their matrix rows map only to
    # contact interventions — so these sit on link-bearing surfaces.
    for surface, cause in (
        ("payment", "card_expired"),
        ("subscription", "mandate_expired"),
        ("payment", "insufficient_funds"),
        ("checkout", "technical_error"),
    ):
        take(
            auto(surface, cause),
            ROLE_PTP_HONORED,
            1,
            _promised_in_days=rng.randint(1, 3),
        )

    # --- Promise-to-pay: broken, follow-up suppressed by policy -------------
    # These are authorized and executed in Part B.5. By the time the promise
    # lapses, the 24h cooldown is running from that execution, so the follow-up
    # is correctly refused and the promise stays broken. The suppression is the
    # point: PTP does not get its own ungated messaging path.
    take(
        auto("receivable", "genuine_delay"),
        ROLE_PTP_SUPPRESSED,
        2,
        _promised_in_days=-rng.randint(2, 5),
    )

    # --- Promise-to-pay: broken, then a follow-up that does go out ---------
    # Held back from the blanket authorize in Part B.4, so their first policy
    # verdict is the one the follow-up asks for. Authorizing them up front would
    # start the cooldown against a reservation and make `reevaluating`
    # unreachable for the whole batch.
    held = take(
        lambda s: s["surface"] == "receivable"
        and s["_band"] == "auto"
        and s["_intended_root_cause"] in ("genuine_delay", "non_responsive"),
        ROLE_PTP_REEVALUATING,
        3,
        _promised_in_days=-rng.randint(2, 6),
    )
    for spec in held:
        spec["_hold_from_authorize"] = True

    # --- Promise-to-pay: still open ---------------------------------------
    # Future dates on review-tier invoices. Checking these proves the deadline
    # branch without resolving them.
    take(
        lambda s: s["surface"] == "receivable"
        and s["_band"] == "review"
        and s["_intended_root_cause"] == "genuine_delay",
        ROLE_PTP_PROMISED,
        2,
        _promised_in_days=rng.randint(3, 6),
    )


API_FIELDS = (
    "event_id",
    "surface",
    "amount",
    "currency",
    "raw_failure_reason",
    "customer_ref",
    "created_at",
)


def api_body(spec: dict[str, Any]) -> dict[str, Any]:
    """Strip planning metadata, leaving exactly the `POST /events` contract.

    `status` is deliberately absent: ingestion sets it via `$setOnInsert` and an
    event that arrived with its own lifecycle state would be a way around that.
    """
    return {key: spec[key] for key in API_FIELDS}


def opted_out_refs(specs: list[dict[str, Any]]) -> list[str]:
    """Customer references this batch needs on the do-not-contact list."""
    return sorted({spec["customer_ref"] for spec in specs if spec["_opted_out"]})


def held_back_ids(specs: list[dict[str, Any]]) -> list[str]:
    """Events to skip in the blanket authorize, so a follow-up can authorize them."""
    return sorted(spec["event_id"] for spec in specs if spec["_hold_from_authorize"])


def roles(specs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group the role-carrying events by role, for the phase scripts to read."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for spec in specs:
        if spec["_role"] is not None:
            grouped.setdefault(spec["_role"], []).append(spec)
    return grouped
