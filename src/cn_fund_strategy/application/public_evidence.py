"""Independent verification of the public G6 accounting-ledger snapshot.

This module intentionally reads an explicitly supplied research artifact.  It is
not a production data adapter and has no account, credential, or order capability.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping


ZERO = Decimal("0")
ONE = Decimal("1")
TRADING_SESSIONS_PER_YEAR = Decimal("252")
METRIC_TOLERANCE = Decimal("1e-36")


class PublicEvidenceError(ValueError):
    """Raised when a public evidence artifact is malformed or inconsistent."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PublicEvidenceError(message)


def _decimal(value: object, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # pragma: no cover - defensive input boundary
        raise PublicEvidenceError(f"invalid decimal for {label}: {value!r}") from exc
    _require(result.is_finite(), f"non-finite decimal for {label}")
    return result


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublicEvidenceError(f"cannot read {label}: {exc}") from exc
    _require(isinstance(value, dict), f"{label} must be a JSON object")
    return value


def recalculate_ledger(ledger: Mapping[str, object]) -> dict[str, object]:
    """Recalculate material accounting and risk metrics from daily rows."""

    with localcontext() as context:
        context.prec = 60
        return _recalculate_ledger_high_precision(ledger)


def _recalculate_ledger_high_precision(
    ledger: Mapping[str, object],
) -> dict[str, object]:
    """Implementation executed inside a private high-precision Decimal context."""

    _require(
        ledger.get("schema_version") == "g6-standard-daily-accounting-ledger-v1",
        "unsupported ledger schema",
    )
    rows = ledger.get("rows")
    _require(isinstance(rows, list) and len(rows) >= 2, "ledger rows are missing")
    _require(ledger.get("row_count") == len(rows), "ledger row_count mismatch")

    high_water = ONE
    maximum_drawdown = ZERO
    maximum_extreme_path_overlay = ZERO
    maximum_extreme_path_overlay_session = ""
    maximum_preaction_extreme_static_loss = ZERO
    maximum_preaction_extreme_static_loss_session = ""
    maximum_postaction_extreme_static_loss = ZERO
    maximum_postaction_extreme_static_loss_session = ""
    maximum_weight_sum_residual = ZERO
    negative_weight_count = 0
    maximum_gross_weight = ZERO
    maximum_gross_weight_session = ""
    previous_session = ""
    ending_nav = ONE

    for ordinal, raw_row in enumerate(rows):
        _require(isinstance(raw_row, Mapping), f"row {ordinal} is not an object")
        session = str(raw_row.get("session") or "")
        _require(len(session) == 10, f"row {ordinal} session is invalid")
        if previous_session:
            _require(previous_session < session, "sessions are not strictly increasing")
        previous_session = session

        nav = _decimal(raw_row.get("nav_decimal"), f"row {ordinal} nav")
        _require(nav > ZERO, f"row {ordinal} NAV is not positive")
        ending_nav = nav
        high_water = max(high_water, nav)
        maximum_drawdown = max(maximum_drawdown, ONE - nav / high_water)

        preaction_loss = _decimal(
            raw_row.get("preaction_extreme_static_loss_decimal"),
            f"row {ordinal} preaction stress",
        )
        postaction_loss = _decimal(
            raw_row.get("postaction_extreme_static_loss_decimal"),
            f"row {ordinal} postaction stress",
        )
        _require(
            ZERO <= preaction_loss <= ONE and ZERO <= postaction_loss <= ONE,
            f"row {ordinal} stress loss is outside [0, 1]",
        )
        if preaction_loss > maximum_preaction_extreme_static_loss:
            maximum_preaction_extreme_static_loss = preaction_loss
            maximum_preaction_extreme_static_loss_session = session
        if postaction_loss > maximum_postaction_extreme_static_loss:
            maximum_postaction_extreme_static_loss = postaction_loss
            maximum_postaction_extreme_static_loss_session = session

        path_overlay = ONE - nav * (ONE - postaction_loss) / high_water
        if path_overlay > maximum_extreme_path_overlay:
            maximum_extreme_path_overlay = path_overlay
            maximum_extreme_path_overlay_session = session

        raw_weights = raw_row.get("weights")
        _require(isinstance(raw_weights, Mapping) and raw_weights, f"row {ordinal} weights missing")
        weights = tuple(
            _decimal(value, f"row {ordinal} weight {key}")
            for key, value in raw_weights.items()
        )
        negative_weight_count += sum(value < ZERO for value in weights)
        gross_weight = sum(weights, ZERO)
        maximum_weight_sum_residual = max(
            maximum_weight_sum_residual,
            abs(gross_weight - ONE),
        )
        if gross_weight > maximum_gross_weight:
            maximum_gross_weight = gross_weight
            maximum_gross_weight_session = session

    periods = Decimal(len(rows) - 1)
    years = periods / TRADING_SESSIONS_PER_YEAR
    net_cagr = ending_nav ** (ONE / years) - ONE

    return {
        "row_count": len(rows),
        "start_session": str(rows[0]["session"]),
        "end_session": str(rows[-1]["session"]),
        "years_252_decimal": format(years, "f"),
        "ending_nav_decimal": format(ending_nav, "f"),
        "net_cagr_decimal": format(net_cagr, "f"),
        "maximum_drawdown_decimal": format(maximum_drawdown, "f"),
        "maximum_preaction_extreme_static_loss_decimal": format(
            maximum_preaction_extreme_static_loss, "f"
        ),
        "maximum_preaction_extreme_static_loss_session": (
            maximum_preaction_extreme_static_loss_session
        ),
        "maximum_postaction_extreme_static_loss_decimal": format(
            maximum_postaction_extreme_static_loss, "f"
        ),
        "maximum_postaction_extreme_static_loss_session": (
            maximum_postaction_extreme_static_loss_session
        ),
        "maximum_extreme_path_overlay_drawdown_decimal": format(
            maximum_extreme_path_overlay, "f"
        ),
        "maximum_extreme_path_overlay_drawdown_session": (
            maximum_extreme_path_overlay_session
        ),
        "maximum_weight_sum_residual_decimal": format(
            maximum_weight_sum_residual, "f"
        ),
        "negative_weight_count": negative_weight_count,
        "maximum_gross_weight_decimal": format(maximum_gross_weight, "f"),
        "maximum_gross_weight_session": maximum_gross_weight_session,
    }


def verify_public_evidence(ledger_path: Path, summary_path: Path) -> dict[str, object]:
    ledger = _load_object(ledger_path, "ledger")
    summary = _load_object(summary_path, "summary")
    _require(
        summary.get("schema_version") == "cn-fund-public-evidence-summary-v1",
        "unsupported summary schema",
    )
    binding = summary.get("ledger_binding")
    _require(isinstance(binding, Mapping), "summary ledger binding missing")
    actual_hash = sha256_file(ledger_path)
    _require(actual_hash == binding.get("sha256"), "ledger SHA-256 mismatch")

    actual = recalculate_ledger(ledger)
    expected = summary.get("ledger_recalculation_targets")
    _require(isinstance(expected, Mapping), "summary recalculation targets missing")
    exact_fields = (
        "row_count",
        "start_session",
        "end_session",
        "maximum_preaction_extreme_static_loss_session",
        "maximum_postaction_extreme_static_loss_session",
        "maximum_extreme_path_overlay_drawdown_session",
        "negative_weight_count",
        "maximum_gross_weight_session",
    )
    decimal_fields = tuple(key for key in expected if key not in exact_fields)
    comparisons: dict[str, bool] = {}
    for field in exact_fields:
        comparisons[field] = actual.get(field) == expected.get(field)
    for field in decimal_fields:
        comparisons[field] = abs(
            _decimal(actual.get(field), f"actual {field}")
            - _decimal(expected.get(field), f"expected {field}")
        ) <= METRIC_TOLERANCE
    _require(all(comparisons.values()), "one or more ledger metrics do not match")
    return {
        "schema_version": "cn-fund-public-evidence-verification-v1",
        "ledger_sha256": actual_hash,
        "all_checks_passed": True,
        "comparisons": comparisons,
        "recalculated": actual,
        "authority": "research-evidence-verification-only-no-trading-authority",
    }


__all__ = [
    "PublicEvidenceError",
    "recalculate_ledger",
    "sha256_file",
    "verify_public_evidence",
]
