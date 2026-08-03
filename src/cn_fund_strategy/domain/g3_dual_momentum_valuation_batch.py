from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Mapping, Sequence

from cn_fund_strategy.domain.g1_global_valuation_batch import (
    BOND_SLEEVE,
    EQUITY_SLEEVES,
    GOLD_SLEEVE,
)


ZERO = Decimal("0")
ONE = Decimal("1")
DOMESTIC_MODULE = "DOMESTIC"
OVERSEAS_MODULE = "OVERSEAS"
MODULE_SLEEVES = {
    DOMESTIC_MODULE: ("CN300", "CN500", "GROWTH"),
    OVERSEAS_MODULE: ("HK", "SP500", "NASDAQ"),
}
FIXED_TIE_ORDER = {sleeve_id: ordinal for ordinal, sleeve_id in enumerate(EQUITY_SLEEVES)}
EXTREME_LOSS_FACTORS = {
    DOMESTIC_MODULE: Decimal("0.60"),
    OVERSEAS_MODULE: Decimal("0.66"),
    GOLD_SLEEVE: Decimal("0.30"),
    BOND_SLEEVE: Decimal("0.10"),
}


class G3DualMomentumValuationBatchError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise G3DualMomentumValuationBatchError(message)


@dataclass(frozen=True, slots=True)
class G3SleevePolicy:
    sleeve_id: str
    module_id: str
    fund_code: str
    index_code: str
    core_weight: Decimal
    batch_weight: Decimal
    maximum_batches: int

    @property
    def swing_capacity(self) -> Decimal:
        """Compatibility name used by shared read-only data loaders."""

        return self.batch_weight * Decimal(self.maximum_batches)


@dataclass(frozen=True, slots=True)
class G3Policy:
    canonical_name: str
    technical_id: str
    sleeves: tuple[G3SleevePolicy, ...]
    gold_weight: Decimal
    valuation_full_target_ceiling: Decimal
    valuation_reduced_target_ceiling: Decimal
    valuation_veto_floor: Decimal
    deep_value_probe_ceiling: Decimal
    historical_missing_valuation_target_batches: int
    concentration_cap: Decimal
    pretrade_static_stress_guard: Decimal
    postguard_static_stress_target: Decimal

    def sleeve(self, sleeve_id: str) -> G3SleevePolicy:
        matches = tuple(item for item in self.sleeves if item.sleeve_id == sleeve_id)
        _require(len(matches) == 1, f"unknown sleeve_id: {sleeve_id}")
        return matches[0]

    def module_sleeves(self, module_id: str) -> tuple[str, ...]:
        _require(module_id in MODULE_SLEEVES, f"unknown module_id: {module_id}")
        return MODULE_SLEEVES[module_id]


@dataclass(frozen=True, slots=True)
class G3ModuleState:
    module_id: str
    batches: tuple[tuple[str, int], ...]

    def as_mapping(self) -> dict[str, int]:
        return dict(self.batches)


@dataclass(frozen=True, slots=True)
class G3ModuleSignal:
    module_id: str
    decision_session: str
    momentum_12m: tuple[tuple[str, Decimal | None], ...]
    bond_momentum_12m: Decimal | None
    valuation_scores: tuple[tuple[str, Decimal | None], ...]
    valuation_sessions: tuple[tuple[str, str | None], ...]

    def momentum_mapping(self) -> dict[str, Decimal | None]:
        return dict(self.momentum_12m)

    def valuation_mapping(self) -> dict[str, Decimal | None]:
        return dict(self.valuation_scores)


@dataclass(frozen=True, slots=True)
class G3ModuleDecision:
    module_id: str
    decision_session: str
    relative_winner: str | None
    target_batches: tuple[tuple[str, int], ...]
    batch_changes: tuple[tuple[str, int], ...]
    bond_batch_change: int
    reason_codes: tuple[str, ...]

    def target_mapping(self) -> dict[str, int]:
        return dict(self.target_batches)

    def change_mapping(self) -> dict[str, int]:
        return dict(self.batch_changes)

    @property
    def is_actionable(self) -> bool:
        return bool(self.bond_batch_change or any(value for _, value in self.batch_changes))


