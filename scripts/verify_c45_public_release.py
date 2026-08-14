#!/usr/bin/env python3
"""Verify C45's public arithmetic, timing and authority boundary."""

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
    / "strategy-research-c45-v1"
    / "001_C45-Aggressive-1_public_contract.json"
)
SUMMARY = ROOT / "evidence" / "c45" / "public_research_summary.json"
DOC = ROOT / "docs" / "16_C45六成权益因子久期桥接进取策略.md"
EVIDENCE_README = ROOT / "evidence" / "c45" / "README.md"


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def close(left: Decimal, right: Decimal, tolerance: str = "1e-14") -> bool:
    return abs(left - right) <= Decimal(tolerance)


def verify() -> dict[str, Any]:
    contract = load(CONTRACT)
    summary = load(SUMMARY)
    boundary = summary["authority_boundary"]
    c45 = summary["formation_metrics"]["base_cost25bp_lag1"]
    c11 = summary["comparisons"]["C11_same_window_base25bp_lag1"]
    c21 = summary["comparisons"]["C21_matching_causal_base25bp_lag1"]
    c44 = summary["comparisons"]["C44_same_window_base25bp_lag1"]
    versus_c11 = summary["comparisons"]["C45_minus_C11"]
    versus_c21 = summary["comparisons"]["C45_minus_matching_C21"]
    versus_c44 = summary["comparisons"]["C45_minus_C44"]
    structure = summary["static_and_current_structure"]
    bridge = summary["historical_availability_bridge"]

    weight_total = sum(Decimal(value) for value in summary["target_weights"].values())
    contract_weight_total = sum(
        Decimal(value) for value in contract["target_weights"].values()
    )
    checks = {
        "candidate_identity_matches": contract["candidate_id"]
        == summary["candidate_id"]
        == "C45-Aggressive-1",
        "technical_identity_matches": contract["technical_id"]
        == summary["technical_id"],
        "summary_weights_sum_to_one": close(weight_total, Decimal("1")),
        "contract_weights_sum_to_one": close(contract_weight_total, Decimal("1")),
        "contract_and_summary_weights_match": contract["target_weights"]
        == summary["target_weights"],
        "nominal_equity_is_sixty_percent": close(
            sum(
                Decimal(summary["target_weights"][key])
                for key in (
                    "024622_free_cash_flow_factor",
                    "021550_dividend_low_volatility",
                    "007593_CSI500_quality_growth",
                )
            ),
            Decimal("0.60"),
        ),
        "C11_cagr_delta": close(
            (Decimal(c45["net_cagr"]) - Decimal(c11["net_cagr"])) * 100,
            Decimal(versus_c11["cagr_percentage_points"]),
        ),
        "C11_mdd_improvement": close(
            (
                Decimal(c11["maximum_drawdown"])
                - Decimal(c45["maximum_drawdown"])
            )
            * 100,
            Decimal(versus_c11["maximum_drawdown_improvement_percentage_points"]),
        ),
        "C11_calmar_improvement": close(
            Decimal(c45["calmar"]) - Decimal(c11["calmar"]),
            Decimal(versus_c11["calmar_improvement"]),
        ),
        "C21_cagr_delta": close(
            (Decimal(c45["net_cagr"]) - Decimal(c21["net_cagr"])) * 100,
            Decimal(versus_c21["cagr_percentage_points"]),
        ),
        "C21_mdd_deterioration": close(
            (
                Decimal(c45["maximum_drawdown"])
                - Decimal(c21["maximum_drawdown"])
            )
            * 100,
            Decimal(versus_c21["maximum_drawdown_deterioration_percentage_points"]),
        ),
        "C21_ending_NAV_advantage": close(
            Decimal(c45["ending_nav"]) / Decimal(c21["ending_nav"]) - 1,
            Decimal(versus_c21["ending_nav_advantage_fraction"]),
        ),
        "C44_cagr_delta": close(
            (Decimal(c45["net_cagr"]) - Decimal(c44["net_cagr"])) * 100,
            Decimal(versus_c44["cagr_percentage_points"]),
        ),
        "C44_mdd_deterioration": close(
            (
                Decimal(c45["maximum_drawdown"])
                - Decimal(c44["maximum_drawdown"])
            )
            * 100,
            Decimal(versus_c44["maximum_drawdown_deterioration_percentage_points"]),
        ),
        "C44_calmar_delta": close(
            Decimal(c45["calmar"]) - Decimal(c44["calmar"]),
            Decimal(versus_c44["calmar"]),
        ),
        "historical_MDD_under_cap": Decimal(c45["maximum_drawdown"])
        <= Decimal("0.35"),
        "normal_loss_under_cap": Decimal(structure["normal_deterministic_loss"])
        <= Decimal(structure["normal_loss_limit"]),
        "extreme_loss_under_cap": Decimal(structure["extreme_deterministic_loss"])
        <= Decimal(structure["extreme_loss_limit"]),
        "stress_headroom_is_only_0_75pp": close(
            (Decimal("0.45") - Decimal(structure["extreme_deterministic_loss"]))
            * 100,
            Decimal(structure["extreme_limit_headroom_percentage_points"]),
        ),
        "route_timing_is_causal": bridge["first_003376_NAV_observation"]
        < bridge["route_switch_execution_close"]
        < bridge["first_003376_return_session"],
        "current_route_is_not_historical_bridge": bridge["current_target_route"]
        == "003376",
        "research_successor_only": boundary[
            "research_successor_to_C11_for_higher_offense_user"
        ]
        and not boundary["immediate_full_liquidation_authorized"],
        "C44_remains_default_for_most_people": boundary["default_for_most_people"]
        == "C44-Standard-1",
        "not_order_ready": boundary["order_ready"] is False,
        "no_clean_OOS_or_promotion": not boundary["clean_oos_claimed"]
        and not boundary["simulation_approved"]
        and not boundary["live_trading_approved"],
        "no_account_or_order": not boundary["account_accessed"]
        and not boundary["credentials_accessed"]
        and boundary["orders_submitted"] == 0,
    }

    sha_pattern = re.compile(r"^[0-9a-f]{64}$")
    checks["all_published_source_hashes_are_SHA256"] = all(
        value is True or (isinstance(value, str) and sha_pattern.fullmatch(value))
        for value in summary["source_bindings"].values()
    )

    forbidden = ("/Users/", "/private/tmp/", "file://", "cookie", "password")
    public_files = (CONTRACT, SUMMARY, DOC, EVIDENCE_README)
    leaks: dict[str, list[str]] = {}
    for path in public_files:
        content = path.read_text(encoding="utf-8")
        found = [fragment for fragment in forbidden if fragment in content]
        if found:
            leaks[str(path.relative_to(ROOT))] = found
    checks["no_machine_path_or_credential_fragment"] = not leaks

    return {
        "schema_version": "c45-public-release-verification-v1",
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
