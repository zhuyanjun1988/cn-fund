from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


PROFILE_URL_TEMPLATE = (
    "https://fund.eastmoney.com/pingzhongdata/{fund_code}.js"
)
CHINA_TIMEZONE = ZoneInfo("Asia/Shanghai")


class EastmoneyProfileNavError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EastmoneyProfileNavObservation:
    nav_date: str
    unit_nav_decimal: str
    accumulated_nav_decimal: str
    daily_return_pct_decimal: str | None
    cash_distribution_per_share_decimal: str | None
    corporate_action_text: str | None


@dataclass(frozen=True, slots=True)
class EastmoneyAssetAllocationObservation:
    report_date: str
    stock_pct_decimal: str | None
    bond_pct_decimal: str | None
    cash_pct_decimal: str | None
    net_assets_cny_decimal: str | None


@dataclass(frozen=True, slots=True)
class EastmoneyProfileNavSeries:
    fund_code: str
    fund_name: str
    observations: tuple[EastmoneyProfileNavObservation, ...]
    asset_allocations: tuple[
        EastmoneyAssetAllocationObservation,
        ...,
    ] = ()


@dataclass(frozen=True, slots=True)
class EastmoneyProfileNavCapture:
    provider: str
    interface: str
    fund_code: str
    source_url: str
    received_at: str
    raw_path: str
    raw_sha256: str
    raw_bytes: int
    manifest_path: str


def _validate_fund_code(value: str) -> str:
    code = str(value).strip()
    if not re.fullmatch(r"\d{6}", code):
        raise EastmoneyProfileNavError(
            f"fund_code must contain six digits: {value!r}"
        )
    return code


def _utc_timestamp(value: str) -> str:
    raw = str(value).strip()
    if not raw.endswith("Z"):
        raise EastmoneyProfileNavError("received_at must end in Z")
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError as exc:
        raise EastmoneyProfileNavError(
            f"invalid received_at: {value!r}"
        ) from exc
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _extract_js_value(content: str, variable_name: str) -> object:
    match = re.search(
        rf"\bvar\s+{re.escape(variable_name)}\s*=\s*",
        content,
    )
    if match is None:
        raise EastmoneyProfileNavError(
            f"missing JavaScript variable {variable_name}"
        )
    decoder = json.JSONDecoder(parse_float=Decimal, parse_int=int)
    try:
        value, _ = decoder.raw_decode(content, match.end())
    except json.JSONDecodeError as exc:
        raise EastmoneyProfileNavError(
            f"invalid JSON value for {variable_name}: {exc}"
        ) from exc
    return value


def _extract_optional_js_value(
    content: str,
    variable_name: str,
) -> object | None:
    if re.search(
        rf"\bvar\s+{re.escape(variable_name)}\s*=\s*",
        content,
    ) is None:
        return None
    return _extract_js_value(content, variable_name)


