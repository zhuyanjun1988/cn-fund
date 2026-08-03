from __future__ import annotations

import argparse
from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

from cn_fund_strategy.application.g1_replay import (
    G1ReplayInputs,
    G1ReplayScenario,
    build_live_decisions,
    run_fixed_event_settlement_stress,
    run_g1_replay,
    run_static_benchmark,
    source_hashes,
    with_g1_decimal_context,
    write_json_create_or_identical,
)
from cn_fund_strategy.domain.g1_global_valuation_batch import (
    G1ProductAvailability,
    gate_order,
    standard_g1_policy,
)


PROFILE_CODES = {
    "CN300": "510300",
    "CN500": "510500",
    "GROWTH": "159915",
    "HK": "000071",
    "SP500": "050025",
    "NASDAQ": "270042",
    "GOLD": "000216",
    "BOND": "161120",
}
VALUATION_CODES = {
    "CN300": "SH000300",
    "CN500": "SH000905",
    "GROWTH": "SZ399006",
    "HK": "HKHSI",
    "SP500": "SP500",
    "NASDAQ": "NDX",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the frozen G1-Standard research replay")
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--status-manifest", type=Path, required=True)
    parser.add_argument("--product-evidence-manifest", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
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


def _inputs(root: Path, start_session: str) -> G1ReplayInputs:
    return G1ReplayInputs(
        profile_paths={
            sleeve_id: root / "eastmoney-profile" / f"{code}.js"
            for sleeve_id, code in PROFILE_CODES.items()
        },
        valuation_directories={
            sleeve_id: root / "danjuan" / code
            for sleeve_id, code in VALUATION_CODES.items()
        },
        start_session=start_session,
    )


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _read_status_rows(path: Path) -> dict[str, dict[str, object]]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(row["capture"]["fund_code"]): row
        for row in manifest["rows"]
    }


def _status_text(row: dict[str, object]) -> str | None:
    projection = row.get("projection")
    if not isinstance(projection, dict):
        return None
    value = projection.get("status_fragment_owner_text")
    return str(value) if value is not None else None


def _daily_cap(text: str | None) -> Decimal | None:
    if not text or "单日累计购买上限" not in text:
        return None
    tail = text.split("单日累计购买上限", 1)[1]
    number = "".join(character for character in tail.split("元", 1)[0] if character.isdigit() or character == ".")
    return Decimal(number) if number else None


def _live_record(
    row: dict[str, object],
    *,
    status_rows: dict[str, dict[str, object]],
    product_evidence: dict[str, object],
    scheduled_execution_session: str,
) -> dict[str, object]:
    code = str(row["fund_code"])
    status_row = status_rows.get(code)
    text = _status_text(status_row) if status_row else None
    decision = row["decision"]
    action_batches = int(row["action_batches"])
    availability = G1ProductAvailability(
        fund_code=code,
        product_name=str(row["fund_name"]),
        as_of="2026-08-02",
        buy_allowed=(
            True
            if text and ("开放申购" in text or "限大额" in text)
            else False if text and "暂停申购" in text else None
        ),
        sell_allowed=True if text and "开放赎回" in text else None,
        daily_buy_cap_cny=_daily_cap(text),
        evidence_status=(
            str(status_row["evidence_batch_status"])
            if status_row
            else "missing-current-status-evidence"
        ),
    )
    gated = gate_order(
        decision,
        availability,
        portfolio_value_cny=Decimal("100000"),
    )
    result = {
        key: value
        for key, value in row.items()
        if key not in {"signal", "decision"}
    }
    result["signal"] = asdict(row["signal"])
    result["decision"] = asdict(decision)
    result["current_status_owner_text"] = text
    result["order_gate_per_100k_model_portfolio"] = asdict(gated)
    result["briefing_session"] = "2026-08-03"
    result["scheduled_execution_session"] = (
        scheduled_execution_session if action_batches else None
    )
    result["same_day_recheck_required"] = bool(action_batches)
    if code == "000071" and action_batches < 0:
        facts = product_evidence["verified_facts"]
        result["product_cost_and_settlement"] = {
            "virtual_batch_holding_period_status": "entered-2024-and-longer-than-7-days",
            "actual-account-lot-holding-period-required": True,
            "redemption_fee_if_actual_lot_gte_7d_decimal": facts[
                "redemption_fee_holding_gte_7d_decimal"
            ],
            "redemption_fee_if_actual_lot_lt_7d_decimal": facts[
                "redemption_fee_holding_lt_7d_decimal"
            ],
            "contractual_redemption_payment_deadline": facts[
                "prospectus_redemption_payment_deadline"
            ],
            "per_100k_gross_redemption_cny": "1250",
            "per_100k_estimated_fee_if_gte_7d_cny": "6.25",
            "per_100k_estimated_net_proceeds_cny": "1243.75",
            "destination": "settlement-cash-then-161120-on-actual-receipt-day-if-current-gate-passes",
        }
    return result


@with_g1_decimal_context
def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    policy = standard_g1_policy()
    inputs = _inputs(args.input_root, args.start_session)
    base_scenario = G1ReplayScenario("base", 2, Decimal("0.005"))
    base = run_g1_replay(inputs, base_scenario, policy=policy)
    high_cost = run_g1_replay(
        inputs,
        G1ReplayScenario("high-cost-100bp", 2, Decimal("0.01")),
        policy=policy,
    )
    extreme = run_g1_replay(
        inputs,
        G1ReplayScenario("extreme-lag10-cost250bp", 10, Decimal("0.025")),
        policy=policy,
    )
    annual = run_static_benchmark(
        inputs,
        G1ReplayScenario("benchmark", 2, Decimal("0.005")),
        annual_rebalance=True,
        policy=policy,
    )
    buyhold = run_static_benchmark(
        inputs,
        G1ReplayScenario("benchmark-diagnostic", 2, Decimal("0.005")),
        annual_rebalance=False,
        policy=policy,
    )
    settlement = run_fixed_event_settlement_stress(
        inputs,
        base,
        redemption_settlement_common_sessions=10,
    )
    live = build_live_decisions(
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
    live_records = [
        _live_record(
            item,
            status_rows=status_rows,
            product_evidence=product_evidence,
            scheduled_execution_session=args.scheduled_execution_session,
        )
        for item in live
    ]

    base_metrics = base.metrics()
    extreme_metrics = extreme.metrics()
    annual_metrics = annual["metrics"]
    net_excess = _decimal(base_metrics["net_cagr_decimal"]) - _decimal(
        annual_metrics["net_cagr_decimal"]
    )
    buyhold_gap = _decimal(base_metrics["net_cagr_decimal"]) - _decimal(
        buyhold["metrics"]["net_cagr_decimal"]
    )
    gates = {
        "net_cagr_gte_10pct": _decimal(base_metrics["net_cagr_decimal"]) >= Decimal("0.10"),
        "normal_mdd_lte_35pct": _decimal(base_metrics["maximum_drawdown_decimal"]) <= Decimal("0.35"),
        "extreme_mdd_lte_45pct": _decimal(extreme_metrics["maximum_drawdown_decimal"]) <= Decimal("0.45"),
        "calmar_gte_0_30": _decimal(base_metrics["calmar_decimal"]) >= Decimal("0.30"),
        "net_excess_vs_static65_annual_gt_zero": net_excess > Decimal("0"),
    }
    payload = {
        "schema_version": "g1-standard-24h-research-result-v1",
        "generated_at": args.generated_at,
        "candidate": {
            "canonical_name": policy.canonical_name,
            "technical_id": policy.technical_id,
            "status": "candidate-historical-development-contaminated",
            "historical_results_are_clean_oos": False,
            "safe_for-account-order-placement": False,
        },
        "source_bindings": {
            "input_hashes": source_hashes(inputs),
            "contract_path": str(args.contract.resolve()),
            "contract_sha256": _hash(args.contract),
            "status_manifest_path": str(args.status_manifest.resolve()),
            "status_manifest_sha256": _hash(args.status_manifest),
            "product_evidence_manifest_path": str(
                args.product_evidence_manifest.resolve()
            ),
            "product_evidence_manifest_sha256": _hash(
                args.product_evidence_manifest
            ),
            "domain_code_sha256": _hash(
                Path(__file__).resolve().parents[1]
                / "domain"
                / "g1_global_valuation_batch.py"
            ),
            "replay_code_sha256": _hash(
                Path(__file__).resolve().parents[1]
                / "application"
                / "g1_replay.py"
            ),
        },
        "historical_development_results": {
            "base": base.as_record(),
            "high_cost_100bp": high_cost.as_record(),
            "extreme_lag10_cost250bp": extreme.as_record(),
            "redemption_settlement_10_common_sessions": settlement,
            "canonical_benchmark_static65_annual": annual,
            "tougher_buyhold_diagnostic": buyhold,
            "net_excess_cagr_vs_canonical_benchmark_decimal": net_excess,
            "net_cagr_gap_vs_tougher_buyhold_diagnostic_decimal": buyhold_gap,
            "objective_gates": gates,
            "all_numeric_objective_gates_passed": all(gates.values()),
        },
        "current_briefing": {
            "decision_session": args.decision_session,
            "valuation_cutoff_session": args.valuation_cutoff_session,
            "briefing_session": "2026-08-03",
            "economic_action_count": sum(
                int(item["action_batches"] != 0) for item in live_records
            ),
            "orders": live_records,
            "defensive_legs": [
                {
                    "fund_code": "000216",
                    "fund_name": "华安黄金ETF联接A",
                    "direction": "HOLD",
                    "marked_weight_decimal": base.ending_weights["GOLD"],
                },
                {
                    "fund_code": "161120",
                    "fund_name": "易方达中债新综指发起式(LOF)C",
                    "direction": "CONDITIONAL_BUY_AFTER_000071_PROCEEDS_SETTLE",
                    "amount": "all-net-000071-redemption-proceeds",
                    "same_day_status_recheck_required": True,
                },
            ],
        },
        "material_limitations": [
            "all-historical-results-were-seen-during-development-and-are-not-clean-OOS",
            "point-in-time-historical-QDII-purchase-limit-history-is-incomplete",
            "base-product-proxy-replay-assumes-economic-fills-and-uses-currently-available-history-not-historical-distributor-gates",
            "single-aggregate-cost-scenarios-are-conservative-sensitivities-not-a-complete-time-varying-fee-table",
            "actual-account-capital-lots-taxes-channel-fees-and-holdings-were-not-accessed",
            "same-day-status-and-actual-lot-check-is-required-before-any-simulated-order-may-be-treated-as-user-actionable",
            "forward-shadow-after-2026-08-02T22:28:53+08:00-remains-required-for-promotion"
        ],
    }
    write_json_create_or_identical(args.output, payload)
    print(json.dumps({"output": str(args.output.resolve()), "gates": gates}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
