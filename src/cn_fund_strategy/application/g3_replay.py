from __future__ import annotations

from bisect import bisect_left
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Mapping, Sequence

from cn_fund_strategy.application.g1_replay import (
    ALL_SLEEVES,
    G1ReplayInputs,
    G1ReplayScenario,
    TRADING_SESSIONS_PER_YEAR,
    load_total_return_levels,
    load_valuation_histories,
    performance_metrics,
    with_g1_decimal_context,
)
from cn_fund_strategy.domain.g1_global_valuation_batch import (
    BOND_SLEEVE,
    EQUITY_SLEEVES,
    GOLD_SLEEVE,
    valuation_score,
)
from cn_fund_strategy.domain.g3_dual_momentum_valuation_batch import (
    DOMESTIC_MODULE,
    EXTREME_LOSS_FACTORS,
    MODULE_SLEEVES,
    OVERSEAS_MODULE,
    G3ModuleDecision,
    G3ModuleSignal,
    G3ModuleState,
    G3Policy,
    apply_filled_module_decision,
    apply_weight_changes,
    decide_module,
    decision_weight_changes,
    extreme_static_loss,
    initial_module_states,
    initial_weights,
    standard_g3_policy,
)


ZERO = Decimal("0")
ONE = Decimal("1")
BASE_LOSS_FACTORS = {
    DOMESTIC_MODULE: Decimal("0.45"),
    OVERSEAS_MODULE: Decimal("0.505"),
    GOLD_SLEEVE: Decimal("0.20"),
    BOND_SLEEVE: Decimal("0.05"),
}


class G3ReplayError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _ScheduledModuleFill:
    signals: tuple[G3ModuleSignal, ...]
    decisions: tuple[G3ModuleDecision, ...]


@dataclass(frozen=True, slots=True)
class _ScheduledGuard:
    concentration_sleeves: tuple[str, ...]
    static_stress: bool


@dataclass(frozen=True, slots=True)
class G3ReplayResult:
    canonical_name: str
    technical_id: str
    scenario: G1ReplayScenario
    start_session: str
    end_session: str
    common_sessions: tuple[str, ...]
    navs: tuple[Decimal, ...]
    returns: tuple[Decimal, ...]
    ending_weights: Mapping[str, Decimal]
    ending_states: Mapping[str, G3ModuleState]
    trades: tuple[Mapping[str, object], ...]
    decision_log: tuple[Mapping[str, object], ...]
    daily_rows: tuple[Mapping[str, object], ...]
    maximum_weights: Mapping[str, Decimal]
    maximum_equity_weight: Decimal
    minimum_bond_weight: Decimal
    maximum_extreme_static_loss: Decimal
    maximum_base_static_loss: Decimal
    maximum_extreme_path_overlay_drawdown: Decimal
    maximum_base_path_overlay_drawdown: Decimal

    @property
    def sessions(self) -> int:
        return len(self.common_sessions)

    def metrics(self) -> dict[str, str | int | None]:
        return performance_metrics(self.navs, self.returns, self.common_sessions)

    def as_record(self) -> dict[str, object]:
        turnover = sum((Decimal(str(item["turnover_decimal"])) for item in self.trades), ZERO)
        return {
            "scenario": {
                "scenario_id": self.scenario.scenario_id,
                "execution_lag_common_sessions": self.scenario.execution_lag_common_sessions,
                "one_way_cost_decimal": _text(self.scenario.one_way_cost_decimal),
            },
            "metrics": self.metrics(),
            "trade_count": len(self.trades),
            "annual_l1_turnover_decimal": _text(
                turnover * TRADING_SESSIONS_PER_YEAR / Decimal(max(1, self.sessions - 1))
            ),
            "ending_weights": _decimal_mapping(self.ending_weights),
            "ending_states": {
                key: _json_ready(asdict(value)) for key, value in self.ending_states.items()
            },
            "maximum_weights": _decimal_mapping(self.maximum_weights),
            "maximum_equity_weight_decimal": _text(self.maximum_equity_weight),
            "minimum_bond_weight_decimal": _text(self.minimum_bond_weight),
            "maximum_base_static_loss_decimal": _text(self.maximum_base_static_loss),
            "maximum_extreme_static_loss_decimal": _text(self.maximum_extreme_static_loss),
            "maximum_base_path_overlay_drawdown_decimal": _text(
                self.maximum_base_path_overlay_drawdown
            ),
            "maximum_extreme_path_overlay_drawdown_decimal": _text(
                self.maximum_extreme_path_overlay_drawdown
            ),
            "rolling_3y": rolling_window_metrics(
                self.navs,
                self.returns,
                self.common_sessions,
                window_sessions=756,
                step_sessions=63,
            ),
            "rolling_5y": rolling_window_metrics(
                self.navs,
                self.returns,
                self.common_sessions,
                window_sessions=1260,
                step_sessions=63,
            ),
            "trades": [_json_ready(item) for item in self.trades],
            "latest_decisions": [_json_ready(item) for item in self.decision_log[-8:]],
        }

    def daily_ledger_record(self) -> dict[str, object]:
        return {
            "schema_version": "g3-standard-daily-accounting-ledger-v1",
            "canonical_name": self.canonical_name,
            "candidate_id": self.technical_id,
            "scenario_id": self.scenario.scenario_id,
            "row_count": len(self.daily_rows),
            "rows": [_json_ready(item) for item in self.daily_rows],
        }


