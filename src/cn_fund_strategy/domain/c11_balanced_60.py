"""Public, deterministic policy core for the fixed 60/40 baseline and C11.

This module deliberately stops at research-policy calculations.  It does not
load market data, inspect an account, select a purchasable share class, or
submit an order.  Historical replay remains an evidence artifact rather than
an implicit promise about future returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import Enum
from typing import Mapping, Sequence


ASSET_ORDER = (
    "022430",
    "006729",
    "021550",
    "006662",
    "012773",
    "007169",
    "CASH",
)
EQUITY_ASSETS = ("022430", "006729", "021550")
ULTRASHORT_BOND_ASSETS = ("006662", "012773")
POLICY_BANK_BOND_ASSET = "007169"

ZERO = Decimal("0")
ONE = Decimal("1")
TWO = Decimal("2")
WEIGHT_TOLERANCE = Decimal("0.000000000001")

VALUATION_LOOKBACK_SESSIONS = 2520
VALUATION_MINIMUM_HISTORY_SESSIONS = 1260
UNDERVALUED_MAXIMUM = Decimal("0.30")
OVERVALUED_MINIMUM = Decimal("0.70")
ORDINARY_DRIFT_TRIGGER = Decimal("0.05")
MARKED_STRESS_MAINTENANCE_TRIGGER = Decimal("0.44")
HARD_STRESS_RESEARCH_LIMIT = Decimal("0.45")

STANDARD_64_TARGET = (
    ("022430", Decimal("0.35")),
    ("006729", Decimal("0.10")),
    ("021550", Decimal("0.15")),
    ("006662", Decimal("0.05")),
    ("012773", Decimal("0.05")),
    ("007169", Decimal("0.20")),
    ("CASH", Decimal("0.10")),
)


class C11PolicyError(ValueError):
    """Raised when a public C11 calculation receives invalid input."""


class C11State(str, Enum):
    B60 = "B60"
    M60 = "M60"
    D60 = "D60"


_C11_TARGETS: Mapping[C11State, tuple[tuple[str, Decimal], ...]] = {
    C11State.B60: (
        ("022430", Decimal("0.10")),
        ("006729", Decimal("0.25")),
        ("021550", Decimal("0.25")),
        ("006662", Decimal("0.05")),
        ("012773", Decimal("0.05")),
        ("007169", Decimal("0.20")),
        ("CASH", Decimal("0.10")),
    ),
    C11State.M60: (
        ("022430", Decimal("0.10")),
        ("006729", Decimal("0.40")),
        ("021550", Decimal("0.10")),
        ("006662", Decimal("0.05")),
        ("012773", Decimal("0.05")),
        ("007169", Decimal("0.20")),
        ("CASH", Decimal("0.10")),
    ),
    C11State.D60: (
        ("022430", Decimal("0.10")),
        ("006729", Decimal("0.10")),
        ("021550", Decimal("0.40")),
        ("006662", Decimal("0.05")),
        ("012773", Decimal("0.05")),
        ("007169", Decimal("0.20")),
        ("CASH", Decimal("0.10")),
    ),
}

_STRESS_LOSS = {
    "022430": Decimal("0.60"),
    "006729": Decimal("0.60"),
    "021550": Decimal("0.60"),
    "006662": Decimal("0.03"),
    "012773": Decimal("0.03"),
    "007169": Decimal("0.05"),
    "CASH": ZERO,
}


@dataclass(frozen=True, slots=True)
class C11Decision:
    previous_state: C11State
    desired_state: C11State
    state_changed: bool
    pe_percentile: Decimal | None
    pb_percentile: Decimal | None
    valuation_composite: Decimal | None
    relative_trend: Decimal | None
    signal_complete: bool
    reason_codes: tuple[str, ...]
    target_weights: tuple[tuple[str, Decimal], ...]

    def target_mapping(self) -> dict[str, Decimal]:
        return dict(self.target_weights)


@dataclass(frozen=True, slots=True)
class MaintenanceAssessment:
    maximum_absolute_gap: Decimal
    marked_static_stress: Decimal
    ordinary_drift_triggered: bool
    stress_maintenance_triggered: bool
    reason_codes: tuple[str, ...]


def _validate_target_rows(
    rows: Sequence[tuple[str, Decimal]],
) -> tuple[tuple[str, Decimal], ...]:
    result = tuple(rows)
    if tuple(asset for asset, _ in result) != ASSET_ORDER:
        raise C11PolicyError("asset identity or order mismatch")
    if any(type(weight) is not Decimal or not weight.is_finite() for _, weight in result):
        raise C11PolicyError("weights must be finite Decimal values")
    if any(weight < ZERO for _, weight in result):
        raise C11PolicyError("long-only weights cannot be negative")
    if sum((weight for _, weight in result), ZERO) != ONE:
        raise C11PolicyError("target weights must sum exactly to one")
    return result


def standard_64_target() -> tuple[tuple[str, Decimal], ...]:
    """Return the exact user baseline: 35/10/15/5/5/20/10."""

    return _validate_target_rows(STANDARD_64_TARGET)


def target_for_c11_state(state: C11State) -> tuple[tuple[str, Decimal], ...]:
    if type(state) is not C11State:
        raise C11PolicyError("state must be C11State")
    return _validate_target_rows(_C11_TARGETS[state])


def midrank_inclusive_current(values: Sequence[Decimal]) -> Decimal:
    """Return the inclusive-current midrank percentile of the final value."""

    rows = tuple(values)
    if not rows:
        raise C11PolicyError("midrank history cannot be empty")
    if any(type(value) is not Decimal or not value.is_finite() for value in rows):
        raise C11PolicyError("midrank history must contain finite Decimal values")
    current = rows[-1]
    less = sum(value < current for value in rows)
    equal = sum(value == current for value in rows)
    with localcontext() as context:
        context.prec = 60
        return +((Decimal(less) + Decimal(equal) / TWO) / Decimal(len(rows)))


def percentile_from_history(values: Sequence[Decimal] | None) -> Decimal | None:
    """Apply C11's 5-year minimum and 10-year maximum valuation window."""

    if values is None:
        return None
    rows = tuple(values)
    if any(type(value) is not Decimal or not value.is_finite() for value in rows):
        raise C11PolicyError("valuation history must contain finite Decimal values")
    if len(rows) < VALUATION_MINIMUM_HISTORY_SESSIONS:
        return None
    return midrank_inclusive_current(rows[-VALUATION_LOOKBACK_SESSIONS:])


