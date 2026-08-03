from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Callable, Mapping
from urllib.request import HTTPRedirectHandler, Request, build_opener


TIANTIAN_161130_URL = "https://fund.eastmoney.com/161130.html"
TIANTIAN_FUND_ROOT = "https://fund.eastmoney.com/"
_ALLOWED_MEDIA_TYPES = frozenset({"application/xhtml+xml", "text/html"})
_DECLARATION_TO_DECODER = {
    "utf-8": "utf-8",
    "utf8": "utf-8",
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "gb18030": "gb18030",
}
_META_SCAN_BYTES = 4096
_MAX_META_TAG_BYTES = 512
_META_START_PATTERN = re.compile(br"<meta\b", re.IGNORECASE)
_ATTRIBUTE_PATTERN = re.compile(
    r"([^\s=/>]+)(?:\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'=<>`]+)))?",
    re.IGNORECASE,
)
_CONTENT_CHARSET_PATTERN = re.compile(
    r"(?:^|;)\s*charset\s*=\s*([a-z0-9._-]+)\s*(?:;|$)",
    re.IGNORECASE,
)


class TiantianProductStatusHtmlR2Error(ValueError):
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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TiantianProductStatusHtmlR2Error(message)


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


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalize_declaration(value: str) -> str:
    normalized = value.strip().strip('"\'').lower()
    _require(bool(normalized), "charset declaration is empty")
    _require(
        normalized in _DECLARATION_TO_DECODER,
        "charset declaration is unsupported",
    )
    return _DECLARATION_TO_DECODER[normalized]


def _parse_media_and_header_charset(value: str) -> tuple[str, tuple[str, ...]]:
    parts = [part.strip() for part in value.split(";")]
    media_type = parts[0].lower()
    _require(media_type in _ALLOWED_MEDIA_TYPES, "Tiantian response is not HTML")
    declarations: list[str] = []
    for part in parts[1:]:
        if part.lower().startswith("charset="):
            declarations.append(_normalize_declaration(part.split("=", 1)[1]))
    _require(
        len(set(declarations)) <= 1,
        "HTTP Content-Type contains conflicting charset declarations",
    )
    return media_type, tuple(declarations)


def _attributes(tag: str) -> Mapping[str, str]:
    _require(tag.lower().startswith("<meta") and tag.endswith(">"), "meta tag shape changed")
    body = tag[5:-1]
    result: dict[str, str] = {}
    for match in _ATTRIBUTE_PATTERN.finditer(body):
        name = match.group(1).lower()
        value = next((item for item in match.groups()[1:] if item is not None), "")
        _require(name not in result, "HTML meta tag contains a duplicate attribute")
        result[name] = value
    return result


def _complete_meta_tags(prefix: bytes) -> tuple[str, ...]:
    tags: list[str] = []
    for match in _META_START_PATTERN.finditer(prefix):
        end = prefix.find(b">", match.start())
        _require(end != -1, "HTML meta tag is incomplete within scan boundary")
        tag_bytes = prefix[match.start() : end + 1]
        _require(
            b"<" not in tag_bytes[1:],
            "HTML meta tag contains nested markup before its closing bracket",
        )
        _require(
            len(tag_bytes) <= _MAX_META_TAG_BYTES,
            "HTML meta tag exceeds byte bound",
        )
        # Latin-1 is used only as a byte-preserving carrier for ASCII tag syntax.
        # It does not select or guess the document's character encoding.
        tags.append(tag_bytes.decode("latin-1"))
    return tuple(tags)