@with_g1_decimal_context
def run_g3_replay(
    inputs: G1ReplayInputs,
    scenario: G1ReplayScenario,
    *,
    policy: G3Policy | None = None,
) -> G3ReplayResult:
    active_policy = policy or standard_g3_policy()
    _validate_scenario(scenario)
    levels, _ = load_total_return_levels(inputs, active_policy)  # type: ignore[arg-type]
    valuations = load_valuation_histories(inputs, active_policy)  # type: ignore[arg-type]
    all_common_sessions = sorted(set.intersection(*(set(item) for item in levels.values())))
    common_sessions = tuple(item for item in all_common_sessions if item >= inputs.start_session)
    if len(common_sessions) <= scenario.execution_lag_common_sessions + 1:
        raise G3ReplayError("not enough common sessions")
    all_month_ends = _month_ends(all_common_sessions)
    month_ends = tuple(item for item in all_month_ends if item >= inputs.start_session)
    month_end_set = set(month_ends)
    all_month_position = {item: ordinal for ordinal, item in enumerate(all_month_ends)}
    session_position = {item: ordinal for ordinal, item in enumerate(common_sessions)}

    weights = {item: ZERO for item in ALL_SLEEVES}
    weights[BOND_SLEEVE] = ONE
    states = initial_module_states(active_policy)
    nav = ONE
    navs: list[Decimal] = []
    returns: list[Decimal] = []
    trades: list[Mapping[str, object]] = []
    decision_log: list[Mapping[str, object]] = []
    daily_rows: list[Mapping[str, object]] = []
    maximum_weights = {item: ZERO for item in ALL_SLEEVES}
    maximum_equity_weight = ZERO
    minimum_bond_weight = ONE
    maximum_extreme_loss = ZERO
    maximum_base_loss = ZERO
    maximum_extreme_overlay = ZERO
    maximum_base_overlay = ZERO
    scheduled_fills: dict[str, list[_ScheduledModuleFill]] = {}
    scheduled_guards: dict[str, _ScheduledGuard] = {}
    initial_session = common_sessions[scenario.execution_lag_common_sessions]
    previous_session: str | None = None
    high_water = ONE

    for ordinal, current_session in enumerate(common_sessions):
        nav_before_session = nav
        if previous_session is not None:
            gross = sum(
                (
                    weights[sleeve_id]
                    * levels[sleeve_id][current_session]
                    / levels[sleeve_id][previous_session]
                    for sleeve_id in ALL_SLEEVES
                ),
                ZERO,
            )
            if gross <= ZERO:
                raise G3ReplayError("non-positive portfolio gross return")
            weights = {
                sleeve_id: weights[sleeve_id]
                * levels[sleeve_id][current_session]
                / levels[sleeve_id][previous_session]
                / gross
                for sleeve_id in ALL_SLEEVES
            }
            nav *= gross

        preaction_extreme_loss = extreme_static_loss(weights)
        preaction_base_loss = static_loss(weights, BASE_LOSS_FACTORS)
        maximum_extreme_loss = max(maximum_extreme_loss, preaction_extreme_loss)
        maximum_base_loss = max(maximum_base_loss, preaction_base_loss)

        if current_session == initial_session:
            nav, weights, trade = _execute_target(
                execution_session=current_session,
                nav=nav,
                weights=weights,
                target=initial_weights(active_policy),
                cost=scenario.one_way_cost_decimal,
                decision_session=common_sessions[0],
                trade_kind="INITIAL_STRATEGIC_ALLOCATION",
                reason_codes=("INITIAL_CORE_GOLD_AND_BOND_ALLOCATION",),
            )
            trades.append(trade)

        for scheduled in scheduled_fills.get(current_session, ()):
            changes = decision_weight_changes(active_policy, scheduled.decisions)
            target = apply_weight_changes(weights, changes)
            nav, weights, trade = _execute_target(
                execution_session=current_session,
                nav=nav,
                weights=weights,
                target=target,
                cost=scenario.one_way_cost_decimal,
                decision_session=scheduled.decisions[0].decision_session,
                trade_kind="MONTHLY_MODULE_BATCH_TRANSITION",
                reason_codes=tuple(
                    code for decision in scheduled.decisions for code in decision.reason_codes
                ),
            )
            trades.append(trade)
            for decision in scheduled.decisions:
                states[decision.module_id] = apply_filled_module_decision(
                    active_policy,
                    states[decision.module_id],
                    decision,
                )

        guard = scheduled_guards.get(current_session)
        if guard is not None:
            concentration_target = concentration_guard_target(
                active_policy,
                weights,
                guard.concentration_sleeves,
            )
            if concentration_target != weights:
                nav, weights, trade = _execute_target(
                    execution_session=current_session,
                    nav=nav,
                    weights=weights,
                    target=concentration_target,
                    cost=scenario.one_way_cost_decimal,
                    decision_session="scheduled-month-end-concentration-guard",
                    trade_kind="CONCENTRATION_GUARD",
                    reason_codes=("TRIM_PREIDENTIFIED_SLEEVE_TO_18PCT",),
                )
                trades.append(trade)
            if guard.static_stress:
                stress_target = static_stress_guard_target(active_policy, weights)
                if stress_target != weights:
                    nav, weights, trade = _execute_target(
                        execution_session=current_session,
                        nav=nav,
                        weights=weights,
                        target=stress_target,
                        cost=scenario.one_way_cost_decimal,
                        decision_session="scheduled-month-end-static-stress-guard",
                        trade_kind="STATIC_STRESS_GUARD",
                        reason_codes=("TACTICAL_FIRST_TRIM_EXTREME_STATIC_LOSS_TO_43_8PCT",),
                    )
                    trades.append(trade)

        if current_session in month_end_set:
            signals = tuple(
                _historical_module_signal(
                    active_policy,
                    module_id,
                    current_session,
                    all_month_position[current_session],
                    all_month_ends,
                    levels,
                    valuations,
                )
                for module_id in (DOMESTIC_MODULE, OVERSEAS_MODULE)
            )
            decisions = tuple(
                decide_module(
                    active_policy,
                    states[signal.module_id],
                    signal,
                    historical_missing_valuation_prior_allowed=True,
                )
                for signal in signals
            )
            decision_log.extend(
                _decision_record(signal, decision)
                for signal, decision in zip(signals, decisions, strict=True)
            )
            actionable = tuple(item for item in decisions if item.is_actionable)
            if actionable and ordinal + scenario.execution_lag_common_sessions < len(common_sessions):
                execution_session = common_sessions[
                    ordinal + scenario.execution_lag_common_sessions
                ]
                signal_by_module = {item.module_id: item for item in signals}
                scheduled_fills.setdefault(execution_session, []).append(
                    _ScheduledModuleFill(
                        signals=tuple(signal_by_module[item.module_id] for item in actionable),
                        decisions=actionable,
                    )
                )

            if ordinal + scenario.execution_lag_common_sessions < len(common_sessions):
                concentration_sleeves = tuple(
                    sleeve_id
                    for sleeve_id in EQUITY_SLEEVES
                    if weights[sleeve_id] > active_policy.concentration_cap
                )
                stress_active = (
                    extreme_static_loss(weights) > active_policy.pretrade_static_stress_guard
                )
                if concentration_sleeves or stress_active:
                    execution_session = common_sessions[
                        ordinal + scenario.execution_lag_common_sessions
                    ]
                    existing = scheduled_guards.get(
                        execution_session,
                        _ScheduledGuard((), False),
                    )
                    scheduled_guards[execution_session] = _ScheduledGuard(
                        concentration_sleeves=tuple(
                            dict.fromkeys(existing.concentration_sleeves + concentration_sleeves)
                        ),
                        static_stress=existing.static_stress or stress_active,
                    )

        postaction_extreme_loss = extreme_static_loss(weights)
        postaction_base_loss = static_loss(weights, BASE_LOSS_FACTORS)
        maximum_extreme_loss = max(maximum_extreme_loss, postaction_extreme_loss)
        maximum_base_loss = max(maximum_base_loss, postaction_base_loss)
        high_water = max(high_water, nav)
        maximum_extreme_overlay = max(
            maximum_extreme_overlay,
            ONE - nav * (ONE - postaction_extreme_loss) / high_water,
        )
        maximum_base_overlay = max(
            maximum_base_overlay,
            ONE - nav * (ONE - postaction_base_loss) / high_water,
        )
        equity_weight = sum((weights[item] for item in EQUITY_SLEEVES), ZERO)
        maximum_equity_weight = max(maximum_equity_weight, equity_weight)
        minimum_bond_weight = min(minimum_bond_weight, weights[BOND_SLEEVE])
        for sleeve_id in ALL_SLEEVES:
            maximum_weights[sleeve_id] = max(maximum_weights[sleeve_id], weights[sleeve_id])

        session_return = nav / nav_before_session - ONE if ordinal else ZERO
        navs.append(nav)
        returns.append(session_return)
        daily_rows.append(
            {
                "session": current_session,
                "nav_decimal": nav,
                "return_decimal": session_return,
                "weights": dict(weights),
                "equity_weight_decimal": equity_weight,
                "preaction_extreme_static_loss_decimal": preaction_extreme_loss,
                "postaction_extreme_static_loss_decimal": postaction_extreme_loss,
                "postaction_base_static_loss_decimal": postaction_base_loss,
            }
        )
        previous_session = current_session

    return G3ReplayResult(
        canonical_name=active_policy.canonical_name,
        technical_id=active_policy.technical_id,
        scenario=scenario,
        start_session=common_sessions[0],
        end_session=common_sessions[-1],
        common_sessions=common_sessions,
        navs=tuple(navs),
        returns=tuple(returns),
        ending_weights=weights,
        ending_states=states,
        trades=tuple(trades),
        decision_log=tuple(decision_log),
        daily_rows=tuple(daily_rows),
        maximum_weights=maximum_weights,
        maximum_equity_weight=maximum_equity_weight,
        minimum_bond_weight=minimum_bond_weight,
        maximum_extreme_static_loss=maximum_extreme_loss,
        maximum_base_static_loss=maximum_base_loss,
        maximum_extreme_path_overlay_drawdown=maximum_extreme_overlay,
        maximum_base_path_overlay_drawdown=maximum_base_overlay,
    )