def relative_total_return_trend(
    *,
    csi500_current: Decimal,
    csi500_twelve_months_ago: Decimal,
    dividend_low_vol_current: Decimal,
    dividend_low_vol_twelve_months_ago: Decimal,
) -> Decimal:
    """Compute the frozen 12-calendar-month relative wealth-factor ratio."""

    values = (
        csi500_current,
        csi500_twelve_months_ago,
        dividend_low_vol_current,
        dividend_low_vol_twelve_months_ago,
    )
    if any(type(value) is not Decimal or not value.is_finite() for value in values):
        raise C11PolicyError("total-return index levels must be finite Decimal values")
    if any(value <= ZERO for value in values):
        raise C11PolicyError("total-return index levels must be strictly positive")
    with localcontext() as context:
        context.prec = 60
        return +(
            (csi500_current / csi500_twelve_months_ago)
            / (dividend_low_vol_current / dividend_low_vol_twelve_months_ago)
            - ONE
        )


def _validate_percentile(value: Decimal | None, *, name: str) -> None:
    if value is None:
        return
    if type(value) is not Decimal or not value.is_finite() or not ZERO <= value <= ONE:
        raise C11PolicyError(f"{name} must be a Decimal in [0, 1] or None")


def decide_c11_style(
    *,
    previous_state: C11State,
    pe_percentile: Decimal | None,
    pb_percentile: Decimal | None,
    relative_trend: Decimal | None,
) -> C11Decision:
    """Evaluate the implemented C11 composite-valuation state transition.

    The frozen replay uses the arithmetic mean of PE and PB percentiles.  This
    implementation detail is authoritative for the published historical
    result even though the short contract prose can be read as requiring both
    components to cross the threshold independently.
    """

    if type(previous_state) is not C11State:
        raise C11PolicyError("previous_state must be C11State")
    _validate_percentile(pe_percentile, name="pe_percentile")
    _validate_percentile(pb_percentile, name="pb_percentile")
    if relative_trend is not None and (
        type(relative_trend) is not Decimal or not relative_trend.is_finite()
    ):
        raise C11PolicyError("relative_trend must be a finite Decimal or None")

    missing: list[str] = []
    if pe_percentile is None or pb_percentile is None:
        missing.append("VALUATION_HISTORY_MISSING_OR_INSUFFICIENT")
    if relative_trend is None:
        missing.append("RELATIVE_TREND_MISSING_OR_INCOMPLETE")
    if missing:
        return C11Decision(
            previous_state=previous_state,
            desired_state=previous_state,
            state_changed=False,
            pe_percentile=pe_percentile,
            pb_percentile=pb_percentile,
            valuation_composite=None,
            relative_trend=relative_trend,
            signal_complete=False,
            reason_codes=tuple(missing),
            target_weights=target_for_c11_state(previous_state),
        )

    if pe_percentile is None or pb_percentile is None or relative_trend is None:
        raise RuntimeError("complete C11 signal unexpectedly lost an input")
    valuation = (pe_percentile + pb_percentile) / TWO
    desired = previous_state
    if valuation <= UNDERVALUED_MAXIMUM and relative_trend > ZERO:
        desired = C11State.M60
        reasons = ("VALUATION_LTE_030", "RELATIVE_TREND_POSITIVE")
    elif valuation >= OVERVALUED_MINIMUM and relative_trend < ZERO:
        desired = C11State.D60
        reasons = ("VALUATION_GTE_070", "RELATIVE_TREND_NEGATIVE")
    else:
        reasons = ("JOINT_EXTREME_NOT_CONFIRMED_RETAIN_STATE",)
    return C11Decision(
        previous_state=previous_state,
        desired_state=desired,
        state_changed=desired is not previous_state,
        pe_percentile=pe_percentile,
        pb_percentile=pb_percentile,
        valuation_composite=valuation,
        relative_trend=relative_trend,
        signal_complete=True,
        reason_codes=reasons,
        target_weights=target_for_c11_state(desired),
    )


