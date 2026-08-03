from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from cn_fund_strategy.adapters.tiantian_share_status_source_shape import (
    TiantianShareStatusSourceShapeError,
    capture_tiantian_share_html,
    project_tiantian_share_status_source_shape,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
ZERO = Decimal("0")
GENESIS_HASH = "0" * 64
MONEY = Decimal("0.01")
FUND_CODE = re.compile(r"[0-9]{6}")
SHA256 = re.compile(r"[0-9a-f]{64}")

SLEEVE_ORDER = (
    "CN300",
    "CN500",
    "GROWTH",
    "HK",
    "SP500",
    "NASDAQ",
    "GOLD",
    "BOND",
)
FUND_IDENTITIES = {
    "CN300": ("510300", "沪深300ETF华泰柏瑞", "exchange-etf"),
    "CN500": ("510500", "中证500ETF南方", "exchange-etf"),
    "GROWTH": ("159915", "创业板ETF易方达", "exchange-etf"),
    "HK": ("000071", "华夏恒生ETF联接A", "offexchange-public-fund"),
    "SP500": ("050025", "博时标普500ETF联接A", "offexchange-QDII"),
    "NASDAQ": (
        "270042",
        "广发纳斯达克100ETF联接人民币(QDII)A",
        "offexchange-QDII",
    ),
    "GOLD": ("000216", "华安黄金ETF联接A", "offexchange-public-fund"),
    "BOND": (
        "161120",
        "易方达中债新综指发起式(LOF)C",
        "offexchange-LOF",
    ),
}
LISTED_ETFS = frozenset({"510300", "510500", "159915"})


class G6ForwardShadowError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise G6ForwardShadowError(message)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _payload_hash(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    _require(path.is_file(), f"hash target is not a file: {path}")
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise G6ForwardShadowError(f"cannot read {label}: {exc}") from exc
    _require(isinstance(value, dict), f"{label} must be an object")
    return value


def _aware_time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise G6ForwardShadowError(f"invalid {label}: {value!r}") from exc
    _require(
        parsed.tzinfo is not None and parsed.utcoffset() is not None,
        f"{label} must be timezone-aware",
    )
    return parsed.astimezone(SHANGHAI)


def _date(value: str, label: str) -> str:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise G6ForwardShadowError(f"invalid {label}: {value!r}") from exc


def _decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise G6ForwardShadowError(f"invalid {label}: {value!r}") from exc
    _require(parsed.is_finite(), f"{label} must be finite")
    return parsed


def _money(value: Decimal) -> str:
    return format(value.quantize(MONEY, rounding=ROUND_HALF_UP), "f")


def _resolve_bound_path(project_root: Path, value: object, label: str) -> Path:
    path = Path(str(value))
    _require(not path.is_absolute() and ".." not in path.parts, f"{label} must be contained")
    result = project_root / path
    _require(result.is_file(), f"{label} is missing: {result}")
    return result


def validate_observation_contract(
    contract: Mapping[str, object],
    *,
    project_root: Path,
) -> None:
    _require(
        contract.get("schema_version")
        == "g6-standard-1-prospective-signal-shadow-contract-v1",
        "forward contract schema changed",
    )
    identity = contract.get("strategy_identity")
    _require(isinstance(identity, Mapping), "strategy identity missing")
    _require(identity.get("canonical_name") == "G6-Standard-1", "strategy name changed")
    _require(
        identity.get("technical_id") == "G6-SCQVT6-E50-G20-T025-Q1-L2-C225-R1",
        "strategy technical id changed",
    )
    _require(identity.get("simulation_approved") is False, "simulation status changed")
    _require(
        identity.get("safe_for_account_order_placement") is False,
        "trading authority changed",
    )
    for key in (
        "economic_contract",
        "technical_correction_contract",
        "frozen_result",
        "result_cas_manifest",
    ):
        binding = identity.get(key)
        _require(isinstance(binding, Mapping), f"{key} binding missing")
        path = _resolve_bound_path(project_root, binding.get("path"), key)
        _require(_file_hash(path) == binding.get("sha256"), f"{key} hash mismatch")
    code_hashes = identity.get("behavioral_code_hashes")
    _require(isinstance(code_hashes, Mapping) and bool(code_hashes), "code hashes missing")
    for raw_path, expected in code_hashes.items():
        path = _resolve_bound_path(project_root, raw_path, "behavioral code")
        _require(_file_hash(path) == expected, f"behavioral code hash mismatch: {raw_path}")
    authority = contract.get("purpose_and_authority")
    _require(isinstance(authority, Mapping), "authority missing")
    for field in (
        "may_access_account_credentials_positions_or_lots",
        "may_submit_cancel_or_modify_orders",
        "may_count_as_clean_historical_OOS",
        "may_count_toward_simulation_promotion",
        "changes_strategy_signal_weight_threshold_timing_or_product_role",
    ):
        _require(authority.get(field) is False, f"forbidden authority opened: {field}")


def _status_rows(manifest: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    rows = manifest.get("rows")
    _require(isinstance(rows, list), "status manifest rows missing")
    result: dict[str, Mapping[str, object]] = {}
    for row in rows:
        _require(isinstance(row, Mapping), "status manifest row must be an object")
        capture = row.get("capture")
        if isinstance(capture, Mapping):
            code = str(capture.get("fund_code") or "")
        else:
            code = str(row.get("fund_code") or "")
        _require(FUND_CODE.fullmatch(code) is not None, "status row fund code invalid")
        _require(code not in result, f"duplicate status row: {code}")
        result[code] = row
    return result


def _owner_text(row: Mapping[str, object] | None) -> str | None:
    if row is None:
        return None
    projection = row.get("projection")
    if not isinstance(projection, Mapping):
        return None
    value = projection.get("status_fragment_owner_text")
    return str(value) if value is not None else None


def _daily_cap(text: str | None) -> Decimal | None:
    if not text or "单日累计购买上限" not in text:
        return None
    tail = text.split("单日累计购买上限", 1)[1].split("元", 1)[0]
    value = "".join(character for character in tail if character.isdigit() or character == ".")
    return Decimal(value) if value else None


def _purchase_status(code: str, text: str | None) -> str:
    if code in LISTED_ETFS:
        return "not-applicable-secondary-market-ETF"
    if text is None:
        return "unknown"
    if "暂停申购" in text:
        return "suspended"
    if "限大额" in text:
        return "limited"
    if "开放申购" in text:
        return "open"
    return "unknown"


def _redemption_status(code: str, text: str | None) -> str:
    if code in LISTED_ETFS:
        return "not-applicable-secondary-market-ETF"
    if text is None:
        return "unknown"
    return "open" if "开放赎回" in text else "unknown"


def _market_phase(created_at: datetime, plan_session: str) -> str:
    local_date = created_at.date().isoformat()
    local_time = created_at.time()
    if local_date < plan_session:
        return "post-close-or-nontrading-gap-before-next-session"
    if local_date > plan_session:
        return "late-plan-after-session-date"
    if local_time < datetime.strptime("09:15", "%H:%M").time():
        return "pre-market"
    if local_time < datetime.strptime("15:00", "%H:%M").time():
        return "intraday"
    return "post-close"


def _verified_previous_record(path: Path) -> dict[str, object]:
    record = _read_object(path, "previous forward record")
    expected = record.get("record_hash")
    _require(isinstance(expected, str) and SHA256.fullmatch(expected) is not None, "previous hash invalid")
    unhashed = dict(record)
    unhashed.pop("record_hash", None)
    _require(_payload_hash(unhashed) == expected, "previous record hash mismatch")
    return record


def _status_capture_latest(rows: Mapping[str, Mapping[str, object]]) -> str | None:
    values: list[datetime] = []
    for row in rows.values():
        capture = row.get("capture")
        if isinstance(capture, Mapping) and capture.get("raw_durable_at"):
            values.append(_aware_time(str(capture["raw_durable_at"]), "status raw durable at"))
    if not values:
        return None
    return max(values).isoformat(timespec="microseconds")


def _execution_gate(
    *,
    code: str,
    direction: str,
    requested_amount: Decimal,
    purchase_status: str,
    redemption_status: str,
    daily_cap: Decimal | None,
) -> tuple[str, tuple[str, ...], Decimal]:
    if direction == "HOLD":
        return "hold-no-economic-order", ("NO_ECONOMIC_ORDER",), ZERO
    if code in LISTED_ETFS:
        return (
            "blocked-missing-same-session-listed-ETF-execution-evidence",
            (
                "SAME_SESSION_QUOTE_SPREAD_DEPTH_TRADING_STATUS_PREMIUM_AND_COST_REQUIRED",
            ),
            ZERO,
        )
    if direction == "BUY":
        if purchase_status == "suspended":
            return "blocked-current-purchase-suspended", ("CURRENT_PURCHASE_SUSPENDED",), ZERO
        if purchase_status == "unknown":
            return "blocked-current-purchase-status-unknown", ("CURRENT_PURCHASE_STATUS_UNKNOWN",), ZERO
        if daily_cap is not None and requested_amount > daily_cap:
            return (
                "blocked-current-daily-purchase-cap-insufficient",
                ("CURRENT_DAILY_CAP_BELOW_REQUEST", "DO_NOT_SPLIT_TO_EVADE_LIMIT"),
                ZERO,
            )
        return (
            "conditional-current-buy-gate-passed-execution-session-recheck-required",
            ("CURRENT_CHANNEL_AND_CAP_PASS_AT_CAPTURE_ONLY",),
            requested_amount,
        )
    if redemption_status != "open":
        return "blocked-current-redemption-status-unknown", ("CURRENT_REDEMPTION_STATUS_UNKNOWN",), ZERO
    return (
        "conditional-current-sell-gate-passed-lot-fee-recheck-required",
        ("CURRENT_REDEMPTION_OPEN", "ACTUAL_LOT_HOLDING_PERIOD_AND_FEE_REQUIRED"),
        requested_amount,
    )


def build_g6_forward_plan(
    *,
    project_root: Path,
    observation_contract_path: Path,
    frozen_result_path: Path,
    status_manifest_path: Path,
    plan_for_session: str,
    information_cutoff_at: str,
    created_at: str,
    previous_record_path: Path | None = None,
) -> dict[str, object]:
    plan_session = _date(plan_for_session, "plan session")
    created = _aware_time(created_at, "created at")
    cutoff = _aware_time(information_cutoff_at, "information cutoff at")
    _require(cutoff <= created, "information cutoff is after plan creation")
    contract = _read_object(observation_contract_path, "observation contract")
    validate_observation_contract(contract, project_root=project_root)
    first = _date(str(contract["first_eligible_plan_session"]), "first eligible plan session")
    _require(plan_session >= first, "forward plan would backfill before the freeze")
    result = _read_object(frozen_result_path, "frozen G6 result")
    identity = contract["strategy_identity"]
    _require(isinstance(identity, Mapping), "strategy identity missing")
    result_binding = identity["frozen_result"]
    _require(isinstance(result_binding, Mapping), "result binding missing")
    _require(_file_hash(frozen_result_path) == result_binding["sha256"], "frozen result hash mismatch")
    candidate = result.get("candidate")
    _require(isinstance(candidate, Mapping), "candidate record missing")
    _require(candidate.get("canonical_name") == identity["canonical_name"], "candidate name mismatch")
    _require(candidate.get("technical_id") == identity["technical_id"], "candidate id mismatch")
    _require(candidate.get("simulation_approved") is False, "candidate approval status changed")
    briefing = result.get("current_research_briefing")
    _require(isinstance(briefing, Mapping), "frozen current briefing missing")
    targets = briefing.get("model_target_weights")
    _require(isinstance(targets, Mapping), "model target weights missing")
    target_weights = {key: _decimal(targets[key], f"target {key}") for key in SLEEVE_ORDER}
    _require(all(value >= ZERO for value in target_weights.values()), "negative target weight")
    _require(sum(target_weights.values(), ZERO) == Decimal("1"), "target weights do not sum to one")

    raw_orders = briefing.get("next_common_session_order_list")
    _require(isinstance(raw_orders, list), "next-session order list missing")
    by_sleeve: dict[str, Mapping[str, object]] = {}
    for row in raw_orders:
        _require(isinstance(row, Mapping), "order row must be an object")
        sleeve = str(row.get("sleeve_id") or "")
        _require(sleeve in SLEEVE_ORDER and sleeve not in by_sleeve, "order sleeve invalid")
        by_sleeve[sleeve] = row

    status_manifest = _read_object(status_manifest_path, "current status manifest")
    statuses = _status_rows(status_manifest)
    status_latest = _status_capture_latest(statuses)
    if status_latest is not None:
        _require(_aware_time(status_latest, "latest status capture") <= created, "status capture is after plan creation")

    reference_nav = _decimal(
        contract["daily_plan_contract"]["reference_portfolio_CNY"],  # type: ignore[index]
        "reference portfolio",
    )
    action_rows: list[dict[str, object]] = []
    initialization_blockers: list[str] = []
    for sleeve in SLEEVE_ORDER:
        code, name, channel = FUND_IDENTITIES[sleeve]
        raw = by_sleeve.get(sleeve)
        if raw is None:
            direction = "HOLD"
            requested_change = ZERO
            reasons = ("BETWEEN_QUARTER_ENDS_HOLD_NO_ORDER",)
        else:
            direction = str(raw.get("direction") or "HOLD").upper()
            requested_change = _decimal(
                raw.get("requested_weight_change_decimal", "0"),
                f"requested change {sleeve}",
            )
            raw_reasons = raw.get("reason_codes")
            reasons = tuple(str(item) for item in raw_reasons) if isinstance(raw_reasons, list) else ("UNSPECIFIED",)
        _require(direction in {"BUY", "SELL", "HOLD"}, f"invalid direction for {sleeve}")
        _require(
            (direction == "HOLD" and requested_change == ZERO)
            or (direction == "BUY" and requested_change > ZERO)
            or (direction == "SELL" and requested_change < ZERO),
            f"direction and weight change mismatch for {sleeve}",
        )
        status_row = statuses.get(code)
        text = _owner_text(status_row)
        cap = _daily_cap(text)
        purchase = _purchase_status(code, text)
        redemption = _redemption_status(code, text)
        requested_amount = abs(requested_change) * reference_nav
        gate, gate_reasons, executable = _execution_gate(
            code=code,
            direction=direction,
            requested_amount=requested_amount,
            purchase_status=purchase,
            redemption_status=redemption,
            daily_cap=cap,
        )
        target_amount = target_weights[sleeve] * reference_nav
        if target_weights[sleeve] > ZERO:
            init_gate, init_reasons, _ = _execution_gate(
                code=code,
                direction="BUY",
                requested_amount=target_amount,
                purchase_status=purchase,
                redemption_status=redemption,
                daily_cap=cap,
            )
            if not init_gate.startswith("conditional-current-buy-gate-passed"):
                initialization_blockers.append(f"{code}:{init_gate}:{','.join(init_reasons)}")
        action_rows.append(
            {
                "sleeve_id": sleeve,
                "fund_code": code,
                "fund_name": name,
                "channel": channel,
                "direction": direction,
                "target_weight_decimal": format(target_weights[sleeve], "f"),
                "reference_target_amount_CNY_per_100k": _money(target_amount),
                "requested_weight_change_decimal": format(requested_change, "f"),
                "requested_amount_CNY_per_100k": _money(requested_amount),
                "currently_executable_amount_CNY_per_100k": _money(executable),
                "trigger_reason_codes": list(reasons),
                "purchase_status": purchase,
                "redemption_status": redemption,
                "purchase_cap_CNY": _money(cap) if cap is not None else None,
                "tradeability_status": (
                    "not-checked-no-order"
                    if direction == "HOLD"
                    else "same-session-evidence-required"
                    if code in LISTED_ETFS
                    else "channel-status-captured-execution-session-recheck-required"
                ),
                "execution_gate_status": gate,
                "execution_gate_reason_codes": list(gate_reasons),
                "same_session_recheck_required": direction != "HOLD",
                "status_evidence_qualification": (
                    status_row.get("evidence_batch_status") if status_row else "missing"
                ),
            }
        )

    previous: dict[str, object] | None = None
    if previous_record_path is not None:
        previous = _verified_previous_record(previous_record_path)
        _require(
            str(previous.get("plan_for_session")) < plan_session,
            "new plan session must be after previous record",
        )
        prior_hash = str(previous["record_hash"])
        ordinal = int(previous.get("record_ordinal", 0)) + 1
    else:
        prior_hash = GENESIS_HASH
        ordinal = 1

    generator_path = Path(__file__).resolve()
    cli_path = project_root / "src/cn_fund_strategy/interfaces/g6_forward_cli.py"
    generator_hash = _file_hash(generator_path)
    cli_hash = _file_hash(cli_path)
    if previous is not None:
        prior_snapshot = previous.get("input_snapshot")
        _require(isinstance(prior_snapshot, Mapping), "previous input snapshot missing")
        _require(
            prior_snapshot.get("forward_generator_code_sha256") == generator_hash
            and prior_snapshot.get("forward_cli_code_sha256") == cli_hash,
            "forward implementation changed; start a new observation lineage",
        )

    decision_payload = {
        "frozen_result_sha256": result_binding["sha256"],
        "source_decision_session": briefing.get("decision_session"),
        "current_phase": briefing.get("current_phase"),
        "target_weights": {key: format(target_weights[key], "f") for key in SLEEVE_ORDER},
        "actions": [
            {
                "fund_code": row["fund_code"],
                "direction": row["direction"],
                "requested_weight_change_decimal": row["requested_weight_change_decimal"],
                "trigger_reason_codes": row["trigger_reason_codes"],
            }
            for row in action_rows
        ],
    }
    decision_id = _payload_hash(decision_payload)
    plan_payload = {
        "decision_id": decision_id,
        "plan_for_session": plan_session,
        "actions": action_rows,
        "initialization_blockers": sorted(initialization_blockers),
    }
    plan_id = _payload_hash(plan_payload)
    record: dict[str, object] = {
        "schema_version": "g6-standard-1-prospective-trade-plan-v1",
        "observation_id": contract["observation_id"],
        "record_type": "pre-session-plan",
        "quality_status": "not-counted-plan",
        "record_ordinal": ordinal,
        "prior_record_hash": prior_hash,
        "supersedes_record_hash": None,
        "created_at": created.isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "market_phase_at_creation": _market_phase(created, plan_session),
        "plan_for_session": plan_session,
        "information_cutoff_at": cutoff.isoformat(timespec="seconds"),
        "source_decision_session": briefing.get("decision_session"),
        "source_signal_phase": briefing.get("current_phase"),
        "next_formal_decision_rule": briefing.get("next_formal_decision_rule"),
        "strategy": {
            "canonical_name": candidate["canonical_name"],
            "technical_id": candidate["technical_id"],
            "candidate_status": candidate["status"],
            "simulation_approved": False,
            "safe_for_account_order_placement": False,
            "mode": "no-op" if all(row["direction"] == "HOLD" for row in action_rows) else "research-shadow-order-plan",
        },
        "decision_id": decision_id,
        "plan_id": plan_id,
        "reference_portfolio_CNY": _money(reference_nav),
        "account_state": "not-accessed-model-reference-only",
        "order_plan": action_rows,
        "new_portfolio_initialization": {
            "status": "blocked" if initialization_blockers else "conditional-current-gates-pass-recheck-required",
            "blockers": sorted(initialization_blockers),
            "unfilled_destination": "BOND-or-cash-defense",
        },
        "input_snapshot": {
            "observation_contract_path": str(observation_contract_path.resolve()),
            "observation_contract_sha256": _file_hash(observation_contract_path),
            "frozen_result_path": str(frozen_result_path.resolve()),
            "frozen_result_sha256": _file_hash(frozen_result_path),
            "current_status_manifest_path": str(status_manifest_path.resolve()),
            "current_status_manifest_sha256": _file_hash(status_manifest_path),
            "current_status_latest_capture_at": status_latest,
            "forward_generator_code_path": str(generator_path),
            "forward_generator_code_sha256": generator_hash,
            "forward_cli_code_path": str(cli_path.resolve()),
            "forward_cli_code_sha256": cli_hash,
        },
        "reconciliation": {
            "expected_orders": sum(row["direction"] != "HOLD" for row in action_rows),
            "submitted_orders": 0,
            "fills": 0,
            "account_positions_cash_NAV_reconciled": False,
            "reason": "pre-session-research-plan-no-account-or-broker-access",
            "complete_forward_session_count_increment": 0,
        },
        "authority": {
            "research_and_simulation_only": True,
            "clean_or_OOS": False,
            "simulation_promotion_evidence": False,
            "account_or_trading_authority": False,
        },
    }
    record["record_hash"] = _payload_hash(record)
    validate_g6_forward_plan(record)
    return record


def validate_g6_forward_plan(record: Mapping[str, object]) -> None:
    _require(
        record.get("schema_version") == "g6-standard-1-prospective-trade-plan-v1",
        "forward plan schema changed",
    )
    _require(record.get("record_type") == "pre-session-plan", "record type changed")
    _require(record.get("quality_status") == "not-counted-plan", "quality status changed")
    expected = record.get("record_hash")
    _require(isinstance(expected, str) and SHA256.fullmatch(expected) is not None, "record hash invalid")
    unhashed = dict(record)
    unhashed.pop("record_hash", None)
    _require(_payload_hash(unhashed) == expected, "record hash mismatch")
    strategy = record.get("strategy")
    _require(isinstance(strategy, Mapping), "strategy record missing")
    _require(strategy.get("simulation_approved") is False, "simulation authority changed")
    _require(strategy.get("safe_for_account_order_placement") is False, "trading authority changed")
    orders = record.get("order_plan")
    _require(isinstance(orders, list) and len(orders) == len(SLEEVE_ORDER), "order plan is incomplete")
    _require(
        tuple(str(row["sleeve_id"]) for row in orders if isinstance(row, Mapping)) == SLEEVE_ORDER,
        "order plan sleeve order changed",
    )
    for row in orders:
        _require(isinstance(row, Mapping), "order row must be an object")
        direction = row.get("direction")
        _require(direction in {"BUY", "SELL", "HOLD"}, "order direction invalid")
        if direction == "HOLD":
            _require(
                row.get("requested_amount_CNY_per_100k") == "0.00"
                and row.get("execution_gate_status") == "hold-no-economic-order",
                "hold row carries an economic order",
            )
    authority = record.get("authority")
    _require(isinstance(authority, Mapping), "plan authority missing")
    _require(authority.get("account_or_trading_authority") is False, "trading authority opened")


def write_create_or_identical(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _require(path.read_bytes() == payload, f"refusing to overwrite different output: {path}")
        return
    temporary = path.with_name(f".{path.name}.tmp")
    _require(not temporary.exists(), f"temporary output already exists: {temporary}")
    temporary.write_bytes(payload)
    temporary.replace(path)


def write_g6_forward_plan(path: Path, record: Mapping[str, object]) -> None:
    validate_g6_forward_plan(record)
    payload = (json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    write_create_or_identical(path, payload)


def render_g6_trade_brief(record: Mapping[str, object]) -> str:
    validate_g6_forward_plan(record)
    orders = record["order_plan"]
    assert isinstance(orders, list)
    strategy = record["strategy"]
    assert isinstance(strategy, Mapping)
    initialization = record["new_portfolio_initialization"]
    assert isinstance(initialization, Mapping)
    lines = [
        f"# {strategy['canonical_name']} 次一交易日研究简报",
        "",
        f"生成时间：{record['created_at']}  ",
        f"计划交易日：{record['plan_for_session']}  ",
        "性质：只读研究型 prospective shadow；不读取账户，不提交订单",
        "",
        "## 当前市场阶段",
        "",
        f"{record['market_phase_at_creation']}；策略处于 {strategy['mode']} 模式。",
        "",
        "## 当前使用的策略",
        "",
        f"{strategy['canonical_name']}（{strategy['technical_id']}）。候选状态：{strategy['candidate_status']}；未获仿真或实盘批准。",
        "",
        "## 趋势和风险状态",
        "",
        "本日不是正式季度决策点，趋势只作预览，不改变仓位。由于未读取真实账户净值和持仓，当前账户回撤与风险状态未知，不作推断。",
        "",
        "## 触发参数和策略路径",
        "",
        f"信息截止：{record['information_cutoff_at']}；正式决策规则：{record['next_formal_decision_rule']}。当前选择不交易路径。",
        "",
        "## 当前目标仓位",
        "",
        "| 代码 | 名称 | 目标仓位 | 每10万元目标金额 |",
        "|---|---|---:|---:|",
    ]
    for row in orders:
        lines.append(
            f"| {row['fund_code']} | {row['fund_name']} | {Decimal(str(row['target_weight_decimal'])):.2%} | {row['reference_target_amount_CNY_per_100k']} 元 |"
        )
    lines.extend(
        [
            "",
            "以上为模型目标，不是已核验的真实账户持仓；没有账户状态时不能计算账户级买卖差额。",
            "",
            "## 具体交易动作",
            "",
            "| 代码 | 动作 | 计划金额/每10万元 | 申购状态 | 限额 | 可交易检查 | 原因 |",
            "|---|---|---:|---|---:|---|---|",
        ]
    )
    for row in orders:
        cap = f"{row['purchase_cap_CNY']} 元" if row["purchase_cap_CNY"] is not None else "—"
        reasons = ", ".join(row["trigger_reason_codes"])
        lines.append(
            f"| {row['fund_code']} | {row['direction']} | {row['requested_amount_CNY_per_100k']} 元 | {row['purchase_status']} | {cap} | {row['execution_gate_status']} | {reasons} |"
        )
    lines.extend(
        [
            "",
            f"新组合初始化状态：{initialization['status']}。未成交目标继续留在债券或现金防守仓。",
            "",
            "## 标的解释",
            "",
            "A股三只ETF承担境内权益；恒生、标普500和纳指100提供境外权益暴露；黄金负责危机与通胀分散；债券负责防守、结算等待与再平衡资金。标普500与纳指100存在明显美国大型成长股重叠。",
            "",
            "## 风控和订单约束",
            "",
            "仅做多、不使用杠杆、不做空；场外基金必须通过同一份额同一渠道的当日申赎与限额检查；场内ETF只有在实际有订单时才要求同一交易时点的报价、价差、深度、溢价和交易状态。未知即阻断，禁止拆单或换高溢价ETF规避QDII限购。",
            "",
            "## 最终一句话",
            "",
            "今天/现在做什么：全部持有，不买、不卖；新资金不要强行一次性建仓。",
            "",
            f"记录哈希：`{record['record_hash']}`",
            "",
        ]
    )
    return "\n".join(lines)


def capture_current_status_batch(
    *,
    output_root: Path,
    fund_codes: Sequence[str] = tuple(item[0] for item in FUND_IDENTITIES.values()),
) -> dict[str, object]:
    _require(not (output_root / "manifest.json").exists(), "status manifest already exists")
    _require(len(set(fund_codes)) == len(fund_codes), "fund codes must be unique")
    rows: list[dict[str, object]] = []
    for code in fund_codes:
        _require(FUND_CODE.fullmatch(code) is not None, f"invalid fund code: {code}")
        raw_path = output_root / "raw" / f"{code}.html"
        try:
            capture = capture_tiantian_share_html(fund_code=code, output_path=raw_path)
        except TiantianShareStatusSourceShapeError as exc:
            rows.append(
                {
                    "fund_code": code,
                    "capture": None,
                    "projection": None,
                    "evidence_batch_status": "quarantine",
                    "qualification": "capture-failed-current-state-unknown",
                    "error": str(exc),
                }
            )
            continue
        try:
            projection = project_tiantian_share_status_source_shape(
                raw_path.read_bytes(),
                fund_code=code,
                charset=capture.charset,
            )
            row = {
                "capture": dict(capture.receipt()),
                "projection": dict(projection.as_record()),
                "evidence_batch_status": "research-ready-for-current-owner-text-only",
                "qualification": "current-secondary-owner-text-not-legal-fee-settlement-or-product-admission",
            }
        except TiantianShareStatusSourceShapeError as exc:
            row = {
                "fund_code": code,
                "capture": dict(capture.receipt()),
                "projection": None,
                "evidence_batch_status": "quarantine",
                "qualification": "projection-failed-current-state-unknown-raw-retained",
                "error": str(exc),
            }
        rows.append(row)
    manifest: dict[str, object] = {
        "schema_version": "g6-forward-current-product-status-batch-v1",
        "rows": rows,
        "authority": {
            "current_owner_text_if_projection_passed": True,
            "legal_fee_settlement_product_admission_order_or_trading": False,
        },
        "required_notice": "read-only-current-source-evidence-not-investment-advice",
    }
    payload = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    write_create_or_identical(output_root / "manifest.json", payload)
    return manifest


__all__ = [
    "G6ForwardShadowError",
    "build_g6_forward_plan",
    "capture_current_status_batch",
    "render_g6_trade_brief",
    "validate_g6_forward_plan",
    "validate_observation_contract",
    "write_g6_forward_plan",
]