def standard_g3_policy() -> G3Policy:
    rows = (
        ("CN300", DOMESTIC_MODULE, "510300", "SH000300"),
        ("CN500", DOMESTIC_MODULE, "510500", "SH000905"),
        ("GROWTH", DOMESTIC_MODULE, "159915", "SZ399006"),
        ("HK", OVERSEAS_MODULE, "000071", "HKHSI"),
        ("SP500", OVERSEAS_MODULE, "050025", "SP500"),
        ("NASDAQ", OVERSEAS_MODULE, "270042", "NDX"),
    )
    policy = G3Policy(
        canonical_name="G3-Standard",
        technical_id="G3-DMVB6-E36-60-G10-B3-M1-L2-C18",
        sleeves=tuple(
            G3SleevePolicy(
                sleeve_id=sleeve_id,
                module_id=module_id,
                fund_code=fund_code,
                index_code=index_code,
                core_weight=Decimal("0.06"),
                batch_weight=Decimal("0.03"),
                maximum_batches=4,
            )
            for sleeve_id, module_id, fund_code, index_code in rows
        ),
        gold_weight=Decimal("0.10"),
        valuation_full_target_ceiling=Decimal("0.80"),
        valuation_reduced_target_ceiling=Decimal("0.90"),
        valuation_veto_floor=Decimal("0.90"),
        deep_value_probe_ceiling=Decimal("0.20"),
        historical_missing_valuation_target_batches=3,
        concentration_cap=Decimal("0.18"),
        pretrade_static_stress_guard=Decimal("0.44"),
        postguard_static_stress_target=Decimal("0.438"),
    )
    validate_g3_policy(policy)
    return policy


def standard_g3_1_policy() -> G3Policy:
    """Return the economic-identical accounting-corrected technical subversion."""

    parent = standard_g3_policy()
    policy = replace(
        parent,
        canonical_name="G3-Standard-1",
        technical_id="G3-DMVB6-E36-60-G10-B3-M1-L2-C18-R1",
    )
    validate_g3_1_policy(policy)
    return policy


def validate_g3_1_policy(policy: G3Policy) -> None:
    _require(policy.canonical_name == "G3-Standard-1", "unexpected corrected canonical_name")
    _require(
        policy.technical_id == "G3-DMVB6-E36-60-G10-B3-M1-L2-C18-R1",
        "unexpected corrected technical_id",
    )
    validate_g3_policy(
        replace(
            policy,
            canonical_name="G3-Standard",
            technical_id="G3-DMVB6-E36-60-G10-B3-M1-L2-C18",
        )
    )


def validate_g3_policy(policy: G3Policy) -> None:
    _require(policy.canonical_name == "G3-Standard", "unexpected canonical_name")
    _require(
        policy.technical_id == "G3-DMVB6-E36-60-G10-B3-M1-L2-C18",
        "unexpected technical_id",
    )
    _require(
        tuple(item.sleeve_id for item in policy.sleeves) == EQUITY_SLEEVES,
        "equity sleeve order or identity mismatch",
    )
    _require(
        tuple(item.module_id for item in policy.sleeves[:3])
        == (DOMESTIC_MODULE,) * 3,
        "domestic module mismatch",
    )
    _require(
        tuple(item.module_id for item in policy.sleeves[3:])
        == (OVERSEAS_MODULE,) * 3,
        "overseas module mismatch",
    )
    for item in policy.sleeves:
        _require(type(item.core_weight) is Decimal, "core_weight must be Decimal")
        _require(type(item.batch_weight) is Decimal, "batch_weight must be Decimal")
        _require(item.core_weight == Decimal("0.06"), "each core must equal six percent")
        _require(item.batch_weight == Decimal("0.03"), "batch must equal three percent")
        _require(item.maximum_batches == 4, "each module winner supports four batches")
    core = sum((item.core_weight for item in policy.sleeves), ZERO)
    _require(core == Decimal("0.36"), "core equity must equal 36 percent")
    maximum_tactical = Decimal("0.12") * Decimal(len(MODULE_SLEEVES))
    _require(core + maximum_tactical == Decimal("0.60"), "maximum equity must equal 60 percent")
    _require(policy.gold_weight == Decimal("0.10"), "gold must equal 10 percent")
    _require(policy.concentration_cap == Decimal("0.18"), "sleeve cap must equal 18 percent")
    _require(
        ZERO <= policy.deep_value_probe_ceiling
        < policy.valuation_full_target_ceiling
        < policy.valuation_veto_floor
        <= ONE,
        "valuation thresholds are not ordered",
    )
    _require(
        policy.valuation_reduced_target_ceiling == policy.valuation_veto_floor,
        "reduced target ceiling and veto floor must meet",
    )
    _require(
        0 <= policy.historical_missing_valuation_target_batches <= 4,
        "invalid missing valuation batch prior",
    )
    nominal = {
        "CN300": Decimal("0.10"),
        "CN500": Decimal("0.10"),
        "GROWTH": Decimal("0.10"),
        "HK": Decimal("0.10"),
        "SP500": Decimal("0.10"),
        "NASDAQ": Decimal("0.10"),
        GOLD_SLEEVE: Decimal("0.10"),
        BOND_SLEEVE: Decimal("0.30"),
    }
    _require(extreme_static_loss(nominal) == Decimal("0.4380"), "stress identity drifted")


