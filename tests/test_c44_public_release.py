import hashlib
import importlib.util
import json
from decimal import Decimal
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "evidence" / "c44" / "public_research_summary.json"
CONTRACT = (
    ROOT
    / "config"
    / "strategy-research-c44-v1"
    / "001_C44-Standard-1_public_contract.json"
)
REGISTRY = ROOT / "research" / "catalog" / "strategy_registry.json"
ROUNDS = ROOT / "research" / "catalog" / "research_rounds.json"
LINEAGE = ROOT / "research" / "catalog" / "lineage.json"
VERIFY_SCRIPT = ROOT / "scripts" / "verify_c44_public_release.py"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_verifier():
    spec = importlib.util.spec_from_file_location("verify_c44_release", VERIFY_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class C44PublicReleaseTests(unittest.TestCase):
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
        self.assertEqual(total, Decimal("1"))
        self.assertEqual(self.summary["target_weights"], self.contract["target_weights"])
        self.assertEqual(
            self.summary["execution_rule"]["primary_execution_lag_common_sessions"],
            1,
        )
        self.assertEqual(
            self.summary["execution_rule"]["drift_trigger_absolute_percentage_points"],
            "5.0",
        )

    def test_replacement_is_research_only_and_phased(self):
        boundary = self.summary["authority_boundary"]
        self.assertTrue(boundary["research_default_successor_to_C11"])
        self.assertFalse(boundary["immediate_full_liquidation_authorized"])
        self.assertFalse(boundary["order_ready"])
        self.assertFalse(boundary["clean_oos_claimed"])
        self.assertFalse(boundary["simulation_approved"])
        self.assertFalse(boundary["live_trading_approved"])
        self.assertFalse(boundary["account_accessed"])
        self.assertFalse(boundary["credentials_accessed"])
        self.assertEqual(boundary["orders_submitted"], 0)

    def test_catalog_and_lineage_register_C44(self):
        rows = {row["canonical_name"]: row for row in self.registry["strategies"]}
        row = rows["C44-Standard-1"]
        self.assertEqual(row["simulation_status"], "NOT_APPROVED")
        self.assertEqual(row["live_status"], "NOT_APPROVED")
        self.assertEqual(row["forward_complete"], 0)
        self.assertEqual(
            self.registry["public_evidence_manifests"]["C44-Standard-1"],
            sha256(SUMMARY),
        )
        decisions = {row["round_id"]: row for row in self.rounds["rounds"]}
        self.assertEqual(
            decisions["ROUND-C44-S1"]["final_decision"],
            "RECOMMEND_RESEARCH_DEFAULT_SUCCESSOR_TO_C11_PHASED_TRANSITION_REQUIRED",
        )
        relations = [
            row
            for row in self.lineage["relations"]
            if row["target_id"] == "C44-Standard-1"
        ]
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]["relation"], "research-default-successor")

    def test_gold_dependency_and_household_warning_are_not_hidden(self):
        stress = self.summary["return_source_stress"]
        self.assertEqual(
            stress["gold_minus_4pct_annual"][
                "cagr_advantage_vs_C11_percentage_points"
            ],
            "0.35102563458531133",
        )
        self.assertIn("GLDM", self.summary["migration_boundary"]["household_gold_warning"])

    def test_public_artifacts_do_not_leak_local_paths(self):
        paths = [
            SUMMARY,
            CONTRACT,
            ROOT / "evidence" / "c44" / "README.md",
            ROOT / "docs" / "15_C44成熟三因子多经理固收加杠铃策略.md",
            REGISTRY,
            ROUNDS,
            LINEAGE,
        ]
        forbidden = ("/Users/", "/private/tmp/", "file://", "cookie", "password")
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for fragment in forbidden:
                self.assertNotIn(fragment, text, f"{fragment!r} leaked in {path}")


if __name__ == "__main__":
    unittest.main()
