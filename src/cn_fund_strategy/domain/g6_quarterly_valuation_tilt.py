from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Mapping

from cn_fund_strategy.domain.g1_global_valuation_batch import BOND_SLEEVE, EQUITY_SLEEVES, GOLD_SLEEVE
from cn_fund_strategy.domain.g3_dual_momentum_valuation_batch import (
    DOMESTIC_MODULE,
    EXTREME_LOSS_FACTORS,
    FIXED_TIE_ORDER,
    OVERSEAS_MODULE,
)


ZERO = Decimal("0")
ONE = Decimal("1")


class G6QuarterlyValuationTiltError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise G6QuarterlyValuationTiltError(message)


@dataclass(frozen=True, slots=True)
class G6SleevePolicy:
    sleeve_id: str
    module_id: str
    fund_code: str
    index_code: str
    core_weight: Decimal

    @property
    def swing_capacity(self) -> Decimal:
        return Decimal("0.025")


@dataclass(frozen=True, slots=True)
class G6Policy:
    canonical_name: str
    technical_id: str
    sleeves: tuple[G6SleevePolicy, ...]
    gold_weight: Decimal
    tilt_weight: Decimal
    receiver_deep_score: Decimal
    receiver_value_score: Decimal
    receiver_trend_floor: Decimal
    donor_score_floor: Decimal

    def sleeve(self, sleeve_id: str) -> G6SleevePolicy:
        matches = tuple(item for item in self.sleeves if item.sleeve_id == sleeve_id)
        _require(len(matches) == 1, f"unknown sleeve: {sleeve_id}")
        return matches[0]


@dataclass(frozen=True, slots=True)
class G6TiltState:
    donor_sleeve: str | None = None
    receiver_sleeve: str | None = None


@dataclass(frozen=True, slots=True)
class G6Signal:
    decision_session: str
    momentum_10m: tuple[tuple[str, Decimal | None], ...]
    valuation_scores: tuple[tuple[str, Decimal | None], ...]
    valuation_sessions: tuple[tuple[str, str | None], ...]

    def momentum_mapping(self) -> dict[str, Decimal | None]:
        return dict(self.momentum_10m)

    def valuation_mapping(self) -> dict[str, Decimal | None]:
        return dict(self.valuation_scores)


@dataclass(frozen=True, slots=True)
class G6Decision:
    decision_session: str
    donor_sleeve: str | None
    receiver_sleeve: str | None
    target_weights: tuple[tuple[str, Decimal], ...]
    reason_codes: tuple[str, ...]

    def target_mapping(self) -> dict[str, Decimal]:
        return dict(self.target_weights)


def standard_g6_policy() -> G6Policy:
    rows = (
        ("CN300", DOMESTIC_MODULE, "510300", "SH000300", "0.05"),
        ("CN500", DOMESTIC_MODULE, "510500", "SH000905", "0.05"),
        ("GROWTH", DOMESTIC_MODULE, "159915", "SZ399006", "0.05"),
        ("HK", OVERSEAS_MODULE, "000071", "HKHSI", "0.00"),
        ("SP500", OVERSEAS_MODULE, "050025", "SP500", "0.15"),
        ("NASDAQ", OVERSEAS_MODULE, "270042", "NDX", "0.20"),
    )
    policy = G6Policy(
        canonical_name="G6-Standard",
        technical_id="G6-SCQVT6-E50-G20-T025-Q1-L2-C225",
        sleeves=tuple(
            G6SleevePolicy(sleeve_id, module_id, fund_code, index_code, Decimal(weight))
            for sleeve_id, module_id, fund_code, index_code, weight in rows
        ),
        gold_weight=Decimal("0.20"),
        tilt_weight=Decimal("0.025"),
        receiver_deep_score=Decimal("0.20"),
        receiver_value_score=Decimal("0.40"),
        receiver_trend_floor=Decimal("-0.10"),
        donor_score_floor=Decimal("0.65"),
    )
    validate_policy(policy)
    return policy


def standard_g6_1_policy() -> G6Policy:
    policy = replace(
        standard_g6_policy(),
        canonical_name="G6-Standard-1",
        technical_id="G6-SCQVT6-E50-G20-T025-Q1-L2-C225-R1",
    )
    validate_policy(policy)
    return policy


def validate_policy(policy: G6Policy) -> None:
    _require(
        (policy.canonical_name, policy.technical_id)
        in {
            ("G6-Standard", "G6-SCQVT6-E50-G20-T025-Q1-L2-C225"),
            ("G6-Standard-1", "G6-SCQVT6-E50-G20-T025-Q1-L2-C225-R1"),
        },
        "candidate identity drifted",
    )
    _require(tuple(item.sleeve_id for item in policy.sleeves) == EQUITY_SLEEVES, "sleeves drifted")
    _require(sum((item.core_weight for item in policy.sleeves), ZERO) == Decimal("0.50"), "equity core drifted")
    _require(policy.gold_weight == Decimal("0.20"), "gold drifted")
    base = strategic_target(policy)
    _require(extreme_static_loss(base) == Decimal("0.4110"), "base stress drifted")
    maximum = dict(base)
    maximum["CN300"] -= policy.tilt_weight
    maximum["HK"] += policy.tilt_weight
    _require(extreme_static_loss(maximum) == Decimal("0.41250"), "tilt stress drifted")


