from pathlib import Path
import unittest

from cn_fund_strategy.application.public_evidence import verify_public_evidence


ROOT = Path(__file__).resolve().parents[1]


class PublicEvidenceTests(unittest.TestCase):
    def test_daily_ledger_recalculates_to_published_summary(self) -> None:
        report = verify_public_evidence(
            ROOT / "evidence/g6-standard-1/daily_accounting_ledger.json",
            ROOT / "evidence/g6-standard-1/public_result_summary.json",
        )
        self.assertTrue(report["all_checks_passed"])
        recalculated = report["recalculated"]
        self.assertEqual(recalculated["row_count"], 2740)
        self.assertEqual(
            recalculated["maximum_extreme_path_overlay_drawdown_session"],
            "2022-10-11",
        )
        self.assertEqual(recalculated["negative_weight_count"], 0)


if __name__ == "__main__":
    unittest.main()