def initial_weights(policy: G3Policy) -> dict[str, Decimal]:
    weights = {item.sleeve_id: item.core_weight for item in policy.sleeves}
    weights[GOLD_SLEEVE] = policy.gold_weight
    weights[BOND_SLEEVE] = ONE - sum(weights.values(), ZERO)
    _validate_weights(weights)
    return weights


def initial_module_states(policy: G3Policy) -> dict[str, G3ModuleState]:
    return {
        module_id: G3ModuleState(
            module_id=module_id,
            batches=tuple((sleeve_id, 0) for sleeve_id in policy.module_sleeves(module_id)),
        )
        for module_id in MODULE_SLEEVES
    }


def decide_module(
    policy: G3Policy,
    state: G3ModuleState,
    signal: G3ModuleSignal,
    *,
    historical_missing_valuation_prior_allowed: bool,
) -> G3ModuleDecision:
    _require(signal.module_id == state.module_id, "signal and state module mismatch")
    module_sleeves = policy.module_sleeves(state.module_id)
    current = state.as_mapping()
    _validate_batches(module_sleeves, current)
    momentum = signal.momentum_mapping()
    valuations = signal.valuation_mapping()
    _require(set(momentum) == set(module_sleeves), "module momentum identity mismatch")
    _require(set(valuations) == set(module_sleeves), "module valuation identity mismatch")

    complete_momentum = signal.bond_momentum_12m is not None and all(
        momentum[item] is not None for item in module_sleeves
    )
    if not complete_momentum:
        return _transition(
            policy,
            state,
            signal,
            winner=None,
            desired_batches=0,
            reason_codes=("INCOMPLETE_12M_MOMENTUM_TARGET_ZERO",),
        )

    winner = max(
        module_sleeves,
        key=lambda item: (momentum[item], -FIXED_TIE_ORDER[item]),
    )
    winner_momentum = momentum[winner]
    _require(winner_momentum is not None, "winner momentum unexpectedly missing")
    bond_momentum = signal.bond_momentum_12m
    _require(bond_momentum is not None, "bond momentum unexpectedly missing")
    valuation = valuations[winner]
    dual_pass = winner_momentum > bond_momentum

    if valuation is None:
        if historical_missing_valuation_prior_allowed and dual_pass:
            desired = policy.historical_missing_valuation_target_batches
            reasons = (
                "DUAL_MOMENTUM_PASS",
                "HISTORICAL_VALUATION_COVERAGE_GAP_NEUTRAL_PRIOR_THREE_BATCHES",
            )
        else:
            desired = 0
            reasons = (
                "VALUATION_MISSING_NO_NEW_BUY_TARGET_ZERO",
                "DUAL_MOMENTUM_PASS" if dual_pass else "ABSOLUTE_HURDLE_FAILED",
            )
    elif valuation >= policy.valuation_veto_floor:
        desired = 0
        reasons = ("EXTREME_VALUATION_VETO_TARGET_ZERO",)
    elif dual_pass and valuation <= policy.valuation_full_target_ceiling:
        desired = 4
        reasons = ("DUAL_MOMENTUM_PASS", "VALUATION_NOT_HIGH_FULL_FOUR_BATCH_TARGET")
    elif dual_pass and valuation < policy.valuation_reduced_target_ceiling:
        desired = 2
        reasons = ("DUAL_MOMENTUM_PASS", "HIGH_BUT_NOT_EXTREME_VALUATION_TWO_BATCH_TARGET")
    elif not dual_pass and valuation <= policy.deep_value_probe_ceiling:
        desired = 1
        reasons = ("ABSOLUTE_HURDLE_FAILED", "DEEP_VALUE_ONE_BATCH_PROBE")
    else:
        desired = 0
        reasons = ("ABSOLUTE_HURDLE_FAILED_TARGET_ZERO",)

    return _transition(
        policy,
        state,
        signal,
        winner=winner,
        desired_batches=desired,
        reason_codes=reasons,
    )


def apply_filled_module_decision(
    policy: G3Policy,
    state: G3ModuleState,
    decision: G3ModuleDecision,
) -> G3ModuleState:
    _require(state.module_id == decision.module_id, "decision and state module mismatch")
    current = state.as_mapping()
    for sleeve_id, change in decision.batch_changes:
        current[sleeve_id] += change
    _validate_batches(policy.module_sleeves(state.module_id), current)
    return G3ModuleState(
        module_id=state.module_id,
        batches=tuple((item, current[item]) for item in policy.module_sleeves(state.module_id)),
    )


