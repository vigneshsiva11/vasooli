"""The rulebook: a fingerprinted snapshot of every ratified policy parameter.

An append-only verdict log is only auditable if each record can be reproduced.
Until now reproduction ran against whatever parameters sit in `app/policy/rules.py`
*today*, which quietly assumes the rulebook never changed. It has changed twice —
both payment links were ratified as contact-type — and each time the audit could
only report that some old verdicts no longer re-derive word for word, without being
able to say what rulebook had actually judged them.

A fingerprint closes that. Every verdict records a short hash of the exact
parameter set in force when it was evaluated, and the superseded parameter sets are
archived below, so an old verdict can be re-derived under the rules that produced
it rather than against the present.

What is hashed is every parameter the engine can read on the way to a conclusion,
not only the numbers in `rules.py`:

* the economics — the ERV floor and its zero-cost exemption;
* the autonomy tiers, and the currency they are compared in;
* the contact set, the cap, the cooldown, and what the cooldown is measured from;
* `NO_ACTION_INTERVENTIONS`, which `decision_is_actionable` tests against;
* `POLICY_CHECKS`, the trail contract itself;
* `REASON_PRECEDENCE`, which decides which failure speaks for the verdict, and
  `REASON_VERDICT`, which decides whether a failure blocks or routes for review.

The last four live in `app/models`, not in `rules.py`, but they are ratified policy
just as much as the numbers are. A fingerprint that omitted them would report "same
rulebook" for two rulebooks that disagree about whether a refusal can be overridden
by a human, which is the most consequential disagreement in the whole stage. This
scope — the full causal set rather than only the five parameters the brief named — is
ratified, so a future reader should treat a field's presence here as deliberate.

Two hashed values were declarative when this module was written: nothing branched on
`tier_currency` or `cooldown_measured_from`. They were included anyway, so that Stage
5 re-pointing the cooldown at a real send time would register as a new rulebook
instead of silently changing what every stored cooldown check meant. That has now
happened: `cooldown_measured_from` moved from `verdict.evaluated_at` to
`execution.executed_at`, the fingerprint changed accordingly, and both
`app/policy/store.py` and `scripts/s4_replay.py` branch on it so an old verdict is
still re-derived under the anchor that judged it. `tier_currency` remains
declarative. Including a field that turns out not to matter makes the fingerprint
over-sensitive, which is the safe direction of error: a false "different rulebook"
gets investigated, a false "same rulebook" gets believed.

A `Rulebook` is also where the parameter-dependent predicates live, so the engine
can be handed one and consult it. That is what makes re-deriving under a historical
rulebook honest rather than half-applied — see `app/policy/engine.py`.

The fingerprint's *shape* is declared in `app/models/policy.py`, beside the field
that validates it, and imported here to build one. Same reason `format_check` sits
next to the pattern it satisfies: the producer and the validator read the same
constants, so they cannot drift apart.

Nothing in this module performs I/O, calls an LLM, or executes anything.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields
from typing import Literal

from app.models.policy import FINGERPRINT_DIGEST_CHARS, FINGERPRINT_SCHEME

AutonomyTier = Literal["auto", "approval_required", "never_auto"]

# ---------------------------------------------------------------------------
# What the contact cooldown is measured from.
# ---------------------------------------------------------------------------
#
# Named constants rather than bare strings, because as of Stage 5 this field is no
# longer declarative: `app/policy/store.py` and `scripts/s4_replay.py` both branch on
# it to decide which timestamp anchors the cooldown. A typo in one of those literals
# would silently select the other behaviour, and the fingerprint would happily record
# the typo as a distinct rulebook.

#: The original meaning: measured from when permission was granted. Stage 4 had no
#: send timestamp to read, so this was the best available proxy.
COOLDOWN_FROM_VERDICT = "verdict.evaluated_at"

#: The current meaning: measured from `ExecutionRecord.executed_at`, the real send
#: time. A verdict that was authorized but never executed still counts against the
#: cap — as a reservation, anchored at `evaluated_at` — because permission to contact
#: somebody has been issued and may yet be used. Only an execution that *failed*
#: releases both. See `app/policy/store.py:prior_authorized_contacts`.
COOLDOWN_FROM_EXECUTION = "execution.executed_at"

COOLDOWN_ANCHORS: frozenset[str] = frozenset(
    {COOLDOWN_FROM_VERDICT, COOLDOWN_FROM_EXECUTION}
)


@dataclass(frozen=True)
class Rulebook:
    """Every ratified policy parameter, as one immutable value.

    Frozen because a rulebook is a historical fact once a verdict has been judged
    under it. Amending policy produces a *different* rulebook with a different
    fingerprint; it does not modify this one.
    """

    # -- Economics ----------------------------------------------------------
    minimum_erv: float
    zero_cost_exempt_from_erv_floor: bool

    # -- Autonomy -----------------------------------------------------------
    auto_authorize_below: float
    never_auto_at_or_above: float
    tier_currency: str

    # -- Customer protection ------------------------------------------------
    contact_interventions: frozenset[str]
    max_contacts_per_event: int
    cooldown_hours: int
    cooldown_measured_from: str

    # -- Tables owned by `app/models`, ratified all the same -----------------
    no_action_interventions: frozenset[str]
    policy_checks: tuple[str, ...]
    reason_precedence: tuple[str, ...]
    #: `REASON_VERDICT` as sorted pairs: a dict is neither hashable nor ordered
    #: in a way a digest can rely on.
    reason_verdict: tuple[tuple[str, str], ...]

    #: Human note on what this rulebook was and when. Documentation only —
    #: excluded from the fingerprint, so rewording an archive entry cannot change
    #: the identity of the rulebook it describes.
    note: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        """Reject a rulebook that is structurally impossible.

        Only invariants that have held for every rulebook that ever existed, so an
        archive entry with a typo is caught while a future re-ratification of any
        individual value stays expressible.
        """
        problems: list[str] = []

        if self.never_auto_at_or_above <= self.auto_authorize_below:
            problems.append(
                f"never_auto_at_or_above ({self.never_auto_at_or_above}) must "
                f"exceed auto_authorize_below ({self.auto_authorize_below})"
            )
        if self.max_contacts_per_event < 1:
            problems.append(
                f"max_contacts_per_event must be at least 1 "
                f"(got {self.max_contacts_per_event})"
            )
        if self.cooldown_hours < 0:
            problems.append(f"cooldown_hours must not be negative ({self.cooldown_hours})")
        if not self.contact_interventions:
            problems.append("contact_interventions is empty")
        if self.cooldown_measured_from not in COOLDOWN_ANCHORS:
            problems.append(
                f"cooldown_measured_from {self.cooldown_measured_from!r} is not one "
                f"of {sorted(COOLDOWN_ANCHORS)}; the field selects behaviour now, so "
                "an unrecognised value would be read as 'the other one'"
            )
        overlap = self.contact_interventions & self.no_action_interventions
        if overlap:
            problems.append(
                f"contact_interventions and no_action_interventions overlap: "
                f"{sorted(overlap)}"
            )
        if problems:
            raise ValueError(
                f"Rulebook {self.note or '<unnamed>'!r} is inconsistent:\n  - "
                + "\n  - ".join(problems)
            )

    # -- The parameter-dependent predicates ---------------------------------
    #
    # These live on the rulebook rather than as module functions reading globals,
    # so that asking "was this a contact under the rules of the time?" is a
    # question you can only ask of a specific rulebook.

    def is_contact(self, intervention: str) -> bool:
        """Whether the intervention puts a message in front of a customer."""
        return intervention in self.contact_interventions

    def is_no_action(self, intervention: str) -> bool:
        """Whether the intervention is one of the ways of doing nothing."""
        return intervention in self.no_action_interventions

    def tier_for(self, amount: float) -> AutonomyTier:
        """Return the autonomy tier for an amount at risk.

        Boundaries are half-open and deliberately asymmetric:
        `auto_authorize_below` is exclusive and `never_auto_at_or_above` inclusive,
        so an amount landing exactly on a threshold falls to the cautious side.
        """
        if amount >= self.never_auto_at_or_above:
            return "never_auto"
        if amount < self.auto_authorize_below:
            return "auto"
        return "approval_required"

    def erv_floor_applies(self, estimated_cost: float) -> bool:
        """Whether the minimum-ERV floor applies to an action of this cost."""
        if self.zero_cost_exempt_from_erv_floor and estimated_cost <= 0:
            return False
        return True

    def verdict_for(self, reason: str) -> str:
        """The only verdict this rulebook lets a reason carry."""
        return dict(self.reason_verdict)[reason]

    # -- Identity -----------------------------------------------------------

    @property
    def fingerprint(self) -> str:
        """This rulebook's fingerprint."""
        return fingerprint_of(self)

    def differences_from(self, other: "Rulebook") -> list[str]:
        """Names of the hashed fields on which two rulebooks disagree.

        The readable half of the fingerprint: the hash says *whether* two rulebooks
        differ, this says *where*, which is what a report needs.
        """
        return [
            name
            for name in HASHED_FIELDS
            if getattr(self, name) != getattr(other, name)
        ]