def strategic_target(policy: G6Policy) -> dict[str, Decimal]:
    result = {item.sleeve_id: item.core_weight for item in policy.sleeves}
    result[GOLD_SLEEVE] = policy.gold_weight
    result[BOND_SLEEVE] = ONE - sum(result.values(), ZERO)
    _validate_weights(result)
    return result


def decide_tilt(policy: G6Policy, signal: G6Signal) -> G6Decision:
    momentum = signal.momentum_mapping()
    values = signal.valuation_mapping()
    _require(set(momentum) == set(EQUITY_SLEEVES), "momentum identities mismatch")
    _require(set(values) == set(EQUITY_SLEEVES), "valuation identities mismatch")
    receivers = tuple(
        item
        for item in EQUITY_SLEEVES
        if values[item] is not None
        and momentum[item] is not None
        and (
            values[item] <= policy.receiver_deep_score  # type: ignore[operator]
            or (
                values[item] <= policy.receiver_value_score  # type: ignore[operator]
                and momentum[item] > policy.receiver_trend_floor  # type: ignore[operator]
            )
        )
    )
    receiver = (
        min(
            receivers,
            key=lambda item: (
                values[item],
                -(momentum[item] if momentum[item] is not None else Decimal("-999")),
                FIXED_TIE_ORDER[item],
            ),
        )
        if receivers
        else None
    )
    donors = tuple(
        item
        for item in EQUITY_SLEEVES
        if item != receiver
        and policy.sleeve(item).core_weight >= policy.tilt_weight
        and values[item] is not None
        and momentum[item] is not None
        and values[item] >= policy.donor_score_floor  # type: ignore[operator]
    )
    donor = (
        max(
            donors,
            key=lambda item: (
                values[item],
                -(momentum[item] if momentum[item] is not None else Decimal("999")),
                -FIXED_TIE_ORDER[item],
            ),
        )
        if receiver is not None and donors
        else None
    )
    target = strategic_target(policy)
    reasons: tuple[str, ...]
    if receiver is not None and donor is not None:
        target[receiver] += policy.tilt_weight
        target[donor] -= policy.tilt_weight
        reasons = ("QUARTERLY_LOW_VALUATION_BUY_HIGH_VALUATION_SELL_TILT",)
    else:
        receiver = None
        donor = None
        reasons = ("NO_COMPLETE_CHEAP_RECEIVER_AND_EXPENSIVE_CORE_DONOR_RETURN_TO_BASE",)
    _validate_weights(target)
    return G6Decision(
        decision_session=signal.decision_session,
        donor_sleeve=donor,
        receiver_sleeve=receiver,
        target_weights=tuple((key, target[key]) for key in EQUITY_SLEEVES + (GOLD_SLEEVE, BOND_SLEEVE)),
        reason_codes=reasons,
    )


def state_from_decision(decision: G6Decision) -> G6TiltState:
    return G6TiltState(decision.donor_sleeve, decision.receiver_sleeve)


def target_with_state(policy: G6Policy, state: G6TiltState) -> dict[str, Decimal]:
    target = strategic_target(policy)
    if state.donor_sleeve is not None or state.receiver_sleeve is not None:
        _require(state.donor_sleeve is not None and state.receiver_sleeve is not None, "partial tilt state")
        target[state.donor_sleeve] -= policy.tilt_weight
        target[state.receiver_sleeve] += policy.tilt_weight
    _validate_weights(target)
    return target


def extreme_static_loss(weights: Mapping[str, Decimal]) -> Decimal:
    domestic = sum((weights[item] for item in ("CN300", "CN500", "GROWTH")), ZERO)
    overseas = sum((weights[item] for item in ("HK", "SP500", "NASDAQ")), ZERO)
    return (
        domestic * EXTREME_LOSS_FACTORS[DOMESTIC_MODULE]
        + overseas * EXTREME_LOSS_FACTORS[OVERSEAS_MODULE]
        + weights[GOLD_SLEEVE] * EXTREME_LOSS_FACTORS[GOLD_SLEEVE]
        + weights[BOND_SLEEVE] * EXTREME_LOSS_FACTORS[BOND_SLEEVE]
    )


def _validate_weights(weights: Mapping[str, Decimal]) -> None:
    _require(set(weights) == set(EQUITY_SLEEVES) | {GOLD_SLEEVE, BOND_SLEEVE}, "weight identities mismatch")
    _require(all(value >= ZERO for value in weights.values()), "negative weight")
    _require(abs(sum(weights.values(), ZERO) - ONE) <= Decimal("1e-20"), "weights must sum to one")