@with_g1_decimal_context
def build_current_g3_decisions(
    inputs: G1ReplayInputs,
    replay: G3ReplayResult,
    *,
    decision_session: str,
    valuation_cutoff_session: str,
    policy: G3Policy | None = None,
) -> list[dict[str, object]]:
    active_policy = policy or standard_g3_policy()
    levels, names = load_total_return_levels(inputs, active_policy)  # type: ignore[arg-type]
    valuations = load_valuation_histories(inputs, active_policy)  # type: ignore[arg-type]
    available_common = sorted(
        item
        for item in set.intersection(*(set(value) for value in levels.values()))
        if item <= decision_session
    )
    month_ends = _month_ends(available_common)
    if len(month_ends) < 13:
        raise G3ReplayError("current decision lacks twelve complete common months")
    signal_session = month_ends[-1]
    signals = tuple(
        _current_module_signal(
            active_policy,
            module_id,
            decision_session,
            signal_session,
            month_ends,
            levels,
            valuations,
            valuation_cutoff_session=valuation_cutoff_session,
        )
        for module_id in (DOMESTIC_MODULE, OVERSEAS_MODULE)
    )
    decisions = tuple(
        decide_module(
            active_policy,
            replay.ending_states[signal.module_id],
            signal,
            historical_missing_valuation_prior_allowed=False,
        )
        for signal in signals
    )
    rows: list[dict[str, object]] = []
    for signal, decision in zip(signals, decisions, strict=True):
        momentum = signal.momentum_mapping()
        valuations_by_sleeve = signal.valuation_mapping()
        valuation_sessions = dict(signal.valuation_sessions)
        changes = decision.change_mapping()
        targets = decision.target_mapping()
        current_batches = replay.ending_states[signal.module_id].as_mapping()
        for sleeve_id in active_policy.module_sleeves(signal.module_id):
            change = changes[sleeve_id]
            rows.append(
                {
                    "module_id": signal.module_id,
                    "sleeve_id": sleeve_id,
                    "fund_code": active_policy.sleeve(sleeve_id).fund_code,
                    "fund_name": names[sleeve_id],
                    "decision_session": decision_session,
                    "signal_common_session": signal_session,
                    "fund_total_return_as_of": signal_session,
                    "valuation_as_of": valuation_sessions[sleeve_id],
                    "valuation_score_decimal": valuations_by_sleeve[sleeve_id],
                    "momentum_12m_decimal": momentum[sleeve_id],
                    "bond_momentum_12m_decimal": signal.bond_momentum_12m,
                    "relative_winner": decision.relative_winner,
                    "current_tactical_batches": current_batches[sleeve_id],
                    "target_tactical_batches": targets[sleeve_id],
                    "action_batches": change,
                    "requested_weight_change_decimal": Decimal(change) * Decimal("0.03"),
                    "marked_weight_before_new_order_decimal": replay.ending_weights[sleeve_id],
                    "target_weight_if_filled_decimal": (
                        replay.ending_weights[sleeve_id] + Decimal(change) * Decimal("0.03")
                    ),
                    "reason_codes": decision.reason_codes,
                }
            )
    return rows


