import hashlib
import json
from decimal import Decimal
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence" / "c21-c26" / "public_research_summary.json"
REGISTRY = ROOT / "research" / "catalog" / "strategy_registry.json"
ROUNDS = ROOT / "research" / "catalog" / "research_rounds.json"
LINEAGE = ROOT / "research" / "catalog" / "lineage.json"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class C21C26PublicReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = _load(EVIDENCE)
        cls.registry = _load(REGISTRY)
        cls.rounds = _load(ROUNDS)
        cls.lineage = _load(LINEAGE)

    def test_release_never_upgrades_formation_evidence(self):
        boundary = self.summary["authority_boundary"]
        self.assertEqual(
            boundary["evidence_class"],
            "contaminated-formation-never-clean-OOS",
        )
        self.assertFalse(boundary["clean_oos_claimed"])
        self.assertFalse(boundary["simulation_approved"])
        self.assertFalse(boundary["live_trading_approved"])
        self.assertFalse(boundary["account_accessed"])
        self.assertFalse(boundary["credentials_accessed"])
        self.assertEqual(boundary["orders_submitted"], 0)
        self.assertEqual(
            boundary["current_account_action"], "NO_AUTOMATIC_SWITCH_FROM_C11"
        )

    def test_selected_static_portfolios_sum_to_one(self):
        for candidate in ("C21-Standard-1", "C23-Standard-1", "C26-Standard-1"):
            weights = self.summary["strategies"][candidate]["target_weights"]
            total = sum(Decimal(value) for value in weights.values())
            self.assertLessEqual(abs(total - Decimal("1")), Decimal("1e-15"))

    def test_failed_candidates_remain_discoverable_and_rejected(self):
        strategies = self.summary["strategies"]
        expected = {
            "C21-Standard-2": "REJECTED_CONTAMINATED_FORMATION_FACTOR_ROTATION",
            "C22-Standard-1": "REJECTED_CONTAMINATED_FORMATION_TIPP_TRANSFER",
            "C24-Standard-1": "REJECTED_CONTAMINATED_FORMATION_CONVEXITY_BRIDGE",
            "C25-Standard-1": "REJECTED_CONTAMINATED_FORMATION_AI_OPTIONALITY_TREND_GATE",
        }
        for candidate, status in expected.items():
            self.assertEqual(strategies[candidate]["status"], status)

        round_decisions = {
            row["round_id"]: row["final_decision"] for row in self.rounds["rounds"]
        }
        self.assertEqual(
            round_decisions["ROUND-C24-S1"],
            "REJECTED_CONTAMINATED_FORMATION_CONVEXITY_BRIDGE",
        )
        self.assertEqual(
            round_decisions["ROUND-C25-S1"],
            "REJECTED_CONTAMINATED_FORMATION_AI_OPTIONALITY_TREND_GATE",
        )

    def test_free_cash_flow_backfill_accounting_is_explicit(self):
        boundary = self.summary["free_cash_flow_history_boundary"]
        counts = boundary["limited_window_2019_01_22_to_2026_07_06"]
        self.assertEqual(boundary["index_launch_date"], "2024-12-11")
        self.assertEqual(boundary["fund_actual_nav_start"], "2025-07-08")
        self.assertEqual(counts["total_common_sessions"], 1805)
        self.assertEqual(counts["pre_index_launch_provider_backfill_sessions"], 1427)
        self.assertEqual(counts["post_launch_pre_fund_sessions"], 138)
        self.assertEqual(counts["actual_fund_nav_sessions_used"], 240)
        self.assertEqual(1427 + 138 + 240, 1805)
        self.assertFalse(boundary["c26_specific_minus_2_and_minus_4_sensitivity_completed"])

    def test_c26_local_delta_is_same_window_and_not_a_c11_supersession(self):
        c26 = self.summary["strategies"]["C26-Standard-1"]
        delta = c26["relative_to_c23_same_window"]
        self.assertEqual(delta["cagr_percentage_points"], "0.3795")
        self.assertEqual(delta["maximum_drawdown_percentage_points"], "-0.2297")
        self.assertEqual(c26["forward_complete_month_ends"], 0)
        non_supersession = {
            row["candidate_id"]: row for row in self.lineage["non_supersession"]
        }
        self.assertIn("C26-Standard-1", non_supersession)
        self.assertEqual(
            non_supersession["C26-Standard-1"]["does_not_supersede"],
            "C11-C8-Style-Switch-Balanced-60",
        )

    def test_registry_binds_the_public_manifest_and_blocks_promotion(self):
        self.assertEqual(
            self.registry["public_evidence_manifest_sha256"], _sha256(EVIDENCE)
        )
        rows = {row["canonical_name"]: row for row in self.registry["strategies"]}
        for candidate in ("C21-Standard-1", "C23-Standard-1", "C26-Standard-1"):
            self.assertEqual(rows[candidate]["simulation_status"], "NOT_APPROVED")
            self.assertEqual(rows[candidate]["live_status"], "NOT_APPROVED")
            self.assertEqual(rows[candidate]["forward_complete"], 0)

    def test_new_public_artifacts_do_not_leak_machine_paths(self):
        paths = [
            EVIDENCE,
            ROOT / "evidence" / "c21-c26" / "README.md",
            REGISTRY,
            ROUNDS,
            LINEAGE,
            ROOT / "research" / "catalog" / "README.md",
            ROOT / "docs" / "13_C21至C26研究谱系与当前结论.md",
            ROOT / "docs" / "14_自由现金流历史数据边界.md",
        ]
        forbidden = ("/Users/", "/private/tmp/", "file://", "cookie", "password")
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for fragment in forbidden:
                self.assertNotIn(fragment, text, f"{fragment!r} leaked in {path}")


if __name__ == "__main__":
    unittest.main()
