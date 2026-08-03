from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from cn_fund_strategy.adapters.eastmoney_profile_nav import (
    capture_eastmoney_profile_nav,
)
from cn_fund_strategy.adapters.g6_global_valuation_capture import (
    capture_g6_global_valuation,
)
from cn_fund_strategy.adapters.sse_closure_calendar import (
    capture_sse_closure_calendar,
    next_trading_session,
)
from cn_fund_strategy.application.g1_replay import (
    G1ReplayInputs,
    _month_ends,
    load_total_return_levels,
    load_valuation_histories,
    with_g1_decimal_context,
)
from cn_fund_strategy.application.g6_forward_shadow import (
    FUND_IDENTITIES,
    GENESIS_HASH,
    LISTED_ETFS,
    MONEY,
    SHANGHAI,
    SLEEVE_ORDER,
    ZERO,
    _aware_time,
    _daily_cap,
    _date,
    _decimal,
    _execution_gate,
    _file_hash,
    _money,
    _owner_text,
    _payload_hash,
    _purchase_status,
    _read_object,
    _redemption_status,
    _require,
    _status_capture_latest,
    _status_rows,
    capture_current_status_batch,
    write_create_or_identical,
)
from cn_fund_strategy.application.g6_replay import (
    _current_signal,
    _historical_signal,
)
from cn_fund_strategy.domain.g1_global_valuation_batch import EQUITY_SLEEVES
from cn_fund_strategy.domain.g6_quarterly_valuation_tilt import (
    decide_tilt,
    standard_g6_1_policy,
)


PROFILE_CODES = {
    "CN300": "510300",
    "CN500": "510500",
    "GROWTH": "159915",
    "HK": "000071",
    "SP500": "050025",
    "NASDAQ": "270042",
    "GOLD": "000216",
    "BOND": "161120",
}
VALUATION_CODES = {
    "CN300": "SH000300",
    "CN500": "SH000905",
    "GROWTH": "SZ399006",
    "HK": "HKHSI",
    "SP500": "SP500",
    "NASDAQ": "NDX",
}
R2_SCHEMA = "g6-standard-1-dynamic-forward-plan-v1"
R2_CONTRACT_SCHEMA = "g6-standard-1-dynamic-forward-operation-contract-v1"
REFERENCE_NAV = Decimal("100000.00")


def _json_ready(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_ready(item) for item in value]
    return value


def _hashes(paths: Sequence[Path]) -> dict[str, str]:
    return {str(path.resolve()): _file_hash(path) for path in paths}


def _utc_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid UTC received_at: {value!r}") from exc
    _require(parsed.tzinfo is not None, "received_at must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def validate_dynamic_contract(
    contract: Mapping[str, object],
    *,
    project_root: Path,
) -> None:
    _require(contract.get("schema_version") == R2_CONTRACT_SCHEMA, "R2 contract schema changed")
    identity = contract.get("strategy_identity")
    _require(isinstance(identity, Mapping), "R2 strategy identity missing")
    _require(identity.get("canonical_name") == "G6-Standard-1", "R2 strategy name changed")
    _require(
        identity.get("technical_id") == "G6-SCQVT6-E50-G20-T025-Q1-L2-C225-R1",
        "R2 technical id changed",
    )
    _require(identity.get("simulation_approved") is False, "R2 simulation authority changed")
    _require(
        identity.get("safe_for_account_order_placement") is False,
        "R2 trading authority changed",
    )
    bindings: list[Mapping[str, object]] = []
    for key in ("economic_contract", "technical_correction_contract", "frozen_result"):
        binding = identity.get(key)
        _require(isinstance(binding, Mapping), f"R2 {key} binding missing")
        bindings.append(binding)
    lineage = contract.get("lineage")
    _require(isinstance(lineage, Mapping), "R2 lineage missing")
    for key in (
        "supersedes_observation_contract_for_new_sessions_only",
        "preserved_genesis_plan",
        "preserved_genesis_cas",
    ):
        binding = lineage.get(key)
        _require(isinstance(binding, Mapping), f"R2 {key} binding missing")
        bindings.append(binding)
    for binding in bindings:
        path = Path(str(binding.get("path")))
        _require(not path.is_absolute() and ".." not in path.parts, "R2 binding escapes project")
        resolved = project_root / path
        _require(resolved.is_file(), f"R2 binding missing: {path}")
        _require(_file_hash(resolved) == binding.get("sha256"), f"R2 binding hash mismatch: {path}")
    frozen_r1 = lineage.get("r1_implementation_is_frozen_and_must_not_be_modified")
    _require(isinstance(frozen_r1, Mapping), "R2 frozen R1 code hashes missing")
    for raw_path, expected in frozen_r1.items():
        path = project_root / str(raw_path)
        _require(path.is_file(), f"R2 frozen R1 path missing: {raw_path}")
        _require(_file_hash(path) == expected, f"R2 frozen R1 code drift: {raw_path}")
    authority = contract.get("authority_and_evidence")
    _require(isinstance(authority, Mapping), "R2 authority missing")
    for key in (
        "may_access_account_credentials_positions_or_lots",
        "may_submit_cancel_or_modify_orders",
        "historical_results_are_clean_OOS",
        "prospective_plan_is_simulation_promotion_evidence",
        "performance_claim_inherited_without_operational_timing_gap_test",
    ):
        _require(authority.get(key) is False, f"R2 forbidden authority opened: {key}")


