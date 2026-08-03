from datetime import date, timedelta
from decimal import Decimal
import unittest
from unittest.mock import patch

from cn_fund_strategy.application.g1_replay import G1ReplayInputs, G1ReplayScenario
from cn_fund_strategy.application.g6_replay import run_g6_fixed_event_settlement_stress, run_g6_replay
from cn_fund_strategy.domain.g1_global_valuation_batch import BOND_SLEEVE, EQUITY_SLEEVES, GOLD_SLEEVE
from cn_fund_strategy.domain.g6_quarterly_valuation_tilt import standard_g6_policy


def _sessions(start: date, count: int) -> tuple[str, ...]:
    result = []
    current = start
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current.isoformat())
        current += timedelta(days=1)
    return tuple(result)


class G6ReplayTests(unittest.TestCase):
    def test_synthetic_quarterly_replay_and_settlement_bridge(self):
        sessions = _sessions(date(2022, 1, 3), 900)
        rates = {
            "CN300": Decimal("0.00015"), "CN500": Decimal("0.00018"),
            "GROWTH": Decimal("0.00025"), "HK": Decimal("0.00012"),
            "SP500": Decimal("0.00030"), "NASDAQ": Decimal("0.00035"),
            GOLD_SLEEVE: Decimal("0.00010"), BOND_SLEEVE: Decimal("0.00004"),
        }
        levels = {
            key: {session: (Decimal("1") + rate) ** i for i, session in enumerate(sessions)}
            for key, rate in rates.items()
        }
        names = {key: key for key in levels}
        valuation_dates = sessions[::10]
        valuations = {
            key: tuple(
                (session, Decimal("10") + Decimal(i) / 10, Decimal("1") + Decimal(i) / 100)
                for i, session in enumerate(valuation_dates)
            )
            for key in EQUITY_SLEEVES
        }
        inputs = G1ReplayInputs({}, {}, sessions[260])
        policy = standard_g6_policy()
        with patch("cn_fund_strategy.application.g6_replay.load_total_return_levels", return_value=(levels, names)), patch(
            "cn_fund_strategy.application.g6_replay.load_valuation_histories", return_value=valuations
        ):
            replay = run_g6_replay(
                inputs, G1ReplayScenario("synthetic", 2, Decimal("0.005")), policy=policy
            )
        with patch("cn_fund_strategy.application.g6_replay.load_total_return_levels", return_value=(levels, names)):
            settlement = run_g6_fixed_event_settlement_stress(
                inputs, replay, redemption_settlement_common_sessions=10, policy=policy
            )
        self.assertEqual(replay.sessions, len(sessions) - 260)
        self.assertEqual(sum(replay.ending_weights.values()), Decimal("1"))
        self.assertLessEqual(replay.maximum_extreme_static_loss, Decimal("0.45"))
        self.assertEqual(replay.daily_ledger_record()["schema_version"], "g6-standard-daily-accounting-ledger-v1")
        self.assertGreater(Decimal(settlement["metrics"]["ending_nav_decimal"]), Decimal("0"))


if __name__ == "__main__":
    unittest.main()