def decision_weight_changes(
    policy: G3Policy,
    decisions: Sequence[G3ModuleDecision],
) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    bond_change = ZERO
    for decision in decisions:
        for sleeve_id, batch_change in decision.batch_changes:
            if batch_change:
                result[sleeve_id] = (
                    result.get(sleeve_id, ZERO)
                    + Decimal(batch_change) * policy.sleeve(sleeve_id).batch_weight
                )
        bond_change += Decimal(decision.bond_batch_change) * Decimal("0.03")
    if bond_change:
        result[BOND_SLEEVE] = result.get(BOND_SLEEVE, ZERO) + bond_change
    _require(sum(result.values(), ZERO) == ZERO, "module decisions are not self-financing")
    return result


def apply_weight_changes(
    marked_weights: Mapping[str, Decimal],
    changes: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    result = {key: value + changes.get(key, ZERO) for key, value in marked_weights.items()}
    _validate_weights(result)
    return result


def extreme_static_loss(weights: Mapping[str, Decimal]) -> Decimal:
    required = set(EQUITY_SLEEVES) | {GOLD_SLEEVE, BOND_SLEEVE}
    _require(set(weights) == required, "stress weight identities mismatch")
    domestic = sum((weights[item] for item in MODULE_SLEEVES[DOMESTIC_MODULE]), ZERO)
    overseas = sum((weights[item] for item in MODULE_SLEEVES[OVERSEAS_MODULE]), ZERO)
    return (
        domestic * EXTREME_LOSS_FACTORS[DOMESTIC_MODULE]
        + overseas * EXTREME_LOSS_FACTORS[OVERSEAS_MODULE]
        + weights[GOLD_SLEEVE] * EXTREME_LOSS_FACTORS[GOLD_SLEEVE]
        + weights[BOND_SLEEVE] * EXTREME_LOSS_FACTORS[BOND_SLEEVE]
    )


def _transition(
    policy: G3Policy,
    state: G3ModuleState,
    signal: G3ModuleSignal,
    *,
    winner: str | None,
    desired_batches: int,
    reason_codes: tuple[str, ...],
) -> G3ModuleDecision:
    module_sleeves = policy.module_sleeves(state.module_id)
    _require(0 <= desired_batches <= 4, "desired batches outside [0, 4]")
    target = {item: desired_batches if item == winner else 0 for item in module_sleeves}
    current = state.as_mapping()
    momentum = signal.momentum_mapping()
    sellers = tuple(item for item in module_sleeves if current[item] > target[item])
    seller: str | None = None
    if sellers:
        seller = max(
            sellers,
            key=lambda item: (
                current[item] - target[item],
                -(momentum[item] if momentum.get(item) is not None else Decimal("-999")),
                -FIXED_TIE_ORDER[item],
            ),
        )
    buyer = winner if winner is not None and current[winner] < target[winner] else None
    changes = {item: 0 for item in module_sleeves}
    bond_batch_change = 0
    transition_reason = "MODULE_ALREADY_AT_TARGET"
    if seller is not None and buyer is not None and seller != buyer:
        changes[seller] = -1
        changes[buyer] = 1
        transition_reason = "TRANSFER_ONE_BATCH_OLD_OR_EXCESS_TO_TARGET_WINNER"
    elif seller is not None:
        changes[seller] = -1
        bond_batch_change = 1
        transition_reason = "SELL_ONE_EXCESS_BATCH_TO_BOND"
    elif buyer is not None:
        _require(sum(current.values()) < 4, "cannot buy above module batch capacity")
        changes[buyer] = 1
        bond_batch_change = -1
        transition_reason = "BUY_ONE_TARGET_BATCH_FROM_BOND"
    return G3ModuleDecision(
        module_id=state.module_id,
        decision_session=signal.decision_session,
        relative_winner=winner,
        target_batches=tuple((item, target[item]) for item in module_sleeves),
        batch_changes=tuple((item, changes[item]) for item in module_sleeves),
        bond_batch_change=bond_batch_change,
        reason_codes=reason_codes + (transition_reason,),
    )


def _validate_batches(module_sleeves: Sequence[str], batches: Mapping[str, int]) -> None:
    _require(set(batches) == set(module_sleeves), "batch sleeve identities mismatch")
    _require(all(type(value) is int for value in batches.values()), "batches must be integers")
    _require(all(0 <= value <= 4 for value in batches.values()), "sleeve batches outside [0, 4]")
    _require(sum(batches.values()) <= 4, "module batches exceed four")


def _validate_weights(weights: Mapping[str, Decimal]) -> None:
    required = set(EQUITY_SLEEVES) | {GOLD_SLEEVE, BOND_SLEEVE}
    _require(set(weights) == required, "weight identities mismatch")
    _require(all(type(value) is Decimal for value in weights.values()), "weights must be Decimal")
    _require(all(value >= ZERO for value in weights.values()), "negative portfolio weight")
    _require(
        abs(sum(weights.values(), ZERO) - ONE) <= Decimal("0.00000000000000000001"),
        "portfolio weights must sum to one",
    )
