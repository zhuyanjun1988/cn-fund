"""Small public CLI for inspecting the frozen policy and verifying evidence."""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
from typing import Sequence

from cn_fund_strategy.application.public_evidence import verify_public_evidence
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
        description="Inspect the G6 research policy and verify its public evidence"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("policy", help="print the frozen research policy")

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
    if args.command == "decision":
        return _decision(args)
    if args.command == "verify-evidence":
        return _verify(args)
    raise AssertionError(f"unreachable command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
