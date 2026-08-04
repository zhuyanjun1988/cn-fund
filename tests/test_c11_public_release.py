import hashlib
import json
from pathlib import Path
import unittest

from cn_fund_strategy.domain.c11_balanced_60 import (
    C11State,
    standard_64_target,
    target_for_c11_state,
)


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence" / "c11"
CONTRACT = (
    ROOT
    / "config"
    / "strategy-research-c11-v1"
    / "001_C11-C8-Style-Switch-Balanced-60_contract.json"
)


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class C11PublicReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = _load(EVIDENCE / "public_result_summary.json")
        cls.result = _load(EVIDENCE / "original_result.json")
        cls.audit = _load(EVIDENCE / "original_audit.json")
        cls.integrity = _load(EVIDENCE / "integrity_evaluation.json")
        cls.contract = _load(CONTRACT)

    def test_byte_exact_public_artifacts_match_bound_hashes(self):
        hashes = self.summary["source_hashes"]
        self.assertEqual(_sha256(CONTRACT), hashes["contract_sha256"])
        self.assertEqual(
            _sha256(EVIDENCE / "original_result.json"), hashes["result_sha256"]
        )
        self.assertEqual(
            _sha256(EVIDENCE / "original_audit.json"),
            hashes["independent_audit_sha256"],
        )
        self.assertEqual(
            _sha256(EVIDENCE / "integrity_evaluation.json"),
            hashes["integrity_evaluation_sha256"],
        )
        self.assertEqual(self.result["claim_sha256"], hashes["claim_sha256"])

    def test_release_does_not_upgrade_research_authority(self):
        identity = self.summary["research_identity"]
        boundary = self.summary["decision_boundary"]
        self.assertEqual(identity["status"], "PASS_CONTAMINATED_RESEARCH_ONLY")
        self.assertFalse(identity["simulation_or_trading_authorized"])
        self.assertEqual(boundary["simulation_status"], "BLOCKED_NOT_EVALUATED")
        self.assertEqual(boundary["live_status"], "BLOCKED")
        self.assertFalse(boundary["clean_OOS_claimed"])
        self.assertFalse(boundary["forward_shadow_claimed"])

    def test_summary_targets_equal_contract_and_public_code(self):
        baseline = self.summary["standard_64_baseline"]["target_weights"]
        self.assertEqual(
            baseline,
            {asset: str(weight) for asset, weight in standard_64_target()},
        )
        for state in C11State:
            expected = {
                asset: str(weight) for asset, weight in target_for_c11_state(state)
            }
            self.assertEqual(
                self.summary["c11_policy"]["target_weights"][state.value], expected
            )
            self.assertEqual(self.contract["target_weights"][state.value], expected)

    def test_summary_scorecard_matches_original_result_and_integrity_review(self):
        base = self.result["scenarios"]["base-cost25bp-lag1"]
        self.assertEqual(
            self.summary["frozen_result"]["base_cost25bp_lag1"]["net_cagr_decimal"],
            base["candidate"]["metrics"]["net_cagr_decimal"],
        )
        self.assertEqual(
            self.summary["frozen_result"]["base_cost25bp_lag1"][
                "maximum_drawdown_decimal"
            ],
            base["candidate"]["metrics"]["maximum_drawdown_decimal"],
        )
        self.assertEqual(
            self.summary["standard_64_baseline"]["base_cost25bp_lag1"][
                "net_cagr_decimal"
            ],
            base["fixed_e60"]["metrics"]["net_cagr_decimal"],
        )
        self.assertEqual(self.audit["status"], "PASS_AUDIT")
        self.assertEqual(
            self.integrity["decision_reconciliation"]["research_status"],
            "PASS_RESEARCH_ONLY",
        )
        self.assertEqual(
            self.integrity["temporal_and_concentration_diagnostics"][
                "executed_state_changes"
            ],
            2,
        )


if __name__ == "__main__":
    unittest.main()