@with_g1_decimal_context
def run_g3_fixed_event_settlement_stress(
    inputs: G1ReplayInputs,
    base_replay: G3ReplayResult,
    *,
    redemption_settlement_common_sessions: int,
    policy: G3Policy | None = None,
) -> dict[str, object]:
    """Hold frozen equity-sale proceeds in zero-return cash before bond release.

    Signals, execution dates, target deltas and transaction costs stay exactly
    as recorded by the canonical base replay.  This sensitivity changes only
    the availability date of equity-sale proceeds; it does not rerun signals.
    """

    if not 1 <= redemption_settlement_common_sessions <= 20:
        raise G3ReplayError("redemption settlement sessions must be in [1, 20]")
    active_policy = policy or standard_g3_policy()
    levels, _ = load_total_return_levels(inputs, active_policy)  # type: ignore[arg-type]
    sessions = base_replay.common_sessions
    trades_by_session: dict[str, list[Mapping[str, object]]] = {}
    for trade in base_replay.trades:
        trades_by_session.setdefault(str(trade["execution_session"]), []).append(trade)

    cash_sleeve = "SETTLEMENT_CASH"
    weights = {item: ZERO for item in ALL_SLEEVES}
    weights[BOND_SLEEVE] = ONE
    weights[cash_sleeve] = ZERO
    nav = ONE
    navs: list[Decimal] = []
    returns: list[Decimal] = []
    previous: str | None = None
    releases: dict[str, list[Decimal]] = {}
    settlement_events: list[dict[str, object]] = []

    for ordinal, current in enumerate(sessions):
        nav_before = nav
        if previous is not None:
            gross = weights[cash_sleeve] + sum(
                (
                    weights[key] * levels[key][current] / levels[key][previous]
                    for key in ALL_SLEEVES
                ),
                ZERO,
            )
            weights = {
                key: weights[key] * levels[key][current] / levels[key][previous] / gross
                for key in ALL_SLEEVES
            } | {cash_sleeve: weights[cash_sleeve] / gross}
            nav *= gross

        for cash_amount in releases.get(current, ()):
            release_weight = min(weights[cash_sleeve], cash_amount / nav)
            weights[cash_sleeve] -= release_weight
            weights[BOND_SLEEVE] += release_weight
            settlement_events.append(
                {
                    "session": current,
                    "event": "settlement-cash-released-to-bond",
                    "cash_amount_nav_units_decimal": cash_amount,
                    "released_weight_decimal": release_weight,
                }
            )

        for frozen_trade in trades_by_session.get(current, ()):
            pretrade = frozen_trade["pretrade_weights"]
            posttrade = frozen_trade["posttrade_weights"]
            if frozen_trade["trade_kind"] == "INITIAL_STRATEGIC_ALLOCATION":
                target = dict(posttrade)
                target[cash_sleeve] = ZERO
            else:
                target = dict(weights)
                cash_before = target[cash_sleeve]
                for sleeve_id in EQUITY_SLEEVES:
                    fixed_change = Decimal(str(posttrade[sleeve_id])) - Decimal(
                        str(pretrade[sleeve_id])
                    )
                    if fixed_change < ZERO:
                        target[sleeve_id] += fixed_change
                        target[cash_sleeve] -= fixed_change
                    elif fixed_change > ZERO:
                        target[sleeve_id] += fixed_change
                        target[BOND_SLEEVE] -= fixed_change
                if target[BOND_SLEEVE] < ZERO:
                    raise G3ReplayError("settlement stress buy exceeds available bond funding")
                new_cash_weight = target[cash_sleeve] - cash_before
                if new_cash_weight > ZERO:
                    release_position = ordinal + redemption_settlement_common_sessions
                    if release_position < len(sessions):
                        release_session = sessions[release_position]
                        cash_amount = (
                            nav
                            * (
                                ONE
                                - Decimal(str(frozen_trade["turnover_decimal"]))
                                * base_replay.scenario.one_way_cost_decimal
                            )
                            * new_cash_weight
                        )
                        releases.setdefault(release_session, []).append(cash_amount)
                        settlement_events.append(
                            {
                                "session": current,
                                "event": "equity-sale-proceeds-enter-settlement-cash",
                                "release_session": release_session,
                                "cash_amount_nav_units_decimal": cash_amount,
                            }
                        )
            if abs(sum(target.values(), ZERO) - ONE) > Decimal("1e-20"):
                raise G3ReplayError("settlement target weights do not sum to one")
            if any(item < ZERO for item in target.values()):
                raise G3ReplayError("settlement target contains a negative weight")
            turnover = Decimal(str(frozen_trade["turnover_decimal"]))
            nav -= nav * turnover * base_replay.scenario.one_way_cost_decimal
            weights = target

        navs.append(nav)
        returns.append(nav / nav_before - ONE if ordinal else ZERO)
        previous = current

    return {
        "scenario_id": "base-fixed-events-with-zero-return-redemption-settlement-cash",
        "signal-and-fill-sessions": "identical-to-base-replay",
        "redemption_settlement_common_sessions": redemption_settlement_common_sessions,
        "cost_model": "identical-to-base-no-second-charge-on-settlement-release",
        "metrics": performance_metrics(tuple(navs), tuple(returns), sessions),
        "ending_weights": {
            **_decimal_mapping({key: weights[key] for key in ALL_SLEEVES}),
            cash_sleeve: _text(weights[cash_sleeve]),
        },
        "settlement_event_count": len(settlement_events),
        "settlement_events": _json_ready(settlement_events),
        "authority": "fixed-event-settlement-sensitivity-not-a-new-signal-backtest",
    }