def capture_dynamic_input_batch(
    *,
    output_root: Path,
    received_at: str,
    timeout_seconds: int = 30,
) -> dict[str, object]:
    """Capture a bounded read-only input batch and retain partial failures."""

    manifest_path = output_root / "manifest.json"
    _require(not manifest_path.exists(), "dynamic input manifest already exists")
    received = _utc_time(received_at)
    local_year = received.astimezone(SHANGHAI).year
    profile_rows: list[dict[str, object]] = []
    valuation_rows: list[dict[str, object]] = []
    artifact_paths: list[Path] = []
    errors: list[str] = []
    for sleeve, code in PROFILE_CODES.items():
        output = output_root / "eastmoney-profile" / f"{code}.js"
        try:
            capture = capture_eastmoney_profile_nav(
                output,
                fund_code=code,
                received_at=received_at,
                timeout_seconds=timeout_seconds,
            )
            row = {"sleeve_id": sleeve, "status": "captured", **asdict(capture)}
            profile_rows.append(row)
            artifact_paths.extend((output, Path(capture.manifest_path)))
        except Exception as exc:
            errors.append(f"PROFILE:{sleeve}:{type(exc).__name__}:{exc}")
            profile_rows.append(
                {"sleeve_id": sleeve, "fund_code": code, "status": "quarantine", "error": str(exc)}
            )
    for sleeve, code in VALUATION_CODES.items():
        output = output_root / "danjuan" / code
        try:
            capture = capture_g6_global_valuation(
                output,
                index_code=code,
                received_at=received_at,
                timeout_seconds=timeout_seconds,
            )
            row = {"sleeve_id": sleeve, "status": "captured", **asdict(capture)}
            valuation_rows.append(row)
            artifact_paths.extend(
                (
                    Path(capture.pe_raw_path),
                    Path(capture.pb_raw_path),
                    Path(capture.manifest_path),
                )
            )
        except Exception as exc:
            errors.append(f"VALUATION:{sleeve}:{type(exc).__name__}:{exc}")
            valuation_rows.append(
                {"sleeve_id": sleeve, "index_code": code, "status": "quarantine", "error": str(exc)}
            )
    calendar_record: dict[str, object]
    try:
        capture = capture_sse_closure_calendar(
            output_root / "sse-calendar",
            calendar_year=local_year,
            received_at=received_at,
            timeout_seconds=timeout_seconds,
        )
        calendar_record = {"status": "captured", **asdict(capture)}
        artifact_paths.extend((Path(capture.raw_path), Path(capture.manifest_path)))
    except Exception as exc:
        errors.append(f"CALENDAR:{type(exc).__name__}:{exc}")
        calendar_record = {"status": "quarantine", "calendar_year": local_year, "error": str(exc)}
    try:
        status = capture_current_status_batch(output_root=output_root / "current-status")
        status_manifest_path = output_root / "current-status" / "manifest.json"
        status_record: dict[str, object] = {
            "status": "captured-with-row-level-qualification",
            "manifest_path": str(status_manifest_path.resolve()),
            "manifest_sha256": _file_hash(status_manifest_path),
            "row_count": len(status["rows"]),
        }
        artifact_paths.append(status_manifest_path)
        for raw in sorted((output_root / "current-status" / "raw").glob("*")):
            if raw.is_file():
                artifact_paths.append(raw)
    except Exception as exc:
        errors.append(f"PRODUCT_STATUS:{type(exc).__name__}:{exc}")
        status_record = {"status": "quarantine", "error": str(exc)}
    signal_ready = (
        all(row["status"] == "captured" for row in profile_rows)
        and all(row["status"] == "captured" for row in valuation_rows)
        and calendar_record["status"] == "captured"
    )
    manifest: dict[str, object] = {
        "schema_version": "g6-dynamic-forward-input-batch-v1",
        "received_at": received.isoformat().replace("+00:00", "Z"),
        "output_root": str(output_root.resolve()),
        "batch_status": "signal-input-ready" if signal_ready else "quarantine-HOLD-only",
        "profile_rows": profile_rows,
        "valuation_rows": valuation_rows,
        "calendar": calendar_record,
        "product_status": status_record,
        "artifact_hashes": _hashes(tuple(dict.fromkeys(artifact_paths))),
        "errors": errors,
        "authority": {
            "read_only_public_capture": True,
            "retrospective_history_is_clean_OOS": False,
            "account_or_order_access": False,
        },
    }
    payload = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    write_create_or_identical(manifest_path, payload)
    return manifest