def decide_c11_style_from_histories(
    *,
    previous_state: C11State,
    pe_history: Sequence[Decimal] | None,
    pb_history: Sequence[Decimal] | None,
    relative_trend: Decimal | None,
) -> C11Decision:
    return decide_c11_style(
        previous_state=previous_state,
        pe_percentile=percentile_from_history(pe_history),
        pb_percentile=percentile_from_history(pb_history),
        relative_trend=relative_trend,
    )


def _validated_weight_mapping(weights: Mapping[str, Decimal]) -> dict[str, Decimal]:
    if not isinstance(weights, Mapping) or set(weights) != set(ASSET_ORDER):
        raise C11PolicyError("weights must contain exactly the seven C11 assets")
    result = {asset: weights[asset] for asset in ASSET_ORDER}
    if any(type(value) is not Decimal or not value.is_finite() for value in result.values()):
        raise C11PolicyError("weights must be finite Decimal values")
    if any(value < ZERO for value in result.values()):
        raise C11PolicyError("long-only weights cannot be negative")
    if abs(sum(result.values(), ZERO) - ONE) > WEIGHT_TOLERANCE:
        raise C11PolicyError("weights must sum to one within the public tolerance")
    return result


def static_stress_loss(weights: Mapping[str, Decimal]) -> Decimal:
    rows = _validated_weight_mapping(weights)
    return sum((rows[asset] * _STRESS_LOSS[asset] for asset in ASSET_ORDER), ZERO)


def assess_maintenance(
    *,
    marked_weights: Mapping[str, Decimal],
    target_weights: Mapping[str, Decimal],
) -> MaintenanceAssessment:
    marked = _validated_weight_mapping(marked_weights)
    target = _validated_weight_mapping(target_weights)
    maximum_gap = max(abs(marked[asset] - target[asset]) for asset in ASSET_ORDER)
    stress = static_stress_loss(marked)
    drift = maximum_gap > ORDINARY_DRIFT_TRIGGER
    stress_trigger = stress > MARKED_STRESS_MAINTENANCE_TRIGGER
    reasons: list[str] = []
    if drift:
        reasons.append("SINGLE_ASSET_DRIFT_STRICT_GT_005")
    if stress_trigger:
        reasons.append("MARKED_STRESS_STRICT_GT_044")
    if not reasons:
        reasons.append("NO_MAINTENANCE_TRIGGER")
    return MaintenanceAssessment(
        maximum_absolute_gap=maximum_gap,
        marked_static_stress=stress,
        ordinary_drift_triggered=drift,
        stress_maintenance_triggered=stress_trigger,
        reason_codes=tuple(reasons),
    )


__all__ = [
    "ASSET_ORDER",
    "HARD_STRESS_RESEARCH_LIMIT",
    "MARKED_STRESS_MAINTENANCE_TRIGGER",
    "ORDINARY_DRIFT_TRIGGER",
    "OVERVALUED_MINIMUM",
    "STANDARD_64_TARGET",
    "UNDERVALUED_MAXIMUM",
    "VALUATION_LOOKBACK_SESSIONS",
    "VALUATION_MINIMUM_HISTORY_SESSIONS",
    "C11Decision",
    "C11PolicyError",
    "C11State",
    "MaintenanceAssessment",
    "assess_maintenance",
    "decide_c11_style",
    "decide_c11_style_from_histories",
    "midrank_inclusive_current",
    "percentile_from_history",
    "relative_total_return_trend",
    "standard_64_target",
    "static_stress_loss",
    "target_for_c11_state",
]
