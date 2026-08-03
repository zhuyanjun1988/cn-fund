from __future__ import annotations

from bisect import bisect_left
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, localcontext
from functools import wraps
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping, Sequence

from cn_fund_strategy.adapters.g1_danjuan_index_valuation import (
    parse_g1_danjuan_index_valuation,
)
from cn_fund_strategy.adapters.eastmoney_profile_nav import (
    parse_eastmoney_profile_nav,
)
from cn_fund_strategy.domain.g1_global_valuation_batch import (
    BOND_SLEEVE,
    EQUITY_SLEEVES,
    GOLD_SLEEVE,
    G1Policy,
    G1Signal,
    G1SleeveDecision,
    G1SleeveState,
    apply_filled_decision,
    apply_weight_changes,
    concentration_trim_changes,
    decide_sleeve,
    initial_sleeve_states,
    initial_weights,
    signed_weight_changes,
    standard_g1_policy,
    valuation_score,
)


ZERO = Decimal("0")
ONE = Decimal("1")
TRADING_SESSIONS_PER_YEAR = Decimal("252")
ALL_SLEEVES = EQUITY_SLEEVES + (GOLD_SLEEVE, BOND_SLEEVE)


class G1ReplayError(ValueError):
    pass


def with_g1_decimal_context(function):
    """Run G1 arithmetic at 40 digits without mutating process-global Decimal."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        with localcontext() as context:
            context.prec = 40
            return function(*args, **kwargs)

    return wrapped


@dataclass(frozen=True, slots=True)
class G1ReplayInputs:
    profile_paths: Mapping[str, Path]
    valuation_directories: Mapping[str, Path]
    start_session: str


@dataclass(frozen=True, slots=True)
class G1ReplayScenario:
    scenario_id: str
    execution_lag_common_sessions: int
    one_way_cost_decimal: Decimal


@dataclass(frozen=True, slots=True)
class _ScheduledEconomicFill:
    signals: tuple[G1Signal, ...]
    decisions: tuple[G1SleeveDecision, ...]


@dataclass(frozen=True, slots=True)
class G1ReplayResult:
    scenario: G1ReplayScenario
    start_session: str
    end_session: str
    sessions: int
    navs: tuple[Decimal, ...]
    returns: tuple[Decimal, ...]
    common_sessions: tuple[str, ...]
    ending_weights: Mapping[str, Decimal]
    ending_states: Mapping[str, G1SleeveState]
    trades: tuple[Mapping[str, object], ...]
    decision_log: tuple[Mapping[str, object], ...]
    maximum_weights: Mapping[str, Decimal]
    maximum_equity_weight: Decimal
    minimum_bond_weight: Decimal

    def metrics(self) -> dict[str, str | int | None]:
        return performance_metrics(self.navs, self.returns, self.common_sessions)

    def as_record(self) -> dict[str, object]:
        return {
            "scenario": {
                "scenario_id": self.scenario.scenario_id,
                "execution_lag_common_sessions": self.scenario.execution_lag_common_sessions,
                "one_way_cost_decimal": _text(self.scenario.one_way_cost_decimal),
            },
            "metrics": self.metrics(),
            "trade_count": len(self.trades),
            "annual_l1_turnover_decimal": _text(
                sum((item["turnover_decimal"] for item in self.trades), ZERO)
                * TRADING_SESSIONS_PER_YEAR
                / Decimal(max(1, self.sessions - 1))
            ),
            "ending_weights": _decimal_mapping(self.ending_weights),
            "ending_states": {
                key: _state_record(value) for key, value in self.ending_states.items()
            },
            "maximum_weights": _decimal_mapping(self.maximum_weights),
            "maximum_equity_weight_decimal": _text(self.maximum_equity_weight),
            "minimum_bond_weight_decimal": _text(self.minimum_bond_weight),
            "rolling_5y": rolling_metrics(self.navs, self.returns, self.common_sessions),
            "trades": [_json_ready(item) for item in self.trades],
            "latest_decisions": [_json_ready(item) for item in self.decision_log[-12:]],
        }


@with_g1_decimal_context
def run_g1_replay(
    inputs: G1ReplayInputs,
    scenario: G1ReplayScenario,
    *,
    policy: G1Policy | None = None,
) -> G1ReplayResult:
    active_policy = policy or standard_g1_policy()
    _validate_scenario(scenario)
    levels, _ = load_total_return_levels(inputs, active_policy)
    valuations = load_valuation_histories(inputs, active_policy)
    common_sessions = _common_sessions(levels, inputs.start_session)
    if len(common_sessions) <= scenario.execution_lag_common_sessions + 1:
        raise G1ReplayError("not enough common sessions")
    month_ends = _month_ends(common_sessions)
    month_end_set = set(month_ends)
    month_position = {item: index for index, item in enumerate(month_ends)}
    session_position = {item: index for index, item in enumerate(common_sessions)}

    weights = {item: ZERO for item in ALL_SLEEVES}
    weights[BOND_SLEEVE] = ONE
    states = {item.sleeve_id: item for item in initial_sleeve_states(active_policy)}
    nav = ONE
    navs: list[Decimal] = []
    returns: list[Decimal] = []
    trades: list[Mapping[str, object]] = []
    decision_log: list[Mapping[str, object]] = []
    maximum_weights = {item: ZERO for item in ALL_SLEEVES}
    maximum_equity_weight = ZERO
    minimum_bond_weight = ONE
    scheduled_fills: dict[str, list[_ScheduledEconomicFill]] = {}
    scheduled_guards: dict[str, tuple[str, ...]] = {}
    initial_session = common_sessions[scenario.execution_lag_common_sessions]
    previous_session: str | None = None

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
                raise G1ReplayError(f"non-positive portfolio gross return on {current_session}")
            weights = {
                sleeve_id: weights[sleeve_id]
                * levels[sleeve_id][current_session]
                / levels[sleeve_id][previous_session]
                / gross
                for sleeve_id in ALL_SLEEVES
            }
            nav *= gross

        if current_session == initial_session:
            target = initial_weights(active_policy)
            nav, weights, trade = _execute_target(
                current_session,
                nav,
                weights,
                target,
                scenario.one_way_cost_decimal,
                reason_codes=("INITIAL_STRATEGIC_ALLOCATION",),
                changed_sleeves=ALL_SLEEVES,
                decision_session=common_sessions[0],
            )
            trades.append(trade)

        for scheduled in scheduled_fills.get(current_session, ()):
            changes = signed_weight_changes(scheduled.decisions)
            target = apply_weight_changes(weights, changes)
            nav, weights, trade = _execute_target(
                current_session,
                nav,
                weights,
                target,
                scenario.one_way_cost_decimal,
                reason_codes=tuple(
                    code
                    for decision in scheduled.decisions
                    for code in decision.reason_codes
                ),
                changed_sleeves=tuple(item.sleeve_id for item in scheduled.decisions),
                decision_session=scheduled.decisions[0].decision_session,
            )
            trades.append(trade)
            for signal, decision in zip(scheduled.signals, scheduled.decisions, strict=True):
                states[signal.sleeve_id] = apply_filled_decision(
                    states[signal.sleeve_id], signal, decision
                )

        guarded_sleeves = scheduled_guards.get(current_session, ())
        if guarded_sleeves:
            changes = scheduled_concentration_trim_changes(
                active_policy, weights, guarded_sleeves
            )
            if changes:
                target = apply_weight_changes(weights, changes)
                nav, weights, trade = _execute_target(
                    current_session,
                    nav,
                    weights,
                    target,
                    scenario.one_way_cost_decimal,
                    reason_codes=(
                        "MONTH_END_DRIFT_ABOVE_25PCT_TRIM_TO_CAP"
                        if active_policy.concentration_cap == Decimal("0.25")
                        else "MONTH_END_DRIFT_ABOVE_SLEEVE_CAP_TRIM_TO_CAP",
                    ),
                    changed_sleeves=guarded_sleeves,
                    decision_session="scheduled-month-end-guard",
                )
                trades.append(trade)

        if current_session in month_end_set:
            signals: list[G1Signal] = []
            decisions: list[G1SleeveDecision] = []
            for sleeve_id in EQUITY_SLEEVES:
                signal = _historical_signal(
                    active_policy,
                    sleeve_id,
                    current_session,
                    month_position[current_session],
                    month_ends,
                    levels,
                    valuations,
                )
                decision = decide_sleeve(active_policy, states[sleeve_id], signal)
                signals.append(signal)
                decisions.append(decision)
                decision_log.append(_decision_record(signal, decision))
            actionable = tuple(item for item in decisions if item.action_batches)
            if actionable:
                if ordinal + scenario.execution_lag_common_sessions < len(common_sessions):
                    execution_session = common_sessions[
                        ordinal + scenario.execution_lag_common_sessions
                    ]
                    signal_by_sleeve = {item.sleeve_id: item for item in signals}
                    scheduled_fills.setdefault(execution_session, []).append(
                        _ScheduledEconomicFill(
                            signals=tuple(signal_by_sleeve[item.sleeve_id] for item in actionable),
                            decisions=actionable,
                        )
                    )
            if ordinal + scenario.execution_lag_common_sessions < len(common_sessions):
                breaches = tuple(
                    sleeve_id
                    for sleeve_id in EQUITY_SLEEVES
                    if weights[sleeve_id] > active_policy.concentration_cap
                )
                if breaches:
                    guard_session = common_sessions[
                        ordinal + scenario.execution_lag_common_sessions
                    ]
                    scheduled_guards[guard_session] = breaches

        for sleeve_id in ALL_SLEEVES:
            maximum_weights[sleeve_id] = max(maximum_weights[sleeve_id], weights[sleeve_id])
        maximum_equity_weight = max(
            maximum_equity_weight,
            sum((weights[item] for item in EQUITY_SLEEVES), ZERO),
        )
        minimum_bond_weight = min(minimum_bond_weight, weights[BOND_SLEEVE])
        session_return = nav / nav_before_session - ONE if ordinal else ZERO
        navs.append(nav)
        returns.append(session_return)
        previous_session = current_session

    return G1ReplayResult(
        scenario=scenario,
        start_session=common_sessions[0],
        end_session=common_sessions[-1],
        sessions=len(common_sessions),
        navs=tuple(navs),
        returns=tuple(returns),
        common_sessions=tuple(common_sessions),
        ending_weights=weights,
        ending_states=states,
        trades=tuple(trades),
        decision_log=tuple(decision_log),
        maximum_weights=maximum_weights,
        maximum_equity_weight=maximum_equity_weight,
        minimum_bond_weight=minimum_bond_weight,
    )


def scheduled_concentration_trim_changes(
    policy: G1Policy,
    marked_weights: Mapping[str, Decimal],
    guarded_sleeves: Sequence[str],
) -> dict[str, Decimal]:
    """Return a self-financing trim for only the sleeves known at decision time.

    The full concentration helper can find additional breaches that developed
    during the execution lag.  Those breaches were not in the scheduled event;
    including their bond proceeds while filtering out their equity legs breaks
    the accounting identity.  Recompute the defensive transfer from the
    selected equity legs only.
    """
    all_changes = concentration_trim_changes(policy, marked_weights)
    selected = {
        key: value
        for key, value in all_changes.items()
        if key != BOND_SLEEVE and key in guarded_sleeves
    }
    if selected:
        selected[BOND_SLEEVE] = -sum(selected.values(), ZERO)
    return selected


@with_g1_decimal_context
def run_static_benchmark(
    inputs: G1ReplayInputs,
    scenario: G1ReplayScenario,
    *,
    annual_rebalance: bool,
    policy: G1Policy | None = None,
) -> dict[str, object]:
    active_policy = policy or standard_g1_policy()
    levels, _ = load_total_return_levels(inputs, active_policy)
    common_sessions = _common_sessions(levels, inputs.start_session)
    target = static_65_target(active_policy)
    execution_sessions = {common_sessions[scenario.execution_lag_common_sessions]}
    if annual_rebalance:
        by_year: dict[int, list[str]] = {}
        for item in common_sessions:
            by_year.setdefault(date.fromisoformat(item).year, []).append(item)
        for year, sessions in by_year.items():
            if year == date.fromisoformat(common_sessions[0]).year:
                continue
            if len(sessions) > scenario.execution_lag_common_sessions:
                execution_sessions.add(sessions[scenario.execution_lag_common_sessions])
    weights = {item: ZERO for item in ALL_SLEEVES}
    weights[BOND_SLEEVE] = ONE
    nav = ONE
    navs: list[Decimal] = []
    returns: list[Decimal] = []
    turnovers: list[Decimal] = []
    previous: str | None = None
    for ordinal, current in enumerate(common_sessions):
        nav_before = nav
        if previous is not None:
            gross = sum(
                (
                    weights[key] * levels[key][current] / levels[key][previous]
                    for key in ALL_SLEEVES
                ),
                ZERO,
            )
            weights = {
                key: weights[key] * levels[key][current] / levels[key][previous] / gross
                for key in ALL_SLEEVES
            }
            nav *= gross
        if current in execution_sessions:
            turnover = sum((abs(target[key] - weights[key]) for key in ALL_SLEEVES), ZERO)
            nav *= ONE - turnover * scenario.one_way_cost_decimal
            weights = dict(target)
            turnovers.append(turnover)
        navs.append(nav)
        returns.append(nav / nav_before - ONE if ordinal else ZERO)
        previous = current
    return {
        "benchmark_id": "STATIC65-ANNUAL" if annual_rebalance else "STATIC65-BUYHOLD-DIAGNOSTIC",
        "rebalance_rule": "third-common-session-each-calendar-year" if annual_rebalance else "initial-only",
        "metrics": performance_metrics(tuple(navs), tuple(returns), tuple(common_sessions)),
        "trade_count": len(turnovers),
        "annual_l1_turnover_decimal": _text(
            sum(turnovers, ZERO) * TRADING_SESSIONS_PER_YEAR / Decimal(len(common_sessions) - 1)
        ),
        "target_weights": _decimal_mapping(target),
        "ending_weights": _decimal_mapping(weights),
    }


@with_g1_decimal_context
def run_fixed_event_settlement_stress(
    inputs: G1ReplayInputs,
    base_replay: G1ReplayResult,
    *,
    redemption_settlement_common_sessions: int,
) -> dict[str, object]:
    """Replay frozen economic fills with sale proceeds held as zero-return cash.

    This is deliberately a sensitivity calculation rather than a new signal run:
    the base candidate's decision and execution sessions stay fixed, while every
    equity sale is moved to a settlement receivable and only transferred to the
    bond sleeve after the specified number of common sessions.  The original
    trade cost is retained and is not charged again on receivable release.
    """
    if not 1 <= redemption_settlement_common_sessions <= 20:
        raise G1ReplayError("redemption settlement sessions must be in [1, 20]")
    policy = standard_g1_policy()
    levels, _ = load_total_return_levels(inputs, policy)
    sessions = list(base_replay.common_sessions)
    session_position = {item: index for index, item in enumerate(sessions)}
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
            reason_codes = tuple(frozen_trade["reason_codes"])
            if "INITIAL_STRATEGIC_ALLOCATION" in reason_codes:
                target = dict(posttrade)
                target[cash_sleeve] = ZERO
            else:
                target = dict(weights)
                cash_before = target[cash_sleeve]
                for sleeve_id in EQUITY_SLEEVES:
                    fixed_change = posttrade[sleeve_id] - pretrade[sleeve_id]
                    if fixed_change < ZERO:
                        target[sleeve_id] += fixed_change
                        target[cash_sleeve] -= fixed_change
                    elif fixed_change > ZERO:
                        target[sleeve_id] += fixed_change
                        target[BOND_SLEEVE] -= fixed_change
                if target[BOND_SLEEVE] < ZERO:
                    raise G1ReplayError("settlement stress buy exceeds bond funding")
                new_cash_weight = target[cash_sleeve] - cash_before
                if new_cash_weight > ZERO:
                    release_position = ordinal + redemption_settlement_common_sessions
                    if release_position < len(sessions):
                        release_session = sessions[release_position]
                        cash_amount = (
                            nav
                            * (
                                ONE
                                - frozen_trade["turnover_decimal"]
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
            total = sum(target.values(), ZERO)
            if abs(total - ONE) > Decimal("0.00000000000000000001"):
                raise G1ReplayError("settlement target weights do not sum to one")
            if any(item < ZERO for item in target.values()):
                raise G1ReplayError("settlement target contains a negative weight")
            turnover = frozen_trade["turnover_decimal"]
            nav -= nav * turnover * base_replay.scenario.one_way_cost_decimal
            weights = target

        navs.append(nav)
        returns.append(nav / nav_before - ONE if ordinal else ZERO)
        previous = current

    metrics = performance_metrics(tuple(navs), tuple(returns), tuple(sessions))
    return {
        "scenario_id": "base-fixed-events-with-zero-return-redemption-settlement-cash",
        "signal_and-fill-sessions": "identical-to-base-replay",
        "redemption_settlement_common_sessions": redemption_settlement_common_sessions,
        "cost_model": "identical-to-base-no-second-charge-on-settlement-release",
        "metrics": metrics,
        "ending_weights": {
            **_decimal_mapping({key: weights[key] for key in ALL_SLEEVES}),
            cash_sleeve: _text(weights[cash_sleeve]),
        },
        "settlement_event_count": len(settlement_events),
        "settlement_events": _json_ready(settlement_events),
        "authority": "fixed-event-settlement-sensitivity-not-a-new-signal-backtest",
    }


@with_g1_decimal_context
def build_live_decisions(
    inputs: G1ReplayInputs,
    replay: G1ReplayResult,
    *,
    decision_session: str,
    valuation_cutoff_session: str,
    policy: G1Policy | None = None,
) -> list[dict[str, object]]:
    active_policy = policy or standard_g1_policy()
    levels, names = load_total_return_levels(inputs, active_policy)
    valuations = load_valuation_histories(inputs, active_policy)
    output: list[dict[str, object]] = []
    for sleeve_id in EQUITY_SLEEVES:
        available_sessions = sorted(
            item for item in levels[sleeve_id] if item <= decision_session
        )
        if not available_sessions:
            raise G1ReplayError(f"no live total-return observation for {sleeve_id}")
        current_fund_session = available_sessions[-1]
        own_month_ends = _month_ends(available_sessions)
        momentum: Decimal | None = None
        if len(own_month_ends) >= 11:
            momentum = (
                levels[sleeve_id][own_month_ends[-1]]
                / levels[sleeve_id][own_month_ends[-11]]
                - ONE
            )
        history = tuple(
            item
            for item in valuations[sleeve_id]
            if item[0] <= valuation_cutoff_session
        )
        score: Decimal | None = None
        valuation_session: str | None = None
        if len(history) >= 13:
            score = valuation_score(
                tuple(item[1] for item in history),
                tuple(item[2] for item in history),
            )
            valuation_session = history[-1][0]
        signal = G1Signal(
            sleeve_id=sleeve_id,
            decision_session=decision_session,
            month_ordinal=_month_ordinal(decision_session),
            valuation_session=valuation_session,
            valuation_score=score,
            momentum_10m=momentum,
            total_return_level=levels[sleeve_id][current_fund_session],
            complete=score is not None and momentum is not None,
        )
        state = replay.ending_states[sleeve_id]
        decision = decide_sleeve(active_policy, state, signal)
        output.append(
            {
                "sleeve_id": sleeve_id,
                "fund_code": active_policy.sleeve(sleeve_id).fund_code,
                "fund_name": names[sleeve_id],
                "fund_total_return_as_of": current_fund_session,
                "valuation_as_of": valuation_session,
                "valuation_score_decimal": score,
                "momentum_10m_decimal": momentum,
                "marked_weight_before_new_order_decimal": replay.ending_weights[sleeve_id],
                "batch_count_before": state.batches,
                "action_batches": decision.action_batches,
                "requested_weight_change_decimal": decision.requested_weight_change,
                "target_weight_if_filled_decimal": (
                    replay.ending_weights[sleeve_id]
                    + decision.requested_weight_change
                ),
                "reason_codes": decision.reason_codes,
                "signal": signal,
                "decision": decision,
            }
        )
    return output


@with_g1_decimal_context
def static_65_target(policy: G1Policy) -> dict[str, Decimal]:
    neutral = {
        item.sleeve_id: item.core_weight + item.swing_capacity / Decimal(2)
        for item in policy.sleeves
    }
    neutral_total = sum(neutral.values(), ZERO)
    target = {
        key: value * Decimal("0.65") / neutral_total
        for key, value in neutral.items()
    }
    target[GOLD_SLEEVE] = Decimal("0.10")
    target[BOND_SLEEVE] = Decimal("0.25")
    return target


@with_g1_decimal_context
def load_total_return_levels(
    inputs: G1ReplayInputs,
    policy: G1Policy,
) -> tuple[dict[str, dict[str, Decimal]], dict[str, str]]:
    required = set(ALL_SLEEVES)
    if set(inputs.profile_paths) != required:
        raise G1ReplayError("profile path sleeve identities mismatch")
    levels: dict[str, dict[str, Decimal]] = {}
    names: dict[str, str] = {}
    expected_codes = {item.sleeve_id: item.fund_code for item in policy.sleeves}
    expected_codes.update({GOLD_SLEEVE: "000216", BOND_SLEEVE: "161120"})
    for sleeve_id in ALL_SLEEVES:
        path = inputs.profile_paths[sleeve_id]
        series = parse_eastmoney_profile_nav(
            path.read_bytes(), expected_fund_code=expected_codes[sleeve_id]
        )
        level = ONE
        observations: dict[str, Decimal] = {}
        for ordinal, observation in enumerate(series.observations):
            if ordinal:
                if observation.daily_return_pct_decimal is None:
                    raise G1ReplayError(
                        f"{series.fund_code} missing published daily return on {observation.nav_date}"
                    )
                level *= ONE + Decimal(observation.daily_return_pct_decimal) / Decimal(100)
            observations[observation.nav_date] = level
        if not observations:
            raise G1ReplayError(f"{series.fund_code} has no NAV observations")
        levels[sleeve_id] = observations
        names[sleeve_id] = series.fund_name
    return levels, names


def load_valuation_histories(
    inputs: G1ReplayInputs,
    policy: G1Policy,
) -> dict[str, tuple[tuple[str, Decimal, Decimal], ...]]:
    if set(inputs.valuation_directories) != set(EQUITY_SLEEVES):
        raise G1ReplayError("valuation directory sleeve identities mismatch")
    histories: dict[str, tuple[tuple[str, Decimal, Decimal], ...]] = {}
    for sleeve_id in EQUITY_SLEEVES:
        item = policy.sleeve(sleeve_id)
        directory = inputs.valuation_directories[sleeve_id]
        series = parse_g1_danjuan_index_valuation(
            (directory / "pe.json").read_bytes(),
            (directory / "pb.json").read_bytes(),
            expected_index_code=item.index_code,
        )
        pe = {row.business_date: Decimal(row.value_decimal) for row in series.pe_observations}
        pb = {row.business_date: Decimal(row.value_decimal) for row in series.pb_observations}
        common = sorted(set(pe) & set(pb))
        histories[sleeve_id] = tuple((day, pe[day], pb[day]) for day in common)
    return histories


@with_g1_decimal_context
def performance_metrics(
    navs: Sequence[Decimal],
    returns: Sequence[Decimal],
    sessions: Sequence[str],
) -> dict[str, str | int | None]:
    if len(navs) != len(returns) or len(navs) != len(sessions) or len(navs) < 2:
        raise G1ReplayError("performance series lengths are invalid")
    count = Decimal(len(navs) - 1)
    cagr = ((navs[-1] / navs[0]).ln() * TRADING_SESSIONS_PER_YEAR / count).exp() - ONE
    high = navs[0]
    maximum_drawdown = ZERO
    for value in navs:
        high = max(high, value)
        maximum_drawdown = max(maximum_drawdown, ONE - value / high)
    sample = tuple(returns[1:])
    mean = sum(sample, ZERO) / Decimal(len(sample))
    variance = sum(((item - mean) ** 2 for item in sample), ZERO) / Decimal(len(sample) - 1)
    volatility = variance.sqrt() * TRADING_SESSIONS_PER_YEAR.sqrt()
    sharpe = mean / variance.sqrt() * TRADING_SESSIONS_PER_YEAR.sqrt() if variance else None
    return {
        "start_session": sessions[0],
        "end_session": sessions[-1],
        "sessions": len(sessions),
        "years_252_decimal": _text(count / TRADING_SESSIONS_PER_YEAR),
        "ending_nav_decimal": _text(navs[-1]),
        "net_cagr_decimal": _text(cagr),
        "maximum_drawdown_decimal": _text(maximum_drawdown),
        "calmar_decimal": _text(cagr / maximum_drawdown) if maximum_drawdown else None,
        "annualized_volatility_decimal": _text(volatility),
        "zero_risk_free_sharpe_decimal": _text(sharpe) if sharpe is not None else None,
    }


@with_g1_decimal_context
def rolling_metrics(
    navs: Sequence[Decimal],
    returns: Sequence[Decimal],
    sessions: Sequence[str],
    *,
    window_sessions: int = 1260,
    step_sessions: int = 63,
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
    cagr_values = tuple(Decimal(str(row["net_cagr_decimal"])) for row in rows)
    mdd_values = tuple(Decimal(str(row["maximum_drawdown_decimal"])) for row in rows)
    return {
        "window_sessions": window_sessions,
        "step_sessions": step_sessions,
        "window_count": len(rows),
        "positive_cagr_fraction_decimal": _text(
            Decimal(sum(item > ZERO for item in cagr_values)) / Decimal(len(rows))
        ) if rows else None,
        "minimum_cagr_decimal": _text(min(cagr_values)) if rows else None,
        "maximum_drawdown_decimal": _text(max(mdd_values)) if rows else None,
        "windows": rows,
    }


def source_hashes(inputs: G1ReplayInputs) -> dict[str, str]:
    paths = list(inputs.profile_paths.values())
    for directory in inputs.valuation_directories.values():
        paths.extend((directory / "pe.json", directory / "pb.json"))
    return {str(path.resolve()): _sha256_file(path) for path in sorted(paths)}


def _historical_signal(
    policy: G1Policy,
    sleeve_id: str,
    decision_session: str,
    month_index: int,
    month_ends: Sequence[str],
    levels: Mapping[str, Mapping[str, Decimal]],
    valuations: Mapping[str, Sequence[tuple[str, Decimal, Decimal]]],
) -> G1Signal:
    history = valuations[sleeve_id]
    dates = [item[0] for item in history]
    position = bisect_left(dates, decision_session) - 1
    score: Decimal | None = None
    valuation_session: str | None = None
    if position >= 12:
        available = history[: position + 1]
        score = valuation_score(
            tuple(item[1] for item in available),
            tuple(item[2] for item in available),
        )
        valuation_session = available[-1][0]
    momentum: Decimal | None = None
    if month_index >= 10:
        momentum = (
            levels[sleeve_id][decision_session]
            / levels[sleeve_id][month_ends[month_index - 10]]
            - ONE
        )
    return G1Signal(
        sleeve_id=sleeve_id,
        decision_session=decision_session,
        month_ordinal=_month_ordinal(decision_session),
        valuation_session=valuation_session,
        valuation_score=score,
        momentum_10m=momentum,
        total_return_level=levels[sleeve_id][decision_session],
        complete=score is not None and momentum is not None,
    )


def _execute_target(
    execution_session: str,
    nav: Decimal,
    weights: Mapping[str, Decimal],
    target: Mapping[str, Decimal],
    cost: Decimal,
    *,
    reason_codes: tuple[str, ...],
    changed_sleeves: Sequence[str],
    decision_session: str,
) -> tuple[Decimal, dict[str, Decimal], Mapping[str, object]]:
    turnover = sum((abs(target[item] - weights[item]) for item in ALL_SLEEVES), ZERO)
    charge = nav * turnover * cost
    post_cost_nav = nav - charge
    if post_cost_nav <= ZERO:
        raise G1ReplayError("transaction cost exhausted portfolio")
    trade = {
        "decision_session": decision_session,
        "execution_session": execution_session,
        "changed_sleeves": tuple(changed_sleeves),
        "reason_codes": reason_codes,
        "turnover_decimal": turnover,
        "cost_cashflow_nav_units_decimal": charge,
        "pretrade_weights": dict(weights),
        "posttrade_weights": dict(target),
    }
    return post_cost_nav, dict(target), trade


def _decision_record(signal: G1Signal, decision: G1SleeveDecision) -> dict[str, object]:
    return {
        "signal": asdict(signal),
        "decision": asdict(decision),
    }


def _common_sessions(
    levels: Mapping[str, Mapping[str, Decimal]],
    start_session: str,
) -> list[str]:
    common = set.intersection(*(set(item) for item in levels.values()))
    sessions = sorted(item for item in common if item >= start_session)
    if not sessions:
        raise G1ReplayError("no common total-return sessions")
    return sessions


def _month_ends(sessions: Sequence[str]) -> list[str]:
    result: list[str] = []
    prior: tuple[int, int] | None = None
    for item in sessions:
        current_date = date.fromisoformat(item)
        key = (current_date.year, current_date.month)
        if prior is not None and key != prior:
            result.append(previous)
        prior = key
        previous = item
    result.append(previous)
    return result


def _month_ordinal(session: str) -> int:
    item = date.fromisoformat(session)
    return item.year * 12 + item.month


def _validate_scenario(scenario: G1ReplayScenario) -> None:
    if scenario.execution_lag_common_sessions < 1 or scenario.execution_lag_common_sessions > 20:
        raise G1ReplayError("execution lag must be in [1, 20]")
    if type(scenario.one_way_cost_decimal) is not Decimal:
        raise G1ReplayError("one_way_cost_decimal must be Decimal")
    if scenario.one_way_cost_decimal < ZERO or scenario.one_way_cost_decimal >= ONE:
        raise G1ReplayError("one_way cost outside [0, 1)")


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text(value: Decimal) -> str:
    return format(value, "f")


def _decimal_mapping(value: Mapping[str, Decimal]) -> dict[str, str]:
    return {key: _text(item) for key, item in value.items()}


def _state_record(value: G1SleeveState) -> dict[str, object]:
    return _json_ready(asdict(value))


def _json_ready(value: object) -> object:
    if isinstance(value, Decimal):
        return _text(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def write_json_create_or_identical(path: Path, payload: Mapping[str, object]) -> None:
    body = (
        json.dumps(_json_ready(payload), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise G1ReplayError(f"refusing to overwrite different result: {path}")
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(body)
    temporary.replace(path)
