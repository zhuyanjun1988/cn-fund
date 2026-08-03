from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
from typing import Callable, Mapping
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cn_fund_strategy.adapters.tiantian_product_status_html_r2 import (
    TiantianProductStatusHtmlR2Error,
    resolve_tiantian_charset,
)


TIANTIAN_FUND_ROOT = "https://fund.eastmoney.com/"
_CODE_PATTERN = re.compile(r"[0-9]{6}")
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_DISCARDED_ELEMENTS = frozenset({"noscript", "script", "style", "svg", "template"})
_NEXT_SECTION_ANCHORS = (
    "申购费率",
    "购买信息",
    "基金档案",
    "基金概况",
)
_FORBIDDEN_STATUS_FIELDS = (
    "单位净值",
    "累计净值",
    "日增长率",
    "近1月",
    "近3月",
    "近6月",
    "近1年",
    "基金规模",
    "管理费",
    "托管费",
    "跟踪误差",
    "收益率",
    "同类排名",
)


class TiantianShareStatusSourceShapeError(ValueError):
    pass


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        file_pointer: object,
        code: int,
        message: str,
        headers: Mapping[str, str],
        new_url: str,
    ) -> None:
        return None


def _open_without_redirects(request: Request, timeout: int) -> object:
    return build_opener(_RejectRedirects()).open(request, timeout=timeout)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TiantianShareStatusSourceShapeError(message)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: object, label: str) -> datetime:
    _require(type(value) is datetime, f"{label} must be datetime")
    parsed = value
    _require(
        parsed.tzinfo is not None and parsed.utcoffset() is not None,
        f"{label} must be timezone-aware",
    )
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return (
        _aware_utc(value, "timestamp")
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _exact_url(fund_code: str) -> str:
    _require(_CODE_PATTERN.fullmatch(fund_code) is not None, "fund code must be six digits")
    return f"{TIANTIAN_FUND_ROOT}{fund_code}.html"


def _write_raw_durably(path: Path, payload: bytes) -> None:
    _require(not path.exists(), f"raw output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    _require(not temporary.exists(), f"temporary output already exists: {temporary}")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


@dataclass(frozen=True, slots=True)
class TiantianShareHtmlCapture:
    fund_code: str
    source_url: str
    content_type: str
    charset: str
    charset_carriers: tuple[str, ...]
    charset_evidence_sha256: str
    byte_size: int
    raw_path: Path
    raw_sha256: str
    request_started_at: datetime
    response_validated_at: datetime
    raw_durable_at: datetime
    final_url: str

    def receipt(self) -> Mapping[str, object]:
        return {
            "schema_version": "tiantian-share-html-source-shape-capture.v1",
            "fund_code": self.fund_code,
            "source_url": self.source_url,
            "HTTP_method": "GET",
            "content_type": self.content_type,
            "charset": self.charset,
            "charset_carriers": list(self.charset_carriers),
            "charset_evidence_sha256": self.charset_evidence_sha256,
            "byte_size": self.byte_size,
            "raw_path": str(self.raw_path),
            "raw_sha256": self.raw_sha256,
            "request_started_at": _timestamp(self.request_started_at),
            "response_validated_at": _timestamp(self.response_validated_at),
            "raw_durable_at": _timestamp(self.raw_durable_at),
            "final_url": self.final_url,
            "redirect_followed": False,
            "credentials_cookie_account_or_personal_device_identifiers": False,
        }


def capture_tiantian_share_html(
    *,
    fund_code: str,
    output_path: Path,
    timeout_seconds: int = 30,
    opener: Callable[..., object] = _open_without_redirects,
    clock: Callable[[], datetime] = _utc_now,
) -> TiantianShareHtmlCapture:
    source_url = _exact_url(fund_code)
    _require(not output_path.exists(), f"raw output already exists: {output_path}")
    _require(timeout_seconds >= 1, "timeout must be positive")
    request = Request(
        source_url,
        method="GET",
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "Referer": TIANTIAN_FUND_ROOT,
            "User-Agent": "Mozilla/5.0 (compatible; CNFundStrategyResearch/2.0)",
        },
    )
    request_started_at = _aware_utc(clock(), "request started at")
    try:
        response_context = opener(request, timeout=timeout_seconds)
        with response_context as response:
            payload = response.read(3_000_001)
            final_url = str(response.geturl())
            content_type_header = str(response.headers.get("Content-Type", ""))
    except (OSError, TimeoutError) as exc:
        raise TiantianShareStatusSourceShapeError(
            f"cannot capture Tiantian exact-share HTML: {exc}"
        ) from exc
    _require(final_url == source_url, "Tiantian exact-share URL redirected")
    _require(10_000 <= len(payload) <= 3_000_000, "Tiantian HTML byte size is outside bound")
    _require(b"<html" in payload[:10000].lower(), "Tiantian response lacks HTML root")
    try:
        content_type, carriers, charset, charset_evidence_sha256 = (
            resolve_tiantian_charset(
                content_type_header=content_type_header,
                payload=payload,
            )
        )
    except TiantianProductStatusHtmlR2Error as exc:
        raise TiantianShareStatusSourceShapeError(str(exc)) from exc
    try:
        document = payload.decode(charset)
    except UnicodeDecodeError as exc:
        raise TiantianShareStatusSourceShapeError(
            f"Tiantian response cannot be strictly decoded: {exc}"
        ) from exc
    _require("<html" in document[:10000].lower(), "decoded response lacks HTML root")
    response_validated_at = _aware_utc(clock(), "response validated at")
    _require(request_started_at <= response_validated_at, "response clock predates request")
    _write_raw_durably(output_path, payload)
    raw_durable_at = _aware_utc(clock(), "raw durable at")
    _require(response_validated_at <= raw_durable_at, "raw durable clock predates response")
    return TiantianShareHtmlCapture(
        fund_code=fund_code,
        source_url=source_url,
        content_type=content_type,
        charset=charset,
        charset_carriers=carriers,
        charset_evidence_sha256=charset_evidence_sha256,
        byte_size=len(payload),
        raw_path=output_path,
        raw_sha256=_sha256(payload),
        request_started_at=request_started_at,
        response_validated_at=response_validated_at,
        raw_durable_at=raw_durable_at,
        final_url=final_url,
    )


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._discard_depth = 0
        self._in_title = False
        self._in_body = False
        self.title_parts: list[str] = []
        self.body_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized = tag.lower()
        if normalized in _DISCARDED_ELEMENTS:
            self._discard_depth += 1
        if normalized == "title" and self._discard_depth == 0:
            self._in_title = True
        if normalized == "body" and self._discard_depth == 0:
            self._in_body = True

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        return None

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized == "title" and self._discard_depth == 0:
            self._in_title = False
        if normalized == "body" and self._discard_depth == 0:
            self._in_body = False
        if normalized in _DISCARDED_ELEMENTS:
            _require(self._discard_depth > 0, "discarded HTML nesting is invalid")
            self._discard_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._discard_depth > 0:
            return
        if self._in_title:
            self.title_parts.append(data)
        if self._in_body:
            self.body_parts.append(data)


def _normalize_whitespace(value: str) -> str:
    return " ".join(value.replace("\u00a0", " ").split())


def _bounded_identity_window(parts: tuple[str, ...], code: str) -> str:
    matching = [index for index, part in enumerate(parts) if code in part]
    _require(bool(matching), "exact fund code is absent from visible body identity region")
    index = matching[0]
    candidate = _normalize_whitespace(
        " ".join(parts[max(0, index - 2) : min(len(parts), index + 3)])
    )
    _require(code in candidate, "bounded identity region lost exact fund code")
    if len(candidate) <= 240:
        return candidate
    position = candidate.index(code)
    start = max(0, position - 117)
    end = min(len(candidate), start + 240)
    start = max(0, end - 240)
    result = candidate[start:end].strip()
    _require(code in result, "truncated identity region lost exact fund code")
    return result


@dataclass(frozen=True, slots=True)
class TiantianShareStatusSourceShapeProjection:
    fund_code: str
    title_owner_text: str
    product_identity_owner_text: str
    status_anchor_count: int
    status_fragment_owner_text: str
    title_sha256: str
    product_identity_sha256: str
    status_fragment_sha256: str
    projection_hash: str

    def _payload(self) -> Mapping[str, object]:
        return {
            "schema_version": "tiantian-share-status-source-shape-projection.v1",
            "fund_code": self.fund_code,
            "title_owner_text": self.title_owner_text,
            "product_identity_owner_text": self.product_identity_owner_text,
            "status_anchor_count": self.status_anchor_count,
            "status_fragment_owner_text": self.status_fragment_owner_text,
            "title_sha256": self.title_sha256,
            "product_identity_sha256": self.product_identity_sha256,
            "status_fragment_sha256": self.status_fragment_sha256,
            "authority": {
                "owner_text_only": True,
                "status_classification": False,
                "numeric_limit_or_capacity_projection": False,
                "legal_identity_share_currency_manager_or_master_inference": False,
                "NAV_price_return_AUM_fee_tracking_popularity_ranking_or_script_state": False,
                "buyability_product_admission_order_or_trading": False,
            },
        }

    def as_record(self) -> Mapping[str, object]:
        return {**self._payload(), "projection_hash": self.projection_hash}


def validate_tiantian_share_status_source_shape_projection(
    projection: TiantianShareStatusSourceShapeProjection,
) -> None:
    _require(
        type(projection) is TiantianShareStatusSourceShapeProjection,
        "projection type changed",
    )
    _require(_CODE_PATTERN.fullmatch(projection.fund_code) is not None, "fund code changed")
    _require(1 <= len(projection.title_owner_text) <= 240, "title length outside bound")
    _require(
        1 <= len(projection.product_identity_owner_text) <= 240,
        "product identity length outside bound",
    )
    _require(
        projection.fund_code in projection.product_identity_owner_text,
        "product identity does not bind exact code",
    )
    _require(projection.status_anchor_count == 1, "status anchor is not unique")
    _require(
        1 <= len(projection.status_fragment_owner_text) <= 240,
        "status fragment length outside bound",
    )
    _require(
        projection.status_fragment_owner_text.startswith("交易状态"),
        "status fragment does not begin with owner anchor",
    )
    _require(
        not any(
            term in projection.status_fragment_owner_text
            for term in _FORBIDDEN_STATUS_FIELDS
        ),
        "status fragment contains a prohibited investment field",
    )
    text_hashes = (
        (projection.title_owner_text, projection.title_sha256),
        (projection.product_identity_owner_text, projection.product_identity_sha256),
        (projection.status_fragment_owner_text, projection.status_fragment_sha256),
    )
    for text, expected_hash in text_hashes:
        _require(
            _HASH_PATTERN.fullmatch(expected_hash) is not None
            and hashlib.sha256(text.encode("utf-8")).hexdigest() == expected_hash,
            "owner-text hash drift",
        )
    _require(
        _HASH_PATTERN.fullmatch(projection.projection_hash) is not None
        and projection.projection_hash == _payload_hash(projection._payload()),
        "projection hash drift",
    )


def project_tiantian_share_status_source_shape(
    payload: bytes,
    *,
    fund_code: str,
    charset: str,
) -> TiantianShareStatusSourceShapeProjection:
    _require(_CODE_PATTERN.fullmatch(fund_code) is not None, "fund code must be six digits")
    _require(charset in {"utf-8", "gb18030"}, "projection charset changed")
    try:
        document = payload.decode(charset)
    except UnicodeDecodeError as exc:
        raise TiantianShareStatusSourceShapeError(
            f"cannot decode Tiantian HTML: {exc}"
        ) from exc
    parser = _VisibleTextParser()
    try:
        parser.feed(document)
        parser.close()
    except (ValueError, AssertionError) as exc:
        raise TiantianShareStatusSourceShapeError(
            f"cannot parse Tiantian HTML: {exc}"
        ) from exc
    _require(parser._discard_depth == 0, "discarded HTML element is unclosed")
    title = _normalize_whitespace(" ".join(parser.title_parts))
    _require(bool(title), "HTML title is empty")
    _require(len(title) <= 240, "HTML title exceeds owner-text bound")
    body_parts = tuple(
        normalized
        for value in parser.body_parts
        if (normalized := _normalize_whitespace(value))
    )
    identity = _bounded_identity_window(body_parts, fund_code)
    visible = _normalize_whitespace(" ".join(body_parts))
    anchor = "交易状态"
    anchor_count = visible.count(anchor)
    _require(anchor_count == 1, "transaction-status anchor is not unique")
    start = visible.index(anchor)
    end = min(len(visible), start + 240)
    for next_anchor in _NEXT_SECTION_ANCHORS:
        position = visible.find(next_anchor, start + len(anchor), end)
        if position != -1:
            end = min(end, position)
    status_fragment = visible[start:end].strip()
    _require(bool(status_fragment), "bounded transaction-status fragment is empty")
    provisional = TiantianShareStatusSourceShapeProjection(
        fund_code=fund_code,
        title_owner_text=title,
        product_identity_owner_text=identity,
        status_anchor_count=anchor_count,
        status_fragment_owner_text=status_fragment,
        title_sha256=hashlib.sha256(title.encode("utf-8")).hexdigest(),
        product_identity_sha256=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        status_fragment_sha256=hashlib.sha256(status_fragment.encode("utf-8")).hexdigest(),
        projection_hash="",
    )
    result = replace(provisional, projection_hash=_payload_hash(provisional._payload()))
    validate_tiantian_share_status_source_shape_projection(result)
    return result