@with_g1_decimal_context
def rolling_window_metrics(
    navs: Sequence[Decimal],
    returns: Sequence[Decimal],
    sessions: Sequence[str],
    *,
    window_sessions: int,
    step_sessions: int,
) -> dict[str, object]:
    rows: list[dict[str, str | int | None]] = []
    for start in range(0, len(navs) - window_sessions, step_sessions):
        end = start + window_sessions
        rows.append(
            performance_metrics(
                navs[start : end + 1],
                returns[start : end + 1],
                sessions[start : end + 1],
            )
        )
    cagrs = tuple(Decimal(str(row["net_cagr_decimal"])) for row in rows)
    mdds = tuple(Decimal(str(row["maximum_drawdown_decimal"])) for row in rows)
    return {
        "window_sessions": window_sessions,
        "step_sessions": step_sessions,
        "window_count": len(rows),
        "minimum_CAGR_decimal": _text(min(cagrs)) if cagrs else None,
        "fraction_CAGR_gte_10pct_decimal": (
            _text(Decimal(sum(item >= Decimal("0.10") for item in cagrs)) / Decimal(len(cagrs)))
            if cagrs
            else None
        ),
        "maximum_drawdown_decimal": _text(max(mdds)) if mdds else None,
        "windows": rows,
    }