#: Fields that contribute to the fingerprint — every field except `note`. Derived
#: from the dataclass rather than typed out again, so adding a parameter to
#: `Rulebook` cannot leave it silently outside the hash.
HASHED_FIELDS: tuple[str, ...] = tuple(
    f.name for f in fields(Rulebook) if f.name != "note"
)


def _declared_type(name: str) -> str:
    """The annotation `Rulebook` declares for a field, as a plain name.

    Numbers are normalised by what the rulebook *says* a parameter is, not by what
    happened to be passed in. A dataclass does not coerce, so `minimum_erv=25`
    stores an int and `minimum_erv=25.0` stores a float; the two rulebooks are equal
    as values and must not be able to hash differently.

    `from __future__ import annotations` makes these strings, but the fallback keeps
    this correct if that ever changes.
    """
    annotation = _ANNOTATIONS[name]
    return annotation if isinstance(annotation, str) else annotation.__name__


_ANNOTATIONS = {f.name: f.type for f in fields(Rulebook)}


def canonical_form(rulebook: Rulebook) -> str:
    """Render a rulebook as the exact string that gets hashed.

    Exposed rather than inlined because a fingerprint nobody can print is a
    fingerprint nobody can check. Everything is normalised so that two rulebooks
    which are equal as values hash identically regardless of how they were built:
    sets are sorted, tuples become lists, and numbers are rendered according to
    their declared type, so `25` and `25.0` cannot produce different digests.
    """
    payload: dict[str, object] = {}
    for name in HASHED_FIELDS:
        value = getattr(rulebook, name)
        declared = _declared_type(name)
        if isinstance(value, frozenset | set):
            payload[name] = sorted(value)
        elif isinstance(value, tuple):
            payload[name] = [
                list(item) if isinstance(item, tuple) else item for item in value
            ]
        elif declared == "bool":
            # Before the numeric branches: bool is a subclass of int.
            payload[name] = bool(value)
        elif declared == "float":
            payload[name] = repr(float(value))
        elif declared == "int":
            payload[name] = int(value)
        else:
            payload[name] = value
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def fingerprint_of(rulebook: Rulebook) -> str:
    """Return the fingerprint of a rulebook: `rb1_` and 16 hex digits."""
    digest = hashlib.sha256(canonical_form(rulebook).encode("utf-8")).hexdigest()
    return f"{FINGERPRINT_SCHEME}_{digest[:FINGERPRINT_DIGEST_CHARS]}"


