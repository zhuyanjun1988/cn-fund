from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path

from cn_fund_strategy.application.g1_replay import G1ReplayScenario, source_hashes, with_g1_decimal_context, write_json_create_or_identical
from cn_fund_strategy.application.g6_replay import (
    build_current_g6_briefing,
    run_g6_fixed_event_settlement_stress,
    run_g6_replay,
)
from cn_fund_strategy.domain.g1_global_valuation_batch import BOND_SLEEVE, GOLD_SLEEVE
from cn_fund_strategy.domain.g6_quarterly_valuation_tilt import standard_g6_1_policy, target_with_state
from cn_fund_strategy.interfaces.g1_research_cli import _inputs, _read_status_rows
from cn_fund_strategy.interfaces.g3_research_cli import _current_order_gate, _decimal, _hash


ZERO = Decimal("0")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the preregistered G6-Standard replay")
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


@with_g1_decimal_context
def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    policy = standard_g6_1_policy()
    inputs = _inputs(args.input_root, args.start_session)
    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    contract_hash = _hash(args.contract)
    if contract["technical_id"] != policy.technical_id:
        raise ValueError("contract and G6 implementation mismatch")
    economic_contract = contract
    if "objective_gates" not in economic_contract:
        parent_binding = contract.get("parent_contract")
        if not isinstance(parent_binding, dict):
            raise ValueError("technical correction contract lacks parent contract binding")
        parent_path = Path(str(parent_binding["path"]))
        if not parent_path.is_absolute():
            parent_path = Path(__file__).resolve().parents[3] / parent_path
        if _hash(parent_path) != str(parent_binding["sha256"]):
            raise ValueError("technical correction parent contract hash mismatch")
        economic_contract = json.loads(parent_path.read_text(encoding="utf-8"))
    benchmark_hash = _hash(args.benchmark_result)
    if benchmark_hash != economic_contract["objective_gates"]["preregistered_benchmark_result_sha256"]:
        raise ValueError("benchmark hash differs from contract")
    benchmark_payload = json.loads(args.benchmark_result.read_text(encoding="utf-8"))
    benchmark_metrics = benchmark_payload["historical_development_results"]["canonical_benchmark_static65_annual"]["metrics"]

    base = run_g6_replay(inputs, G1ReplayScenario("base-cost50bp-lag2", 2, Decimal("0.005")), policy=policy)
    high = run_g6_replay(inputs, G1ReplayScenario("high-cost100bp-lag2", 2, Decimal("0.01")), policy=policy)
    extreme = run_g6_replay(inputs, G1ReplayScenario("extreme-cost250bp-lag10", 10, Decimal("0.025")), policy=policy)
    settlement = run_g6_fixed_event_settlement_stress(
        inputs, base, redemption_settlement_common_sessions=10, policy=policy
    )
    current_rows = build_current_g6_briefing(
        inputs,
        base,
        decision_session=args.decision_session,
        valuation_cutoff_session=args.valuation_cutoff_session,
        policy=policy,
    )
    status_rows = _read_status_rows(args.status_manifest)
    product_evidence = json.loads(args.product_evidence_manifest.read_text(encoding="utf-8"))
    current_orders = [
        _current_order_gate(
            row,
            status_rows=status_rows,
            scheduled_execution_session=args.scheduled_execution_session,
            product_evidence=product_evidence,
        )
        for row in current_rows
    ]

    base_record, high_record, extreme_record = base.as_record(), high.as_record(), extreme.as_record()
    bm, hm, em = base_record["metrics"], high_record["metrics"], extreme_record["metrics"]
    r3, r5 = base_record["rolling_3y"], base_record["rolling_5y"]
    excess = _decimal(bm["net_cagr_decimal"]) - _decimal(benchmark_metrics["net_cagr_decimal"])
    max_stress = max(base.maximum_extreme_static_loss, high.maximum_extreme_static_loss, extreme.maximum_extreme_static_loss)
    gates = {
        "history_at_least_2521_common_sessions": base.sessions >= 2521,
        "base_net_CAGR_gte_10pct": _decimal(bm["net_cagr_decimal"]) >= Decimal("0.10"),
        "base_normal_MDD_lte_35pct": _decimal(bm["maximum_drawdown_decimal"]) <= Decimal("0.35"),
        "maximum_extreme_static_loss_lte_45pct": max_stress <= Decimal("0.45"),
        "base_Calmar_gte_0_30": _decimal(bm["calmar_decimal"]) >= Decimal("0.30"),
        "net_excess_CAGR_vs_frozen_G1_static65_gt_zero": excess > ZERO,
        "high_cost_100bp_net_CAGR_gte_9_5pct": _decimal(hm["net_cagr_decimal"]) >= Decimal("0.095"),
        "lag10_cost250bp_net_CAGR_gte_9pct": _decimal(em["net_cagr_decimal"]) >= Decimal("0.09"),
        "rolling_3y_minimum_CAGR_gte_zero": _decimal(r3["minimum_CAGR_decimal"]) >= ZERO,
        "rolling_3y_fraction_CAGR_gte_10pct_gte_0_50": _decimal(r3["fraction_CAGR_gte_10pct_decimal"]) >= Decimal("0.50"),
        "rolling_5y_minimum_CAGR_gte_7pct": _decimal(r5["minimum_CAGR_decimal"]) >= Decimal("0.07"),
        "rolling_5y_fraction_CAGR_gte_10pct_gte_0_60": _decimal(r5["fraction_CAGR_gte_10pct_decimal"]) >= Decimal("0.60"),
    }
    all_gates = all(gates.values())
    state = base.ending_states["GLOBAL"]
    model_target = target_with_state(policy, state)  # type: ignore[arg-type]
    names = {row["sleeve_id"]: row["fund_name"] for row in current_rows}
    initialization: list[dict[str, object]] = []
    for sleeve_id in tuple(names) + (GOLD_SLEEVE, BOND_SLEEVE):
        code = policy.sleeve(sleeve_id).fund_code if sleeve_id in names else "000216" if sleeve_id == GOLD_SLEEVE else "161120"
        name = str(names[sleeve_id]) if sleeve_id in names else "华安黄金ETF联接A" if sleeve_id == GOLD_SLEEVE else "易方达中债新综指发起式(LOF)C"
        weight = model_target[sleeve_id]
        initialization.append(
            _current_order_gate(
                {
                    "sleeve_id": sleeve_id,
                    "fund_code": code,
                    "fund_name": name,
                    "action_batches": 1 if weight > ZERO else 0,
                    "requested_weight_change_decimal": weight,
                    "target_weight_decimal": weight,
                    "reason_codes": ("NEW_MODEL_PORTFOLIO_INITIALIZATION_ONLY",),
                },
                status_rows=status_rows,
                scheduled_execution_session=args.scheduled_execution_session,
                product_evidence=product_evidence,
            )
        )

    project_root = Path(__file__).resolve().parents[3]
    payload = {
        "schema_version": "g6-standard-quarterly-valuation-tilt-result-v1",
        "generated_at": args.generated_at,
        "candidate": {
            "canonical_name": policy.canonical_name,
            "technical_id": policy.technical_id,
            "status": "L1-historical-economic-gates-passed-product-and-forward-closure-required" if all_gates else "rejected-L1-historical-economic-gates-failed",
            "historical_results_are_clean_OOS": False,
            "formation_was_performance_informed": True,
            "simulation_approved": False,
            "safe_for_account_order_placement": False,
        },
        "source_bindings": {
            "input_hashes": source_hashes(inputs),
            "contract_path": str(args.contract.resolve()), "contract_sha256": contract_hash,
            "benchmark_result_path": str(args.benchmark_result.resolve()), "benchmark_result_sha256": benchmark_hash,
            "domain_code_sha256": _hash(project_root / "src/cn_fund_strategy/domain/g6_quarterly_valuation_tilt.py"),
            "replay_code_sha256": _hash(project_root / "src/cn_fund_strategy/application/g6_replay.py"),
            "generator_code_sha256": _hash(Path(__file__)),
            "status_manifest_path": str(args.status_manifest.resolve()), "status_manifest_sha256": _hash(args.status_manifest),
            "product_evidence_manifest_path": str(args.product_evidence_manifest.resolve()),
            "product_evidence_manifest_sha256": _hash(args.product_evidence_manifest),
        },
        "historical_L1_contaminated_results": {
            "base": base_record,
            "high_cost_100bp": high_record,
            "extreme_lag10_cost250bp": extreme_record,
            "redemption_settlement_10_common_sessions": settlement,
            "preregistered_frozen_G1_static65_benchmark_metrics": benchmark_metrics,
            "net_excess_CAGR_vs_preregistered_benchmark_decimal": excess,
            "maximum_extreme_static_loss_across_scenarios_decimal": max_stress,
            "objective_and_robustness_gates": gates,
            "all_gates_passed": all_gates,
        },
        "current_research_briefing": {
            "decision_session": args.decision_session,
            "current_phase": "between-quarter-ends",
            "next_formal_decision_rule": "last-common-session-of-September-2026-then-two-common-session-lag",
            "next_common_session_order_list": current_orders,
            "model_target_weights": {key: format(value, "f") for key, value in model_target.items()},
            "model_marked_weights": {key: format(value, "f") for key, value in base.ending_weights.items()},
            "current_tilt_state": _json_state(state),
            "new_portfolio_initialization_gates_per_100k": initialization,
            "real_account_instruction": "NO_TRADE-research-only-no-account-state-accessed",
        },
        "material_limitations": [
            "all-history-and-structure-selection-are-contaminated-formation-evidence-not-clean-OOS",
            "50pct-equity-core-contains-material-Nasdaq-SP500-US-growth-overlap",
            "retrospective-valuation-capture-is-not-strict-contemporaneous-PIT",
            "historical-product-availability-and-QDII-limit-history-is-incomplete",
            "current-QDII-suspension-or-cap-can-block-new-portfolio-initialization",
            "listed-ETF-substitution-requires-same-session-premium-spread-depth-and-trading-state",
            "50bp-is-an-aggregate-cost-envelope-not-a-complete-point-in-time-fee-table",
            "no-account-holdings-lots-credentials-or-orders-were-accessed",
            "historical-pass-cannot-authorize-trading-before-independent-recalc-product-closure-and-forward-shadow",
        ],
    }
    write_json_create_or_identical(args.daily_ledger_output, base.daily_ledger_record())
    write_json_create_or_identical(args.output, payload)
    print(json.dumps({"output": str(args.output.resolve()), "daily_ledger": str(args.daily_ledger_output.resolve()), "all_gates_passed": all_gates, "gates": gates}, ensure_ascii=False))
    return 0


def _json_state(state: object) -> dict[str, object]:
    return {
        "donor_sleeve": getattr(state, "donor_sleeve", None),
        "receiver_sleeve": getattr(state, "receiver_sleeve", None),
    }


if __name__ == "__main__":
    raise SystemExit(main())
