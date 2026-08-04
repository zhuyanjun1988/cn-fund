"""Small public CLI for inspecting the frozen policy and verifying evidence."""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
from typing import Sequence

from cn_fund_strategy.application.public_evidence import verify_public_evidence
from cn_fund_strategy.domain.c11_balanced_60 import (
    ASSET_ORDER as C11_ASSETS,
    HARD_STRESS_RESEARCH_LIMIT,
    MARKED_STRESS_MAINTENANCE_TRIGGER,
    ORDINARY_DRIFT_TRIGGER,
    OVERVALUED_MINIMUM,
    UNDERVALUED_MAXIMUM,
    C11State,
    assess_maintenance,
    decide_c11_style,
    standard_64_target,
    static_stress_loss,
    target_for_c11_state,
)
from cn_fund_strategy.domain.g1_global_valuation_batch import EQUITY_SLEEVES
from cn_fund_strategy.domain.g6_quarterly_valuation_tilt import (
    G6Signal,
    decide_tilt,
    standard_g6_1_policy,
    strategic_target,
)


def _json_ready(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_ready(item) for item in value]
    return value


def _emit(value: object) -> None:
    print(json.dumps(_json_ready(value), ensure_ascii=False, indent=2, sort_keys=True))


def _key_values(
    raw: str,
    *,
    allow_missing: bool,
) -> tuple[tuple[str, Decimal | None], ...]:
    values: dict[str, Decimal | None] = {}
    for part in raw.split(","):
        key, separator, text = part.strip().partition("=")
        if not separator or key not in EQUITY_SLEEVES or key in values:
            raise argparse.ArgumentTypeError(
                f"invalid or duplicate sleeve assignment: {part!r}"
            )
        if allow_missing and text.upper() in {"NA", "NONE", "NULL"}:
            values[key] = None
        else:
            try:
                values[key] = Decimal(text)
            except Exception as exc:
                raise argparse.ArgumentTypeError(f"invalid decimal: {part!r}") from exc
    if set(values) != set(EQUITY_SLEEVES):
        missing = sorted(set(EQUITY_SLEEVES) - set(values))
        raise argparse.ArgumentTypeError(f"missing sleeves: {missing}")
    return tuple((key, values[key]) for key in EQUITY_SLEEVES)


def _c11_asset_weights(raw: str) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {}
    for part in raw.split(","):
        key, separator, text = part.strip().partition("=")
        if not separator or key not in C11_ASSETS or key in values:
            raise argparse.ArgumentTypeError(
                f"invalid or duplicate C11 asset assignment: {part!r}"
            )
        try:
            values[key] = Decimal(text)
        except Exception as exc:
            raise argparse.ArgumentTypeError(f"invalid decimal: {part!r}") from exc
    if set(values) != set(C11_ASSETS):
        missing = sorted(set(C11_ASSETS) - set(values))
        raise argparse.ArgumentTypeError(f"missing C11 assets: {missing}")
    return {key: values[key] for key in C11_ASSETS}


def _policy() -> int:
    policy = standard_g6_1_policy()
    _emit(
        {
            "canonical_name": policy.canonical_name,
            "technical_id": policy.technical_id,
            "base_target_weights": strategic_target(policy),
            "formal_decision_frequency": "quarter-end",
            "execution_lag_common_sessions": 2,
            "valuation_receiver_rules": {
                "deep_score_lte": policy.receiver_deep_score,
                "value_score_lte": policy.receiver_value_score,
                "ten_month_momentum_gt": policy.receiver_trend_floor,
            },
            "valuation_donor_score_gte": policy.donor_score_floor,
            "equity_internal_tilt_weight": policy.tilt_weight,
            "authority": "research-only-not-a-trading-instruction",
        }
    )
    return 0


def _standard_64_policy() -> int:
    target = dict(standard_64_target())
    _emit(
        {
            "canonical_name": "Fixed-E60-35-10-15-5-5-20-10",
            "target_weights": target,
            "equity_weight": sum(target[key] for key in ("022430", "006729", "021550")),
            "bond_weight": sum(target[key] for key in ("006662", "012773", "007169")),
            "cash_weight": target["CASH"],
            "target_static_stress": static_stress_loss(target),
            "ordinary_drift_trigger": "strictly-greater-than-0.05",
            "marked_stress_maintenance_trigger": "strictly-greater-than-0.44",
            "authority": "research-baseline-only-not-an-executable-order",
        }
    )
    return 0


def _c11_policy() -> int:
    _emit(
        {
            "canonical_name": "C11-C8-Style-Switch-Balanced-60",
            "technical_id": "C11-C8STYLE-E60-STRESS44-M1",
            "states": {
                state.value: dict(target_for_c11_state(state)) for state in C11State
            },
            "valuation_semantics": "arithmetic mean of PE and PB point-in-time percentiles",
            "state_rules": {
                "M60": "valuation_composite <= 0.30 and relative_trend > 0",
                "D60": "valuation_composite >= 0.70 and relative_trend < 0",
                "otherwise": "retain the previously confirmed state",
            },
            "thresholds": {
                "undervalued_maximum": UNDERVALUED_MAXIMUM,
                "overvalued_minimum": OVERVALUED_MINIMUM,
                "ordinary_drift_strictly_greater_than": ORDINARY_DRIFT_TRIGGER,
                "marked_stress_strictly_greater_than": MARKED_STRESS_MAINTENANCE_TRIGGER,
                "hard_research_stress_limit": HARD_STRESS_RESEARCH_LIMIT,
            },
            "target_state_static_stress": static_stress_loss(
                dict(target_for_c11_state(C11State.B60))
            ),
            "authority": "research-policy-only-not-an-executable-order",
        }
    )
    return 0