# ---------------------------------------------------------------------------
# The archive of superseded rulebooks.
# ---------------------------------------------------------------------------
#
# Spelled out as literals rather than derived from `app.policy.rules`, and this is
# the whole point: an archive that follows the present is not an archive. If the ERV
# floor is re-ratified tomorrow, these entries must keep the value they were
# actually evaluated under, or the fingerprints they claim to identify would change
# retroactively and the history they exist to explain would evaporate.
#
# Getting a literal wrong degrades visibly rather than silently: the backfill only
# stamps a historical fingerprint on a verdict that re-derives under it exactly, so
# a wrong entry matches nothing and is reported as unidentified.

#: The contact set as the Stage 4 brief originally defined it.
_CONTACT_SET_AT_LAUNCH = frozenset(
    {"reminder", "escalating_reminder_sequence", "manual_escalation"}
)

#: After `payment_method_update_link` was ratified as contact-type.
_CONTACT_SET_AFTER_FIRST_AMENDMENT = _CONTACT_SET_AT_LAUNCH | {
    "payment_method_update_link"
}

#: After `recovery_payment_link` followed on the same reasoning. Still the set in
#: force; the third archive entry differs from today's rulebook only in where the
#: cooldown is measured from.
_CONTACT_SET_AFTER_SECOND_AMENDMENT = _CONTACT_SET_AFTER_FIRST_AMENDMENT | {
    "recovery_payment_link"
}


