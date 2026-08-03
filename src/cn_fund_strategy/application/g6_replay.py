from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from cn_fund_strategy.application.g1_replay import (
    ALL_SLEEVES,
    G1ReplayInputs,
    G1ReplayScenario,
    load_total_return_levels,
    load_valuation_histories,
    performance_metrics,
    with_g1_decimal_context,
)
from cn_fund_strategy.application.g3_replay import (
    BASE_LOSS_FACTORS,
    G3ReplayError,
    G3ReplayResult,
    _execute_target,
    _json_ready,
    _month_ends,
    _valuation_before,
    static_loss,
)
from cn_fund_strategy.domain.g1_global_valuation_batch import BOND_SLEEVE, EQUITY_SLEEVES, GOLD_SLEEVE
from cn_fund_strategy.domain.g6_quarterly_valuation_tilt import (
    G6Decision,
    G6Policy,
    G6Signal,
    G6TiltState,
    decide_tilt,
    extreme_static_loss,
    standard_g6_policy,
    state_from_decision,
    strategic_target,
    target_with_state,
)


ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class _ScheduledQuarterlyFill:
    signal: G6Signal
    decision: G6Decision


@dataclass(frozen=True, slots=True)
class G6ReplayResult(G3ReplayResult):
    def daily_ledger_record(self) -> dict[str, object]:
        return {
            "schema_version": "g6-standard-daily-accounting-ledger-v1",
            "canonical_name": self.canonical_name,
            "candidate_id": self.technical_id,
            "scenario_id": self.scenario.scenario_id,
            "row_count": len(self.daily_rows),
            "rows": [_json_ready(item) for item in self.daily_rows],
        }


