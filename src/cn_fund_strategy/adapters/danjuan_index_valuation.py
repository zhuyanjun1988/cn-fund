from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


BASE_URL = "https://danjuanfunds.com"
VALUE_CENTER_URL = (
    "https://danjuanfunds.com/djmodule/value-center?channel=1300100141"
)
ALLOWED_INDEX_CODES = frozenset(
    {"SH000016", "SH000300", "SH000852", "SH000905"}
)
CHINA_TIMEZONE = ZoneInfo("Asia/Shanghai")


class DanjuanIndexValuationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DanjuanMetricObservation:
    business_date: str
    value_decimal: str


@dataclass(frozen=True, slots=True)
class DanjuanIndexValuationSeries:
    index_code: str
    pe_field: str
    pb_field: str
    pe_observations: tuple[DanjuanMetricObservation, ...]
    pb_observations: tuple[DanjuanMetricObservation, ...]


@dataclass(frozen=True, slots=True)
class DanjuanIndexValuationCapture:
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


def _validate_index_code(value: str) -> str:
    code = str(value).strip().upper()
    if code not in ALLOWED_INDEX_CODES:
        raise DanjuanIndexValuationError(
            f"unsupported index_code {value!r}; expected one of "
            f"{sorted(ALLOWED_INDEX_CODES)!r}"
        )
    return code


def _utc_timestamp(value: str) -> str:
    raw = str(value).strip()
    if not raw.endswith("Z"):
        raise DanjuanIndexValuationError("received_at must end in Z")
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError as exc:
        raise DanjuanIndexValuationError(
            f"invalid received_at: {value!r}"
        ) from exc
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _date_from_timestamp_ms(value: object, *, field_name: str) -> str:
    if isinstance(value, bool) or value is None:
        raise DanjuanIndexValuationError(f"invalid {field_name}: {value!r}")
    raw = str(value).strip()
    if not re.fullmatch(r"\d+", raw):
        raise DanjuanIndexValuationError(
            f"{field_name} must be a millisecond timestamp"
        )
    timestamp_ms = int(raw)
    seconds, milliseconds = divmod(timestamp_ms, 1000)
    if timestamp_ms <= 0 or milliseconds != 0:
        raise DanjuanIndexValuationError(
            f"{field_name} must be a positive whole-second timestamp"
        )
    try:
        return datetime.fromtimestamp(
            seconds,
            tz=timezone.utc,
        ).astimezone(CHINA_TIMEZONE).date().isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise DanjuanIndexValuationError(
            f"{field_name} is outside the supported timestamp range"
        ) from exc