def _rulebook_as_ratified_at_stage_4_launch(
    *, contact_interventions: frozenset[str], note: str
) -> Rulebook:
    """A rulebook with the Stage 4 launch parameters and a varying contact set.

    Every amendment through the end of Stage 4 changed only which interventions
    count as contacting somebody; every other value below is as first ratified,
    including `cooldown_measured_from`, which stayed on the verdict timestamp for the
    whole of Stage 4 because there was no send timestamp to point at. Keeping the
    shared literals in one place means the three archive entries differ in exactly
    the field that actually differed.
    """
    return Rulebook(
        minimum_erv=25.0,
        zero_cost_exempt_from_erv_floor=True,
        auto_authorize_below=5_000.0,
        never_auto_at_or_above=25_000.0,
        tier_currency="INR",
        contact_interventions=contact_interventions,
        max_contacts_per_event=3,
        cooldown_hours=24,
        cooldown_measured_from=COOLDOWN_FROM_VERDICT,
        no_action_interventions=frozenset(
            {"no_action", "no_action_low_confidence", "no_action_negative_erv"}
        ),
        policy_checks=(
            "decision_is_actionable",
            "customer_opt_out",
            "contact_cap",
            "contact_cooldown",
            "erv_minimum",
            "amount_tier",
        ),
        reason_precedence=(
            "no_action_recommended",
            "customer_opted_out",
            "contact_cap_exceeded",
            "cooldown_active",
            "erv_below_minimum",
            "amount_never_auto",
            "amount_requires_approval",
        ),
        reason_verdict=(
            ("amount_never_auto", "requires_manual_review"),
            ("amount_requires_approval", "requires_manual_review"),
            ("contact_cap_exceeded", "blocked"),
            ("cooldown_active", "blocked"),
            ("customer_opted_out", "blocked"),
            ("erv_below_minimum", "blocked"),
            ("no_action_recommended", "blocked"),
            ("ok", "authorized"),
        ),
        note=note,
    )


#: Every rulebook that has been in force and is no longer, oldest first.
SUPERSEDED_RULEBOOKS: tuple[Rulebook, ...] = (
    _rulebook_as_ratified_at_stage_4_launch(
        contact_interventions=_CONTACT_SET_AT_LAUNCH,
        note=(
            "Stage 4 as first ratified: only reminder, escalating_reminder_sequence "
            "and manual_escalation counted as contacting the customer, so neither "
            "payment link was gated by consent or consumed a contact"
        ),
    ),
    _rulebook_as_ratified_at_stage_4_launch(
        contact_interventions=_CONTACT_SET_AFTER_FIRST_AMENDMENT,
        note=(
            "After payment_method_update_link was ratified as contact-type, before "
            "recovery_payment_link followed on the same reasoning"
        ),
    ),
    _rulebook_as_ratified_at_stage_4_launch(
        contact_interventions=_CONTACT_SET_AFTER_SECOND_AMENDMENT,
        note=(
            "All of Stage 4 after both payment links were ratified as contact-type: "
            "the contact set as it still stands, but with the cooldown measured from "
            "the verdict's evaluated_at, because Stage 4 sent nothing and had no real "
            "send timestamp to measure from. Superseded by Stage 5 re-pointing the "
            "cooldown at ExecutionRecord.executed_at"
        ),
    ),
)
