from decimal import Decimal
import unittest

from cn_fund_strategy.domain.g6_quarterly_valuation_tilt import (
    G6Signal,
    decide_tilt,
    extreme_static_loss,
    standard_g6_1_policy,
    standard_g6_policy,
    strategic_target,
)


SLEEVES = ("CN300", "CN500", "GROWTH", "HK", "SP500", "NASDAQ")


def _signal(scores, trends=None):
    trends = trends or ("0.1",) * 6
    return G6Signal(
        decision_session="2026-06-30",
        momentum_10m=tuple((k, Decimal(v)) for k, v in zip(SLEEVES, trends, strict=True)),
        valuation_scores=tuple((k, None if v is None else Decimal(v)) for k, v in zip(SLEEVES, scores, strict=True)),
        valuation_sessions=tuple((k, "2026-06-29") for k in SLEEVES),
    )


class G6QuarterlyValuationTiltTests(unittest.TestCase):
    def test_accounting_subversion_changes_only_identity(self):
        parent = standard_g6_policy()
        corrected = standard_g6_1_policy()
        self.assertEqual(corrected.canonical_name, "G6-Standard-1")
        self.assertEqual(corrected.technical_id, "G6-SCQVT6-E50-G20-T025-Q1-L2-C225-R1")
        self.assertEqual(corrected.sleeves, parent.sleeves)
        self.assertEqual(corrected.gold_weight, parent.gold_weight)
        self.assertEqual(corrected.tilt_weight, parent.tilt_weight)

    def test_base_and_worst_tilt_stress_are_bound(self):
        policy = standard_g6_policy()
        base = strategic_target(policy)
        self.assertEqual(extreme_static_loss(base), Decimal("0.4110"))
        decision = decide_tilt(policy, _signal(("0.9", "0.7", "0.1", "0.2", "0.8", "0.95")))
        target = decision.target_mapping()
        self.assertEqual(decision.receiver_sleeve, "GROWTH")
        self.assertEqual(decision.donor_sleeve, "NASDAQ")
        self.assertEqual(target["GROWTH"], Decimal("0.075"))
        self.assertEqual(target["NASDAQ"], Decimal("0.175"))
        self.assertEqual(sum(target.values()), Decimal("1"))

    def test_without_both_cheap_receiver_and_expensive_donor_returns_base(self):
        policy = standard_g6_policy()
        decision = decide_tilt(policy, _signal(("0.5", "0.5", "0.5", "0.5", "0.5", "0.5")))
        self.assertIsNone(decision.receiver_sleeve)
        self.assertEqual(decision.target_mapping(), strategic_target(policy))

    def test_zero_core_receiver_is_allowed_but_not_zero_core_donor(self):
        policy = standard_g6_policy()
        decision = decide_tilt(policy, _signal(("0.7", "0.7", "0.7", "0.1", "0.8", "0.95")))
        self.assertEqual(decision.receiver_sleeve, "HK")
        self.assertEqual(decision.donor_sleeve, "NASDAQ")
        self.assertEqual(decision.target_mapping()["HK"], Decimal("0.025"))


if __name__ == "__main__":
    unittest.main()
