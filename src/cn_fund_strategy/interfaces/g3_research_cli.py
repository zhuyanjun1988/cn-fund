from __future__ import annotations

import argparse
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re

from cn_fund_strategy.application.g1_replay import (
    G1ReplayScenario,
    source_hashes,
    with_g1_decimal_context,
    write_json_create_or_identical,
)
from cn_fund_strategy.application.g3_replay import (
    build_current_g3_decisions,
    run_g3_fixed_event_settlement_stress,
    run_g3_replay,
)
from cn_fund_strategy.domain.g1_global_valuation_batch import BOND_SLEEVE, GOLD_SLEEVE
from cn_fund_strategy.domain.g3_dual_momentum_valuation_batch import standard_g3_1_policy
from cn_fund_strategy.interfaces.g1_research_cli import (
    _daily_cap,
    _inputs,
    _read_status_rows,
    _status_text,
)


ZERO = Decimal("0")
LISTED_DOMESTIC_ETFS = {"510300", "510500", "159915"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the preregistered G3-Standard research replay")
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--status-manifest", type=Path, required=True)
    parser.add_argument("--product-evidence-manifest", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--benchmark-result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--daily-ledger-output", type=Path, required=True)
    parser.add_argument("--generated-at", required=True)
    parser.add_argument("--start-session", default="2015-01-05")
    parser.add_argument("--decision-session", required=True)
    parser.add_argument("--valuation-cutoff-session", required=True)
    parser.add_argument("--scheduled-execution-session", required=True)
    return parser


def _hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _displayed_purchase_fee_rates(text: str | None) -> tuple[str, ...]:
    if not text or "购买手续费" not in text:
        return ()
    fragment = text.split("购买手续费", 1)[1].split("费率详情", 1)[0]
    return tuple(re.findall(r"\d+(?:\.\d+)?%", fragment))


def _current_order_gate(
    row: dict[str, object],
    *,
    status_rows: dict[str, dict[str, object]],
    scheduled_execution_session: str,
    product_evidence: dict[str, object],
) -> dict[str, object]:
    code = str(row["fund_code"])
    batches = int(row["action_batches"])
    requested_amount = abs(_decimal(row["requested_weight_change_decimal"])) * Decimal("100000")
    direction = "HOLD" if batches == 0 else "BUY" if batches > 0 else "SELL"
    status_row = status_rows.get(code)
    text = _status_text(status_row) if status_row else None
    daily_cap = _daily_cap(text)
    reasons: list[str] = []
    status = "hold-no-economic-order"
    executable = ZERO
    if direction == "HOLD":
        reasons.append("NO_BATCH_CHANGE")
    elif code in LISTED_DOMESTIC_ETFS:
        status = "blocked-missing-same-session-listed-ETF-quote-spread-depth-and-trading-state"
        reasons.append("CURRENT_ETF_EXECUTION_EVIDENCE_QUARANTINED")
    elif text is None:
        status = "blocked-missing-current-product-status"
        reasons.append("NO_CURRENT_STATUS_OWNER_TEXT")
    elif direction == "BUY":
        if "暂停申购" in text:
            status = "blocked-current-purchase-suspended"
            reasons.append("CURRENT_PURCHASE_SUSPENDED")
        elif daily_cap is not None and requested_amount > daily_cap:
            status = "blocked-current-daily-purchase-cap-insufficient"
            reasons.extend(("CURRENT_DAILY_CAP_BELOW_REQUEST", "DO_NOT_SPLIT_TO_EVADE_LIMIT"))
        elif "开放申购" in text or "限大额" in text:
            status = "conditional-current-buy-gate-passed-recheck-on-execution-session"
            executable = requested_amount
            reasons.append("CURRENT_BUY_STATUS_AND_CAP_PASS_AT_CAPTURE_ONLY")
        else:
            status = "blocked-current-buy-state-unresolved"
            reasons.append("CURRENT_BUY_STATE_UNRESOLVED")
    else:
        if "开放赎回" in text:
            status = "conditional-current-sell-gate-passed-lot-fee-and-execution-day-recheck-required"
            executable = requested_amount
            reasons.extend(("CURRENT_REDEMPTION_OPEN", "ACTUAL_LOT_HOLDING_PERIOD_REQUIRED"))
        else:
            status = "blocked-current-sell-state-unresolved"
            reasons.append("CURRENT_REDEMPTION_STATE_UNRESOLVED")

    fee_record: dict[str, object] = {
        "displayed_purchase_fee_rates": _displayed_purchase_fee_rates(text),
        "historical_replay_cost_envelope_decimal": "0.005",
        "historical_cost_envelope_is_exact_current_fee_table": False,
    }
    if code == "000071" and direction == "SELL":
        facts = product_evidence.get("verified_facts", {})
        fee_record.update(
            {
                "redemption_fee_holding_gte_7d_decimal": facts.get(
                    "redemption_fee_holding_gte_7d_decimal"
                ),
                "redemption_fee_holding_lt_7d_decimal": facts.get(
                    "redemption_fee_holding_lt_7d_decimal"
                ),
                "contractual_redemption_payment_deadline": facts.get(
                    "prospectus_redemption_payment_deadline"
                ),
            }
        )
    result = dict(row)
    result.update(
        {
            "direction": direction,
            "requested_amount_per_100k_model_CNY": requested_amount,
            "currently_executable_amount_per_100k_model_CNY": executable,
            "current_order_gate_status": status,
            "current_order_gate_reasons": tuple(reasons),
            "current_status_owner_text": text,
            "current_daily_buy_cap_CNY": daily_cap,
            "current_status_evidence_status": (
                status_row.get("evidence_batch_status") if status_row else "missing"
            ),
            "scheduled_execution_session": scheduled_execution_session if batches else None,
            "same_session_recheck_required": bool(batches),
            "cost_and_settlement": fee_record,
        }
    )
    return result


@with_g1_decimal_context
def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    policy = standard_g3_1_policy()
    inputs = _inputs(args.input_root, args.start_session)
    contract_hash = _hash(args.contract)
    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    if contract["technical_id"] != policy.technical_id:
        raise ValueError("contract and G3 implementation technical_id mismatch")

    benchmark_hash = _hash(args.benchmark_result)
    if benchmark_hash != contract["objective_gates"]["preregistered_benchmark_result_sha256"]:
        raise ValueError("benchmark result hash differs from preregistration")
    benchmark_payload = json.loads(args.benchmark_result.read_text(encoding="utf-8"))
    benchmark_metrics = benchmark_payload["historical_development_results"][
        "canonical_benchmark_static65_annual"
    ]["metrics"]

    base = run_g3_replay(
        inputs,
        G1ReplayScenario("base-cost50bp-lag2", 2, Decimal("0.005")),
        policy=policy,
    )
    high_cost = run_g3_replay(
        inputs,
        G1ReplayScenario("high-cost100bp-lag2", 2, Decimal("0.01")),
        policy=policy,
    )
    extreme = run_g3_replay(
        inputs,
        G1ReplayScenario("extreme-cost250bp-lag10", 10, Decimal("0.025")),
        policy=policy,
    )
    settlement = run_g3_fixed_event_settlement_stress(
        inputs,
        base,
        redemption_settlement_common_sessions=10,
        policy=policy,
    )

    current_rows = build_current_g3_decisions(
        inputs,
        base,
        decision_session=args.decision_session,
        valuation_cutoff_session=args.valuation_cutoff_session,
        policy=policy,
    )
    status_rows = _read_status_rows(args.status_manifest)
    product_evidence = json.loads(
        args.product_evidence_manifest.read_text(encoding="utf-8")
    )
    current_orders = [
        _current_order_gate(
            row,
            status_rows=status_rows,
            scheduled_execution_session=args.scheduled_execution_session,
            product_evidence=product_evidence,
        )
        for row in current_rows
    ]

    base_record = base.as_record()
    high_record = high_cost.as_record()
    extreme_record = extreme.as_record()
    base_metrics = base_record["metrics"]
    high_metrics = high_record["metrics"]
    extreme_metrics = extreme_record["metrics"]
    rolling_3y = base_record["rolling_3y"]
    rolling_5y = base_record["rolling_5y"]
    benchmark_cagr = _decimal(benchmark_metrics["net_cagr_decimal"])
    net_excess = _decimal(base_metrics["net_cagr_decimal"]) - benchmark_cagr
    maximum_extreme_static_loss = max(
        base.maximum_extreme_static_loss,
        high_cost.maximum_extreme_static_loss,
        extreme.maximum_extreme_static_loss,
    )
    gates = {
        "history_at_least_2521_common_sessions": base.sessions >= 2521,
        "base_net_CAGR_gte_10pct": _decimal(base_metrics["net_cagr_decimal"])
        >= Decimal("0.10"),
        "base_normal_MDD_lte_35pct": _decimal(base_metrics["maximum_drawdown_decimal"])
        <= Decimal("0.35"),
        "maximum_extreme_static_loss_lte_45pct": maximum_extreme_static_loss
        <= Decimal("0.45"),
        "base_Calmar_gte_0_30": _decimal(base_metrics["calmar_decimal"])
        >= Decimal("0.30"),
        "net_excess_CAGR_vs_frozen_G1_static65_gt_zero": net_excess > ZERO,
        "high_cost_100bp_net_CAGR_gte_9_5pct": _decimal(high_metrics["net_cagr_decimal"])
        >= Decimal("0.095"),
        "lag10_cost250bp_net_CAGR_gte_9pct": _decimal(extreme_metrics["net_cagr_decimal"])
        >= Decimal("0.09"),
        "rolling_3y_minimum_CAGR_gte_zero": _decimal(rolling_3y["minimum_CAGR_decimal"])
        >= ZERO,
        "rolling_3y_fraction_CAGR_gte_10pct_gte_0_50": _decimal(
            rolling_3y["fraction_CAGR_gte_10pct_decimal"]
        )
        >= Decimal("0.50"),
        "rolling_5y_minimum_CAGR_gte_7pct": _decimal(rolling_5y["minimum_CAGR_decimal"])
        >= Decimal("0.07"),
        "rolling_5y_fraction_CAGR_gte_10pct_gte_0_60": _decimal(
            rolling_5y["fraction_CAGR_gte_10pct_decimal"]
        )
        >= Decimal("0.60"),
    }
    all_gates_passed = all(gates.values())
    economic_action_count = sum(int(row["action_batches"] != 0) for row in current_orders)
    bond_intent = -sum(
        (_decimal(row["requested_weight_change_decimal"]) for row in current_orders),
        ZERO,
    )

    payload = {
        "schema_version": "g3-standard-1-dual-momentum-valuation-batch-result-v1",
        "generated_at": args.generated_at,
        "candidate": {
            "canonical_name": policy.canonical_name,
            "technical_id": policy.technical_id,
            "status": (
                "L1-historical-economic-gates-passed-forward-closure-required"
                if all_gates_passed
                else "rejected-L1-historical-economic-gates-failed"
            ),
            "historical_results_are_clean_OOS": False,
            "simulation_approved": False,
            "safe_for_account_order_placement": False,
        },
        "source_bindings": {
            "input_hashes": source_hashes(inputs),
            "contract_path": str(args.contract.resolve()),
            "contract_sha256": contract_hash,
            "benchmark_result_path": str(args.benchmark_result.resolve()),
            "benchmark_result_sha256": benchmark_hash,
            "domain_code_sha256": _hash(
                Path(__file__).resolve().parents[1]
                / "domain"
                / "g3_dual_momentum_valuation_batch.py"
            ),
            "replay_code_sha256": _hash(
                Path(__file__).resolve().parents[1] / "application" / "g3_replay.py"
            ),
            "generator_code_sha256": _hash(Path(__file__)),
            "status_manifest_path": str(args.status_manifest.resolve()),
            "status_manifest_sha256": _hash(args.status_manifest),
            "product_evidence_manifest_path": str(
                args.product_evidence_manifest.resolve()
            ),
            "product_evidence_manifest_sha256": _hash(args.product_evidence_manifest),
        },
        "historical_L1_contaminated_results": {
            "base": base_record,
            "high_cost_100bp": high_record,
            "extreme_lag10_cost250bp": extreme_record,
            "redemption_settlement_10_common_sessions": settlement,
            "preregistered_frozen_G1_static65_benchmark_metrics": benchmark_metrics,
            "net_excess_CAGR_vs_preregistered_benchmark_decimal": net_excess,
            "maximum_extreme_static_loss_across_scenarios_decimal": maximum_extreme_static_loss,
            "objective_and_robustness_gates": gates,
            "all_gates_passed": all_gates_passed,
        },
        "current_research_briefing": {
            "decision_session": args.decision_session,
            "scheduled_execution_session": args.scheduled_execution_session,
            "valuation_cutoff_session": args.valuation_cutoff_session,
            "economic_action_count": economic_action_count,
            "orders_per_100k_model_portfolio": current_orders,
            "defensive_legs": [
                {
                    "fund_code": "000216",
                    "fund_name": "华安黄金ETF联接A",
                    "direction": "HOLD",
                    "marked_weight_decimal": base.ending_weights[GOLD_SLEEVE],
                },
                {
                    "fund_code": "161120",
                    "fund_name": "易方达中债新综指发起式(LOF)C",
                    "economic_direction": (
                        "BUY_AFTER_NET_REDEMPTION_SETTLEMENT"
                        if bond_intent > ZERO
                        else "SELL_OR_REDEEM_TO_FUND_ELIGIBLE_BUYS"
                        if bond_intent < ZERO
                        else "HOLD"
                    ),
                    "economic_weight_change_decimal": bond_intent,
                    "execution_rule": "only-the-net-of-currently-executable-equity-legs-after-same-session-recheck",
                },
            ],
            "real_account_instruction": "NO_TRADE-research-only-no-account-state-accessed",
        },
        "material_limitations": [
            "all-2015-to-2026-history-is-contaminated-L1-formation-evidence-not-clean-OOS",
            "Danjuan-valuation-history-is-a-2026-retrospective-capture-not-a-strict-contemporaneous-PIT-archive",
            "historical-valuation-provider-coverage-before-2016-08-uses-the-preregistered-neutral-prior",
            "historical-product-availability-and-QDII-limit-history-is-incomplete",
            "50bp-cost-is-a-conservative-aggregate-envelope-not-a-complete-point-in-time-product-fee-table",
            "listed-ETF-current-orders-require-same-session-quote-spread-depth-and-trading-state",
            "QDII-current-buy-caps-can-block-the-economic-target",
            "actual-lots-holding-period-capital-taxes-channel-fees-and-account-state-were-not-accessed",
            "historical-pass-if-any-cannot-authorize-simulation-or-trading-without-forward-and-product-closure",
        ],
    }
    write_json_create_or_identical(args.daily_ledger_output, base.daily_ledger_record())
    write_json_create_or_identical(args.output, payload)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "daily_ledger": str(args.daily_ledger_output.resolve()),
                "all_gates_passed": all_gates_passed,
                "gates": gates,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