@with_g1_decimal_context
def run_g6_replay(
    inputs: G1ReplayInputs,
    scenario: G1ReplayScenario,
    *,
    policy: G6Policy | None = None,
) -> G6ReplayResult:
    active_policy = policy or standard_g6_policy()
    if not 1 <= scenario.execution_lag_common_sessions <= 20:
        raise G3ReplayError("execution lag must be in [1, 20]")
    levels, _ = load_total_return_levels(inputs, active_policy)  # type: ignore[arg-type]
    valuations = load_valuation_histories(inputs, active_policy)  # type: ignore[arg-type]
    all_sessions = sorted(set.intersection(*(set(item) for item in levels.values())))
    sessions = tuple(item for item in all_sessions if item >= inputs.start_session)
    if len(sessions) <= scenario.execution_lag_common_sessions + 1:
        raise G3ReplayError("not enough common sessions")
    all_month_ends = _month_ends(all_sessions)
    quarter_ends = tuple(item for item in all_month_ends if item[5:7] in {"03", "06", "09", "12"})
    quarter_end_set = {item for item in quarter_ends if item >= inputs.start_session}
    month_position = {item: i for i, item in enumerate(all_month_ends)}

    weights = {item: ZERO for item in ALL_SLEEVES}
    weights[BOND_SLEEVE] = ONE
    state = G6TiltState()
    nav = ONE
    navs: list[Decimal] = []
    returns: list[Decimal] = []
    trades: list[Mapping[str, object]] = []
    decisions: list[Mapping[str, object]] = []
    daily_rows: list[Mapping[str, object]] = []
    maximum_weights = {item: ZERO for item in ALL_SLEEVES}
    maximum_equity = ZERO
    minimum_bond = ONE
    maximum_extreme = ZERO
    maximum_base = ZERO
    maximum_extreme_overlay = ZERO
    maximum_base_overlay = ZERO
    scheduled: dict[str, list[_ScheduledQuarterlyFill]] = {}
    initial_session = sessions[scenario.execution_lag_common_sessions]
    previous: str | None = None
    high_water = ONE

    for ordinal, current in enumerate(sessions):
        before = nav
        if previous is not None:
            gross = sum(
                weights[key] * levels[key][current] / levels[key][previous]
                for key in ALL_SLEEVES
            )
            if gross <= ZERO:
                raise G3ReplayError("non-positive gross return")
            weights = {
                key: weights[key] * levels[key][current] / levels[key][previous] / gross
                for key in ALL_SLEEVES
            }
            nav *= gross

        pre_extreme = extreme_static_loss(weights)
        pre_base = static_loss(weights, BASE_LOSS_FACTORS)
        maximum_extreme = max(maximum_extreme, pre_extreme)
        maximum_base = max(maximum_base, pre_base)

        if current == initial_session:
            nav, weights, trade = _execute_target(
                execution_session=current,
                nav=nav,
                weights=weights,
                target=strategic_target(active_policy),
                cost=scenario.one_way_cost_decimal,
                decision_session=sessions[0],
                trade_kind="INITIAL_STRATEGIC_ALLOCATION",
                reason_codes=("INITIAL_FIXED_50_EQUITY_20_GOLD_30_BOND",),
            )
            trades.append(trade)

        for item in scheduled.get(current, ()):
            target = item.decision.target_mapping()
            nav, weights, trade = _execute_target(
                execution_session=current,
                nav=nav,
                weights=weights,
                target=target,
                cost=scenario.one_way_cost_decimal,
                decision_session=item.decision.decision_session,
                trade_kind="QUARTERLY_STRATEGIC_AND_VALUATION_TILT_REBALANCE",
                reason_codes=item.decision.reason_codes,
            )
            trades.append(trade)
            state = state_from_decision(item.decision)

        if current in quarter_end_set:
            signal = _historical_signal(
                current,
                month_position[current],
                all_month_ends,
                levels,
                valuations,
            )
            decision = decide_tilt(active_policy, signal)
            decisions.append({"signal": asdict(signal), "decision": asdict(decision)})
            execute_at = ordinal + scenario.execution_lag_common_sessions
            if execute_at < len(sessions):
                scheduled.setdefault(sessions[execute_at], []).append(
                    _ScheduledQuarterlyFill(signal, decision)
                )

        post_extreme = extreme_static_loss(weights)
        post_base = static_loss(weights, BASE_LOSS_FACTORS)
        maximum_extreme = max(maximum_extreme, post_extreme)
        maximum_base = max(maximum_base, post_base)
        high_water = max(high_water, nav)
        maximum_extreme_overlay = max(
            maximum_extreme_overlay, ONE - nav * (ONE - post_extreme) / high_water
        )
        maximum_base_overlay = max(
            maximum_base_overlay, ONE - nav * (ONE - post_base) / high_water
        )
        equity = sum((weights[item] for item in EQUITY_SLEEVES), ZERO)
        maximum_equity = max(maximum_equity, equity)
        minimum_bond = min(minimum_bond, weights[BOND_SLEEVE])
        for key in ALL_SLEEVES:
            maximum_weights[key] = max(maximum_weights[key], weights[key])
        session_return = nav / before - ONE if ordinal else ZERO
        navs.append(nav)
        returns.append(session_return)
        daily_rows.append(
            {
                "session": current,
                "nav_decimal": nav,
                "return_decimal": session_return,
                "weights": dict(weights),
                "equity_weight_decimal": equity,
                "preaction_extreme_static_loss_decimal": pre_extreme,
                "postaction_extreme_static_loss_decimal": post_extreme,
                "postaction_base_static_loss_decimal": post_base,
                "tilt_donor_sleeve": state.donor_sleeve,
                "tilt_receiver_sleeve": state.receiver_sleeve,
            }
        )
        previous = current

    return G6ReplayResult(
        canonical_name=active_policy.canonical_name,
        technical_id=active_policy.technical_id,
        scenario=scenario,
        start_session=sessions[0],
        end_session=sessions[-1],
        common_sessions=sessions,
        navs=tuple(navs),
        returns=tuple(returns),
        ending_weights=weights,
        ending_states={"GLOBAL": state},  # type: ignore[dict-item]
        trades=tuple(trades),
        decision_log=tuple(decisions),
        daily_rows=tuple(daily_rows),
        maximum_weights=maximum_weights,
        maximum_equity_weight=maximum_equity,
        minimum_bond_weight=minimum_bond,
        maximum_extreme_static_loss=maximum_extreme,
        maximum_base_static_loss=maximum_base,
        maximum_extreme_path_overlay_drawdown=maximum_extreme_overlay,
        maximum_base_path_overlay_drawdown=maximum_base_overlay,
    )