def _c11_decision(args: argparse.Namespace) -> int:
    previous = C11State(args.previous_state)
    decision = decide_c11_style(
        previous_state=previous,
        pe_percentile=Decimal(args.pe_percentile),
        pb_percentile=Decimal(args.pb_percentile),
        relative_trend=Decimal(args.relative_trend),
    )
    output: dict[str, object] = {
        "previous_state": decision.previous_state.value,
        "desired_state": decision.desired_state.value,
        "state_changed": decision.state_changed,
        "valuation_composite": decision.valuation_composite,
        "relative_trend": decision.relative_trend,
        "reason_codes": decision.reason_codes,
        "target_weights": decision.target_mapping(),
        "formal_clock": "month-end; valuation cutoff is two XSHG sessions earlier",
        "execution_clock": "no earlier than the next common session",
        "product_and_limit_gate": "not evaluated by this command",
        "authority": "signal-calculation-only-not-an-executable-order",
    }
    if args.marked_weights is not None:
        assessment = assess_maintenance(
            marked_weights=_c11_asset_weights(args.marked_weights),
            target_weights=decision.target_mapping(),
        )
        output["maintenance"] = {
            "maximum_absolute_gap": assessment.maximum_absolute_gap,
            "marked_static_stress": assessment.marked_static_stress,
            "ordinary_drift_triggered": assessment.ordinary_drift_triggered,
            "stress_maintenance_triggered": assessment.stress_maintenance_triggered,
            "reason_codes": assessment.reason_codes,
        }
    _emit(output)
    return 0


def _decision(args: argparse.Namespace) -> int:
    valuations = _key_values(args.valuations, allow_missing=True)
    momentum = _key_values(args.momentum, allow_missing=True)
    signal = G6Signal(
        decision_session=args.session,
        momentum_10m=momentum,
        valuation_scores=valuations,
        valuation_sessions=tuple((key, args.valuation_session) for key in EQUITY_SLEEVES),
    )
    decision = decide_tilt(standard_g6_1_policy(), signal)
    _emit(
        {
            "decision_session": decision.decision_session,
            "donor_sleeve": decision.donor_sleeve,
            "receiver_sleeve": decision.receiver_sleeve,
            "target_weights": decision.target_mapping(),
            "reason_codes": decision.reason_codes,
            "funding_rule": "2.5% moves only between equity sleeves; bond and gold are unchanged",
            "product_and_limit_gate": "not evaluated by this command",
            "authority": "signal-calculation-only-not-an-executable-order",
        }
    )
    return 0


def _verify(args: argparse.Namespace) -> int:
    _emit(verify_public_evidence(args.ledger, args.summary))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect public fund-strategy research policies and evidence"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("policy", help="print the frozen research policy")
    subparsers.add_parser(
        "standard-64-policy",
        help="print the exact fixed 35/10/15/5/5/20/10 research baseline",
    )
    subparsers.add_parser(
        "c11-policy", help="print the public C11 state machine and risk thresholds"
    )

    c11_decision = subparsers.add_parser(
        "c11-decision",
        help="calculate a C11 state from explicit point-in-time research signals",
    )
    c11_decision.add_argument(
        "--previous-state", required=True, choices=[state.value for state in C11State]
    )
    c11_decision.add_argument("--pe-percentile", required=True)
    c11_decision.add_argument("--pb-percentile", required=True)
    c11_decision.add_argument("--relative-trend", required=True)
    c11_decision.add_argument(
        "--marked-weights",
        help="optional seven asset=weight assignments for drift/stress assessment",
    )

    decision = subparsers.add_parser(
        "decision",
        help="calculate a quarter-end target from explicitly supplied research signals",
    )
    decision.add_argument("--session", required=True, help="decision session, YYYY-MM-DD")
    decision.add_argument(
        "--valuation-session",
        required=True,
        help="latest valuation observation session strictly before the decision",
    )
    decision.add_argument(
        "--valuations",
        required=True,
        help="six comma-separated sleeve=percentile decimals; NA is allowed",
    )
    decision.add_argument(
        "--momentum",
        required=True,
        help="six comma-separated sleeve=10-month-return decimals; NA is allowed",
    )

    verify = subparsers.add_parser(
        "verify-evidence", help="recalculate metrics from the public daily ledger"
    )
    verify.add_argument("--ledger", type=Path, required=True)
    verify.add_argument("--summary", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "policy":
        return _policy()
    if args.command == "standard-64-policy":
        return _standard_64_policy()
    if args.command == "c11-policy":
        return _c11_policy()
    if args.command == "c11-decision":
        return _c11_decision(args)
    if args.command == "decision":
        return _decision(args)
    if args.command == "verify-evidence":
        return _verify(args)
    raise AssertionError(f"unreachable command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