def concentration_guard_target(
    policy: G3Policy,
    weights: Mapping[str, Decimal],
    guarded_sleeves: Sequence[str],
) -> dict[str, Decimal]:
    target = dict(weights)
    proceeds = ZERO
    for sleeve_id in guarded_sleeves:
        if target[sleeve_id] > policy.concentration_cap:
            proceeds += target[sleeve_id] - policy.concentration_cap
            target[sleeve_id] = policy.concentration_cap
    target[BOND_SLEEVE] += proceeds
    return target


def static_stress_guard_target(
    policy: G3Policy,
    weights: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    target = dict(weights)
    current_loss = extreme_static_loss(target)
    if current_loss <= policy.postguard_static_stress_target:
        return target
    ordered = MODULE_SLEEVES[OVERSEAS_MODULE] + MODULE_SLEEVES[DOMESTIC_MODULE]
    for sleeve_id in ordered:
        if current_loss <= policy.postguard_static_stress_target:
            break
        module_id = policy.sleeve(sleeve_id).module_id
        factor = EXTREME_LOSS_FACTORS[module_id]
        reducible = max(ZERO, target[sleeve_id] - policy.sleeve(sleeve_id).core_weight)
        if not reducible:
            continue
        needed = (current_loss - policy.postguard_static_stress_target) / (
            factor - EXTREME_LOSS_FACTORS[BOND_SLEEVE]
        )
        cut = min(reducible, needed)
        target[sleeve_id] -= cut
        target[BOND_SLEEVE] += cut
        current_loss = extreme_static_loss(target)
    if current_loss > policy.postguard_static_stress_target:
        for sleeve_id in ordered:
            if current_loss <= policy.postguard_static_stress_target:
                break
            factor = EXTREME_LOSS_FACTORS[policy.sleeve(sleeve_id).module_id]
            needed = (current_loss - policy.postguard_static_stress_target) / (
                factor - EXTREME_LOSS_FACTORS[BOND_SLEEVE]
            )
            cut = min(target[sleeve_id], needed)
            target[sleeve_id] -= cut
            target[BOND_SLEEVE] += cut
            current_loss = extreme_static_loss(target)
    if current_loss > policy.postguard_static_stress_target + Decimal("1e-30"):
        raise G3ReplayError("static stress guard could not restore target")
    return target


def static_loss(
    weights: Mapping[str, Decimal],
    factors: Mapping[str, Decimal],
) -> Decimal:
    domestic = sum((weights[item] for item in MODULE_SLEEVES[DOMESTIC_MODULE]), ZERO)
    overseas = sum((weights[item] for item in MODULE_SLEEVES[OVERSEAS_MODULE]), ZERO)
    return (
        domestic * factors[DOMESTIC_MODULE]
        + overseas * factors[OVERSEAS_MODULE]
        + weights[GOLD_SLEEVE] * factors[GOLD_SLEEVE]
        + weights[BOND_SLEEVE] * factors[BOND_SLEEVE]
    )


def _historical_module_signal(
    policy: G3Policy,
    module_id: str,
    decision_session: str,
    month_index: int,
    month_ends: Sequence[str],
    levels: Mapping[str, Mapping[str, Decimal]],
    valuations: Mapping[str, Sequence[tuple[str, Decimal, Decimal]]],
) -> G3ModuleSignal:
    momentum = {
        sleeve_id: (
            levels[sleeve_id][decision_session]
            / levels[sleeve_id][month_ends[month_index - 12]]
            - ONE
            if month_index >= 12
            else None
        )
        for sleeve_id in policy.module_sleeves(module_id)
    }
    bond_momentum = (
        levels[BOND_SLEEVE][decision_session]
        / levels[BOND_SLEEVE][month_ends[month_index - 12]]
        - ONE
        if month_index >= 12
        else None
    )
    scores: dict[str, Decimal | None] = {}
    score_sessions: dict[str, str | None] = {}
    for sleeve_id in policy.module_sleeves(module_id):
        score, score_session = _valuation_before(
            valuations[sleeve_id],
            decision_session=decision_session,
            cutoff_session=None,
        )
        scores[sleeve_id] = score
        score_sessions[sleeve_id] = score_session
    return G3ModuleSignal(
        module_id=module_id,
        decision_session=decision_session,
        momentum_12m=tuple((item, momentum[item]) for item in policy.module_sleeves(module_id)),
        bond_momentum_12m=bond_momentum,
        valuation_scores=tuple((item, scores[item]) for item in policy.module_sleeves(module_id)),
        valuation_sessions=tuple(
            (item, score_sessions[item]) for item in policy.module_sleeves(module_id)
        ),
    )


def _current_module_signal(
    policy: G3Policy,
    module_id: str,
    decision_session: str,
    signal_session: str,
    month_ends: Sequence[str],
    levels: Mapping[str, Mapping[str, Decimal]],
    valuations: Mapping[str, Sequence[tuple[str, Decimal, Decimal]]],
    *,
    valuation_cutoff_session: str,
) -> G3ModuleSignal:
    prior = month_ends[-13]
    momentum = {
        sleeve_id: levels[sleeve_id][signal_session] / levels[sleeve_id][prior] - ONE
        for sleeve_id in policy.module_sleeves(module_id)
    }
    bond_momentum = levels[BOND_SLEEVE][signal_session] / levels[BOND_SLEEVE][prior] - ONE
    scores: dict[str, Decimal | None] = {}
    score_sessions: dict[str, str | None] = {}
    for sleeve_id in policy.module_sleeves(module_id):
        score, score_session = _valuation_before(
            valuations[sleeve_id],
            decision_session=decision_session,
            cutoff_session=valuation_cutoff_session,
        )
        scores[sleeve_id] = score
        score_sessions[sleeve_id] = score_session
    return G3ModuleSignal(
        module_id=module_id,
        decision_session=decision_session,
        momentum_12m=tuple((item, momentum[item]) for item in policy.module_sleeves(module_id)),
        bond_momentum_12m=bond_momentum,
        valuation_scores=tuple((item, scores[item]) for item in policy.module_sleeves(module_id)),
        valuation_sessions=tuple(
            (item, score_sessions[item]) for item in policy.module_sleeves(module_id)
        ),
    )


def _valuation_before(
    history: Sequence[tuple[str, Decimal, Decimal]],
    *,
    decision_session: str,
    cutoff_session: str | None,
) -> tuple[Decimal | None, str | None]:
    dates = [item[0] for item in history]
    strict_position = bisect_left(dates, decision_session) - 1
    if cutoff_session is not None:
        cutoff_position = bisect_left(dates, cutoff_session + "\uffff") - 1
        position = min(strict_position, cutoff_position)
    else:
        position = strict_position
    if position < 12:
        return None, None
    available = history[: position + 1]
    return (
        valuation_score(
            tuple(item[1] for item in available),
            tuple(item[2] for item in available),
        ),
        available[-1][0],
    )


def _decision_record(
    signal: G3ModuleSignal,
    decision: G3ModuleDecision,
) -> dict[str, object]:
    return {"signal": asdict(signal), "decision": asdict(decision)}


def _execute_target(
    *,
    execution_session: str,
    nav: Decimal,
    weights: Mapping[str, Decimal],
    target: Mapping[str, Decimal],
    cost: Decimal,
    decision_session: str,
    trade_kind: str,
    reason_codes: tuple[str, ...],
) -> tuple[Decimal, dict[str, Decimal], Mapping[str, object]]:
    if set(weights) != set(target):
        raise G3ReplayError("target weight identities mismatch")
    if any(value < ZERO for value in target.values()):
        raise G3ReplayError("negative target weight")
    if abs(sum(target.values(), ZERO) - ONE) > Decimal("1e-30"):
        raise G3ReplayError("target weights do not sum to one")
    turnover = sum((abs(target[item] - weights[item]) for item in ALL_SLEEVES), ZERO)
    charge = nav * turnover * cost
    post_cost_nav = nav - charge
    if post_cost_nav <= ZERO:
        raise G3ReplayError("transaction cost exhausted portfolio")
    trade = {
        "trade_kind": trade_kind,
        "decision_session": decision_session,
        "execution_session": execution_session,
        "reason_codes": reason_codes,
        "turnover_decimal": turnover,
        "cost_cashflow_nav_units_decimal": charge,
        "pretrade_weights": dict(weights),
        "posttrade_weights": dict(target),
    }
    return post_cost_nav, dict(target), trade


def _month_ends(sessions: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    prior: tuple[int, int] | None = None
    previous = ""
    for item in sessions:
        parsed = date.fromisoformat(item)
        key = (parsed.year, parsed.month)
        if prior is not None and key != prior:
            result.append(previous)
        prior = key
        previous = item
    if previous:
        result.append(previous)
    return tuple(result)


def _validate_scenario(scenario: G1ReplayScenario) -> None:
    if not 1 <= scenario.execution_lag_common_sessions <= 20:
        raise G3ReplayError("execution lag must be in [1, 20]")
    if type(scenario.one_way_cost_decimal) is not Decimal:
        raise G3ReplayError("one_way_cost_decimal must be Decimal")
    if not ZERO <= scenario.one_way_cost_decimal < ONE:
        raise G3ReplayError("one_way cost outside [0, 1)")


def _text(value: Decimal) -> str:
    return format(value, "f")


def _decimal_mapping(value: Mapping[str, Decimal]) -> dict[str, str]:
    return {key: _text(item) for key, item in value.items()}


def _json_ready(value: object) -> object:
    if isinstance(value, Decimal):
        return _text(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value
