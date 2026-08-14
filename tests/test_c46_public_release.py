import hashlib
import importlib.util
import json
from decimal import Decimal
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "evidence" / "c46" / "public_research_summary.json"
CONTRACT = (
    ROOT
    / "config"
    / "strategy-research-c46-v1"
    / "001_C46-Aggressive-1_public_contract.json"
)
REGISTRY = ROOT / "research" / "catalog" / "strategy_registry.json"
ROUNDS = ROOT / "research" / "catalog" / "research_rounds.json"
LINEAGE = ROOT / "research" / "catalog" / "lineage.json"
VERIFY_SCRIPT = ROOT / "scripts" / "verify_c46_public_release.py"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_verifier():
    spec = importlib.util.spec_from_file_location("verify_c46_release", VERIFY_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class C46PublicReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = load(SUMMARY)
        cls.contract = load(CONTRACT)
        cls.registry = load(REGISTRY)
        cls.rounds = load(ROUNDS)
        cls.lineage = load(LINEAGE)

    def test_public_verifier_passes(self):
        result = load_verifier().verify()
        self.assertTrue(result["passed_all"], result)

    def test_weights_and_execution_are_frozen(self):
        total = sum(Decimal(value) for value in self.summary["target_weights"].values())
        self.assertEqual(total, Decimal("1.00000000000000000"))
        self.assertEqual(self.summary["target_weights"], self.contract["target_weights"])
        equity = sum(
            Decimal(self.summary["target_weights"][key])
            for key in (
                "024622_free_cash_flow_factor",
                "021550_dividend_low_volatility",
                "007593_CSI500_quality_growth",
            )
        )
        self.assertEqual(equity, Decimal("0.60000000000000000"))
        self.assertEqual(
            self.summary["execution_rule"]["drift_trigger_absolute_percentage_points"],
            "5.0",
        )
        self.assertEqual(
            self.summary["execution_rule"]["primary_execution_lag_common_sessions"],
            1,
        )

    def test_negative_evidence_and_preperformance_correction_are_preserved(self):
        negative = self.summary["negative_evidence"]
        self.assertEqual(
            negative["C45_Aggressive_2_growth_satellite"]["decision"],
            "PERMANENTLY_REJECTED_NO_RESCUE_ON_THIS_WINDOW",
        )
        self.assertGreater(
            Decimal(
                negative["C45_Aggressive_2_growth_satellite"][
                    "maximum_drawdown_deterioration_percentage_points"
                ]
            ),
            0,
        )
        self.assertGreater(
            Decimal(
                negative["pre_performance_four_percent_equal_carry_structure"][
                    "extreme_deterministic_loss"
                ]
            ),
            Decimal("0.45"),
        )
        self.assertTrue(
            self.contract["pre_performance_correction"][
                "rejected_structure_may_not_be_revived"
            ]
        )

    def test_risk_and_authority_boundaries_are_honest(self):
        structure = self.summary["static_and_current_structure"]
        self.assertLessEqual(
            Decimal(structure["normal_deterministic_loss"]), Decimal("0.35")
        )
        self.assertLessEqual(
            Decimal(structure["extreme_deterministic_loss"]), Decimal("0.45")
        )
        self.assertEqual(
            Decimal(structure["extreme_limit_headroom_percentage_points"]),
            Decimal("0.05"),
        )
        boundary = self.summary["authority_boundary"]
        self.assertFalse(boundary["universal_replacement_of_C45"])
        self.assertEqual(boundary["default_for_most_people"], "C44-Standard-1")
        self.assertFalse(boundary["order_ready"])
        self.assertFalse(boundary["clean_oos_claimed"])
        self.assertFalse(boundary["simulation_approved"])
        self.assertFalse(boundary["live_trading_approved"])
        self.assertEqual(boundary["orders_submitted"], 0)

    def test_independent_verifier_correction_is_append_only(self):
        verification = self.summary["independent_verification"]
        self.assertTrue(verification["v1_result_preserved_as_failed"])
        self.assertTrue(verification["correction_frozen_before_v2"])
        self.assertFalse(verification["strategy_weight_gate_or_primary_result_changed"])
        self.assertTrue(
            verification["all_cost_date_robustness_and_stress_paths_reconciled"]
        )
        self.assertEqual(len(verification["v1_false_negative_causes"]), 2)

    def test_breakthrough_discloses_zero_return_cash_benchmark(self):
        limitation = self.summary["cash_benchmark_limitation"]
        self.assertEqual(limitation["parent_C45_cash_return_assumption"], "0.0")
        self.assertTrue(limitation["conditional_breakthrough_only"])
        self.assertFalse(limitation["yield_bearing_cash_counterfactual_formally_replayed"])
        self.assertTrue(
            self.contract["cash_benchmark_limitation"][
                "incremental_breakthrough_is_conditional_on_frozen_parent_accounting"
            ]
        )

    def test_catalog_registers_positive_and_negative_rounds(self):
        rows = {row["canonical_name"]: row for row in self.registry["strategies"]}
        self.assertEqual(rows["C46-Aggressive-1"]["parent_id"], "C45-Aggressive-1")
        self.assertEqual(rows["C46-Aggressive-1"]["simulation_status"], "NOT_APPROVED")
        self.assertEqual(rows["C46-Aggressive-1"]["live_status"], "NOT_APPROVED")
        self.assertEqual(rows["C45-Aggressive-2"]["terminal"]["status"], "rejected")
        self.assertEqual(
            self.registry["public_evidence_manifests"]["C46-Aggressive-1"],
            sha256(SUMMARY),
        )
        decisions = {row["round_id"]: row for row in self.rounds["rounds"]}
        self.assertIsNone(decisions["ROUND-C45-A2"]["selected_id"])
        self.assertEqual(decisions["ROUND-C46-A1"]["selected_id"], "C46-Aggressive-1")
        relations = [
            row
            for row in self.lineage["relations"]
            if row["target_id"] == "C46-Aggressive-1"
        ]
        self.assertEqual(
            {row["relation"] for row in relations},
            {"derivative", "optimized-research-successor"},
        )

    def test_public_artifacts_do_not_leak_local_paths(self):
        paths = [
            SUMMARY,
            CONTRACT,
            ROOT / "evidence" / "c46" / "README.md",
            ROOT / "docs" / "17_C46六成权益与三层流动性收益增强进取策略.md",
            REGISTRY,
            ROUNDS,
            LINEAGE,
        ]
        forbidden = ("/Users/", "/private/tmp/", "file://", "cookie", "password")
        for path in paths:
            content = path.read_text(encoding="utf-8")
            for fragment in forbidden:
                self.assertNotIn(fragment, content, f"{fragment!r} leaked in {path}")


if __name__ == "__main__":
    unittest.main()