def _decimal_text(
    value: object,
    *,
    field_name: str,
) -> str:
    if isinstance(value, bool) or value is None:
        raise DanjuanIndexValuationError(
            f"{field_name} must be numeric"
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DanjuanIndexValuationError(
            f"invalid {field_name}: {value!r}"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise DanjuanIndexValuationError(
            f"{field_name} must be finite and positive"
        )
    rendered = format(parsed, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _parse_metric(
    content: bytes | str,
    *,
    metric: str,
) -> tuple[DanjuanMetricObservation, ...]:
    if metric not in {"pe", "pb"}:
        raise DanjuanIndexValuationError(f"unsupported metric {metric!r}")
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DanjuanIndexValuationError(
                f"Danjuan {metric} response is not valid UTF-8"
            ) from exc
    else:
        text = content.lstrip("\ufeff")
    try:
        payload = json.loads(
            text,
            parse_float=Decimal,
            parse_int=int,
        )
    except json.JSONDecodeError as exc:
        raise DanjuanIndexValuationError(
            f"invalid Danjuan {metric} JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("result_code") != 0:
        raise DanjuanIndexValuationError(
            f"Danjuan {metric} result_code is not zero"
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise DanjuanIndexValuationError(
            f"Danjuan {metric} data must be an object"
        )
    rows = data.get(f"index_eva_{metric}_growths")
    if not isinstance(rows, list) or not rows:
        raise DanjuanIndexValuationError(
            f"Danjuan {metric} history must be a non-empty array"
        )
    observations: dict[str, DanjuanMetricObservation] = {}
    for row_number, raw in enumerate(rows, start=1):
        if not isinstance(raw, dict):
            raise DanjuanIndexValuationError(
                f"Danjuan {metric} row {row_number} must be an object"
            )
        current_date = _date_from_timestamp_ms(
            raw.get("ts"),
            field_name=f"Danjuan {metric} row {row_number} ts",
        )
        if current_date in observations:
            raise DanjuanIndexValuationError(
                f"Danjuan {metric} has duplicate date {current_date}"
            )
        observations[current_date] = DanjuanMetricObservation(
            business_date=current_date,
            value_decimal=_decimal_text(
                raw.get(metric),
                field_name=f"Danjuan {metric} row {row_number} {metric}",
            ),
        )
    return tuple(observations[key] for key in sorted(observations))


def parse_danjuan_index_valuation(
    pe_content: bytes | str,
    pb_content: bytes | str,
    *,
    expected_index_code: str,
) -> DanjuanIndexValuationSeries:
    code = _validate_index_code(expected_index_code)
    return DanjuanIndexValuationSeries(
        index_code=code,
        pe_field="pe",
        pb_field="pb",
        pe_observations=_parse_metric(pe_content, metric="pe"),
        pb_observations=_parse_metric(pb_content, metric="pb"),
    )


def _url(index_code: str, metric: str) -> str:
    return (
        f"{BASE_URL}/djapi/index_eva/{metric}_history/"
        f"{index_code}?day=all"
    )


def _capture(url: str, *, timeout_seconds: int, label: str) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/json,text/plain,*/*",
            "Referer": VALUE_CENTER_URL,
            "User-Agent": "cn-fund-strategy/0.1 local-research",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise DanjuanIndexValuationError(
                    f"{label} HTTP status {response.status}"
                )
            body = response.read()
    except DanjuanIndexValuationError:
        raise
    except OSError as exc:
        raise DanjuanIndexValuationError(
            f"{label} capture failed: {exc}"
        ) from exc
    if not body:
        raise DanjuanIndexValuationError(f"{label} response is empty")
    return body


def _write_immutable(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise DanjuanIndexValuationError(
                f"capture target {path} exists with different bytes; "
                "use a new revision directory"
            )
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(body)
    temporary.replace(path)


def capture_danjuan_index_valuation(
    output_directory: Path,
    *,
    index_code: str,
    received_at: str,
    timeout_seconds: int = 30,
) -> DanjuanIndexValuationCapture:
    code = _validate_index_code(index_code)
    received = _utc_timestamp(received_at)
    if timeout_seconds < 1 or timeout_seconds > 120:
        raise DanjuanIndexValuationError(
            "timeout_seconds must be in [1, 120]"
        )
    pe_url = _url(code, "pe")
    pb_url = _url(code, "pb")
    pe_body = _capture(
        pe_url,
        timeout_seconds=timeout_seconds,
        label="Danjuan PE",
    )
    pb_body = _capture(
        pb_url,
        timeout_seconds=timeout_seconds,
        label="Danjuan PB",
    )
    parse_danjuan_index_valuation(
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
        "schema_version": "danjuan-index-valuation-capture-v1",
        "provider": "DANJUAN_PUBLIC",
        "interface": "pe_history+pb_history",
        "index_code": code,
        "received_at": received,
        "qualification": "research-limited",
        "point_in_time_status": "current-replay-contaminated",
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
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    _write_immutable(manifest_path, manifest_body)
    return DanjuanIndexValuationCapture(
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


def capture_as_dict(
    capture: DanjuanIndexValuationCapture,
) -> dict[str, object]:
    return asdict(capture)
