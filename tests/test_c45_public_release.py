import hashlib
import importlib.util
import json
from decimal import Decimal
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "evidence" / "c45" / "public_research_summary.json"
CONTRACT = (
    ROOT
    / "config"
    / "strategy-research-c45-v1"
    / "001_C45-Aggressive-1_public_contract.json"
)
REGISTRY = ROOT / "research" / "catalog" / "strategy_registry.json"
ROUNDS = ROOT / "research" / "catalog" / "research_rounds.json"
LINEAGE = ROOT / "research" / "catalog" / "lineage.json"
VERIFY_SCRIPT = ROOT / "scripts" / "verify_c45_public_release.py"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_verifier():
    spec = importlib.util.spec_from_file_location("verify_c45_release", VERIFY_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class C45PublicReleaseTests(unittest.TestCase):
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

    def test_weights_route_and_execution_are_frozen(self):
        total = sum(Decimal(value) for value in self.summary["target_weights"].values())
        self.assertEqual(total, Decimal("1"))
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
        bridge = self.summary["historical_availability_bridge"]
        self.assertEqual(bridge["current_target_route"], "003376")
        self.assertEqual(bridge["route_switch_execution_close"], "2016-09-27")
        self.assertEqual(bridge["first_003376_return_session"], "2016-09-28")
        self.assertEqual(
            self.summary["execution_rule"]["primary_execution_lag_common_sessions"],
            1,
        )

    def test_risk_boundary_and_replacement_scope_are_honest(self):
        structure = self.summary["static_and_current_structure"]
        self.assertLessEqual(
            Decimal(structure["normal_deterministic_loss"]), Decimal("0.35")
        )
        self.assertLessEqual(
            Decimal(structure["extreme_deterministic_loss"]), Decimal("0.45")
        )
        self.assertEqual(
            Decimal(structure["extreme_limit_headroom_percentage_points"]),
            Decimal("0.75"),
        )
        boundary = self.summary["authority_boundary"]
        self.assertTrue(boundary["research_successor_to_C11_for_higher_offense_user"])
        self.assertEqual(boundary["default_for_most_people"], "C44-Standard-1")
        self.assertFalse(boundary["immediate_full_liquidation_authorized"])
        self.assertFalse(boundary["order_ready"])
        self.assertFalse(boundary["clean_oos_claimed"])
        self.assertFalse(boundary["simulation_approved"])
        self.assertFalse(boundary["live_trading_approved"])
        self.assertEqual(boundary["orders_submitted"], 0)

    def test_catalog_round_and_lineage_register_C45(self):
        rows = {row["canonical_name"]: row for row in self.registry["strategies"]}
        row = rows["C45-Aggressive-1"]
        self.assertEqual(row["parent_id"], "C21-Standard-1")
        self.assertEqual(row["simulation_status"], "NOT_APPROVED")
        self.assertEqual(row["live_status"], "NOT_APPROVED")
        self.assertEqual(row["forward_complete"], 0)
        self.assertEqual(
            self.registry["public_evidence_manifests"]["C45-Aggressive-1"],
            sha256(SUMMARY),
        )
        decisions = {row["round_id"]: row for row in self.rounds["rounds"]}
        self.assertEqual(
            decisions["ROUND-C45-A1"]["final_decision"],
            "RECOMMEND_HIGHER_OFFENSE_RESEARCH_SUCCESSOR_TO_C11_PHASED_TRANSITION_REQUIRED_C44_REMAINS_MOST_PEOPLE_DEFAULT",
        )
        relations = [
            row
            for row in self.lineage["relations"]
            if row["target_id"] == "C45-Aggressive-1"
        ]
        self.assertEqual(len(relations), 2)
        self.assertEqual(
            {row["relation"] for row in relations}, {"derivative", "supersedes"}
        )

    def test_migration_remains_hold_wait_and_no_order(self):
        migration = self.summary["migration_boundary"]
        self.assertIn("HOLD", migration["existing_first_C11_batch"])
        self.assertEqual(migration["second_C11_batch"], "WAIT")
        self.assertIn("SAME_SESSION_PRODUCT_GATES", migration["new_cash"])
        self.assertFalse(self.summary["current_product_health"]["order_ready"])

    def test_public_artifacts_do_not_leak_local_paths(self):
        paths = [
            SUMMARY,
            CONTRACT,
            ROOT / "evidence" / "c45" / "README.md",
            ROOT / "docs" / "16_C45六成权益因子久期桥接进取策略.md",
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
