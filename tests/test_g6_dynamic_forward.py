from __future__ import annotations

from copy import deepcopy
import unittest

from cn_fund_strategy.application.g6_dynamic_forward import (
    R2_SCHEMA,
    validate_g6_dynamic_plan,
)
from cn_fund_strategy.application.g6_forward_shadow import (
    FUND_IDENTITIES,
    SLEEVE_ORDER,
    _payload_hash,
)


def _record() -> dict[str, object]:
    rows = []
    for sleeve in SLEEVE_ORDER:
        code, name, channel = FUND_IDENTITIES[sleeve]
        rows.append(
            {
                "sleeve_id": sleeve,
                "fund_code": code,
                "fund_name": name,
                "channel": channel,
                "direction": "HOLD",
                "target_weight_decimal": "0",
                "reference_target_amount_CNY_per_100k": "0.00",
                "requested_weight_change_decimal": "0",
                "requested_amount_CNY_per_100k": "0.00",
                "execution_gate_status": "hold-no-economic-order",
            }
        )
    record: dict[str, object] = {
        "schema_version": R2_SCHEMA,
        "record_type": "post-close-next-session-research-plan",
        "strategy": {
            "simulation_approved": False,
            "safe_for_account_order_placement": False,
        },
        "order_plan": rows,
        "authority": {"account_or_trading_authority": False},
    }
    record["record_hash"] = _payload_hash(record)
    return record


class G6DynamicForwardTests(unittest.TestCase):
    def test_validates_complete_hold_plan(self) -> None:
        validate_g6_dynamic_plan(_record())

    def test_rejects_hash_tampering(self) -> None:
        record = _record()
        record["order_plan"][0]["direction"] = "BUY"  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            validate_g6_dynamic_plan(record)

    def test_rejects_hold_with_money(self) -> None:
        record = deepcopy(_record())
        record["order_plan"][0]["requested_amount_CNY_per_100k"] = "1.00"  # type: ignore[index]
        record["record_hash"] = _payload_hash({k: v for k, v in record.items() if k != "record_hash"})
        with self.assertRaisesRegex(ValueError, "HOLD carries"):
            validate_g6_dynamic_plan(record)


if __name__ == "__main__":
    unittest.main()