def _validate_input_manifest(
    manifest: Mapping[str, object],
    *,
    created: datetime,
) -> tuple[Path, Path, tuple[str, ...]]:
    _require(
        manifest.get("schema_version") == "g6-dynamic-forward-input-batch-v1",
        "dynamic input schema changed",
    )
    _require(manifest.get("batch_status") == "signal-input-ready", "dynamic signal inputs quarantined")
    received = _aware_time(str(manifest.get("received_at")), "input received at")
    _require(received <= created, "dynamic input was received after plan creation")
    root = Path(str(manifest.get("output_root"))).resolve()
    _require(root.is_dir(), "dynamic input root missing")
    hashes = manifest.get("artifact_hashes")
    _require(isinstance(hashes, Mapping) and bool(hashes), "dynamic input artifact hashes missing")
    for raw_path, expected in hashes.items():
        path = Path(str(raw_path)).resolve()
        _require(path.is_relative_to(root), "dynamic input artifact escapes batch root")
        _require(path.is_file(), f"dynamic input artifact missing: {path}")
        _require(_file_hash(path) == expected, f"dynamic input artifact hash mismatch: {path}")
    status = manifest.get("product_status")
    _require(isinstance(status, Mapping), "dynamic product status binding missing")
    status_path = Path(str(status.get("manifest_path"))).resolve()
    _require(status_path.is_relative_to(root), "status manifest escapes input root")
    _require(_file_hash(status_path) == status.get("manifest_sha256"), "status manifest hash mismatch")
    calendar = manifest.get("calendar")
    _require(isinstance(calendar, Mapping) and calendar.get("status") == "captured", "calendar missing")
    calendar_manifest_path = Path(str(calendar.get("manifest_path"))).resolve()
    _require(calendar_manifest_path.is_relative_to(root), "calendar manifest escapes input root")
    calendar_manifest = _read_object(calendar_manifest_path, "SSE calendar manifest")
    closed = calendar_manifest.get("closed_dates")
    _require(isinstance(closed, list), "SSE closed dates missing")
    return root, status_path, tuple(str(item) for item in closed)


def _inputs(root: Path) -> G1ReplayInputs:
    return G1ReplayInputs(
        profile_paths={
            sleeve: root / "eastmoney-profile" / f"{code}.js"
            for sleeve, code in PROFILE_CODES.items()
        },
        valuation_directories={
            sleeve: root / "danjuan" / code for sleeve, code in VALUATION_CODES.items()
        },
        start_session="2015-01-05",
    )


