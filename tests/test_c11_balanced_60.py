from decimal import Decimal, localcontext
import unittest

from cn_fund_strategy.domain.c11_balanced_60 import (
    C11State,
    assess_maintenance,
    decide_c11_style,
    midrank_inclusive_current,
    relative_total_return_trend,
    standard_64_target,
    static_stress_loss,
    target_for_c11_state,
)


class C11Balanced60PolicyTests(unittest.TestCase):
    def test_standard_and_each_c11_state_keep_exact_60_30_10_budget(self):
        targets = [standard_64_target()]
        targets.extend(target_for_c11_state(state) for state in C11State)
        for rows in targets:
            target = dict(rows)
            equity = target["022430"] + target["006729"] + target["021550"]
            bonds = target["006662"] + target["012773"] + target["007169"]
            self.assertEqual(equity, Decimal("0.60"))
            self.assertEqual(bonds, Decimal("0.30"))
            self.assertEqual(target["CASH"], Decimal("0.10"))
            self.assertEqual(sum(target.values()), Decimal("1"))

    def test_standard_64_identity_is_exact_user_baseline(self):
        self.assertEqual(
            dict(standard_64_target()),
            {
                "022430": Decimal("0.35"),
                "006729": Decimal("0.10"),
                "021550": Decimal("0.15"),
                "006662": Decimal("0.05"),
                "012773": Decimal("0.05"),
                "007169": Decimal("0.20"),
                "CASH": Decimal("0.10"),
            },
        )

    def test_cheap_composite_and_positive_relative_trend_select_m60(self):
        decision = decide_c11_style(
            previous_state=C11State.D60,
            pe_percentile=Decimal("0.20"),
            pb_percentile=Decimal("0.30"),
            relative_trend=Decimal("0.01"),
        )
        self.assertEqual(decision.valuation_composite, Decimal("0.25"))
        self.assertEqual(decision.desired_state, C11State.M60)
        self.assertTrue(decision.state_changed)
        self.assertEqual(decision.target_mapping()["006729"], Decimal("0.40"))

    def test_expensive_composite_and_negative_relative_trend_select_d60(self):
        decision = decide_c11_style(
            previous_state=C11State.M60,
            pe_percentile=Decimal("0.80"),
            pb_percentile=Decimal("0.70"),
            relative_trend=Decimal("-0.01"),
        )
        self.assertEqual(decision.valuation_composite, Decimal("0.75"))
        self.assertEqual(decision.desired_state, C11State.D60)
        self.assertEqual(decision.target_mapping()["021550"], Decimal("0.40"))

    def test_high_valuation_but_positive_trend_retains_previous_state(self):
        decision = decide_c11_style(
            previous_state=C11State.M60,
            pe_percentile=Decimal("0.800595"),
            pb_percentile=Decimal("0.760516"),
            relative_trend=Decimal("0.171615"),
        )
        self.assertEqual(decision.valuation_composite, Decimal("0.7805555"))
        self.assertEqual(decision.desired_state, C11State.M60)
        self.assertFalse(decision.state_changed)
        self.assertEqual(
            decision.reason_codes,
            ("JOINT_EXTREME_NOT_CONFIRMED_RETAIN_STATE",),
        )

    def test_missing_required_signal_fails_closed_by_retaining_state(self):
        decision = decide_c11_style(
            previous_state=C11State.D60,
            pe_percentile=None,
            pb_percentile=Decimal("0.80"),
            relative_trend=None,
        )
        self.assertFalse(decision.signal_complete)
        self.assertEqual(decision.desired_state, C11State.D60)
        self.assertFalse(decision.state_changed)

    def test_midrank_is_inclusive_of_current_and_ties(self):
        self.assertEqual(
            midrank_inclusive_current(
                (Decimal("1"), Decimal("2"), Decimal("2"), Decimal("2"))
            ),
            Decimal("0.625"),
        )

    def test_relative_trend_uses_wealth_factor_ratio_not_return_difference(self):
        trend = relative_total_return_trend(
            csi500_current=Decimal("1.218279"),
            csi500_twelve_months_ago=Decimal("1"),
            dividend_low_vol_current=Decimal("1.039829"),
            dividend_low_vol_twelve_months_ago=Decimal("1"),
        )
        with localcontext() as context:
            context.prec = 60
            expected = Decimal("1.218279") / Decimal("1.039829") - Decimal("1")
        self.assertEqual(trend, expected)

    def test_target_static_stress_is_37_3_percent(self):
        for state in C11State:
            self.assertEqual(
                static_stress_loss(dict(target_for_c11_state(state))),
                Decimal("0.3730"),
            )
        self.assertEqual(
            static_stress_loss(dict(standard_64_target())), Decimal("0.3730")
        )

    def test_ordinary_drift_and_stress_thresholds_are_strict(self):
        target = dict(target_for_c11_state(C11State.M60))
        exactly_five = dict(target)
        exactly_five["006729"] -= Decimal("0.05")
        exactly_five["CASH"] += Decimal("0.05")
        assessment = assess_maintenance(
            marked_weights=exactly_five,
            target_weights=target,
        )
        self.assertEqual(assessment.maximum_absolute_gap, Decimal("0.05"))
        self.assertFalse(assessment.ordinary_drift_triggered)

        over_five = dict(target)
        over_five["006729"] -= Decimal("0.051")
        over_five["CASH"] += Decimal("0.051")
        assessment = assess_maintenance(
            marked_weights=over_five,
            target_weights=target,
        )
        self.assertTrue(assessment.ordinary_drift_triggered)


if __name__ == "__main__":
    unittest.main()