@with_g1_decimal_context
def build_current_g6_briefing(
    inputs: G1ReplayInputs,
    replay: G6ReplayResult,
    *,
    decision_session: str,
    valuation_cutoff_session: str,
    policy: G6Policy | None = None,
) -> list[dict[str, object]]:
    active_policy = policy or standard_g6_policy()
    levels, names = load_total_return_levels(inputs, active_policy)  # type: ignore[arg-type]
    valuations = load_valuation_histories(inputs, active_policy)  # type: ignore[arg-type]
    available = sorted(
        item
        for item in set.intersection(*(set(value) for value in levels.values()))
        if item <= decision_session
    )
    month_ends = _month_ends(available)
    signal_session = month_ends[-1]
    preview = _current_signal(
        decision_session,
        signal_session,
        month_ends,
        levels,
        valuations,
        valuation_cutoff_session=valuation_cutoff_session,
    )
    preview_decision = decide_tilt(active_policy, preview)
    state = replay.ending_states["GLOBAL"]
    target = target_with_state(active_policy, state)  # type: ignore[arg-type]
    scores = preview.valuation_mapping()
    momentum = preview.momentum_mapping()
    value_sessions = dict(preview.valuation_sessions)
    return [
        {
            "module_id": "QUARTERLY_RELATIVE_VALUATION_TILT",
            "sleeve_id": sleeve_id,
            "fund_code": active_policy.sleeve(sleeve_id).fund_code,
            "fund_name": names[sleeve_id],
            "decision_session": decision_session,
            "signal_common_session": signal_session,
            "valuation_as_of": value_sessions[sleeve_id],
            "valuation_score_decimal": scores[sleeve_id],
            "momentum_10m_decimal": momentum[sleeve_id],
            "action_batches": 0,
            "requested_weight_change_decimal": ZERO,
            "marked_weight_decimal": replay.ending_weights[sleeve_id],
            "current_quarterly_target_weight_decimal": target[sleeve_id],
            "preview_next_quarter_receiver": preview_decision.receiver_sleeve,
            "preview_next_quarter_donor": preview_decision.donor_sleeve,
            "reason_codes": ("BETWEEN_QUARTER_ENDS_HOLD_NO_ORDER",),
        }
        for sleeve_id in EQUITY_SLEEVES
    ]


@with_g1_decimal_context
def run_g6_fixed_event_settlement_stress(
    inputs: G1ReplayInputs,
    base_replay: G6ReplayResult,
    *,
    redemption_settlement_common_sessions: int,
    policy: G6Policy,
) -> dict[str, object]:
    """Replay frozen G6 fills while keeping sale proceeds in zero-return cash.

    The cash-delayed path can drift away from the base path.  Therefore each
    frozen fill uses the frozen *post-trade risky targets*, not the base path's
    signed weight deltas.  Current-path sales enter settlement cash and
    current-path purchases use only already available bond capital.
    """

    if not 1 <= redemption_settlement_common_sessions <= 20:
        raise G3ReplayError("redemption settlement must be in [1, 20]")
    levels, _ = load_total_return_levels(inputs, policy)  # type: ignore[arg-type]
    sessions = base_replay.common_sessions
    trades_by_session: dict[str, list[Mapping[str, object]]] = {}
    for trade in base_replay.trades:
        trades_by_session.setdefault(str(trade["execution_session"]), []).append(trade)
    cash = "SETTLEMENT_CASH"
    weights = {item: ZERO for item in ALL_SLEEVES} | {cash: ZERO}
    weights[BOND_SLEEVE] = ONE
    nav = ONE
    navs: list[Decimal] = []
    returns: list[Decimal] = []
    releases: dict[str, list[Decimal]] = {}
    events: list[dict[str, object]] = []
    previous: str | None = None
    risky = EQUITY_SLEEVES + (GOLD_SLEEVE,)
    for ordinal, current in enumerate(sessions):
        before = nav
        if previous is not None:
            gross = weights[cash] + sum(
                weights[key] * levels[key][current] / levels[key][previous]
                for key in ALL_SLEEVES
            )
            weights = {
                key: weights[key] * levels[key][current] / levels[key][previous] / gross
                for key in ALL_SLEEVES
            } | {cash: weights[cash] / gross}
            nav *= gross
        for amount in releases.get(current, ()):
            release_weight = min(weights[cash], amount / nav)
            weights[cash] -= release_weight
            weights[BOND_SLEEVE] += release_weight
        for trade in trades_by_session.get(current, ()):
            post = trade["posttrade_weights"]
            if trade["trade_kind"] == "INITIAL_STRATEGIC_ALLOCATION":
                weights = {key: Decimal(str(post[key])) for key in ALL_SLEEVES} | {cash: ZERO}
            else:
                cash_before = weights[cash]
                for sleeve_id in risky:
                    frozen_target = Decimal(str(post[sleeve_id]))
                    delta = frozen_target - weights[sleeve_id]
                    if delta < ZERO:
                        weights[cash] -= delta
                    elif delta > ZERO:
                        weights[BOND_SLEEVE] -= delta
                    weights[sleeve_id] = frozen_target
                if weights[BOND_SLEEVE] < ZERO:
                    raise G3ReplayError("corrected settlement path lacks bond funding")
                new_cash = weights[cash] - cash_before
                if new_cash > ZERO:
                    release_at = ordinal + redemption_settlement_common_sessions
                    if release_at < len(sessions):
                        amount = nav * new_cash
                        releases.setdefault(sessions[release_at], []).append(amount)
                        events.append(
                            {
                                "sale_session": current,
                                "release_session": sessions[release_at],
                                "cash_amount_nav_units_decimal": amount,
                            }
                        )
            turnover = Decimal(str(trade["turnover_decimal"]))
            nav -= nav * turnover * base_replay.scenario.one_way_cost_decimal
            if any(value < ZERO for value in weights.values()):
                raise G3ReplayError("corrected settlement path contains negative weight")
            if abs(sum(weights.values(), ZERO) - ONE) > Decimal("1e-20"):
                raise G3ReplayError("corrected settlement weights do not sum to one")
        navs.append(nav)
        returns.append(nav / before - ONE if ordinal else ZERO)
        previous = current
    return {
        "scenario_id": "base-fixed-events-corrected-zero-return-redemption-settlement-cash",
        "redemption_settlement_common_sessions": redemption_settlement_common_sessions,
        "signal-and-fill-sessions": "identical-to-base-replay",
        "risky-target-rule": "frozen-posttrade-targets-applied-to-current-cash-delayed-path",
        "metrics": performance_metrics(tuple(navs), tuple(returns), sessions),
        "ending_weights": {key: format(value, "f") for key, value in weights.items()},
        "settlement_event_count": len(events),
        "settlement_events": _json_ready(events),
        "authority": "fixed-event-settlement-sensitivity-not-a-new-signal-backtest",
    }


