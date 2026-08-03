from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from html import unescape
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen


SSE_CLOSURE_URL = "https://www.sse.com.cn/disclosure/dealinstruc/closed/"


class SSEClosureCalendarError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SSEClosureCalendarCapture:
    provider: str
    source_url: str
    calendar_year: int
    received_at: str
    raw_path: str
    raw_sha256: str
    manifest_path: str


def _received_at(value: str) -> str:
    raw = str(value).strip()
    if not raw.endswith("Z"):
        raise SSEClosureCalendarError("received_at must end in Z")
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError as exc:
        raise SSEClosureCalendarError(f"invalid received_at: {value!r}") from exc
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_immutable(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise SSEClosureCalendarError(
                f"capture target {path} exists with different bytes; use a new revision"
            )
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(body)
    temporary.replace(path)


def _plain_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", "", unescape(without_tags))


def parse_sse_closed_dates(content: bytes | str, *, calendar_year: int) -> tuple[str, ...]:
    if not 2000 <= calendar_year <= 2100:
        raise SSEClosureCalendarError("calendar_year outside [2000, 2100]")
    if isinstance(content, bytes):
        try:
            html = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SSEClosureCalendarError("SSE page is not UTF-8") from exc
    else:
        html = content.lstrip("\ufeff")
    marker = re.search(rf"<strong>\s*{calendar_year}年休市安排\s*</strong>", html)
    if marker is None:
        raise SSEClosureCalendarError(f"SSE page lacks {calendar_year} closure heading")
    table_match = re.search(r"<table\b[^>]*>(.*?)</table>", html[marker.end() :], re.S | re.I)
    if table_match is None:
        raise SSEClosureCalendarError("SSE closure table missing")
    rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", table_match.group(1), re.S | re.I)
    if not rows:
        raise SSEClosureCalendarError("SSE closure table has no rows")
    closed: set[date] = set()
    range_pattern = re.compile(
        r"(?P<start_month>\d{1,2})月(?P<start_day>\d{1,2})日"
        r"(?:（[^）]+）)?至"
        r"(?:(?P<end_month>\d{1,2})月)?(?P<end_day>\d{1,2})日"
        r"(?:（[^）]+）)?休市"
    )
    for row in rows:
        text = _plain_text(row)
        match = range_pattern.search(text)
        if match is None:
            raise SSEClosureCalendarError(f"cannot parse SSE closure row: {text}")
        start_month = int(match.group("start_month"))
        end_month = int(match.group("end_month") or start_month)
        start = date(calendar_year, start_month, int(match.group("start_day")))
        end = date(calendar_year, end_month, int(match.group("end_day")))
        if end < start or (end - start).days > 31:
            raise SSEClosureCalendarError(f"invalid SSE closure range: {text}")
        current = start
        while current <= end:
            closed.add(current)
            current += timedelta(days=1)
    return tuple(item.isoformat() for item in sorted(closed))


def trading_sessions(
    *,
    start: str,
    end: str,
    closed_dates: tuple[str, ...],
) -> tuple[str, ...]:
    try:
        first = date.fromisoformat(start)
        last = date.fromisoformat(end)
        closed = {date.fromisoformat(item) for item in closed_dates}
    except ValueError as exc:
        raise SSEClosureCalendarError("invalid ISO date") from exc
    if last < first:
        raise SSEClosureCalendarError("calendar end precedes start")
    sessions: list[str] = []
    current = first
    while current <= last:
        if current.weekday() < 5 and current not in closed:
            sessions.append(current.isoformat())
        current += timedelta(days=1)
    return tuple(sessions)


def next_trading_session(day: str, *, closed_dates: tuple[str, ...]) -> str:
    try:
        current = date.fromisoformat(day) + timedelta(days=1)
        closed = {date.fromisoformat(item) for item in closed_dates}
    except ValueError as exc:
        raise SSEClosureCalendarError("invalid ISO date") from exc
    for _ in range(40):
        if current.weekday() < 5 and current not in closed:
            return current.isoformat()
        current += timedelta(days=1)
    raise SSEClosureCalendarError("no next session within 40 calendar days")


def capture_sse_closure_calendar(
    output_directory: Path,
    *,
    calendar_year: int,
    received_at: str,
    timeout_seconds: int = 30,
) -> SSEClosureCalendarCapture:
    received = _received_at(received_at)
    if not 1 <= timeout_seconds <= 120:
        raise SSEClosureCalendarError("timeout_seconds must be in [1, 120]")
    request = Request(
        SSE_CLOSURE_URL,
        headers={
            "Accept": "text/html,*/*;q=0.8",
            "User-Agent": "cn-fund-strategy/0.1 local-research",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise SSEClosureCalendarError(f"SSE HTTP status {response.status}")
            body = response.read()
    except SSEClosureCalendarError:
        raise
    except OSError as exc:
        raise SSEClosureCalendarError(f"SSE closure capture failed: {exc}") from exc
    if not body:
        raise SSEClosureCalendarError("SSE closure response is empty")
    closed_dates = parse_sse_closed_dates(body, calendar_year=calendar_year)
    raw_path = output_directory / "sse-closures.html"
    _write_immutable(raw_path, body)
    manifest_path = output_directory / "capture.manifest.json"
    manifest = {
        "schema_version": "sse-closure-calendar-current-capture-v1",
        "provider": "SHANGHAI_STOCK_EXCHANGE_OFFICIAL",
        "source_url": SSE_CLOSURE_URL,
        "calendar_year": calendar_year,
        "received_at": received,
        "raw_path": str(raw_path.resolve()),
        "raw_sha256": sha256(body).hexdigest(),
        "closed_dates": list(closed_dates),
        "weekends_closed": True,
        "read_only": True,
    }
    manifest_body = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _write_immutable(manifest_path, manifest_body)
    return SSEClosureCalendarCapture(
        provider="SHANGHAI_STOCK_EXCHANGE_OFFICIAL",
        source_url=SSE_CLOSURE_URL,
        calendar_year=calendar_year,
        received_at=received,
        raw_path=str(raw_path.resolve()),
        raw_sha256=sha256(body).hexdigest(),
        manifest_path=str(manifest_path.resolve()),
    )


def capture_as_dict(capture: SSEClosureCalendarCapture) -> dict[str, object]:
    return asdict(capture)


__all__ = [
    "SSEClosureCalendarCapture",
    "SSEClosureCalendarError",
    "capture_as_dict",
    "capture_sse_closure_calendar",
    "next_trading_session",
    "parse_sse_closed_dates",
    "trading_sessions",
]