def _decimal_text(
    value: object,
    *,
    field_name: str,
    positive: bool,
) -> str:
    if isinstance(value, bool):
        raise EastmoneyProfileNavError(f"{field_name} cannot be boolean")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise EastmoneyProfileNavError(
            f"invalid {field_name}: {value!r}"
        ) from exc
    if not parsed.is_finite() or (positive and parsed <= 0):
        requirement = "finite and positive" if positive else "finite"
        raise EastmoneyProfileNavError(
            f"{field_name} must be {requirement}"
        )
    rendered = format(parsed, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _date_from_timestamp_ms(value: object, *, field_name: str) -> str:
    if isinstance(value, bool):
        raise EastmoneyProfileNavError(f"{field_name} cannot be boolean")
    try:
        timestamp_ms = int(value)
    except (TypeError, ValueError) as exc:
        raise EastmoneyProfileNavError(
            f"invalid {field_name}: {value!r}"
        ) from exc
    if str(timestamp_ms) != str(value) or timestamp_ms <= 0:
        raise EastmoneyProfileNavError(
            f"{field_name} must be a positive integer millisecond timestamp"
        )
    seconds, milliseconds = divmod(timestamp_ms, 1000)
    if milliseconds != 0:
        raise EastmoneyProfileNavError(
            f"{field_name} must resolve to a whole second"
        )
    try:
        current = datetime.fromtimestamp(
            seconds,
            tz=timezone.utc,
        ).astimezone(CHINA_TIMEZONE)
    except (OverflowError, OSError, ValueError) as exc:
        raise EastmoneyProfileNavError(
            f"{field_name} is outside the supported timestamp range"
        ) from exc
    return current.date().isoformat()


def _cash_distribution(action_text: str) -> str | None:
    if not action_text:
        return None
    match = re.fullmatch(
        r"分红：每份派现金(?P<amount>\d+(?:\.\d+)?)元",
        action_text,
    )
    if match is None:
        return None
    return _decimal_text(
        match.group("amount"),
        field_name="cash distribution per share",
        positive=True,
    )


def _optional_nonnegative_decimal(
    value: object,
    *,
    field_name: str,
    multiplier: Decimal = Decimal("1"),
) -> str | None:
    if value is None:
        return None
    rendered = _decimal_text(
        value,
        field_name=field_name,
        positive=False,
    )
    parsed = Decimal(rendered)
    if parsed < 0:
        raise EastmoneyProfileNavError(
            f"{field_name} must be non-negative"
        )
    return _decimal_text(
        parsed * multiplier,
        field_name=field_name,
        positive=False,
    )


def _parse_asset_allocations(
    content: str,
) -> tuple[EastmoneyAssetAllocationObservation, ...]:
    raw = _extract_optional_js_value(content, "Data_assetAllocation")
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise EastmoneyProfileNavError(
            "Data_assetAllocation must be an object"
        )
    categories = raw.get("categories")
    series = raw.get("series")
    if not isinstance(categories, list) or not isinstance(series, list):
        raise EastmoneyProfileNavError(
            "Data_assetAllocation categories and series must be arrays"
        )
    values_by_name: dict[str, list[object]] = {}
    for item in series:
        if not isinstance(item, dict):
            raise EastmoneyProfileNavError(
                "Data_assetAllocation series item must be an object"
            )
        name = item.get("name")
        values = item.get("data")
        if not isinstance(name, str) or not isinstance(values, list):
            raise EastmoneyProfileNavError(
                "Data_assetAllocation series item is incomplete"
            )
        if len(values) != len(categories):
            raise EastmoneyProfileNavError(
                f"Data_assetAllocation {name} length mismatch"
            )
        values_by_name[name] = values
    observations: list[EastmoneyAssetAllocationObservation] = []
    previous_date: str | None = None
    for index, raw_date in enumerate(categories):
        report_date = str(raw_date)
        try:
            date.fromisoformat(report_date)
        except ValueError as exc:
            raise EastmoneyProfileNavError(
                f"invalid asset-allocation date: {report_date!r}"
            ) from exc
        if previous_date is not None and report_date <= previous_date:
            raise EastmoneyProfileNavError(
                "asset-allocation dates must be strictly increasing"
            )

        def value(name: str) -> object | None:
            values = values_by_name.get(name)
            return None if values is None else values[index]

        observations.append(
            EastmoneyAssetAllocationObservation(
                report_date=report_date,
                stock_pct_decimal=_optional_nonnegative_decimal(
                    value("股票占净比"),
                    field_name=f"{report_date} stock allocation",
                ),
                bond_pct_decimal=_optional_nonnegative_decimal(
                    value("债券占净比"),
                    field_name=f"{report_date} bond allocation",
                ),
                cash_pct_decimal=_optional_nonnegative_decimal(
                    value("现金占净比"),
                    field_name=f"{report_date} cash allocation",
                ),
                net_assets_cny_decimal=_optional_nonnegative_decimal(
                    value("净资产"),
                    field_name=f"{report_date} net assets",
                    multiplier=Decimal("100000000"),
                ),
            )
        )
        previous_date = report_date
    return tuple(observations)


def parse_eastmoney_profile_nav(
    content: str | bytes,
    *,
    expected_fund_code: str,
) -> EastmoneyProfileNavSeries:
    code = _validate_fund_code(expected_fund_code)
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise EastmoneyProfileNavError(
                "profile response is not valid UTF-8"
            ) from exc
    else:
        text = content.lstrip("\ufeff")

    actual_code = _extract_js_value(text, "fS_code")
    if actual_code != code:
        raise EastmoneyProfileNavError(
            f"profile fund code mismatch: {actual_code!r} != {code!r}"
        )
    fund_name = _extract_js_value(text, "fS_name")
    if not isinstance(fund_name, str) or not fund_name.strip():
        raise EastmoneyProfileNavError("profile fund name must be non-empty")

    raw_unit = _extract_js_value(text, "Data_netWorthTrend")
    raw_accumulated = _extract_js_value(text, "Data_ACWorthTrend")
    if not isinstance(raw_unit, list) or not raw_unit:
        raise EastmoneyProfileNavError(
            "Data_netWorthTrend must be a non-empty array"
        )
    if not isinstance(raw_accumulated, list) or not raw_accumulated:
        raise EastmoneyProfileNavError(
            "Data_ACWorthTrend must be a non-empty array"
        )

    accumulated_by_date: dict[str, str] = {}
    previous_accumulated_date: str | None = None
    for row_number, raw in enumerate(raw_accumulated, start=1):
        if not isinstance(raw, list) or len(raw) < 2:
            raise EastmoneyProfileNavError(
                f"accumulated NAV row {row_number} is incomplete"
            )
        current_date = _date_from_timestamp_ms(
            raw[0],
            field_name=f"accumulated NAV row {row_number} timestamp",
        )
        if (
            previous_accumulated_date is not None
            and current_date <= previous_accumulated_date
        ):
            raise EastmoneyProfileNavError(
                "accumulated NAV dates must be strictly increasing"
            )
        accumulated_by_date[current_date] = _decimal_text(
            raw[1],
            field_name=f"accumulated NAV row {row_number} value",
            positive=True,
        )
        previous_accumulated_date = current_date

    observations: list[EastmoneyProfileNavObservation] = []
    previous_date: str | None = None
    unit_dates: set[str] = set()
    for row_number, raw in enumerate(raw_unit, start=1):
        if not isinstance(raw, dict):
            raise EastmoneyProfileNavError(
                f"unit NAV row {row_number} must be an object"
            )
        current_date = _date_from_timestamp_ms(
            raw.get("x"),
            field_name=f"unit NAV row {row_number} timestamp",
        )
        if previous_date is not None and current_date <= previous_date:
            raise EastmoneyProfileNavError(
                "unit NAV dates must be strictly increasing"
            )
        if current_date not in accumulated_by_date:
            raise EastmoneyProfileNavError(
                f"missing accumulated NAV for {current_date}"
            )
        action_text = str(raw.get("unitMoney") or "").strip() or None
        daily_return = raw.get("equityReturn")
        observations.append(
            EastmoneyProfileNavObservation(
                nav_date=current_date,
                unit_nav_decimal=_decimal_text(
                    raw.get("y"),
                    field_name=f"unit NAV row {row_number} value",
                    positive=True,
                ),
                accumulated_nav_decimal=accumulated_by_date[current_date],
                daily_return_pct_decimal=(
                    None
                    if daily_return is None
                    else _decimal_text(
                        daily_return,
                        field_name=(
                            f"unit NAV row {row_number} daily return"
                        ),
                        positive=False,
                    )
                ),
                cash_distribution_per_share_decimal=(
                    _cash_distribution(action_text or "")
                ),
                corporate_action_text=action_text,
            )
        )
        unit_dates.add(current_date)
        previous_date = current_date

    extra_accumulated_dates = set(accumulated_by_date) - unit_dates
    if extra_accumulated_dates:
        first_extra = sorted(extra_accumulated_dates)[0]
        raise EastmoneyProfileNavError(
            f"accumulated NAV has no unit NAV counterpart at {first_extra}"
        )
    return EastmoneyProfileNavSeries(
        fund_code=code,
        fund_name=fund_name.strip(),
        observations=tuple(observations),
        asset_allocations=_parse_asset_allocations(text),
    )


def capture_eastmoney_profile_nav(
    output_path: Path,
    *,
    fund_code: str,
    received_at: str,
    timeout_seconds: int = 30,
) -> EastmoneyProfileNavCapture:
    code = _validate_fund_code(fund_code)
    received = _utc_timestamp(received_at)
    if timeout_seconds < 1 or timeout_seconds > 120:
        raise EastmoneyProfileNavError(
            "timeout_seconds must be in [1, 120]"
        )
    url = PROFILE_URL_TEMPLATE.format(fund_code=code)
    request = Request(
        url,
        headers={
            "Accept": "text/javascript,*/*;q=0.8",
            "Referer": "https://fund.eastmoney.com/",
            "User-Agent": "cn-fund-strategy/0.1 local-research",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise EastmoneyProfileNavError(
                    f"profile NAV HTTP status {response.status}"
                )
            body = response.read()
    except EastmoneyProfileNavError:
        raise
    except OSError as exc:
        raise EastmoneyProfileNavError(
            f"profile NAV capture failed: {exc}"
        ) from exc
    if not body:
        raise EastmoneyProfileNavError("profile NAV response is empty")
    parse_eastmoney_profile_nav(body, expected_fund_code=code)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() and output_path.read_bytes() != body:
        raise EastmoneyProfileNavError(
            "capture target exists with different bytes; use a new revision path"
        )
    if not output_path.exists():
        temporary = output_path.with_suffix(output_path.suffix + ".tmp")
        temporary.write_bytes(body)
        temporary.replace(output_path)

    raw_hash = hashlib.sha256(body).hexdigest()
    manifest_path = output_path.with_suffix(
        output_path.suffix + ".manifest.json"
    )
    capture = EastmoneyProfileNavCapture(
        provider="eastmoney_profile_js",
        interface="pingzhongdata Data_netWorthTrend/Data_ACWorthTrend",
        fund_code=code,
        source_url=url,
        received_at=received,
        raw_path=str(output_path),
        raw_sha256=raw_hash,
        raw_bytes=len(body),
        manifest_path=str(manifest_path),
    )
    manifest_body = (
        json.dumps(
            {
                **asdict(capture),
                "read_only": True,
                "point_in_time_status": "conservative-received-only",
                "qualification": "research-limited",
                "schema_version": "eastmoney-profile-nav-capture-v1",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if manifest_path.exists() and manifest_path.read_bytes() != manifest_body:
        raise EastmoneyProfileNavError(
            "capture manifest exists with different bytes"
        )
    if not manifest_path.exists():
        temporary_manifest = manifest_path.with_suffix(
            manifest_path.suffix + ".tmp"
        )
        temporary_manifest.write_bytes(manifest_body)
        temporary_manifest.replace(manifest_path)
    return capture