def _historical_signal(
    decision_session: str,
    month_index: int,
    month_ends: Sequence[str],
    levels: Mapping[str, Mapping[str, Decimal]],
    valuations: Mapping[str, Sequence[tuple[str, Decimal, Decimal]]],
) -> G6Signal:
    momentum = {
        item: (
            levels[item][decision_session] / levels[item][month_ends[month_index - 10]] - ONE
            if month_index >= 10
            else None
        )
        for item in EQUITY_SLEEVES
    }
    scores: dict[str, Decimal | None] = {}
    score_sessions: dict[str, str | None] = {}
    for item in EQUITY_SLEEVES:
        score, session = _valuation_before(
            valuations[item], decision_session=decision_session, cutoff_session=None
        )
        scores[item] = score
        score_sessions[item] = session
    return G6Signal(
        decision_session=decision_session,
        momentum_10m=tuple((item, momentum[item]) for item in EQUITY_SLEEVES),
        valuation_scores=tuple((item, scores[item]) for item in EQUITY_SLEEVES),
        valuation_sessions=tuple((item, score_sessions[item]) for item in EQUITY_SLEEVES),
    )


def _current_signal(
    decision_session: str,
    signal_session: str,
    month_ends: Sequence[str],
    levels: Mapping[str, Mapping[str, Decimal]],
    valuations: Mapping[str, Sequence[tuple[str, Decimal, Decimal]]],
    *,
    valuation_cutoff_session: str,
) -> G6Signal:
    prior = month_ends[-11]
    momentum = {
        item: levels[item][signal_session] / levels[item][prior] - ONE for item in EQUITY_SLEEVES
    }
    scores: dict[str, Decimal | None] = {}
    score_sessions: dict[str, str | None] = {}
    for item in EQUITY_SLEEVES:
        score, session = _valuation_before(
            valuations[item],
            decision_session=decision_session,
            cutoff_session=valuation_cutoff_session,
        )
        scores[item] = score
        score_sessions[item] = session
    return G6Signal(
        decision_session=decision_session,
        momentum_10m=tuple((item, momentum[item]) for item in EQUITY_SLEEVES),
        valuation_scores=tuple((item, scores[item]) for item in EQUITY_SLEEVES),
        valuation_sessions=tuple((item, score_sessions[item]) for item in EQUITY_SLEEVES),
    )
