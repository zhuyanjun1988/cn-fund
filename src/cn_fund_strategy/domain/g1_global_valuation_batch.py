from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Mapping, Sequence


ZERO = Decimal("0")
ONE = Decimal("1")
EQUITY_SLEEVES = (
    "CN300",
    "CN500",
    "GROWTH",
    "HK",
    "SP500",
    "NASDAQ",
)
GOLD_SLEEVE = "GOLD"
BOND_SLEEVE = "BOND"


class G1GlobalValuationBatchError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise G1GlobalValuationBatchError(message)


def _decimal(
    value: object,
    label: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    _require(type(value) is Decimal, f"{label} must be Decimal")
    parsed = value
    _require(parsed.is_finite(), f"{label} must be finite")
    if minimum is not None:
        _require(parsed >= minimum, f"{label} below minimum")
    if maximum is not None:
        _require(parsed <= maximum, f"{label} above maximum")
    return parsed


@dataclass(frozen=True, slots=True)
class G1SleevePolicy:
    sleeve_id: str
    fund_code: str
    index_code: str
    core_weight: Decimal
    swing_capacity: Decimal
    maximum_batches: int = 4

    @property
    def batch_weight(self) -> Decimal:
        return self.swing_capacity / Decimal(self.maximum_batches)


@dataclass(frozen=True, slots=True)
class G1Policy:
    canonical_name: str
    technical_id: str
    sleeves: tuple[G1SleevePolicy, ...]
    initial_batches: int
    gold_weight: Decimal
    cooldown_months: int
    deep_value_score: Decimal
    value_score: Decimal
    extreme_score: Decimal
    expensive_score: Decimal
    above_fair_score: Decimal
    entry_trend_floor: Decimal
    sharp_trend_break: Decimal
    entry_price_space: Decimal
    entry_score_space: Decimal
    exit_price_space: Decimal
    exit_score_space: Decimal
    concentration_cap: Decimal

    def sleeve(self, sleeve_id: str) -> G1SleevePolicy:
        matches = tuple(item for item in self.sleeves if item.sleeve_id == sleeve_id)
        _require(len(matches) == 1, f"unknown sleeve_id: {sleeve_id}")
        return matches[0]


@dataclass(frozen=True, slots=True)
class G1SleeveState:
    sleeve_id: str
    batches: int
    last_action_month_ordinal: int | None = None
    last_entry_level: Decimal | None = None
    last_entry_score: Decimal | None = None
    last_exit_level: Decimal | None = None
    last_exit_score: Decimal | None = None


@dataclass(frozen=True, slots=True)
class G1Signal:
    sleeve_id: str
    decision_session: str
    month_ordinal: int
    valuation_session: str | None
    valuation_score: Decimal | None
    momentum_10m: Decimal | None
    total_return_level: Decimal | None
    complete: bool


@dataclass(frozen=True, slots=True)
class G1SleeveDecision:
    sleeve_id: str
    decision_session: str
    action_batches: int
    requested_weight_change: Decimal
    batches_before: int
    batches_after_if_filled: int
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class G1ProductAvailability:
    fund_code: str
    product_name: str
    as_of: str
    buy_allowed: bool | None
    sell_allowed: bool | None
    daily_buy_cap_cny: Decimal | None
    evidence_status: str


@dataclass(frozen=True, slots=True)
class G1OrderGateResult:
    fund_code: str
    product_name: str
    direction: str
    requested_amount_cny: Decimal
    executable_amount_cny: Decimal
    status: str
    reason_codes: tuple[str, ...]


def standard_g1_policy() -> G1Policy:
    policy = G1Policy(
        canonical_name="G1-Standard",
        technical_id="G1-GVB6-E50-85-G10-B4-M1-L2-C25",
        sleeves=(
            G1SleevePolicy(
                "CN300", "510300", "SH000300", Decimal("0.05"), Decimal("0.075")
            ),
            G1SleevePolicy(
                "CN500", "510500", "SH000905", Decimal("0.05"), Decimal("0.075")
            ),
            G1SleevePolicy(
                "GROWTH", "159915", "SZ399006", Decimal("0.05"), Decimal("0.05")
            ),
            G1SleevePolicy(
                "HK", "000071", "HKHSI", Decimal("0.05"), Decimal("0.05")
            ),
            G1SleevePolicy(
                "SP500", "050025", "SP500", Decimal("0.15"), Decimal("0.05")
            ),
            G1SleevePolicy(
                "NASDAQ", "270042", "NDX", Decimal("0.15"), Decimal("0.05")
            ),
        ),
        initial_batches=3,
        gold_weight=Decimal("0.10"),
        cooldown_months=3,
        deep_value_score=Decimal("0.20"),
        value_score=Decimal("0.40"),
        extreme_score=Decimal("0.90"),
        expensive_score=Decimal("0.80"),
        above_fair_score=Decimal("0.65"),
        entry_trend_floor=Decimal("-0.10"),
        sharp_trend_break=Decimal("-0.15"),
        entry_price_space=Decimal("0.05"),
        entry_score_space=Decimal("0.10"),
        exit_price_space=Decimal("0.08"),
        exit_score_space=Decimal("0.08"),
        concentration_cap=Decimal("0.25"),
    )
    validate_policy(policy)
    return policy


def validate_policy(policy: G1Policy) -> None:
    _require(policy.canonical_name == "G1-Standard", "unexpected canonical_name")
    _require(tuple(item.sleeve_id for item in policy.sleeves) == EQUITY_SLEEVES, "sleeve order or identity mismatch")
    _require(len({item.fund_code for item in policy.sleeves}) == len(policy.sleeves), "duplicate fund_code")
    _require(len({item.index_code for item in policy.sleeves}) == len(policy.sleeves), "duplicate index_code")
    for item in policy.sleeves:
        _decimal(item.core_weight, f"{item.sleeve_id}.core_weight", minimum=ZERO, maximum=ONE)
        _decimal(item.swing_capacity, f"{item.sleeve_id}.swing_capacity", minimum=ZERO, maximum=ONE)
        _require(item.maximum_batches > 0, "maximum_batches must be positive")
    _require(0 <= policy.initial_batches <= min(item.maximum_batches for item in policy.sleeves), "invalid initial_batches")
    _require(policy.cooldown_months >= 0, "cooldown_months must not be negative")
    for label in (
        "gold_weight",
        "deep_value_score",
        "value_score",
        "extreme_score",
        "expensive_score",
        "above_fair_score",
        "entry_price_space",
        "entry_score_space",
        "exit_price_space",
        "exit_score_space",
        "concentration_cap",
    ):
        _decimal(getattr(policy, label), label, minimum=ZERO, maximum=ONE)
    _decimal(policy.entry_trend_floor, "entry_trend_floor", minimum=-ONE, maximum=ONE)
    _decimal(policy.sharp_trend_break, "sharp_trend_break", minimum=-ONE, maximum=ONE)
    _require(policy.deep_value_score <= policy.value_score <= policy.above_fair_score <= policy.expensive_score <= policy.extreme_score, "valuation thresholds must be ordered")
    core = sum((item.core_weight for item in policy.sleeves), ZERO)
    swing = sum((item.swing_capacity for item in policy.sleeves), ZERO)
    _require(core == Decimal("0.50"), "core equity must equal 0.50")
    _require(core + swing == Decimal("0.85"), "maximum equity must equal 0.85")
    initial_equity = core + swing * Decimal(policy.initial_batches) / Decimal(4)
    _require(initial_equity + policy.gold_weight <= ONE, "initial allocation exceeds one")


def initial_sleeve_states(policy: G1Policy) -> tuple[G1SleeveState, ...]:
    return tuple(
        G1SleeveState(sleeve_id=item.sleeve_id, batches=policy.initial_batches)
        for item in policy.sleeves
    )


def initial_weights(policy: G1Policy) -> dict[str, Decimal]:
    weights = {
        item.sleeve_id: item.core_weight
        + item.batch_weight * Decimal(policy.initial_batches)
        for item in policy.sleeves
    }
    weights[GOLD_SLEEVE] = policy.gold_weight
    weights[BOND_SLEEVE] = ONE - sum(weights.values(), ZERO)
    _validate_weights(weights)
    return weights


def _complete_signal(signal: G1Signal) -> tuple[Decimal, Decimal, Decimal]:
    _require(signal.valuation_score is not None, "complete signal missing valuation_score")
    _require(signal.momentum_10m is not None, "complete signal missing momentum_10m")
    _require(signal.total_return_level is not None, "complete signal missing total_return_level")
    score = _decimal(signal.valuation_score, "valuation_score", minimum=ZERO, maximum=ONE)
    momentum = _decimal(signal.momentum_10m, "momentum_10m")
    level = _decimal(signal.total_return_level, "total_return_level", minimum=ZERO)
    _require(level > ZERO, "total_return_level must be positive")
    return score, momentum, level


def decide_sleeve(
    policy: G1Policy,
    state: G1SleeveState,
    signal: G1Signal,
) -> G1SleeveDecision:
    sleeve = policy.sleeve(state.sleeve_id)
    _require(signal.sleeve_id == state.sleeve_id, "signal and state sleeve mismatch")
    _require(0 <= state.batches <= sleeve.maximum_batches, "state batches outside policy")
    if not signal.complete:
        return _hold(state, signal, "INCOMPLETE_SIGNAL_NO_ACTION")
    score, momentum, level = _complete_signal(signal)
    if (
        state.last_action_month_ordinal is not None
        and signal.month_ordinal - state.last_action_month_ordinal < policy.cooldown_months
    ):
        return _hold(state, signal, "SLEEVE_COOLDOWN_ACTIVE")

    entry_space = (
        state.last_entry_level is None
        or state.last_entry_score is None
        or level <= state.last_entry_level * (ONE - policy.entry_price_space)
        or score <= state.last_entry_score - policy.entry_score_space
    )
    exit_space = (
        state.last_exit_level is None
        or state.last_exit_score is None
        or level >= state.last_exit_level * (ONE + policy.exit_price_space)
        or score >= state.last_exit_score + policy.exit_score_space
        or momentum < ZERO
    )

    if state.batches < sleeve.maximum_batches and entry_space:
        if score <= policy.deep_value_score:
            return _action(state, signal, sleeve.batch_weight, 1, "DEEP_VALUE_SPACED_ADD_ONE_BATCH")
        if score <= policy.value_score and momentum > policy.entry_trend_floor:
            return _action(state, signal, sleeve.batch_weight, 1, "LOW_VALUE_NOT_COLLAPSING_SPACED_ADD_ONE_BATCH")

    if state.batches > 0 and exit_space:
        if score >= policy.extreme_score:
            return _action(state, signal, sleeve.batch_weight, -1, "EXTREME_VALUATION_SPACED_REMOVE_ONE_BATCH")
        if score >= policy.expensive_score and momentum < ZERO:
            return _action(state, signal, sleeve.batch_weight, -1, "HIGH_VALUE_NEGATIVE_TREND_SPACED_REMOVE_ONE_BATCH")
        if score >= policy.above_fair_score and momentum < policy.sharp_trend_break:
            return _action(state, signal, sleeve.batch_weight, -1, "ABOVE_FAIR_SHARP_TREND_BREAK_SPACED_REMOVE_ONE_BATCH")

    reasons: list[str] = ["NO_RULE_TRIGGERED"]
    if state.batches == sleeve.maximum_batches:
        reasons.append("SWING_BATCHES_AT_MAXIMUM")
    if state.batches == 0:
        reasons.append("SWING_BATCHES_AT_FLOOR")
    if not entry_space:
        reasons.append("ENTRY_SPACING_NOT_MET")
    if not exit_space:
        reasons.append("EXIT_SPACING_NOT_MET")
    return _hold(state, signal, *reasons)


def _hold(
    state: G1SleeveState,
    signal: G1Signal,
    *reason_codes: str,
) -> G1SleeveDecision:
    return G1SleeveDecision(
        sleeve_id=state.sleeve_id,
        decision_session=signal.decision_session,
        action_batches=0,
        requested_weight_change=ZERO,
        batches_before=state.batches,
        batches_after_if_filled=state.batches,
        reason_codes=tuple(reason_codes),
    )


def _action(
    state: G1SleeveState,
    signal: G1Signal,
    batch_weight: Decimal,
    action_batches: int,
    reason_code: str,
) -> G1SleeveDecision:
    _require(action_batches in {-1, 1}, "action_batches must be -1 or 1")
    return G1SleeveDecision(
        sleeve_id=state.sleeve_id,
        decision_session=signal.decision_session,
        action_batches=action_batches,
        requested_weight_change=batch_weight * Decimal(action_batches),
        batches_before=state.batches,
        batches_after_if_filled=state.batches + action_batches,
        reason_codes=(reason_code,),
    )


def apply_filled_decision(
    state: G1SleeveState,
    signal: G1Signal,
    decision: G1SleeveDecision,
) -> G1SleeveState:
    _require(decision.sleeve_id == state.sleeve_id == signal.sleeve_id, "filled decision sleeve mismatch")
    _require(decision.batches_before == state.batches, "stale decision batches")
    if decision.action_batches == 0:
        return state
    score, _, level = _complete_signal(signal)
    updated = replace(
        state,
        batches=decision.batches_after_if_filled,
        last_action_month_ordinal=signal.month_ordinal,
    )
    if decision.action_batches > 0:
        return replace(updated, last_entry_level=level, last_entry_score=score)
    return replace(updated, last_exit_level=level, last_exit_score=score)


def signed_weight_changes(
    decisions: Sequence[G1SleeveDecision],
) -> dict[str, Decimal]:
    changes: dict[str, Decimal] = {}
    for decision in decisions:
        if decision.requested_weight_change:
            _require(decision.sleeve_id not in changes, "duplicate sleeve decision")
            changes[decision.sleeve_id] = decision.requested_weight_change
    changes[BOND_SLEEVE] = -sum(changes.values(), ZERO)
    return changes


def apply_weight_changes(
    marked_weights: Mapping[str, Decimal],
    changes: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    target = dict(marked_weights)
    for sleeve_id, change in changes.items():
        _decimal(change, f"change[{sleeve_id}]")
        _require(sleeve_id in target, f"change targets unknown sleeve {sleeve_id}")
        target[sleeve_id] += change
    _validate_weights(target)
    return target


def concentration_trim_changes(
    policy: G1Policy,
    marked_weights: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    changes: dict[str, Decimal] = {}
    released = ZERO
    for sleeve_id in EQUITY_SLEEVES:
        weight = _decimal(marked_weights[sleeve_id], f"marked_weights[{sleeve_id}]", minimum=ZERO)
        if weight > policy.concentration_cap:
            change = policy.concentration_cap - weight
            changes[sleeve_id] = change
            released -= change
    if changes:
        changes[BOND_SLEEVE] = released
    return changes


def _validate_weights(weights: Mapping[str, Decimal]) -> None:
    expected = set(EQUITY_SLEEVES) | {GOLD_SLEEVE, BOND_SLEEVE}
    _require(set(weights) == expected, "portfolio weight identities mismatch")
    total = ZERO
    for key, value in weights.items():
        total += _decimal(value, f"weight[{key}]", minimum=ZERO, maximum=ONE)
    _require(abs(total - ONE) <= Decimal("0.00000000000000000001"), "portfolio weights must sum to one")


def gate_order(
    decision: G1SleeveDecision,
    availability: G1ProductAvailability,
    *,
    portfolio_value_cny: Decimal,
) -> G1OrderGateResult:
    value = _decimal(portfolio_value_cny, "portfolio_value_cny", minimum=ZERO)
    requested = abs(decision.requested_weight_change) * value
    if decision.action_batches == 0:
        return G1OrderGateResult(
            fund_code=availability.fund_code,
            product_name=availability.product_name,
            direction="HOLD",
            requested_amount_cny=ZERO,
            executable_amount_cny=ZERO,
            status="hold-no-economic-order",
            reason_codes=decision.reason_codes,
        )
    if decision.action_batches > 0:
        if availability.buy_allowed is not True:
            return _blocked_order(decision, availability, requested, "CURRENT_BUY_PERMISSION_NOT_PROVEN")
        if availability.daily_buy_cap_cny is not None and requested > availability.daily_buy_cap_cny:
            return _blocked_order(decision, availability, requested, "REQUEST_EXCEEDS_CURRENT_DAILY_BUY_CAP")
        direction = "BUY"
    else:
        if availability.sell_allowed is not True:
            return _blocked_order(decision, availability, requested, "CURRENT_SELL_PERMISSION_NOT_PROVEN")
        direction = "SELL"
    return G1OrderGateResult(
        fund_code=availability.fund_code,
        product_name=availability.product_name,
        direction=direction,
        requested_amount_cny=requested,
        executable_amount_cny=requested,
        status="execution-gate-ready",
        reason_codes=decision.reason_codes,
    )


def _blocked_order(
    decision: G1SleeveDecision,
    availability: G1ProductAvailability,
    requested: Decimal,
    reason_code: str,
) -> G1OrderGateResult:
    return G1OrderGateResult(
        fund_code=availability.fund_code,
        product_name=availability.product_name,
        direction="BLOCKED_BUY" if decision.action_batches > 0 else "BLOCKED_SELL",
        requested_amount_cny=requested,
        executable_amount_cny=ZERO,
        status="blocked-fail-closed",
        reason_codes=decision.reason_codes + (reason_code, availability.evidence_status),
    )


def expanding_percentile(values: Sequence[Decimal], current: Decimal) -> Decimal:
    _require(bool(values), "percentile values must not be empty")
    parsed = tuple(_decimal(value, "percentile value") for value in values)
    item = _decimal(current, "percentile current")
    return Decimal(sum(value <= item for value in parsed)) / Decimal(len(parsed))


def valuation_score(
    pe_history: Sequence[Decimal],
    pb_history: Sequence[Decimal],
) -> Decimal:
    _require(len(pe_history) == len(pb_history), "PE/PB history lengths differ")
    _require(len(pe_history) >= 13, "valuation history needs at least 13 observations")
    return (
        expanding_percentile(pe_history, pe_history[-1])
        + expanding_percentile(pb_history, pb_history[-1])
    ) / Decimal(2)
