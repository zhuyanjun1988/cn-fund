#!/usr/bin/env python3
"""Verify the self-contained arithmetic and authority boundary of C44 release."""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = (
    ROOT
    / "config"
    / "strategy-research-c44-v1"
    / "001_C44-Standard-1_public_contract.json"
)
SUMMARY = ROOT / "evidence" / "c44" / "public_research_summary.json"
DOC = ROOT / "docs" / "15_C44成熟三因子多经理固收加杠铃策略.md"


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
    c44 = summary["formation_metrics"]["base_cost25bp_lag1"]
    c11 = summary["comparisons"]["C11_same_window_base25bp_lag1"]
    c21 = summary["comparisons"]["C21_same_window_base25bp"]
    versus_c11 = summary["comparisons"]["C44_minus_C11"]
    versus_c21 = summary["comparisons"]["C44_minus_C21"]
    structure = summary["static_and_current_structure"]

    weight_total = sum(Decimal(value) for value in summary["target_weights"].values())
    contract_weight_total = sum(
        Decimal(value) for value in contract["target_weights"].values()
    )
    checks = {
        "candidate_identity_matches": contract["candidate_id"]
        == summary["candidate_id"]
        == "C44-Standard-1",
        "summary_weights_sum_to_one": close(weight_total, Decimal("1")),
        "contract_weights_sum_to_one": close(contract_weight_total, Decimal("1")),
        "contract_and_summary_weights_match": contract["target_weights"]
        == summary["target_weights"],
        "C11_cagr_delta": close(
            (Decimal(c44["net_cagr"]) - Decimal(c11["net_cagr"])) * 100,
            Decimal(versus_c11["cagr_percentage_points"]),
        ),
        "C11_mdd_delta": close(
            (
                Decimal(c44["maximum_drawdown"])
                - Decimal(c11["maximum_drawdown"])
            )
            * 100,
            Decimal(versus_c11["maximum_drawdown_percentage_points"]),
        ),
        "C11_calmar_delta": close(
            Decimal(c44["calmar"]) - Decimal(c11["calmar"]),
            Decimal(versus_c11["calmar"]),
        ),
        "C21_cagr_delta": close(
            (Decimal(c44["net_cagr"]) - Decimal(c21["net_cagr"])) * 100,
            Decimal(versus_c21["cagr_percentage_points"]),
        ),
        "C21_mdd_improvement": close(
            (
                Decimal(c21["maximum_drawdown"])
                - Decimal(c44["maximum_drawdown"])
            )
            * 100,
            Decimal(versus_c21["maximum_drawdown_improvement_percentage_points"]),
        ),
        "C21_calmar_delta": close(
            Decimal(c44["calmar"]) - Decimal(c21["calmar"]),
            Decimal(versus_c21["calmar"]),
        ),
        "effective_equity_under_cap": Decimal(structure["effective_equity"])
        <= Decimal(structure["effective_equity_limit"]),
        "static_loss_under_extreme_cap": Decimal(structure["extreme_static_loss"])
        <= Decimal("0.45"),
        "research_successor_only": boundary["research_default_successor_to_C11"]
        and not boundary["immediate_full_liquidation_authorized"],
        "not_order_ready": boundary["order_ready"] is False,
        "no_clean_OOS_or_promotion": not boundary["clean_oos_claimed"]
        and not boundary["simulation_approved"]
        and not boundary["live_trading_approved"],
        "no_account_or_order": not boundary["account_accessed"]
        and not boundary["credentials_accessed"]
        and boundary["orders_submitted"] == 0,
    }

    forbidden = ("/Users/", "/private/tmp/", "file://", "cookie", "password")
    public_files = (CONTRACT, SUMMARY, DOC, ROOT / "evidence" / "c44" / "README.md")
    leaks: dict[str, list[str]] = {}
    for path in public_files:
        text = path.read_text(encoding="utf-8")
        found = [fragment for fragment in forbidden if fragment in text]
        if found:
            leaks[str(path.relative_to(ROOT))] = found
    checks["no_machine_path_or_credential_fragment"] = not leaks

    return {
        "schema_version": "c44-public-release-verification-v1",
        "candidate_id": summary["candidate_id"],
        "checks": checks,
        "leaks": leaks,
        "artifact_sha256": {
            "contract": sha256(CONTRACT),
            "summary": sha256(SUMMARY),
            "document": sha256(DOC),
        },
        "passed_all": all(checks.values()),
    }


def main() -> int:
    result = verify()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["passed_all"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