def resolve_tiantian_charset(
    *,
    content_type_header: str,
    payload: bytes,
) -> tuple[str, tuple[str, ...], str, str]:
    media_type, header_declarations = _parse_media_and_header_charset(
        content_type_header
    )
    observations: list[tuple[str, str]] = [
        ("HTTP-Content-Type-charset", declaration)
        for declaration in header_declarations
    ]
    if payload.startswith(b"\xef\xbb\xbf"):
        observations.append(("UTF-8-BOM", "utf-8"))
    _require(
        not payload.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")),
        "response carries an unsupported Unicode BOM",
    )
    prefix = payload[:_META_SCAN_BYTES]
    for tag in _complete_meta_tags(prefix):
        attributes = _attributes(tag)
        direct = attributes.get("charset")
        if direct is not None:
            observations.append(
                ("HTML-meta-charset", _normalize_declaration(direct))
            )
        http_equiv = attributes.get("http-equiv")
        content = attributes.get("content")
        if http_equiv is not None and http_equiv.strip().lower() == "content-type":
            _require(content is not None, "Content-Type meta tag lacks content")
            match = _CONTENT_CHARSET_PATTERN.search(content)
            _require(match is not None, "Content-Type meta tag lacks charset")
            observations.append(
                (
                    "HTML-meta-http-equiv-content-type",
                    _normalize_declaration(match.group(1)),
                )
            )
    _require(bool(observations), "no supported charset declaration found")
    decoders = {decoder for _, decoder in observations}
    _require(len(decoders) == 1, "charset declaration carriers conflict")
    decoder = next(iter(decoders))
    carriers = tuple(sorted({carrier for carrier, _ in observations}))
    evidence = {
        "schema_version": "tiantian-charset-evidence-r2.v1",
        "decoder": decoder,
        "carriers": list(carriers),
    }
    return media_type, carriers, decoder, _payload_hash(evidence)


@dataclass(frozen=True, slots=True)
class TiantianProductHtmlCaptureR2:
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
            "schema_version": "tiantian-product-html-capture-r2.v1",
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
            "credentials_or_personal_device_identifiers": False,
        }


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


def capture_tiantian_161130_html_r2(
    *,
    output_path: Path,
    timeout_seconds: int = 30,
    opener: Callable[..., object] = _open_without_redirects,
    clock: Callable[[], datetime] = _utc_now,
) -> TiantianProductHtmlCaptureR2:
    _require(not output_path.exists(), f"raw output already exists: {output_path}")
    _require(timeout_seconds >= 1, "timeout must be positive")
    request = Request(
        TIANTIAN_161130_URL,
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
        raise TiantianProductStatusHtmlR2Error(
            f"cannot capture Tiantian product HTML R2: {exc}"
        ) from exc
    _require(final_url == TIANTIAN_161130_URL, "Tiantian product URL redirected")
    _require(10_000 <= len(payload) <= 3_000_000, "Tiantian HTML byte size is outside bound")
    _require(b"<html" in payload[:10000].lower(), "Tiantian response lacks HTML root")
    content_type, charset_carriers, charset, charset_evidence_sha256 = (
        resolve_tiantian_charset(
            content_type_header=content_type_header,
            payload=payload,
        )
    )
    try:
        document = payload.decode(charset)
    except UnicodeDecodeError as exc:
        raise TiantianProductStatusHtmlR2Error(
            f"Tiantian response cannot be strictly decoded: {exc}"
        ) from exc
    _require("<html" in document[:10000].lower(), "decoded response lacks HTML root")
    response_validated_at = _aware_utc(clock(), "response validated at")
    _require(
        request_started_at <= response_validated_at,
        "response validation clock predates request",
    )
    _write_raw_durably(output_path, payload)
    raw_durable_at = _aware_utc(clock(), "raw durable at")
    _require(
        response_validated_at <= raw_durable_at,
        "raw durable clock predates response validation",
    )
    return TiantianProductHtmlCaptureR2(
        fund_code="161130",
        source_url=TIANTIAN_161130_URL,
        content_type=content_type,
        charset=charset,
        charset_carriers=charset_carriers,
        charset_evidence_sha256=charset_evidence_sha256,
        byte_size=len(payload),
        raw_path=output_path,
        raw_sha256=_sha256(payload),
        request_started_at=request_started_at,
        response_validated_at=response_validated_at,
        raw_durable_at=raw_durable_at,
        final_url=final_url,
    )