def _seed_state(result: Mapping[str, object]) -> tuple[str, dict[str, Decimal]]:
    history = result.get("historical_L1_contaminated_results")
    _require(isinstance(history, Mapping), "frozen G6 history missing")
    base = history.get("base")
    _require(isinstance(base, Mapping), "frozen G6 base result missing")
    decisions = base.get("latest_decisions")
    _require(isinstance(decisions, list) and decisions, "frozen G6 latest decisions missing")
    latest = decisions[-1]
    _require(isinstance(latest, Mapping), "frozen latest decision invalid")
    decision = latest.get("decision")
    _require(isinstance(decision, Mapping), "frozen decision missing")
    raw_target = decision.get("target_weights")
    _require(isinstance(raw_target, list), "frozen target weights missing")
    target = {str(row[0]): _decimal(row[1], "frozen target") for row in raw_target}
    _require(tuple(target) == SLEEVE_ORDER, "frozen target identity/order changed")
    _require(sum(target.values(), ZERO) == Decimal("1"), "frozen targets do not sum to one")
    return str(decision.get("decision_session")), target


def _verified_previous(path: Path) -> dict[str, object]:
    previous = _read_object(path, "previous R2 forward record")
    validate_g6_dynamic_plan(previous)
    return previous


def _formal_decisions(
    *,
    common_sessions: Sequence[str],
    month_ends: Sequence[str],
    levels: Mapping[str, Mapping[str, Decimal]],
    valuations: Mapping[str, Sequence[tuple[str, Decimal, Decimal]]],
    after_session: str,
) -> list[dict[str, object]]:
    policy = standard_g6_1_policy()
    finalized = set(month_ends[:-1])
    positions = {item: index for index, item in enumerate(month_ends)}
    records: list[dict[str, object]] = []
    for session in month_ends:
        current = date.fromisoformat(session)
        if session <= after_session or current.month not in {3, 6, 9, 12} or session not in finalized:
            continue
        signal = _historical_signal(
            session,
            positions[session],
            month_ends,
            levels,
            valuations,
        )
        decision = decide_tilt(policy, signal)
        position = common_sessions.index(session)
        original_due = (
            common_sessions[position + 2] if position + 2 < len(common_sessions) else None
        )
        records.append(
            {
                "signal": _json_ready(asdict(signal)),
                "decision": _json_ready(asdict(decision)),
                "original_two_common_session_due": original_due,
            }
        )
    return records


def _signal_preview(
    *,
    plan_session: str,
    latest_common: str,
    month_ends: Sequence[str],
    levels: Mapping[str, Mapping[str, Decimal]],
    valuations: Mapping[str, Sequence[tuple[str, Decimal, Decimal]]],
    cutoff_session: str,
) -> tuple[dict[str, object], dict[str, object]]:
    _require(len(month_ends) >= 11, "fewer than eleven common month ends")
    signal = _current_signal(
        plan_session,
        latest_common,
        month_ends,
        levels,
        valuations,
        valuation_cutoff_session=cutoff_session,
    )
    decision = decide_tilt(standard_g6_1_policy(), signal)
    return _json_ready(asdict(signal)), _json_ready(asdict(decision))  # type: ignore[return-value]


