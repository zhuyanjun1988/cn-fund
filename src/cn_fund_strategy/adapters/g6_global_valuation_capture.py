from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path

from cn_fund_strategy.adapters.danjuan_index_valuation import (
    DanjuanIndexValuationError,
    _capture,
    _url,
    _utc_timestamp,
    _write_immutable,
)
from cn_fund_strategy.adapters.g1_danjuan_index_valuation import (
    G1_ALLOWED_INDEX_CODES,
    parse_g1_danjuan_index_valuation,
)


@dataclass(frozen=True, slots=True)
class G6GlobalValuationCapture:
    provider: str
    interface: str
    index_code: str
    received_at: str
    pe_source_url: str
    pb_source_url: str
    pe_raw_path: str
    pb_raw_path: str
    pe_raw_sha256: str
    pb_raw_sha256: str
    manifest_path: str


def capture_g6_global_valuation(
    output_directory: Path,
    *,
    index_code: str,
    received_at: str,
    timeout_seconds: int = 30,
) -> G6GlobalValuationCapture:
    """Capture the already-frozen G6 valuation identities without widening them.

    The downloaded history is retrospective and therefore is not promoted to a
    point-in-time archive.  The receipt only proves what was observable at this
    capture time; subsequent forward records bind its exact bytes.
    """

    code = str(index_code).strip().upper()
    if code not in G1_ALLOWED_INDEX_CODES:
        raise DanjuanIndexValuationError(
            f"unsupported G6 index_code {index_code!r}; expected one of "
            f"{sorted(G1_ALLOWED_INDEX_CODES)!r}"
        )
    received = _utc_timestamp(received_at)
    if not 1 <= timeout_seconds <= 120:
        raise DanjuanIndexValuationError("timeout_seconds must be in [1, 120]")
    pe_url = _url(code, "pe")
    pb_url = _url(code, "pb")
    pe_body = _capture(pe_url, timeout_seconds=timeout_seconds, label="Danjuan PE")
    pb_body = _capture(pb_url, timeout_seconds=timeout_seconds, label="Danjuan PB")
    parse_g1_danjuan_index_valuation(
        pe_body,
        pb_body,
        expected_index_code=code,
    )
    pe_path = output_directory / "pe.json"
    pb_path = output_directory / "pb.json"
    _write_immutable(pe_path, pe_body)
    _write_immutable(pb_path, pb_body)
    manifest_path = output_directory / "capture.manifest.json"
    manifest = {
        "schema_version": "g6-global-valuation-current-capture-v1",
        "provider": "DANJUAN_PUBLIC",
        "interface": "pe_history+pb_history",
        "index_code": code,
        "received_at": received,
        "qualification": "current-forward-input-research-only",
        "historical_point_in_time_status": "retrospective-history-contaminated",
        "prospective_authority": "new-tail-observation-first-seen-at-received-at-only",
        "read_only": True,
        "artifacts": [
            {
                "metric": "pe",
                "source_url": pe_url,
                "raw_path": str(pe_path.resolve()),
                "raw_sha256": sha256(pe_body).hexdigest(),
                "raw_bytes": len(pe_body),
            },
            {
                "metric": "pb",
                "source_url": pb_url,
                "raw_path": str(pb_path.resolve()),
                "raw_sha256": sha256(pb_body).hexdigest(),
                "raw_bytes": len(pb_body),
            },
        ],
    }
    manifest_body = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _write_immutable(manifest_path, manifest_body)
    return G6GlobalValuationCapture(
        provider="DANJUAN_PUBLIC",
        interface="pe_history+pb_history",
        index_code=code,
        received_at=received,
        pe_source_url=pe_url,
        pb_source_url=pb_url,
        pe_raw_path=str(pe_path.resolve()),
        pb_raw_path=str(pb_path.resolve()),
        pe_raw_sha256=sha256(pe_body).hexdigest(),
        pb_raw_sha256=sha256(pb_body).hexdigest(),
        manifest_path=str(manifest_path.resolve()),
    )


def capture_as_dict(capture: G6GlobalValuationCapture) -> dict[str, object]:
    return asdict(capture)


__all__ = [
    "G6GlobalValuationCapture",
    "capture_as_dict",
    "capture_g6_global_valuation",
]
