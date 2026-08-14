#!/usr/bin/env python3
"""Verify C46's public arithmetic, negative evidence and authority boundary."""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = (
    ROOT
    / "config"
    / "strategy-research-c46-v1"
    / "001_C46-Aggressive-1_public_contract.json"
)
SUMMARY = ROOT / "evidence" / "c46" / "public_research_summary.json"
DOC = ROOT / "docs" / "17_C46六成权益与三层流动性收益增强进取策略.md"
EVIDENCE_README = ROOT / "evidence" / "c46" / "README.md"
REGISTRY = ROOT / "research" / "catalog" / "strategy_registry.json"
ROUNDS = ROOT / "research" / "catalog" / "research_rounds.json"
LINEAGE = ROOT / "research" / "catalog" / "lineage.json"


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def close(left: Decimal, right: Decimal, tolerance: str = "1e-14") -> bool:
    return abs(left - right) <= Decimal(tolerance)


def verify() -> dict[str, Any]:
    contract = load(CONTRACT)
    summary = load(SUMMARY)
    registry = load(REGISTRY)
    rounds = load(ROUNDS)
    lineage = load(LINEAGE)

    boundary = summary["authority_boundary"]
    c46 = summary["formation_metrics"]["base_cost25bp_lag1"]
    c45 = summary["comparisons"]["C45_same_window_base25bp_lag1"]
    c44 = summary["comparisons"]["C44_same_window_base25bp_lag1"]
    c11 = summary["comparisons"]["C11_same_window_base25bp_lag1"]
    versus_c45 = summary["comparisons"]["C46_minus_C45"]
    versus_c44 = summary["comparisons"]["C46_minus_C44"]
    versus_c11 = summary["comparisons"]["C46_minus_C11"]
    structure = summary["static_and_current_structure"]
    negative = summary["negative_evidence"]

    weight_total = sum(Decimal(value) for value in summary["target_weights"].values())
    contract_weight_total = sum(
        Decimal(value) for value in contract["target_weights"].values()
    )
    equity_keys = (
        "024622_free_cash_flow_factor",
        "021550_dividend_low_volatility",
        "007593_CSI500_quality_growth",
    )
    rows = {row["canonical_name"]: row for row in registry["strategies"]}
    round_rows = {row["round_id"]: row for row in rounds["rounds"]}
    target_relations = [
        row for row in lineage["relations"] if row["target_id"] == "C46-Aggressive-1"
    ]

    checks = {
        "candidate_identity_matches": contract["candidate_id"]
        == summary["candidate_id"]
        == "C46-Aggressive-1",
        "technical_identity_matches": contract["technical_id"]
        == summary["technical_id"],
        "summary_weights_sum_to_one": close(weight_total, Decimal("1")),
        "contract_weights_sum_to_one": close(contract_weight_total, Decimal("1")),
        "contract_and_summary_weights_match": contract["target_weights"]
        == summary["target_weights"],
        "nominal_equity_is_sixty_percent": close(
            sum(Decimal(summary["target_weights"][key]) for key in equity_keys),
            Decimal("0.60"),
        ),
        "carry_short_debt_and_cash_are_exactly_five_percent": close(
            sum(
                Decimal(summary["target_weights"][key])
                for key in (
                    "000047_fixed_income_plus",
                    "110008_fixed_income_plus",
                    "110017_fixed_income_plus",
                    "007169_short_policy_bank_bond_current_route",
                    "cash_cny",
                )
            ),
            Decimal("0.05"),
        ),
        "C45_cagr_delta": close(
            (Decimal(c46["net_cagr"]) - Decimal(c45["net_cagr"])) * 100,
            Decimal(versus_c45["cagr_percentage_points"]),
        ),
        "C45_mdd_improvement": close(
            (
                Decimal(c45["maximum_drawdown"])
                - Decimal(c46["maximum_drawdown"])
            )
            * 100,
            Decimal(versus_c45["maximum_drawdown_improvement_percentage_points"]),
        ),
        "C45_calmar_improvement": close(
            Decimal(c46["calmar"]) - Decimal(c45["calmar"]),
            Decimal(versus_c45["calmar_improvement"]),
        ),
        "C45_ending_NAV_advantage": close(
            Decimal(c46["ending_nav"]) / Decimal(c45["ending_nav"]) - 1,
            Decimal(versus_c45["ending_nav_relative_advantage"]),
        ),
        "C44_cagr_delta": close(
            (Decimal(c46["net_cagr"]) - Decimal(c44["net_cagr"])) * 100,
            Decimal(versus_c44["cagr_percentage_points"]),
        ),
        "C44_mdd_improvement": close(
            (
                Decimal(c44["maximum_drawdown"])
                - Decimal(c46["maximum_drawdown"])
            )
            * 100,
            Decimal(versus_c44["maximum_drawdown_improvement_percentage_points"]),
        ),
        "C11_cagr_delta": close(
            (Decimal(c46["net_cagr"]) - Decimal(c11["net_cagr"])) * 100,
            Decimal(versus_c11["cagr_percentage_points"]),
        ),
        "C11_mdd_improvement": close(
            (
                Decimal(c11["maximum_drawdown"])
                - Decimal(c46["maximum_drawdown"])
            )
            * 100,
            Decimal(versus_c11["maximum_drawdown_improvement_percentage_points"]),
        ),
        "historical_MDD_under_cap": Decimal(c46["maximum_drawdown"])
        <= Decimal("0.35"),
        "normal_role_loss_under_cap": Decimal(
            structure["normal_deterministic_loss"]
        )
        <= Decimal(structure["normal_loss_limit"]),
        "extreme_role_loss_under_cap": Decimal(
            structure["extreme_deterministic_loss"]
        )
        <= Decimal(structure["extreme_loss_limit"]),
        "extreme_headroom_is_only_0_05pp": close(
            (
                Decimal(structure["extreme_loss_limit"])
                - Decimal(structure["extreme_deterministic_loss"])
            )
            * 100,
            Decimal(structure["extreme_limit_headroom_percentage_points"]),
        ),
        "preperformance_four_percent_carry_failed": Decimal(
            negative["pre_performance_four_percent_equal_carry_structure"][
                "extreme_deterministic_loss"
            ]
        )
        > Decimal("0.45"),
        "growth_satellite_negative_evidence_preserved": negative[
            "C45_Aggressive_2_growth_satellite"
        ]["decision"]
        == "PERMANENTLY_REJECTED_NO_RESCUE_ON_THIS_WINDOW",
        "growth_satellite_worsened_mdd_and_calmar": Decimal(
            negative["C45_Aggressive_2_growth_satellite"][
                "maximum_drawdown_deterioration_percentage_points"
            ]
        )
        > 0
        and Decimal(
            negative["C45_Aggressive_2_growth_satellite"]["calmar_change"]
        )
        < 0,
        "all_return_path_stresses_passed": summary["return_path_stress"][
            "all_frozen_return_path_groups_passed"
        ],
        "independent_v1_failure_preserved": summary["independent_verification"][
            "v1_result_preserved_as_failed"
        ],
        "independent_correction_did_not_change_strategy": not summary[
            "independent_verification"
        ]["strategy_weight_gate_or_primary_result_changed"],
        "independent_paths_reconciled": summary["independent_verification"][
            "all_cost_date_robustness_and_stress_paths_reconciled"
        ],
        "effective_equity_within_cap": Decimal(
            structure["effective_equity_including_current_carry_stocks"]
        )
        <= Decimal(structure["effective_equity_cap"]),
        "industry_and_security_within_caps": Decimal(
            structure["buffered_industry_upper_bound"]
        )
        <= Decimal(structure["industry_limit"])
        and Decimal(structure["buffered_single_security_upper_bound"])
        <= Decimal(structure["single_security_limit"]),
        "strategic_product_mapping_passed_but_not_order_ready": summary[
            "current_product_health"
        ]["strategic_product_mapping_passed"]
        and not summary["current_product_health"]["order_ready"],
        "C44_remains_default_for_most_people": boundary["default_for_most_people"]
        == "C44-Standard-1",
        "C46_is_not_universal_C45_replacement": not boundary[
            "universal_replacement_of_C45"
        ],
        "no_clean_OOS_or_promotion": not boundary["clean_oos_claimed"]
        and not boundary["simulation_approved"]
        and not boundary["live_trading_approved"],
        "no_account_or_order": not boundary["account_accessed"]
        and not boundary["credentials_accessed"]
        and boundary["orders_submitted"] == 0,
        "catalog_registers_C46": "C46-Aggressive-1" in rows
        and rows["C46-Aggressive-1"]["simulation_status"] == "NOT_APPROVED"
        and rows["C46-Aggressive-1"]["live_status"] == "NOT_APPROVED",
        "catalog_preserves_C45_A2_rejection": "C45-Aggressive-2" in rows
        and rows["C45-Aggressive-2"]["terminal"]["status"] == "rejected",
        "research_rounds_register_both_paths": round_rows["ROUND-C45-A2"][
            "selected_id"
        ]
        is None
        and round_rows["ROUND-C46-A1"]["selected_id"] == "C46-Aggressive-1",
        "lineage_preserves_derivative_and_rejected_neighbor": {
            row["relation"] for row in target_relations
        }
        == {"derivative", "optimized-research-successor"},
        "registry_manifest_matches_public_summary": registry[
            "public_evidence_manifests"
        ]["C46-Aggressive-1"]
        == sha256(SUMMARY),
    }

    sha_pattern = re.compile(r"^[0-9a-f]{64}$")
    checks["all_published_source_hashes_are_SHA256"] = all(
        value is True or (isinstance(value, str) and sha_pattern.fullmatch(value))
        for value in summary["source_bindings"].values()
    ) and all(
        isinstance(value, str) and sha_pattern.fullmatch(value)
        for value in contract["source_bindings"].values()
    )

    forbidden = ("/Users/", "/private/tmp/", "file://", "cookie", "password")
    public_files = (
        CONTRACT,
        SUMMARY,
        DOC,
        EVIDENCE_README,
        REGISTRY,
        ROUNDS,
        LINEAGE,
    )
    leaks: dict[str, list[str]] = {}
    for path in public_files:
        content = path.read_text(encoding="utf-8")
        found = [fragment for fragment in forbidden if fragment in content]
        if found:
            leaks[str(path.relative_to(ROOT))] = found
    checks["no_machine_path_or_credential_fragment"] = not leaks

    return {
        "schema_version": "c46-public-release-verification-v1",
        "candidate_id": summary["candidate_id"],
        "checks": checks,
        "leaks": leaks,
        "artifact_sha256": {
            "contract": sha256(CONTRACT),
            "summary": sha256(SUMMARY),
            "document": sha256(DOC),
            "evidence_readme": sha256(EVIDENCE_README),
        },
        "passed_all": all(checks.values()),
    }


def main() -> int:
    result = verify()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["passed_all"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