@with_g1_decimal_context
def build_g6_dynamic_plan(
    *,
    project_root: Path,
    operation_contract_path: Path,
    frozen_result_path: Path,
    input_manifest_path: Path,
    plan_for_session: str,
    information_cutoff_at: str,
    created_at: str,
    previous_record_path: Path | None = None,
) -> dict[str, object]:
    plan_session = _date(plan_for_session, "R2 plan session")
    created = _aware_time(created_at, "R2 created at")
    cutoff = _aware_time(information_cutoff_at, "R2 information cutoff")
    _require(cutoff <= created, "R2 information cutoff is after creation")
    _require(created.time() >= datetime.strptime("15:00", "%H:%M").time(), "R2 daily plan must be created after mainland close")
    contract = _read_object(operation_contract_path, "R2 operation contract")
    validate_dynamic_contract(contract, project_root=project_root)
    first = _date(str(contract.get("first_eligible_plan_session")), "R2 first plan session")
    _require(plan_session >= first, "R2 plan would backfill before freeze")
    result = _read_object(frozen_result_path, "frozen G6 result")
    identity = contract["strategy_identity"]
    _require(isinstance(identity, Mapping), "R2 identity missing")
    binding = identity["frozen_result"]
    _require(isinstance(binding, Mapping), "R2 frozen result binding missing")
    _require(_file_hash(frozen_result_path) == binding["sha256"], "R2 frozen result hash mismatch")
    input_manifest = _read_object(input_manifest_path, "R2 input manifest")
    input_root, status_manifest_path, closed_dates = _validate_input_manifest(
        input_manifest,
        created=created,
    )
    expected_plan = next_trading_session(cutoff.date().isoformat(), closed_dates=closed_dates)
    _require(plan_session == expected_plan, "R2 plan session is not the next official SSE session")
    inputs = _inputs(input_root)
    policy = standard_g6_1_policy()
    levels, names = load_total_return_levels(inputs, policy)  # type: ignore[arg-type]
    valuations = load_valuation_histories(inputs, policy)  # type: ignore[arg-type]
    common = sorted(
        item
        for item in set.intersection(*(set(series) for series in levels.values()))
        if inputs.start_session <= item <= cutoff.date().isoformat()
    )
    _require(bool(common), "R2 has no completed common NAV sessions by cutoff")
    month_ends = _month_ends(common)
    latest_common = common[-1]
    seed_decision_session, seed_target = _seed_state(result)
    previous: dict[str, object] | None = None
    if previous_record_path is not None:
        previous = _verified_previous(previous_record_path)
        _require(str(previous.get("plan_for_session")) < plan_session, "R2 plans must advance")
        prior_hash = str(previous["record_hash"])
        ordinal = int(previous["record_ordinal"]) + 1
        prior_state = previous.get("model_state")
        _require(isinstance(prior_state, Mapping), "previous R2 model state missing")
        last_decision = str(prior_state.get("last_formal_decision_session"))
        raw_prior_target = prior_state.get("desired_target_weights")
        _require(isinstance(raw_prior_target, Mapping), "previous desired target missing")
        prior_target = {key: _decimal(raw_prior_target[key], "previous target") for key in SLEEVE_ORDER}
    else:
        prior_hash = GENESIS_HASH
        ordinal = 1
        last_decision = seed_decision_session
        prior_target = seed_target
    formal = _formal_decisions(
        common_sessions=common,
        month_ends=month_ends,
        levels=levels,
        valuations=valuations,
        after_session=last_decision,
    )
    latest_formal = formal[-1] if formal else None
    due_session: str | None = None
    effective_decision_session = last_decision
    target = dict(prior_target)
    trigger = ("BETWEEN_QUARTER_ENDS_HOLD_NO_ORDER",)
    changes = {key: ZERO for key in SLEEVE_ORDER}
    timing_note = "no-new-formal-decision"
    if latest_formal is not None:
        decision = latest_formal["decision"]
        _require(isinstance(decision, Mapping), "R2 formal decision invalid")
        effective_decision_session = str(decision["decision_session"])
        raw_target = decision["target_weights"]
        _require(isinstance(raw_target, list), "R2 formal target invalid")
        target = {str(row[0]): _decimal(row[1], "R2 formal target") for row in raw_target}
        original_due = latest_formal.get("original_two_common_session_due")
        if original_due is None:
            first_due = next_trading_session(effective_decision_session, closed_dates=closed_dates)
            original_due = next_trading_session(first_due, closed_dates=closed_dates)
        due_session = max(str(original_due), plan_session)
        timing_note = (
            "operational-due-delayed-to-first-plan-after-durable-evidence"
            if due_session != original_due
            else "two-common-session-due-observable"
        )
        if plan_session == due_session:
            changes = {key: target[key] - prior_target[key] for key in SLEEVE_ORDER}
            raw_reasons = decision.get("reason_codes")
            trigger = tuple(str(item) for item in raw_reasons) if isinstance(raw_reasons, list) else ("FORMAL_QUARTERLY_DECISION",)
    preview_signal, preview_decision = _signal_preview(
        plan_session=plan_session,
        latest_common=latest_common,
        month_ends=month_ends,
        levels=levels,
        valuations=valuations,
        cutoff_session=cutoff.date().isoformat(),
    )
    statuses = _status_rows(_read_object(status_manifest_path, "R2 status manifest"))
    status_latest = _status_capture_latest(statuses)
    if status_latest is not None:
        _require(_aware_time(status_latest, "R2 status capture") <= created, "R2 status captured after plan creation")
    rows: list[dict[str, object]] = []
    initialization_blockers: list[str] = []
    for sleeve in SLEEVE_ORDER:
        code, default_name, channel = FUND_IDENTITIES[sleeve]
        name = names.get(sleeve, default_name)
        change = changes[sleeve]
        direction = "BUY" if change > ZERO else "SELL" if change < ZERO else "HOLD"
        reasons = trigger if direction != "HOLD" else ("BETWEEN_QUARTER_ENDS_HOLD_NO_ORDER",)
        status_row = statuses.get(code)
        text = _owner_text(status_row)
        cap = _daily_cap(text)
        purchase = _purchase_status(code, text)
        redemption = _redemption_status(code, text)
        requested_amount = abs(change) * REFERENCE_NAV
        gate, gate_reasons, executable = _execution_gate(
            code=code,
            direction=direction,
            requested_amount=requested_amount,
            purchase_status=purchase,
            redemption_status=redemption,
            daily_cap=cap,
        )
        target_amount = target[sleeve] * REFERENCE_NAV
        if target[sleeve] > ZERO:
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
        rows.append(
            {
                "sleeve_id": sleeve,
                "fund_code": code,
                "fund_name": name,
                "channel": channel,
                "direction": direction,
                "target_weight_decimal": format(target[sleeve], "f"),
                "reference_target_amount_CNY_per_100k": _money(target_amount),
                "requested_weight_change_decimal": format(change, "f"),
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
    generator_path = Path(__file__).resolve()
    cli_path = project_root / "src/cn_fund_strategy/interfaces/g6_dynamic_forward_cli.py"
    generator_hash = _file_hash(generator_path)
    cli_hash = _file_hash(cli_path)
    if previous is not None:
        prior_snapshot = previous.get("input_snapshot")
        _require(isinstance(prior_snapshot, Mapping), "previous R2 input snapshot missing")
        _require(
            prior_snapshot.get("dynamic_generator_code_sha256") == generator_hash
            and prior_snapshot.get("dynamic_cli_code_sha256") == cli_hash,
            "R2 implementation changed; start a new lineage",
        )
    candidate = result.get("candidate")
    _require(isinstance(candidate, Mapping), "frozen candidate missing")
    record: dict[str, object] = {
        "schema_version": R2_SCHEMA,
        "operation_id": contract["operation_id"],
        "record_type": "post-close-next-session-research-plan",
        "quality_status": "prospective-input-bound-not-simulation-promotion-evidence",
        "record_ordinal": ordinal,
        "prior_record_hash": prior_hash,
        "supersedes_r1_record_hash": (
            "7a563fc1b5b0bad7df170ae1c03247425f7530edccf3f17a18e3039bf16187b2"
            if previous is None
            else None
        ),
        "created_at": created.isoformat(timespec="seconds"),
        "information_cutoff_at": cutoff.isoformat(timespec="seconds"),
        "plan_for_session": plan_session,
        "strategy": {
            "canonical_name": candidate["canonical_name"],
            "technical_id": candidate["technical_id"],
            "candidate_status": candidate["status"],
            "simulation_approved": False,
            "safe_for_account_order_placement": False,
            "research_mode": "dynamic-forward-shadow",
        },
        "signal_snapshot": {
            "latest_completed_common_NAV_session": latest_common,
            "common_session_count": len(common),
            "last_preexisting_formal_decision_session": last_decision,
            "new_formal_decision": latest_formal,
            "formal_execution_due_session": due_session,
            "operational_timing_note": timing_note,
            "current_preview_signal": preview_signal,
            "current_preview_decision": preview_decision,
            "history_qualification": "retrospective-contaminated; newly captured tail prospective-only",
        },
        "model_state": {
            "last_formal_decision_session": effective_decision_session,
            "desired_target_weights": {key: format(target[key], "f") for key in SLEEVE_ORDER},
            "actual_account_weights": "not-accessed-and-unknown",
            "unreconciled_model_vs_account": True,
        },
        "order_plan": rows,
        "new_portfolio_initialization": {
            "status": "blocked" if initialization_blockers else "conditional-current-gates-pass-recheck-required",
            "blockers": sorted(initialization_blockers),
            "unfilled_destination": "BOND-or-cash-defense",
        },
        "input_snapshot": {
            "operation_contract_path": str(operation_contract_path.resolve()),
            "operation_contract_sha256": _file_hash(operation_contract_path),
            "frozen_result_path": str(frozen_result_path.resolve()),
            "frozen_result_sha256": _file_hash(frozen_result_path),
            "dynamic_input_manifest_path": str(input_manifest_path.resolve()),
            "dynamic_input_manifest_sha256": _file_hash(input_manifest_path),
            "current_status_manifest_path": str(status_manifest_path),
            "current_status_manifest_sha256": _file_hash(status_manifest_path),
            "current_status_latest_capture_at": status_latest,
            "dynamic_generator_code_path": str(generator_path),
            "dynamic_generator_code_sha256": generator_hash,
            "dynamic_cli_code_path": str(cli_path.resolve()),
            "dynamic_cli_code_sha256": cli_hash,
        },
        "reconciliation": {
            "expected_model_orders": sum(row["direction"] != "HOLD" for row in rows),
            "submitted_orders": 0,
            "fills": 0,
            "account_positions_cash_lots_fees_and_NAV_reconciled": False,
            "complete_forward_session_count_increment": 0,
        },
        "authority": {
            "research_and_simulation_only": True,
            "clean_historical_or_OOS": False,
            "simulation_promotion_evidence": False,
            "account_or_trading_authority": False,
        },
    }
    record["decision_id"] = _payload_hash(
        {
            "formal_decision": latest_formal,
            "target": record["model_state"],
            "actions": [
                {
                    "fund_code": row["fund_code"],
                    "direction": row["direction"],
                    "requested_weight_change_decimal": row["requested_weight_change_decimal"],
                }
                for row in rows
            ],
        }
    )
    record["plan_id"] = _payload_hash(
        {
            "decision_id": record["decision_id"],
            "plan_for_session": plan_session,
            "input_manifest_sha256": record["input_snapshot"]["dynamic_input_manifest_sha256"],  # type: ignore[index]
        }
    )
    record["record_hash"] = _payload_hash(record)
    validate_g6_dynamic_plan(record)
    return record


def validate_g6_dynamic_plan(record: Mapping[str, object]) -> None:
    _require(record.get("schema_version") == R2_SCHEMA, "R2 plan schema changed")
    _require(
        record.get("record_type") == "post-close-next-session-research-plan",
        "R2 record type changed",
    )
    expected = record.get("record_hash")
    _require(isinstance(expected, str) and len(expected) == 64, "R2 record hash invalid")
    unhashed = dict(record)
    unhashed.pop("record_hash", None)
    _require(_payload_hash(unhashed) == expected, "R2 record hash mismatch")
    strategy = record.get("strategy")
    _require(isinstance(strategy, Mapping), "R2 strategy missing")
    _require(strategy.get("simulation_approved") is False, "R2 simulation authority changed")
    _require(strategy.get("safe_for_account_order_placement") is False, "R2 trading authority changed")
    rows = record.get("order_plan")
    _require(isinstance(rows, list) and len(rows) == len(SLEEVE_ORDER), "R2 plan incomplete")
    _require(
        tuple(str(row["sleeve_id"]) for row in rows if isinstance(row, Mapping)) == SLEEVE_ORDER,
        "R2 sleeve order changed",
    )
    for row in rows:
        _require(isinstance(row, Mapping), "R2 action row invalid")
        direction = row.get("direction")
        _require(direction in {"BUY", "SELL", "HOLD"}, "R2 direction invalid")
        if direction == "HOLD":
            _require(
                row.get("requested_amount_CNY_per_100k") == "0.00"
                and row.get("execution_gate_status") == "hold-no-economic-order",
                "R2 HOLD carries an economic order",
            )
    authority = record.get("authority")
    _require(isinstance(authority, Mapping), "R2 authority missing")
    _require(authority.get("account_or_trading_authority") is False, "R2 trading authority opened")


def write_g6_dynamic_plan(path: Path, record: Mapping[str, object]) -> None:
    validate_g6_dynamic_plan(record)
    payload = (json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    write_create_or_identical(path, payload)


def render_g6_dynamic_brief(record: Mapping[str, object]) -> str:
    validate_g6_dynamic_plan(record)
    rows = record["order_plan"]
    assert isinstance(rows, list)
    strategy = record["strategy"]
    signal = record["signal_snapshot"]
    initialization = record["new_portfolio_initialization"]
    assert isinstance(strategy, Mapping) and isinstance(signal, Mapping) and isinstance(initialization, Mapping)
    lines = [
        "# G6-Standard-1 次一交易日动态研究清单",
        "",
        f"生成时间：{record['created_at']}  ",
        f"信息截止：{record['information_cutoff_at']}  ",
        f"计划交易日：{record['plan_for_session']}  ",
        "性质：只读研究型 shadow；不读取账户，不提交订单",
        "",
        "## 今日结论",
        "",
    ]
    active = [row for row in rows if row["direction"] != "HOLD"]
    if active:
        lines.append("模型出现季度调仓动作；仅通过当日执行门的金额才具备研究层面的可执行资格。")
    else:
        lines.append("全部持有：本次没有新的正式季度调仓动作，不买、不卖。")
    lines.extend(
        [
            "",
            "## 信号状态",
            "",
            f"最新完整共同净值日：{signal['latest_completed_common_NAV_session']}；当前候选状态：{strategy['candidate_status']}。",
            f"正式调仓日：{signal['formal_execution_due_session'] or '无'}；时序说明：{signal['operational_timing_note']}。",
            "",
            "## 基金、目标仓位与动作（每 10 万元模型组合）",
            "",
            "| 代码 | 名称 | 动作 | 目标仓位 | 目标金额 | 本次金额 | 申购状态 | 限额 | 执行门 |",
            "|---|---|---|---:|---:|---:|---|---:|---|",
        ]
    )
    for row in rows:
        cap = f"{row['purchase_cap_CNY']} 元" if row["purchase_cap_CNY"] is not None else "—"
        lines.append(
            f"| {row['fund_code']} | {row['fund_name']} | {row['direction']} | "
            f"{Decimal(str(row['target_weight_decimal'])):.2%} | "
            f"{row['reference_target_amount_CNY_per_100k']} 元 | "
            f"{row['requested_amount_CNY_per_100k']} 元 | {row['purchase_status']} | "
            f"{cap} | {row['execution_gate_status']} |"
        )
    lines.extend(
        [
            "",
            f"新组合一次性初始化：{initialization['status']}；受阻资金留在债券或现金防守仓。",
            "",
            "## 重要边界",
            "",
            "目标金额是每 10 万元模型组合的参考值，不是个人账户指令。没有真实持仓、成本批次和现金数据时，不能生成个人化份额；场内 ETF 在下单时仍须复核报价、价差、深度、溢价和交易状态；QDII 必须复核同份额同渠道当日限购，禁止拆单规避。",
            "",
            "历史回测仍是污染形成证据，且该候选滚动 3 年稳定性门未通过；本清单不改变候选被拒绝、未获仿真批准的状态。",
            "",
            f"记录哈希：`{record['record_hash']}`",
            "",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "build_g6_dynamic_plan",
    "capture_dynamic_input_batch",
    "render_g6_dynamic_brief",
    "validate_dynamic_contract",
    "validate_g6_dynamic_plan",
    "write_g6_dynamic_plan",
]
