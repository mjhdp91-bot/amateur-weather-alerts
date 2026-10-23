"""Shared helpers for the local Danville GRIB guidance pipeline.

This module deliberately uses the Python standard library for networking and
filesystem work.  Model-specific decoders are imported lazily by the stages
that need them, keeping dry-run and discovery modes lightweight.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDANCE_ROOT = REPO_ROOT / "grib-data"
PROTECTED_OUTPUT_NAMES = frozenset(
    {
        "forecast.json",
        "forecast-history.json",
        "forecast-verifications.json",
        "forecast-accuracy.json",
        "index.html",
        "forecast.html",
    }
)


class GuidanceError(RuntimeError):
    """Base exception for a recoverable guidance-pipeline failure."""


class DownloadError(GuidanceError):
    """Raised when an HTTP resource cannot be retrieved reliably."""


class IntegrityError(GuidanceError):
    """Raised when downloaded content is not the expected product."""


@dataclass(frozen=True)
class DownloadResult:
    """Metadata returned after an atomic download succeeds."""

    path: Path
    byte_size: int
    sha256: str
    content_type: str | None


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp and require/normalize a timezone."""

    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp must include a UTC offset: {value!r}")
    return parsed.astimezone(UTC)


def iso_z(value: datetime) -> str:
    """Format a datetime as second-precision UTC ISO-8601."""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def cycle_id(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%HZ")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def atomic_write_json(path: Path, value: Any) -> None:
    """Write JSON through a sibling temporary file and atomically replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def repo_relative(path: Path, repo_root: Path = REPO_ROOT) -> str:
    resolved = path.resolve()
    root = repo_root.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(f"Path is outside the repository: {resolved}") from error
    return relative.as_posix()


def resolve_repo_path(value: str | Path, repo_root: Path = REPO_ROOT) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    resolved = candidate.resolve()
    root = repo_root.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(f"Refusing path outside repository: {resolved}") from error
    return resolved


def resolve_guidance_output_path(
    value: str | Path,
    *,
    repo_root: Path = REPO_ROOT,
    guidance_root: Path | None = None,
) -> Path:
    """Resolve a derived-output file and confine it to the ignored GRIB tree."""

    root = repo_root.resolve()
    allowed_root = (guidance_root or (root / "grib-data")).resolve()
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    if resolved.name.casefold() in PROTECTED_OUTPUT_NAMES:
        raise ValueError(f"Refusing protected output path: {resolved}")
    if resolved == allowed_root:
        raise ValueError(f"Guidance output must be a file beneath {allowed_root}")
    try:
        resolved.relative_to(allowed_root)
    except ValueError as error:
        raise ValueError(
            f"Guidance output must remain beneath {allowed_root}: {resolved}"
        ) from error
    return resolved


def resolve_guidance_root_path(
    value: str | Path,
    *,
    repo_root: Path = REPO_ROOT,
    guidance_root: Path | None = None,
) -> Path:
    """Resolve a runtime-data directory confined to the ignored GRIB tree."""

    root = repo_root.resolve()
    allowed_root = (guidance_root or (root / "grib-data")).resolve()
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(allowed_root)
    except ValueError as error:
        raise ValueError(
            f"GRIB runtime data must remain beneath {allowed_root}: {resolved}"
        ) from error
    return resolved


def candidate_cycles(
    reference_time: datetime,
    cycle_hours: Iterable[int],
    count: int,
) -> list[datetime]:
    """Return newest-to-oldest nominal cycles at or before reference time."""

    if count < 1:
        return []
    hours = sorted({int(hour) for hour in cycle_hours}, reverse=True)
    if not hours or any(hour < 0 or hour > 23 for hour in hours):
        raise ValueError("Cycle hours must be integers from 0 through 23")
    reference = reference_time.astimezone(UTC)
    candidates: list[datetime] = []
    day = reference.date()
    while len(candidates) < count:
        for hour in hours:
            candidate = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
            if candidate <= reference:
                candidates.append(candidate)
                if len(candidates) >= count:
                    break
        day -= timedelta(days=1)
    return sorted(candidates, reverse=True)


def maximum_lead(model: Mapping[str, Any], cycle: datetime) -> int:
    extended = model.get("extendedCycles")
    standard = model.get("standardMaximumLeadHours")
    if extended is not None and standard is not None:
        if cycle.hour not in {int(value) for value in extended}:
            return int(standard)
    return int(model["maximumLeadHours"])


def available_leads(model: Mapping[str, Any], max_lead: int) -> list[int]:
    """Expand model output cadence segments into an ordered lead list."""

    output: set[int] = set()
    for segment in model.get("availability", []):
        through = min(int(segment["through"]), max_lead)
        every = int(segment["every"])
        if every < 1:
            raise ValueError("Availability cadence must be positive")
        output.update(range(0, through + 1, every))
    if not output:
        output.update(range(0, max_lead + 1))
    return sorted(output)


def select_leads(
    model: Mapping[str, Any],
    cycle: datetime,
    issued_at: datetime,
    valid_through: datetime,
) -> tuple[list[int], bool]:
    """Select available forecast steps without pretending partial models cover 72 h."""

    max_lead = maximum_lead(model, cycle)
    available = available_leads(model, max_lead)
    sampling = model.get("sampling", {})
    base_interval = int(sampling.get("baseIntervalHours", 1))
    fine_interval = int(sampling.get("fineIntervalHours", base_interval))
    fine_window = float(sampling.get("fineWindowHours", 0))
    earliest = issued_at - timedelta(hours=max(base_interval, 1))
    coverage_end = cycle + timedelta(hours=available[-1]) if available else cycle
    partial = coverage_end < valid_through
    if partial:
        selection_end = valid_through
    else:
        endpoint_lead = next(
            lead
            for lead in available
            if cycle + timedelta(hours=lead) >= valid_through
        )
        selection_end = cycle + timedelta(hours=endpoint_lead)
    usable = [
        lead
        for lead in available
        if cycle + timedelta(hours=lead) >= earliest
        and cycle + timedelta(hours=lead) <= selection_end
    ]
    selected: list[int] = []
    for lead in usable:
        valid_time = cycle + timedelta(hours=lead)
        hours_after_issue = (valid_time - issued_at).total_seconds() / 3600
        interval = fine_interval if hours_after_issue <= fine_window else base_interval
        if lead % max(interval, 1) == 0:
            selected.append(lead)
    if usable:
        selected.extend((usable[0], usable[-1]))
    selected = sorted(set(selected))
    return selected, partial


def safe_rmtree(path: Path, allowed_root: Path) -> None:
    """Remove only a resolved descendant of the explicitly supplied root."""

    resolved = path.resolve()
    root = allowed_root.resolve()
    if resolved == root:
        raise ValueError(f"Refusing to remove root directory {root}")
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(f"Refusing to remove path outside {root}: {resolved}") from error
    if resolved.exists():
        shutil.rmtree(resolved)


class HttpClient:
    """Small retrying HTTP client with global NOMADS Grib Filter pacing."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float = 90,
        attempts: int = 3,
        backoff_seconds: float = 3,
        nomads_minimum_interval_seconds: float = 10,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.attempts = attempts
        self.backoff_seconds = backoff_seconds
        self.nomads_minimum_interval_seconds = nomads_minimum_interval_seconds
        self._last_nomads_filter_request = 0.0

    def _pace(self, url: str) -> None:
        if "nomads.ncep.noaa.gov/cgi-bin/filter_" not in url:
            return
        elapsed = time.monotonic() - self._last_nomads_filter_request
        remaining = self.nomads_minimum_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_nomads_filter_request = time.monotonic()

    def _open(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        request_headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(url, headers=request_headers)
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                self._pace(url)
                return urllib.request.urlopen(request, timeout=self.timeout_seconds)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
                last_error = error
                if isinstance(error, urllib.error.HTTPError) and error.code in {400, 401, 403, 404}:
                    break
                if attempt < self.attempts:
                    delay = self.backoff_seconds * (2 ** (attempt - 1))
                    delay += random.uniform(0, min(delay / 4, 1))
                    time.sleep(delay)
        raise DownloadError(f"Unable to retrieve {url}: {last_error}") from last_error

    def get_text(self, url: str, *, limit_bytes: int = 16 * 1024 * 1024) -> str:
        with self._open(url) as response:
            payload = response.read(limit_bytes + 1)
            if len(payload) > limit_bytes:
                raise DownloadError(f"Text response exceeded {limit_bytes} bytes: {url}")
            charset = response.headers.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset)
        except UnicodeDecodeError as error:
            raise IntegrityError(f"Response is not decodable text: {url}") from error

    def download(self, url: str, target: Path) -> DownloadResult:
        """Stream a URL to a sibling part file and atomically promote it."""

        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
        digest = hashlib.sha256()
        byte_size = 0
        content_type: str | None = None
        declared_length: int | None = None
        try:
            with self._open(url) as response, part.open("wb") as output:
                content_type = response.headers.get("Content-Type")
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except (TypeError, ValueError) as error:
                        raise IntegrityError(
                            f"Invalid Content-Length from {url}: {content_length!r}"
                        ) from error
                    if declared_length < 0:
                        raise IntegrityError(
                            f"Invalid Content-Length from {url}: {content_length!r}"
                        )
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    byte_size += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            if byte_size == 0:
                raise IntegrityError(f"Empty response from {url}")
            if declared_length is not None and byte_size != declared_length:
                raise IntegrityError(
                    f"Incomplete response from {url}: received {byte_size} bytes; "
                    f"Content-Length declared {declared_length}"
                )
            os.replace(part, target)
            return DownloadResult(
                path=target,
                byte_size=byte_size,
                sha256=digest.hexdigest(),
                content_type=content_type,
            )
        finally:
            part.unlink(missing_ok=True)
