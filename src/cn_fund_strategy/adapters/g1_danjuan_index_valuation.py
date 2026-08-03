from __future__ import annotations

from cn_fund_strategy.adapters.danjuan_index_valuation import (
    DanjuanIndexValuationError,
    DanjuanIndexValuationSeries,
    _parse_metric,
)


G1_ALLOWED_INDEX_CODES = frozenset(
    {
        "HKHSI",
        "NDX",
        "SH000300",
        "SH000905",
        "SP500",
        "SZ399006",
    }
)


def parse_g1_danjuan_index_valuation(
    pe_content: bytes | str,
    pb_content: bytes | str,
    *,
    expected_index_code: str,
) -> DanjuanIndexValuationSeries:
    """Parse the bounded G1 global-equity identities without widening V1.

    The original adapter is hash-bound by older strategy contracts.  G1 uses a
    separate identity allowlist while deliberately reusing the already-tested
    strict metric parser and immutable value objects.
    """
    code = str(expected_index_code).strip().upper()
    if code not in G1_ALLOWED_INDEX_CODES:
        raise DanjuanIndexValuationError(
            f"unsupported G1 index_code {expected_index_code!r}; expected one of "
            f"{sorted(G1_ALLOWED_INDEX_CODES)!r}"
        )
    return DanjuanIndexValuationSeries(
        index_code=code,
        pe_field="pe",
        pb_field="pb",
        pe_observations=_parse_metric(pe_content, metric="pe"),
        pb_observations=_parse_metric(pb_content, metric="pb"),
    )
