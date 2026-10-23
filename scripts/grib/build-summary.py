#!/usr/bin/env python3
"""Build a reviewable Danville guidance summary from extracted GRIB records.

The output is advisory data for a human forecast workflow.  This script never
opens, edits, or publishes ``forecast.json``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import repo_relative, resolve_guidance_output_path, sha256_file
from profile_diagnostics import (
    build_profile_consensus,
    build_profile_diagnostics,
    derive_nominal_cadence_hours,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = REPO_ROOT / "grib-data" / "extracted" / "danville-hourly.json"
DEFAULT_OUTPUT = REPO_ROOT / "grib-data" / "summary" / "danville-guidance.json"
DEFAULT_TIME_ZONE = "America/New_York"
OFFICIAL_BENCHMARK_STATIONS = {"KDVK", "KLEX"}
FORECAST_MODEL_NAMES = {
    "hrrr": "HRRR",
    "rap": "RAP",
    "nam": "NAM",
    "nam_conusnest": "NAM 3-km Nest",
    "gfs": "GFS",
    "gefs": "GEFS",
    "ecmwf": "ECMWF IFS",
}

EXPECTED_DAILY_FIELDS = (
    "temperatureF",
    "dewpointF",
    "windSpeedMph",
    "gustMph",
    "precipitationIn",
    "cloudCoverPct",
    "visibilityMi",
)
TEMPERATURE_EXTREMA_INTERVAL_KEYS = {
    "maximumTemperatureF": "maximumTemperatureInterval",
    "minimumTemperatureF": "minimumTemperatureInterval",
}
DAILY_COVERAGE_FIELDS = {
    "maximumWindMph": "windSpeedMph",
    "maximumGustMph": "gustMph",
    "meanCloudCoverPct": "cloudCoverPct",
    "minimumVisibilityMi": "visibilityMi",
}
DAILY_COVERAGE_MAXIMUM_CADENCE_HOURS = 6.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=True, allow_nan=False)
            handle.write("\n")
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def ensure_safe_output(path: Path) -> Path:
    """Resolve a derived file beneath grib-data and reject publishing targets."""

    try:
        return resolve_guidance_output_path(path, repo_root=REPO_ROOT)
    except ValueError as error:
        raise SystemExit(
            f"{error}. GRIB summaries may only write derived files beneath grib-data."
        ) from error


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def finite_values(values: Iterable[Any]) -> list[float]:
    result: list[float] = []
    for value in values:
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            result.append(float(value))
    return result


def round_number(value: float | None, digits: int = 2) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def summary_stats(values: Iterable[Any], digits: int = 2) -> dict[str, Any] | None:
    numbers = finite_values(values)
    if not numbers:
        return None
    return {
        "count": len(numbers),
        "min": round_number(min(numbers), digits),
        "max": round_number(max(numbers), digits),
        "mean": round_number(statistics.fmean(numbers), digits),
        "median": round_number(statistics.median(numbers), digits),
        "spread": round_number(max(numbers) - min(numbers), digits),
    }


def local_date(iso_timestamp: str, zone: ZoneInfo) -> date:
    parsed = parse_time(iso_timestamp)
    if parsed is None:
        raise ValueError(f"Invalid timestamp: {iso_timestamp}")
    return parsed.astimezone(zone).date()


def select_latest_cycles(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    latest: dict[str, datetime] = {}
    for record in records:
        model = str(record.get("model") or "unknown")
        cycle = parse_time(str(record.get("cycle") or ""))
        if cycle and (model not in latest or cycle > latest[model]):
            latest[model] = cycle
    selected: list[dict[str, Any]] = []
    dropped: defaultdict[str, set[str]] = defaultdict(set)
    for record in records:
        model = str(record.get("model") or "unknown")
        cycle = parse_time(str(record.get("cycle") or ""))
        if cycle is None or latest.get(model) == cycle:
            selected.append(record)
        else:
            dropped[model].add(str(record.get("cycle")))
    warnings = [
        f"Ignored older {model} cycle(s): {', '.join(sorted(cycles))}; models are never mixed across cycles."
        for model, cycles in sorted(dropped.items())
    ]
    return selected, warnings


PRECIPITATION_CONFLICT_TOLERANCE_IN = 0.001


def _precipitation_amount(item: dict[str, Any]) -> float | None:
    amount = item.get("precipitationIn")
    if isinstance(amount, dict):
        amount = amount.get("mean")
    if not isinstance(amount, (int, float)) or not math.isfinite(float(amount)):
        return None
    return float(amount)


def _precipitation_interval(item: dict[str, Any]) -> tuple[float, float] | None:
    interval = item.get("precipitationInterval")
    if not isinstance(interval, dict):
        return None
    start = interval.get("startHour")
    end = interval.get("endHour")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return None
    if not math.isfinite(float(start)) or not math.isfinite(float(end)) or end < start:
        return None
    return float(start), float(end)


def _precipitation_candidate(
    start: datetime,
    end: datetime,
    amount: float,
    method: str,
    step_range: Any,
) -> dict[str, Any]:
    return {
        "startTime": start.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "endTime": end.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "amountIn": round_number(max(0.0, amount), 6),
        "method": method,
        "sourceStepRange": step_range,
    }


def add_precip_increments(hourly: list[dict[str, Any]]) -> list[str]:
    """Normalize native and cumulative GRIB windows into exact candidates.

    Redundant one- and three-hour accumulation products are retained as
    alternatives.  A later exact-path step chooses a non-overlapping cover, so
    overlapping source windows cannot be double-counted.
    """

    warnings: list[str] = []
    previous_by_start: dict[float, tuple[float, float, datetime]] = {}
    for item in sorted(hourly, key=lambda value: value["validTime"]):
        item.pop("precipitationQualityFlag", None)
        item["precipitationIncrementCandidates"] = []
        item["precipitationIncrementIn"] = None
        item["precipitationIncrementStartTime"] = None
        item["precipitationIncrementEndTime"] = None
        amount = _precipitation_amount(item)
        valid_time = parse_time(str(item.get("validTime") or ""))
        interval = _precipitation_interval(item)
        warning: str | None = None
        if amount is None:
            continue
        if interval is None or valid_time is None:
            warning = "precipitation interval metadata missing or invalid"
        else:
            start_hour, end_hour = interval
            native_start = valid_time - timedelta(hours=end_hour - start_hour)
            native = _precipitation_candidate(
                native_start,
                valid_time,
                amount,
                "native-accumulation-window",
                item.get("precipitationInterval", {}).get("stepRange"),
            )
            candidates = [native]
            previous = previous_by_start.get(start_hour)
            if previous is not None:
                previous_end, previous_amount, previous_valid_time = previous
                if previous_end < end_hour:
                    difference = amount - previous_amount
                    if difference >= -PRECIPITATION_CONFLICT_TOLERANCE_IN:
                        candidates.insert(
                            0,
                            _precipitation_candidate(
                                previous_valid_time,
                                valid_time,
                                max(0.0, difference),
                                "same-start-cumulative-difference",
                                item.get("precipitationInterval", {}).get("stepRange"),
                            ),
                        )
                    else:
                        warning = (
                            "cumulative precipitation decreased for a shared step start"
                        )
            item["precipitationIncrementCandidates"] = candidates
            primary = candidates[0]
            item["precipitationIncrementIn"] = round_number(primary["amountIn"], 3)
            item["precipitationIncrementStartTime"] = primary["startTime"]
            item["precipitationIncrementEndTime"] = primary["endTime"]
            previous_by_start[start_hour] = (end_hour, amount, valid_time)
        if warning:
            item["precipitationQualityFlag"] = warning
            warnings.append(f"{item['validTime']}: {warning}")
    return warnings


def _item_precipitation_candidates(item: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = item.get("precipitationIncrementCandidates")
    if isinstance(candidates, list) and candidates:
        return [candidate for candidate in candidates if isinstance(candidate, dict)]
    amount = item.get("precipitationIncrementIn")
    start = item.get("precipitationIncrementStartTime")
    end = item.get("precipitationIncrementEndTime")
    if isinstance(amount, (int, float)) and isinstance(start, str) and isinstance(end, str):
        return [
            {
                "startTime": start,
                "endTime": end,
                "amountIn": float(amount),
                "method": "legacy-normalized-increment",
                "sourceStepRange": None,
            }
        ]
    return []


def _qpf_summary(
    items: list[dict[str, Any]],
    period_start: datetime | None,
    period_end: datetime | None,
) -> tuple[float | None, bool, list[str], dict[str, Any] | None]:
    """Return an exact QPF total only when increment intervals cover the period.

    Boundary-crossing zero increments are safe to clip. A positive increment
    crossing a summary boundary cannot be apportioned without inventing an
    hourly distribution, so that candidate is excluded. The total is retained
    only if other candidates still form an exact path across the period.
    """

    precip_items = [item for item in items if _representative(item, "precipitationIn") is not None]
    quality_flags: set[str] = set()
    if period_start is None or period_end is None:
        quality_flags.update(
            str(item["precipitationQualityFlag"])
            for item in precip_items
            if item.get("precipitationQualityFlag")
        )
        complete = bool(precip_items) and not quality_flags and all(
            isinstance(item.get("precipitationIncrementIn"), (int, float))
            for item in precip_items
        )
        values = finite_values(item.get("precipitationIncrementIn") for item in precip_items)
        return (
            round_number(sum(values), 3) if complete else None,
            complete,
            sorted(quality_flags),
            None,
        )

    normalized: list[dict[str, Any]] = []
    positive_boundary_crossing_count = 0
    intersecting_item_count = 0
    for item in precip_items:
        item_candidates = _item_precipitation_candidates(item)
        if not item_candidates:
            quality_flags.add("precipitation increment coverage metadata missing or invalid")
            continue
        item_intersects = False
        for candidate in item_candidates:
            interval_start = parse_time(str(candidate.get("startTime") or ""))
            interval_end = parse_time(str(candidate.get("endTime") or ""))
            increment = candidate.get("amountIn")
            if (
                interval_start is None
                or interval_end is None
                or interval_end <= interval_start
                or not isinstance(increment, (int, float))
                or not math.isfinite(float(increment))
                or float(increment) < -PRECIPITATION_CONFLICT_TOLERANCE_IN
            ):
                quality_flags.add("precipitation increment coverage metadata missing or invalid")
                continue
            if interval_end <= period_start or interval_start >= period_end:
                continue
            item_intersects = True
            clipped_start = max(interval_start, period_start)
            clipped_end = min(interval_end, period_end)
            crosses_boundary = clipped_start != interval_start or clipped_end != interval_end
            if crosses_boundary and abs(float(increment)) > 0.0005:
                positive_boundary_crossing_count += 1
                continue
            normalized.append(
                {
                    **candidate,
                    "start": clipped_start,
                    "end": clipped_end,
                    "amount": max(0.0, float(increment)),
                    "boundaryClipped": crosses_boundary,
                }
            )
        if item_intersects:
            intersecting_item_count += 1
            if item.get("precipitationQualityFlag"):
                quality_flags.add(str(item["precipitationQualityFlag"]))

    # Collapse equivalent candidates and make disagreement for an identical
    # interval a hard failure rather than choosing an arbitrary source window.
    grouped_candidates: defaultdict[tuple[datetime, datetime], list[dict[str, Any]]] = defaultdict(list)
    for candidate in normalized:
        grouped_candidates[(candidate["start"], candidate["end"])].append(candidate)
    candidates: list[dict[str, Any]] = []
    for interval, values in sorted(grouped_candidates.items()):
        amounts = [float(value["amount"]) for value in values]
        if max(amounts) - min(amounts) > PRECIPITATION_CONFLICT_TOLERANCE_IN:
            quality_flags.add("conflicting precipitation increments for an identical interval")
        chosen = min(
            values,
            key=lambda value: (
                0 if value.get("method") == "same-start-cumulative-difference" else 1,
                str(value.get("method") or ""),
            ),
        )
        candidates.append(chosen)

    adjacency: defaultdict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        adjacency[candidate["start"]].append(candidate)
    states: dict[datetime, dict[float, list[dict[str, Any]]]] = {period_start: {0.0: []}}
    timeline = sorted({period_start, period_end, *(candidate["start"] for candidate in candidates), *(candidate["end"] for candidate in candidates)})
    for cursor in timeline:
        if cursor not in states:
            continue
        for candidate in adjacency.get(cursor, []):
            if candidate["end"] > period_end:
                continue
            destination = states.setdefault(candidate["end"], {})
            for total_so_far, path in list(states[cursor].items()):
                new_total = round(total_so_far + float(candidate["amount"]), 6)
                new_path = [*path, candidate]
                existing = destination.get(new_total)
                if existing is None or len(new_path) < len(existing):
                    destination[new_total] = new_path

    complete_paths = states.get(period_end, {})
    selected_path: list[dict[str, Any]] = []
    selected_total: float | None = None
    if complete_paths:
        totals = sorted(complete_paths)
        if totals[-1] - totals[0] > PRECIPITATION_CONFLICT_TOLERANCE_IN:
            quality_flags.add("conflicting exact precipitation paths disagree on total")
        selected_total, selected_path = min(
            complete_paths.items(), key=lambda item: (len(item[1]), item[0])
        )
    else:
        quality_flags.add("no exact non-overlapping precipitation interval path covers summary period")
        if positive_boundary_crossing_count:
            quality_flags.add(
                "positive precipitation interval crosses summary boundary and was not apportioned"
            )

    coverage_intervals = [(candidate["start"], candidate["end"]) for candidate in candidates]
    merged: list[list[datetime]] = []
    for start, end in sorted(coverage_intervals):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    gaps: list[dict[str, Any]] = []
    cursor = period_start
    for start, end in merged:
        if start > cursor:
            gaps.append(
                {
                    "start": cursor.isoformat(timespec="seconds").replace("+00:00", "Z"),
                    "end": start.isoformat(timespec="seconds").replace("+00:00", "Z"),
                    "hours": round_number((start - cursor).total_seconds() / 3600, 2),
                }
            )
        cursor = max(cursor, end)
    if cursor < period_end:
        gaps.append(
            {
                "start": cursor.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "end": period_end.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "hours": round_number((period_end - cursor).total_seconds() / 3600, 2),
            }
        )
    if gaps:
        quality_flags.add("precipitation increments do not cover the complete summary period")

    complete = bool(selected_path) and not quality_flags and not gaps
    selected_intervals = [
        {
            "start": candidate["start"].isoformat(timespec="seconds").replace("+00:00", "Z"),
            "end": candidate["end"].isoformat(timespec="seconds").replace("+00:00", "Z"),
            "amountIn": round_number(float(candidate["amount"]), 6),
            "method": candidate.get("method"),
            "sourceStepRange": candidate.get("sourceStepRange"),
            "boundaryClipped": bool(candidate.get("boundaryClipped")),
        }
        for candidate in selected_path
    ]
    coverage = {
        "requiredStart": period_start.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "requiredEnd": period_end.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "coveredStart": merged[0][0].isoformat(timespec="seconds").replace("+00:00", "Z")
        if merged
        else None,
        "coveredEnd": merged[-1][1].isoformat(timespec="seconds").replace("+00:00", "Z")
        if merged
        else None,
        "gaps": gaps,
        "selectionMethod": "exact-non-overlapping-interval-path",
        "sourceItemCount": intersecting_item_count,
        "candidateIntervalCount": len(candidates),
        "excludedPositiveBoundaryCrossingCount": positive_boundary_crossing_count,
        "selectedIntervalCount": len(selected_intervals),
        "selectedIntervals": selected_intervals,
        "exactPathTotalsIn": [round_number(value, 6) for value in sorted(complete_paths)],
    }
    return (
        round_number(selected_total, 3) if complete and selected_total is not None else None,
        complete,
        sorted(quality_flags),
        coverage,
    )


def hourly_point(record: dict[str, Any], station: str) -> dict[str, Any]:
    raw = record.get("points", {}).get(station, {})
    point = dict(raw) if isinstance(raw, dict) else {}
    point.pop("gridPoint", None)
    return {
        "validTime": record.get("validTime"),
        "leadTimeHours": record.get("leadTimeHours"),
        **point,
    }


def hourly_neighborhood(record: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {
        "validTime": record.get("validTime"),
        "leadTimeHours": record.get("leadTimeHours"),
    }
    neighborhood = record.get("neighborhood", {})
    if isinstance(neighborhood, dict):
        for field, stats in neighborhood.items():
            if isinstance(stats, dict):
                output[field] = stats
    field_metadata = record.get("fieldMetadata", {})
    if isinstance(field_metadata, dict):
        interval_fields = {
            "precipitationIn": "precipitationInterval",
            **TEMPERATURE_EXTREMA_INTERVAL_KEYS,
        }
        for field, interval_key in interval_fields.items():
            metadata = field_metadata.get(field)
            if isinstance(metadata, dict) and field in output:
                output[interval_key] = {
                    key: metadata.get(key)
                    for key in ("stepRange", "startHour", "endHour", "durationHours")
                }
    return output


def _representative(item: dict[str, Any], field: str) -> float | None:
    value = item.get(field)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        mean = value.get("mean")
        if isinstance(mean, (int, float)):
            return float(mean)
    return None


def _first_representative(item: dict[str, Any], fields: Iterable[str]) -> float | None:
    for field in fields:
        value = _representative(item, field)
        if value is not None:
            return value
    return None


FORECAST_CAPE_FIELDS = (
    "forecastCapeJkg",
    "mostUnstableCapeJkg",
    "cape255To0MbJkg",
    "cape180To0MbJkg",
    "surfaceCapeJkg",
)


def _timestamp_eligibility(
    timestamps: Iterable[datetime],
    period_start: datetime | None,
    period_end: datetime | None,
    model_cadence_hours: float | None = None,
) -> dict[str, Any]:
    ordered = sorted(set(timestamps))
    nominal_cadence_hours = derive_nominal_cadence_hours(
        ordered,
        model_cadence_hours,
        DAILY_COVERAGE_MAXIMUM_CADENCE_HOURS,
    )
    maximum_gap_hours = nominal_cadence_hours + 0.01

    def format_time(value: datetime | None) -> str | None:
        return (
            value.isoformat(timespec="seconds").replace("+00:00", "Z")
            if value is not None
            else None
        )

    gaps: list[dict[str, Any]] = []

    def add_gap(start: datetime, end: datetime) -> None:
        hours = (end - start).total_seconds() / 3600
        if hours > maximum_gap_hours:
            gaps.append(
                {
                    "start": format_time(start),
                    "end": format_time(end),
                    "hours": round_number(hours, 2),
                }
            )

    if period_start is not None and period_end is not None:
        if ordered:
            add_gap(period_start, ordered[0])
            for left, right in zip(ordered, ordered[1:]):
                add_gap(left, right)
            add_gap(ordered[-1], period_end)
        elif period_end > period_start:
            gaps.append(
                {
                    "start": format_time(period_start),
                    "end": format_time(period_end),
                    "hours": round_number(
                        (period_end - period_start).total_seconds() / 3600, 2
                    ),
                }
            )

    return {
        "eligible": bool(ordered) and not gaps,
        "requiredStart": format_time(period_start),
        "requiredEnd": format_time(period_end),
        "coveredStart": format_time(ordered[0]) if ordered else None,
        "coveredEnd": format_time(ordered[-1]) if ordered else None,
        "sampleCount": len(ordered),
        "nominalCadenceHours": nominal_cadence_hours,
        "maximumGapHours": maximum_gap_hours,
        "cadenceMethod": "slowest-repeated-field-cadence-capped-at-six-hours",
        "gaps": gaps,
    }


def _instantaneous_field_eligibility(
    items: list[dict[str, Any]],
    field: str,
    period_start: datetime | None,
    period_end: datetime | None,
    model_cadence_hours: float | None = None,
) -> dict[str, Any]:
    """Prove that an instantaneous field represents the full forecast day."""

    timestamps = {
        parsed
        for item in items
        if _representative(item, field) is not None
        if (parsed := parse_time(str(item.get("validTime") or ""))) is not None
        if period_start is None or parsed >= period_start
        if period_end is None or parsed <= period_end
    }
    return _timestamp_eligibility(
        timestamps, period_start, period_end, model_cadence_hours
    )


def _fog_diagnostic_available(item: dict[str, Any]) -> bool:
    visibility = _representative(item, "visibilityMi")
    depression = _representative(item, "dewpointDepressionF")
    temperature = _representative(item, "temperatureF")
    dewpoint = _representative(item, "dewpointF")
    return visibility is not None or depression is not None or (
        temperature is not None and dewpoint is not None
    )


def _predicate_eligibility(
    items: list[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
    period_start: datetime | None,
    period_end: datetime | None,
    model_cadence_hours: float | None = None,
) -> dict[str, Any]:
    timestamps = {
        parsed
        for item in items
        if predicate(item)
        if (parsed := parse_time(str(item.get("validTime") or ""))) is not None
        if period_start is None or parsed >= period_start
        if period_end is None or parsed <= period_end
    }
    return _timestamp_eligibility(
        timestamps, period_start, period_end, model_cadence_hours
    )


def _extrema_interval_bounds(
    item: dict[str, Any], field: str
) -> tuple[datetime, datetime] | None:
    """Return the valid interval for an explicit TMAX/TMIN field.

    GRIB extrema describe an interval ending at (or near) the record valid time,
    not an instantaneous value.  Prefer the encoded step-range endpoints when
    the forecast lead is available; fall back to duration when necessary.
    """

    interval_key = TEMPERATURE_EXTREMA_INTERVAL_KEYS.get(field)
    interval = item.get(interval_key) if interval_key is not None else None
    valid_time = parse_time(str(item.get("validTime") or ""))
    if not isinstance(interval, dict) or valid_time is None:
        return None

    start_hour = interval.get("startHour")
    end_hour = interval.get("endHour")
    lead_hour = item.get("leadTimeHours")
    numeric_step_range = all(
        isinstance(value, (int, float)) and math.isfinite(float(value))
        for value in (start_hour, end_hour, lead_hour)
    )
    if numeric_step_range:
        start_value = float(start_hour)
        end_value = float(end_hour)
        if end_value < start_value:
            return None
        cycle_time = valid_time - timedelta(hours=float(lead_hour))
        return (
            cycle_time + timedelta(hours=start_value),
            cycle_time + timedelta(hours=end_value),
        )

    duration = interval.get("durationHours")
    if not isinstance(duration, (int, float)) or not math.isfinite(float(duration)):
        return None
    duration_value = float(duration)
    if duration_value < 0:
        return None
    return valid_time - timedelta(hours=duration_value), valid_time


def _filter_explicit_extrema(
    items: list[dict[str, Any]],
    field: str,
    *,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    forecast_window_start: datetime | None = None,
    forecast_window_end: datetime | None = None,
) -> tuple[list[float], set[int], dict[str, int]]:
    """Keep explicit extrema only when their full intervals belong to the period."""

    values: list[float] = []
    accepted_item_ids: set[int] = set()
    quality = {
        "candidateCount": 0,
        "acceptedCount": 0,
        "excludedBoundaryCrossingCount": 0,
        "unknownIntervalCount": 0,
        "excludedUnknownIntervalCount": 0,
    }
    lower_bounds = [value for value in (period_start, forecast_window_start) if value]
    upper_bounds = [value for value in (period_end, forecast_window_end) if value]
    require_known_interval = bool(lower_bounds or upper_bounds)

    for item in items:
        value = _representative(item, field)
        if value is None:
            continue
        quality["candidateCount"] += 1
        interval_bounds = _extrema_interval_bounds(item, field)
        if interval_bounds is None:
            quality["unknownIntervalCount"] += 1
            if require_known_interval:
                quality["excludedUnknownIntervalCount"] += 1
                continue
        else:
            interval_start, interval_end = interval_bounds
            crosses_lower_bound = any(interval_start < boundary for boundary in lower_bounds)
            crosses_upper_bound = any(interval_end > boundary for boundary in upper_bounds)
            if crosses_lower_bound or crosses_upper_bound:
                quality["excludedBoundaryCrossingCount"] += 1
                continue
        values.append(value)
        accepted_item_ids.add(id(item))
        quality["acceptedCount"] += 1

    return values, accepted_item_ids, quality


def infer_regime(items: list[dict[str, Any]]) -> str:
    qpf = sum(finite_values(item.get("precipitationIncrementIn") for item in items))
    cape = finite_values(_first_representative(item, FORECAST_CAPE_FIELDS) for item in items)
    clouds = finite_values(_representative(item, "cloudCoverPct") for item in items)
    winds = finite_values(_representative(item, "windSpeedMph") for item in items)
    pressures = finite_values(_representative(item, "seaLevelPressureHpa") for item in items)
    if qpf >= 0.05 and cape and max(cape) >= 500:
        return "convective"
    if qpf >= 0.03:
        return "stratiform-precipitation"
    if clouds and statistics.fmean(clouds) >= 75:
        return "cloudy-low-pressure"
    if pressures and max(pressures) >= 1020 and (not clouds or statistics.fmean(clouds) <= 50):
        return "dry-high-pressure"
    if winds and max(winds) >= 20:
        return "windy-mixed"
    return "mixed-or-unspecified"


def summarize_day(
    day: date,
    items: list[dict[str, Any]],
    is_neighborhood: bool = False,
    *,
    following_night_items: list[dict[str, Any]] | None = None,
    zone: ZoneInfo | None = None,
    forecast_window_start: datetime | None = None,
    forecast_window_end: datetime | None = None,
    model_cadence_hours: float | None = None,
) -> dict[str, Any]:
    base_items = sorted(items, key=lambda item: str(item.get("validTime", "")))
    night_items = following_night_items if following_night_items is not None else base_items
    combined_by_time: dict[str, dict[str, Any]] = {}
    for item in base_items + night_items:
        timestamp = str(item.get("validTime", ""))
        if timestamp:
            combined_by_time[timestamp] = item
    analysis_items = [combined_by_time[key] for key in sorted(combined_by_time)]

    high_interval_start: datetime | None = None
    high_interval_end: datetime | None = None
    high_window_start: datetime | None = None
    high_coverage_target: datetime | None = None
    high_window_end: datetime | None = None
    night_window_start: datetime | None = None
    low_coverage_target: datetime | None = None
    night_window_end: datetime | None = None
    if zone is not None:
        next_day = day + timedelta(days=1)
        high_interval_start = datetime.combine(day, time(0, 0), tzinfo=zone)
        high_interval_end = datetime.combine(next_day, time(0, 0), tzinfo=zone)
        high_window_start = datetime.combine(day, time(12, 0), tzinfo=zone)
        high_coverage_target = datetime.combine(day, time(16, 0), tzinfo=zone)
        high_window_end = datetime.combine(day, time(20, 0), tzinfo=zone)
        night_window_start = datetime.combine(day, time(18, 0), tzinfo=zone)
        low_coverage_target = datetime.combine(next_day, time(6, 0), tzinfo=zone)
        night_window_end = datetime.combine(next_day, time(8, 0), tzinfo=zone)

    low_items = night_items
    if zone is not None:
        low_items = []
        for item in night_items:
            parsed = parse_time(str(item.get("validTime") or ""))
            if parsed is None:
                continue
            local = parsed.astimezone(zone)
            if night_window_start <= local <= night_window_end:
                low_items.append(item)

    temperatures = finite_values(_representative(item, "temperatureF") for item in base_items)
    explicit_highs, accepted_explicit_high_items, high_extrema_quality = _filter_explicit_extrema(
        base_items,
        "maximumTemperatureF",
        period_start=high_interval_start,
        period_end=high_interval_end,
        forecast_window_start=forecast_window_start,
        forecast_window_end=forecast_window_end,
    )
    night_temperatures = finite_values(
        _representative(item, "temperatureF") for item in low_items
    )
    explicit_lows, accepted_explicit_low_items, low_extrema_quality = _filter_explicit_extrema(
        low_items,
        "minimumTemperatureF",
        period_start=night_window_start,
        period_end=night_window_end,
        forecast_window_start=forecast_window_start,
        forecast_window_end=forecast_window_end,
    )
    dewpoints = finite_values(_representative(item, "dewpointF") for item in analysis_items)
    winds = finite_values(_representative(item, "windSpeedMph") for item in base_items)
    gusts = finite_values(_representative(item, "gustMph") for item in base_items)
    visibility = finite_values(_representative(item, "visibilityMi") for item in analysis_items)
    cloud_cover = finite_values(_representative(item, "cloudCoverPct") for item in base_items)
    qpf_period_start: datetime | None = None
    qpf_period_end: datetime | None = None
    if zone is not None and forecast_window_start is not None and forecast_window_end is not None:
        first_forecast_date = forecast_window_start.astimezone(zone).date()
        local_period_start = datetime.combine(day, time(8, 0), tzinfo=zone).astimezone(
            timezone.utc
        )
        following_morning_end = datetime.combine(
            day + timedelta(days=1), time(8, 0), tzinfo=zone
        ).astimezone(timezone.utc)
        qpf_period_start = (
            forecast_window_start
            if day == first_forecast_date
            else max(forecast_window_start, local_period_start)
        )
        qpf_period_end = min(forecast_window_end, following_morning_end)
    qpf_in, qpf_complete, qpf_quality_flags, qpf_coverage = _qpf_summary(
        analysis_items, qpf_period_start, qpf_period_end
    )
    daily_field_eligibility = {
        output_field: _instantaneous_field_eligibility(
            analysis_items,
            source_field,
            qpf_period_start,
            qpf_period_end,
            model_cadence_hours,
        )
        for output_field, source_field in DAILY_COVERAGE_FIELDS.items()
    }
    daily_field_eligibility["fogSignal"] = _predicate_eligibility(
        analysis_items,
        _fog_diagnostic_available,
        qpf_period_start,
        qpf_period_end,
        model_cadence_hours,
    )
    atmospheric_profile = (
        build_profile_diagnostics(
            analysis_items,
            qpf_period_start,
            qpf_period_end,
            include_period_end=qpf_period_end == forecast_window_end,
            model_cadence_hours=model_cadence_hours,
        )
        if qpf_period_start is not None and qpf_period_end is not None
        else None
    )
    precip_rates = finite_values(
        _representative(item, "precipitationRateInPerHour") for item in analysis_items
    )
    capes = finite_values(
        _first_representative(item, FORECAST_CAPE_FIELDS) for item in base_items
    )
    ensemble_temperature_spread = finite_values(
        _representative(item, "temperatureSpreadF") for item in base_items
    )

    high_candidates = explicit_highs + temperatures
    low_candidates = explicit_lows + night_temperatures
    high = max(high_candidates) if high_candidates else None
    low = min(low_candidates) if low_candidates else None

    high_eligible = bool(high_candidates)
    low_eligible = bool(low_candidates)
    high_last_sample: str | None = None
    low_last_sample: str | None = None
    if zone is not None:
        high_samples: list[datetime] = []
        for item in base_items:
            parsed = parse_time(str(item.get("validTime") or ""))
            if parsed is None:
                continue
            local = parsed.astimezone(zone)
            if (
                high_window_start <= local <= high_window_end
                and (
                    _representative(item, "temperatureF") is not None
                    or id(item) in accepted_explicit_high_items
                )
            ):
                high_samples.append(local)
        night_samples: list[datetime] = []
        for item in low_items:
            parsed = parse_time(str(item.get("validTime") or ""))
            if parsed is None:
                continue
            local = parsed.astimezone(zone)
            if (
                night_window_start <= local <= night_window_end
                and (
                    _representative(item, "temperatureF") is not None
                    or id(item) in accepted_explicit_low_items
                )
            ):
                night_samples.append(local)
        high_eligible = bool(high_samples and max(high_samples) >= high_coverage_target)
        low_eligible = bool(night_samples and max(night_samples) >= low_coverage_target)
        if high_samples:
            high_last_sample = max(high_samples).astimezone(timezone.utc).isoformat(
                timespec="minutes"
            ).replace("+00:00", "Z")
        if night_samples:
            low_last_sample = max(night_samples).astimezone(timezone.utc).isoformat(
                timespec="minutes"
            ).replace("+00:00", "Z")

    fog_times: list[str] = []
    wind_times: list[str] = []
    precipitation_times: list[str] = []
    final_period_includes_end = (
        qpf_period_end is not None and qpf_period_end == forecast_window_end
    )
    signal_items: list[dict[str, Any]] = []
    for item in analysis_items:
        parsed = parse_time(str(item.get("validTime") or ""))
        if parsed is None:
            continue
        if qpf_period_start is not None and parsed < qpf_period_start:
            continue
        if qpf_period_end is not None and (
            parsed > qpf_period_end
            or (parsed == qpf_period_end and not final_period_includes_end)
        ):
            continue
        signal_items.append(item)

    for item in signal_items:
        temp = _representative(item, "temperatureF")
        dew = _representative(item, "dewpointF")
        spread = temp - dew if temp is not None and dew is not None else _representative(item, "dewpointDepressionF")
        rh = _representative(item, "relativeHumidity2mPct")
        wind = _representative(item, "windSpeedMph")
        vis = _representative(item, "visibilityMi")
        if (
            (vis is not None and vis <= 3)
            or (
                spread is not None
                and spread <= 3
                and (rh is None or rh >= 90)
                and (wind is None or wind <= 8)
            )
        ):
            fog_times.append(str(item["validTime"]))
        gust = _representative(item, "gustMph")
        if (gust is not None and gust >= 25) or (wind is not None and wind >= 20):
            wind_times.append(str(item["validTime"]))
        if (
            (
                isinstance(item.get("precipitationIncrementIn"), (int, float))
                and item["precipitationIncrementIn"] > 0.001
            )
            or (_representative(item, "precipitationRateInPerHour") or 0) > 0.001
        ):
            precipitation_times.append(str(item["validTime"]))

    present_fields = {
        field
        for field in EXPECTED_DAILY_FIELDS
        if any(_representative(item, field) is not None for item in analysis_items)
    }
    result: dict[str, Any] = {
        "date": day.isoformat(),
        "sampleCount": len(analysis_items),
        "calendarSampleCount": len(base_items),
        "followingNightSampleCount": len(night_items),
        "firstValidTime": min(
            (str(item["validTime"]) for item in analysis_items), default=None
        ),
        "lastValidTime": max(
            (str(item["validTime"]) for item in analysis_items), default=None
        ),
        "highF": round_number(high, 1),
        "lowF": round_number(low, 1),
        "highFEligible": high_eligible,
        "lowFEligible": low_eligible,
        "fieldEligibility": {
            "highF": {
                "eligible": high_eligible,
                "requiredThroughLocalHour": 16 if zone is not None else None,
                "lastQualifyingSample": high_last_sample,
                "explicitExtremaIntervals": high_extrema_quality,
            },
            "lowF": {
                "eligible": low_eligible,
                "requiredThroughFollowingLocalHour": 6 if zone is not None else None,
                "lastQualifyingSample": low_last_sample,
                "explicitExtremaIntervals": low_extrema_quality,
            },
            **daily_field_eligibility,
        },
        "dewpointRangeF": {
            "min": round_number(min(dewpoints), 1),
            "max": round_number(max(dewpoints), 1),
        }
        if dewpoints
        else None,
        "qpfIn": qpf_in,
        "qpfComplete": qpf_complete,
        "qpfQualityFlags": qpf_quality_flags,
        "qpfCoverage": qpf_coverage,
        "maximumPrecipitationRateInPerHour": round_number(max(precip_rates), 3) if precip_rates else None,
        "maximumWindMph": round_number(max(winds), 1) if winds else None,
        "maximumGustMph": round_number(max(gusts), 1) if gusts else None,
        "minimumVisibilityMi": round_number(min(visibility), 1) if visibility else None,
        "meanCloudCoverPct": round_number(statistics.fmean(cloud_cover), 0) if cloud_cover else None,
        "maximumCapeJkg": round_number(max(capes), 0) if capes else None,
        "ensembleTemperatureSpreadF": round_number(max(ensemble_temperature_spread), 1)
        if ensemble_temperature_spread
        else None,
        "atmosphericProfile": atmospheric_profile,
        "weatherRegime": infer_regime(base_items),
        "signals": {
            "periodStart": (
                qpf_period_start.isoformat(timespec="seconds").replace("+00:00", "Z")
                if qpf_period_start is not None
                else None
            ),
            "periodEnd": (
                qpf_period_end.isoformat(timespec="seconds").replace("+00:00", "Z")
                if qpf_period_end is not None
                else None
            ),
            "boundarySemantics": (
                "start-inclusive/end-inclusive"
                if final_period_includes_end
                else "start-inclusive/end-exclusive"
            ),
            "fog": bool(fog_times),
            "wind": bool(wind_times),
            "precipitation": bool(precipitation_times),
            "fogTimes": fog_times,
            "windTimes": wind_times,
            "precipitationTimes": precipitation_times,
        },
        "missingFields": sorted(set(EXPECTED_DAILY_FIELDS) - present_fields),
    }
    if result["highF"] is None:
        result["missingFields"].append("highF")
    if result["lowF"] is None:
        result["missingFields"].append("lowF")
    if not qpf_complete:
        result["missingFields"].append("qpfIn")
    result["missingFields"] = sorted(set(result["missingFields"]))
    if is_neighborhood:
        raw_temperature_mins = finite_values(
            item.get("temperatureF", {}).get("min")
            for item in analysis_items
            if isinstance(item.get("temperatureF"), dict)
        )
        raw_temperature_maxs = finite_values(
            item.get("temperatureF", {}).get("max")
            for item in analysis_items
            if isinstance(item.get("temperatureF"), dict)
        )
        result["temperatureEnvelopeF"] = {
            "min": round_number(min(raw_temperature_mins), 1) if raw_temperature_mins else None,
            "max": round_number(max(raw_temperature_maxs), 1) if raw_temperature_maxs else None,
        }
    return result


def group_days(
    hourly: list[dict[str, Any]],
    forecast_dates: set[date],
    zone: ZoneInfo,
    is_neighborhood: bool = False,
    *,
    forecast_window_start: datetime | None = None,
    forecast_window_end: datetime | None = None,
    model_cadence_hours: float | None = None,
) -> list[dict[str, Any]]:
    parsed_items: list[tuple[dict[str, Any], datetime]] = []
    for item in hourly:
        timestamp = item.get("validTime")
        if not isinstance(timestamp, str):
            continue
        parsed = parse_time(timestamp)
        if parsed is not None:
            parsed_items.append((item, parsed.astimezone(zone)))
    grouped: defaultdict[date, list[dict[str, Any]]] = defaultdict(list)
    for item, local in parsed_items:
        if local.date() in forecast_dates:
            grouped[local.date()].append(item)
    final_forecast_date = max(forecast_dates, default=None)

    def following_night_end(day: date) -> datetime:
        standard_end = datetime.combine(
            day + timedelta(days=1), time(8, 0), tzinfo=zone
        )
        if day == final_forecast_date and forecast_window_end is not None:
            # The third day owns the remaining records through the exact 72-hour
            # boundary. summarize_day still limits low/QPF diagnostics to 08:00.
            return forecast_window_end.astimezone(zone)
        return standard_end

    return [
        summarize_day(
            day,
            sorted(grouped.get(day, []), key=lambda item: item["validTime"]),
            is_neighborhood,
            following_night_items=[
                item
                for item, local in parsed_items
                if datetime.combine(day, time(18, 0), tzinfo=zone)
                <= local
                <= following_night_end(day)
            ],
            zone=zone,
            forecast_window_start=forecast_window_start,
            forecast_window_end=forecast_window_end,
            model_cadence_hours=model_cadence_hours,
        )
        for day in sorted(forecast_dates)
    ]


def detect_cadence_gaps(
    valid_times: Iterable[datetime],
    window_start: datetime,
    window_end: datetime,
    maximum_gap_hours: float = 6.01,
) -> tuple[float | None, list[dict[str, Any]]]:
    """Find missing forecast intervals using the slowest supported cadence.

    The configured operational inputs are sampled no less often than every six
    hours.  A larger separation therefore represents missing guidance rather
    than an intentional switch between hourly, three-hourly and six-hourly data.
    """

    ordered = sorted({value for value in valid_times if window_start <= value <= window_end})
    deltas = [
        (right - left).total_seconds() / 3600
        for left, right in zip(ordered, ordered[1:])
        if right > left
    ]
    nominal = min(deltas) if deltas else None
    gaps: list[dict[str, Any]] = []
    if ordered and (ordered[0] - window_start).total_seconds() / 3600 > maximum_gap_hours:
        gaps.append(
            {
                "start": window_start.isoformat(timespec="minutes").replace("+00:00", "Z"),
                "end": ordered[0].isoformat(timespec="minutes").replace("+00:00", "Z"),
                "hours": round_number((ordered[0] - window_start).total_seconds() / 3600, 1),
            }
        )
    for left, right in zip(ordered, ordered[1:]):
        gap_hours = (right - left).total_seconds() / 3600
        if gap_hours > maximum_gap_hours:
            gaps.append(
                {
                    "start": left.isoformat(timespec="minutes").replace("+00:00", "Z"),
                    "end": right.isoformat(timespec="minutes").replace("+00:00", "Z"),
                    "hours": round_number(gap_hours, 1),
                }
            )
    return round_number(nominal, 1), gaps


def build_model_summary(
    model: str,
    records: list[dict[str, Any]],
    station_names: list[str],
    forecast_dates: set[date],
    window_start: datetime,
    window_end: datetime,
    zone: ZoneInfo,
    extraction_degraded: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    records = sorted(records, key=lambda item: str(item.get("validTime", "")))
    cycle = str(records[0].get("cycle") or "") if records else ""
    valid_times = [parse_time(str(record.get("validTime") or "")) for record in records]
    valid_times = [value for value in valid_times if value]
    coverage_cadence_hours = derive_nominal_cadence_hours(valid_times)
    warnings: list[str] = []
    stations: dict[str, Any] = {}
    for station in station_names:
        all_hourly = [
            hourly_point(record, station)
            for record in records
            if station in record.get("points", {})
        ]
        warnings.extend(f"{station}: {warning}" for warning in add_precip_increments(all_hourly))
        hourly = [
            item
            for item in all_hourly
            if (parsed := parse_time(str(item.get("validTime") or ""))) is not None
            and window_start <= parsed <= window_end
        ]
        stations[station] = {
            "hourly": hourly,
            "days": group_days(
                hourly,
                forecast_dates,
                zone,
                forecast_window_start=window_start,
                forecast_window_end=window_end,
                model_cadence_hours=coverage_cadence_hours,
            ),
        }

    all_neighborhood_hourly = [hourly_neighborhood(record) for record in records]
    warnings.extend(
        f"neighborhood: {warning}" for warning in add_precip_increments(all_neighborhood_hourly)
    )
    neighborhood_hourly = [
        item
        for item in all_neighborhood_hourly
        if (parsed := parse_time(str(item.get("validTime") or ""))) is not None
        and window_start <= parsed <= window_end
    ]

    source_urls = sorted(
        {
            str(url)
            for record in records
            for url in (record.get("sourceUrls") or [])
            if isinstance(url, str)
        }
    )
    horizon = max(finite_values(record.get("leadTimeHours") for record in records), default=0)
    expected_hours = (window_end - window_start).total_seconds() / 3600
    available_hours = (
        (max(valid_times) - min(valid_times)).total_seconds() / 3600 if len(valid_times) >= 2 else 0
    )
    cadence_hours, cadence_gaps = detect_cadence_gaps(valid_times, window_start, window_end)
    horizon_complete = bool(valid_times and max(valid_times) >= window_end)
    if not horizon_complete:
        status = "partial-horizon"
    elif extraction_degraded or cadence_gaps:
        status = "partial"
    else:
        status = "complete"
    if status == "partial-horizon":
        warnings.append(
            f"{model} ends at "
            f"{max(valid_times).isoformat(timespec='minutes').replace('+00:00', 'Z') if valid_times else 'no valid time'} "
            f"and is partial-horizon guidance for this {round_number(expected_hours, 0)}-hour window."
        )
    if cadence_gaps:
        warnings.append(
            f"{model} has {len(cadence_gaps)} guidance cadence gap(s) longer than six hours."
        )
    if extraction_degraded:
        warnings.append(
            f"{model} cannot be marked complete because extraction reported failed files or warnings."
        )
    return (
        {
            "model": model,
            "cycle": cycle,
            "status": status,
            "sourceUrls": source_urls,
            "horizonHours": round_number(horizon, 1),
            "availableSpanHours": round_number(available_hours, 1),
            "cadenceHours": cadence_hours,
            "coverageCadenceHours": coverage_cadence_hours,
            "cadenceGaps": cadence_gaps,
            "stations": stations,
            "neighborhood": {
                "hourly": neighborhood_hourly,
                "days": group_days(
                    neighborhood_hourly,
                    forecast_dates,
                    zone,
                    is_neighborhood=True,
                    forecast_window_start=window_start,
                    forecast_window_end=window_end,
                    model_cadence_hours=coverage_cadence_hours,
                ),
            },
            "warnings": list(warnings),
        },
        warnings,
    )


def _consensus_day(date_text: str, days: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    sample_contributors = [
        (model, day)
        for model, day in days
        if isinstance(day.get("sampleCount"), (int, float)) and day["sampleCount"] > 0
    ]
    excluded_models = [model for model, day in days if (day.get("sampleCount") or 0) <= 0]
    high_contributors = [
        (model, day)
        for model, day in sample_contributors
        if day.get("highFEligible") is True and isinstance(day.get("highF"), (int, float))
    ]
    low_contributors = [
        (model, day)
        for model, day in sample_contributors
        if day.get("lowFEligible") is True and isinstance(day.get("lowF"), (int, float))
    ]
    qpf_contributors = [
        (model, day)
        for model, day in sample_contributors
        if day.get("qpfComplete") is True and isinstance(day.get("qpfIn"), (int, float))
    ]
    gust_contributors = [
        (model, day)
        for model, day in sample_contributors
        if day.get("fieldEligibility", {})
        .get("maximumGustMph", {})
        .get("eligible")
        is True
        if isinstance(day.get("maximumGustMph"), (int, float))
    ]
    cloud_contributors = [
        (model, day)
        for model, day in sample_contributors
        if day.get("fieldEligibility", {})
        .get("meanCloudCoverPct", {})
        .get("eligible")
        is True
        if isinstance(day.get("meanCloudCoverPct"), (int, float))
    ]
    visibility_contributors = [
        (model, day)
        for model, day in sample_contributors
        if day.get("fieldEligibility", {})
        .get("minimumVisibilityMi", {})
        .get("eligible")
        is True
        if isinstance(day.get("minimumVisibilityMi"), (int, float))
    ]
    wind_contributors = [
        (model, day)
        for model, day in sample_contributors
        if (
            day.get("fieldEligibility", {})
            .get("maximumWindMph", {})
            .get("eligible")
            is True
            and isinstance(day.get("maximumWindMph"), (int, float))
        )
        or (
            day.get("fieldEligibility", {})
            .get("maximumGustMph", {})
            .get("eligible")
            is True
            and isinstance(day.get("maximumGustMph"), (int, float))
        )
    ]
    fog_contributors = [
        (model, day)
        for model, day in sample_contributors
        if day.get("fieldEligibility", {})
        .get("fogSignal", {})
        .get("eligible")
        is True
    ]

    high_stats = summary_stats((day.get("highF") for _, day in high_contributors), 1)
    low_stats = summary_stats((day.get("lowF") for _, day in low_contributors), 1)
    qpf_stats = summary_stats((day.get("qpfIn") for _, day in qpf_contributors), 3)
    gust_stats = summary_stats((day.get("maximumGustMph") for _, day in gust_contributors), 1)
    cloud_stats = summary_stats(
        (day.get("meanCloudCoverPct") for _, day in cloud_contributors), 0
    )
    visibility_stats = summary_stats(
        (day.get("minimumVisibilityMi") for _, day in visibility_contributors), 1
    )
    primary_models = sorted(
        {model for model, _ in high_contributors} | {model for model, _ in low_contributors}
    )
    model_count = len(primary_models)
    excluded_partial_models = sorted(
        model
        for model, _ in sample_contributors
        if model not in set(primary_models)
    )

    def confidence(contributor_count: int) -> str:
        return (
            "high"
            if contributor_count >= 4
            else "medium"
            if contributor_count >= 2
            else "low"
            if contributor_count == 1
            else "unavailable"
        )

    missing: list[str] = []
    for name, value in (
        ("highF", high_stats),
        ("lowF", low_stats),
        ("qpfIn", qpf_stats),
        ("maximumGustMph", gust_stats),
        ("meanCloudCoverPct", cloud_stats),
        ("minimumVisibilityMi", visibility_stats),
    ):
        if value is None:
            missing.append(name)
    fog_supporters = sorted(
        model for model, day in fog_contributors if day.get("signals", {}).get("fog")
    )
    fog_support = len(fog_supporters)
    wind_support = sum(bool(day.get("signals", {}).get("wind")) for _, day in wind_contributors)
    wind_supporters = sorted(
        model for model, day in wind_contributors if day.get("signals", {}).get("wind")
    )
    precip_support = sum(
        bool((day.get("qpfIn") or 0) > 0.001) for _, day in qpf_contributors
    )
    precip_supporters = sorted(
        model for model, day in qpf_contributors if (day.get("qpfIn") or 0) > 0.001
    )
    regimes = [
        str(day.get("weatherRegime"))
        for model, day in sample_contributors
        if model in set(primary_models)
        if day.get("weatherRegime")
    ]
    regime = statistics.mode(regimes) if regimes else "mixed-or-unspecified"
    confidence_count = min(len(high_contributors), len(low_contributors))
    atmospheric_profile = build_profile_consensus(sample_contributors)
    return {
        "date": date_text,
        "models": primary_models,
        "excludedZeroSampleModels": sorted(excluded_models),
        "excludedPartialDayModels": excluded_partial_models,
        "modelCount": model_count,
        "fieldContributorCounts": {
            "highF": high_stats["count"] if high_stats else 0,
            "lowF": low_stats["count"] if low_stats else 0,
            "qpfIn": qpf_stats["count"] if qpf_stats else 0,
            "maximumGustMph": gust_stats["count"] if gust_stats else 0,
            "meanCloudCoverPct": cloud_stats["count"] if cloud_stats else 0,
            "minimumVisibilityMi": visibility_stats["count"] if visibility_stats else 0,
        },
        "fieldContributors": {
            "highF": sorted(model for model, _ in high_contributors),
            "lowF": sorted(model for model, _ in low_contributors),
            "qpfIn": sorted(model for model, _ in qpf_contributors),
            "maximumGustMph": sorted(model for model, _ in gust_contributors),
            "meanCloudCoverPct": sorted(model for model, _ in cloud_contributors),
            "minimumVisibilityMi": sorted(model for model, _ in visibility_contributors),
        },
        "fieldConfidence": {
            "highF": confidence(len(high_contributors)),
            "lowF": confidence(len(low_contributors)),
            "qpfIn": confidence(len(qpf_contributors)),
            "maximumGustMph": confidence(len(gust_contributors)),
            "meanCloudCoverPct": confidence(len(cloud_contributors)),
            "minimumVisibilityMi": confidence(len(visibility_contributors)),
        },
        "highF": high_stats,
        "lowF": low_stats,
        "qpfIn": qpf_stats,
        "maximumGustMph": gust_stats,
        "meanCloudCoverPct": cloud_stats,
        "minimumVisibilityMi": visibility_stats,
        "atmosphericProfile": atmospheric_profile,
        "eventSupport": {
            "fog": {
                "models": fog_support,
                "contributors": len(fog_contributors),
                "contributorModels": sorted(model for model, _ in fog_contributors),
                "supportingModels": fog_supporters,
                "fraction": round_number(fog_support / len(fog_contributors), 2)
                if fog_contributors
                else None,
            },
            "wind": {
                "models": wind_support,
                "contributors": len(wind_contributors),
                "contributorModels": sorted(model for model, _ in wind_contributors),
                "supportingModels": wind_supporters,
                "fraction": round_number(wind_support / len(wind_contributors), 2)
                if wind_contributors
                else None,
            },
            "precipitation": {
                "models": precip_support,
                "contributors": len(qpf_contributors),
                "contributorModels": sorted(model for model, _ in qpf_contributors),
                "supportingModels": precip_supporters,
                "fraction": round_number(precip_support / len(qpf_contributors), 2)
                if qpf_contributors
                else None,
            },
        },
        "weatherRegime": regime,
        "confidence": confidence(confidence_count),
        "missingFields": missing,
    }


def build_consensus(model_summaries: list[dict[str, Any]], station_names: list[str]) -> dict[str, Any]:
    stations: dict[str, Any] = {}
    for station in station_names:
        by_date: defaultdict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for model in model_summaries:
            for day in model.get("stations", {}).get(station, {}).get("days", []):
                by_date[str(day["date"])].append((str(model["model"]), day))
        stations[station] = {
            "days": [_consensus_day(day, values) for day, values in sorted(by_date.items())]
        }
    primary = "Danville" if "Danville" in stations else "KDVK" if "KDVK" in stations else station_names[0]
    return {"stations": stations, "days": stations.get(primary, {}).get("days", [])}


def coalesce_windows(events: list[dict[str, Any]], maximum_gap_hours: float = 4) -> list[dict[str, Any]]:
    grouped: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    seen_events: set[tuple[str, str, str]] = set()
    for event in events:
        model = str(event["model"])
        station = str(event["station"])
        valid_time = str(event["validTime"])
        event_key = (model, station, valid_time)
        if event_key in seen_events:
            continue
        seen_events.add(event_key)
        grouped[(model, station)].append(event)
    windows: list[dict[str, Any]] = []
    for (model, station), values in sorted(grouped.items()):
        ordered = sorted(values, key=lambda item: item["validTime"])
        current: list[dict[str, Any]] = []
        for event in ordered:
            timestamp = parse_time(event["validTime"])
            previous = parse_time(current[-1]["validTime"]) if current else None
            if current and timestamp and previous and (timestamp - previous).total_seconds() / 3600 > maximum_gap_hours:
                windows.append(
                    {
                        "model": model,
                        "station": station,
                        "start": current[0]["validTime"],
                        "end": current[-1]["validTime"],
                        "samples": len(current),
                    }
                )
                current = []
            current.append(event)
        if current:
            windows.append(
                {
                    "model": model,
                    "station": station,
                    "start": current[0]["validTime"],
                    "end": current[-1]["validTime"],
                    "samples": len(current),
                }
            )
    return windows


def build_signals(model_summaries: list[dict[str, Any]]) -> dict[str, Any]:
    events: dict[str, list[dict[str, Any]]] = {"fog": [], "wind": [], "precipitation": []}
    for model in model_summaries:
        for station, station_data in model.get("stations", {}).items():
            for day in station_data.get("days", []):
                for signal in events:
                    for timestamp in day.get("signals", {}).get(f"{signal}Times", []):
                        events[signal].append(
                            {"model": model["model"], "station": station, "validTime": timestamp}
                        )
    return {
        "fogWindows": coalesce_windows(events["fog"]),
        "windWindows": coalesce_windows(events["wind"]),
        "precipitationWindows": coalesce_windows(events["precipitation"]),
    }


def benchmark_lead(cycle: str, day: str, zone: ZoneInfo) -> int:
    cycle_time = parse_time(cycle)
    target_day = date.fromisoformat(day)
    local_noon = datetime.combine(target_day, time(12, 0), tzinfo=zone).astimezone(timezone.utc)
    if cycle_time is None:
        return 0
    return max(0, int(round((local_noon - cycle_time).total_seconds() / 3600)))


def build_benchmarks(
    model_summaries: list[dict[str, Any]],
    zone: ZoneInfo,
    source_attributions: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    benchmarks: list[dict[str, Any]] = []
    attributions = source_attributions if isinstance(source_attributions, dict) else {}
    for model in model_summaries:
        cycle = str(model.get("cycle") or "")
        source_url = (model.get("sourceUrls") or [None])[0]
        model_key = str(model.get("model") or "").lower()
        attribution = attributions.get(model_key)
        for station in sorted(OFFICIAL_BENCHMARK_STATIONS):
            station_data = model.get("stations", {}).get(station)
            if not isinstance(station_data, dict):
                continue
            for day in station_data.get("days", []):
                high = day.get("highF") if day.get("highFEligible") is True else None
                low = day.get("lowF") if day.get("lowFEligible") is True else None
                if not isinstance(high, (int, float)) and not isinstance(low, (int, float)):
                    continue
                benchmark: dict[str, Any] = {
                    "model": FORECAST_MODEL_NAMES.get(str(model["model"]), str(model["model"])),
                    "run": cycle,
                    "region": "kentucky",
                    "station": station,
                    "targetDate": day["date"],
                    "leadTimeHours": benchmark_lead(cycle, day["date"], zone),
                    "weatherRegime": day.get("weatherRegime", "mixed-or-unspecified"),
                    "sourceUrl": source_url,
                    "note": (
                        "Locally extracted GRIB guidance for manual review. It is not an official "
                        "forecast and is never copied into forecast.json automatically."
                    ),
                }
                if isinstance(attribution, dict):
                    benchmark["attribution"] = dict(attribution)
                    benchmark["note"] = (
                        f"{attribution.get('statement', '')} "
                        f"Licensed as {attribution.get('licenseName', '')}: "
                        f"{attribution.get('licenseUrl', '')}. "
                        f"{attribution.get('modificationNotice', '')} "
                        "Guidance is for manual review, is not an official forecast, and is never "
                        "copied into forecast.json automatically."
                    ).strip()
                if isinstance(high, (int, float)):
                    benchmark["highF"] = high
                if isinstance(low, (int, float)):
                    benchmark["lowF"] = low
                if day.get("qpfComplete") is True and isinstance(day.get("qpfIn"), (int, float)):
                    benchmark["precipitationIn"] = day["qpfIn"]
                benchmarks.append(benchmark)
    return benchmarks


def extraction_is_degraded(extracted: dict[str, Any]) -> bool:
    warnings = extracted.get("warnings")
    failed_files = extracted.get("statistics", {}).get("failedFileCount", 0)
    return bool(isinstance(warnings, list) and warnings) or (
        isinstance(failed_files, (int, float)) and failed_files > 0
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start", help="UTC ISO start for the exact 72-hour guidance window")
    parser.add_argument("--time-zone", default=DEFAULT_TIME_ZONE)
    parser.add_argument("--hours", type=int, default=72)
    args = parser.parse_args(argv)
    args.output = ensure_safe_output(args.output)
    if args.hours <= 0:
        raise SystemExit("--hours must be positive")

    extracted = read_json(args.input)
    raw_records = extracted.get("records")
    if not isinstance(raw_records, list) or not raw_records:
        raise SystemExit(f"No extracted records found in {args.input}")
    records = [record for record in raw_records if isinstance(record, dict)]
    records, cycle_warnings = select_latest_cycles(records)
    zone = ZoneInfo(args.time_zone)

    requested_start = parse_time(args.start)
    generated_time = parse_time(str(extracted.get("generatedAt") or ""))
    valid_times = [parse_time(str(record.get("validTime") or "")) for record in records]
    valid_times = [value for value in valid_times if value]
    if not valid_times:
        raise SystemExit("Extracted records contain no valid timestamps")
    window_start = requested_start or generated_time or min(valid_times)
    # If generatedAt is later than all fixture/historical valid times, use the
    # first valid time. Real-time runs keep generatedAt as their exact boundary.
    if window_start > max(valid_times):
        window_start = min(valid_times)
    window_end = window_start + timedelta(hours=args.hours)
    local_start_date = window_start.astimezone(zone).date()
    forecast_dates = {local_start_date + timedelta(days=offset) for offset in range(3)}

    locations = extracted.get("locations") if isinstance(extracted.get("locations"), dict) else {}
    station_names = list(locations) or sorted(
        {
            str(name)
            for record in records
            for name in (record.get("points") or {})
        }
    )
    for required in ("Danville", "KDVK", "KLEX"):
        if required not in station_names:
            station_names.append(required)

    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record.get("model") or "unknown")].append(record)

    issued_at = extracted.get("issuedAt")
    raw_model_freshness = extracted.get("modelFreshness")
    model_freshness = (
        {
            str(model).casefold(): value
            for model, value in raw_model_freshness.items()
            if isinstance(value, dict)
        }
        if isinstance(raw_model_freshness, dict)
        else {}
    )
    model_summaries: list[dict[str, Any]] = []
    extractor_warnings = [str(item) for item in extracted.get("warnings", [])]
    extraction_degraded = extraction_is_degraded(extracted)
    warnings = list(cycle_warnings) + extractor_warnings
    for model, model_records in sorted(grouped.items()):
        summary, model_warnings = build_model_summary(
            model,
            model_records,
            station_names,
            forecast_dates,
            window_start,
            window_end,
            zone,
            extraction_degraded=extraction_degraded,
        )
        source_freshness = model_freshness.get(model.casefold(), {})
        summary["freshness"] = {
            "issuedAt": issued_at,
            "cycleTime": source_freshness.get("cycleTime"),
            "cycleAgeHours": source_freshness.get("cycleAgeHours"),
            "maximumCycleAgeHours": source_freshness.get("maximumCycleAgeHours"),
            "freshnessStatus": source_freshness.get("freshnessStatus"),
        }
        summary["warnings"] = model_warnings
        warnings.extend(f"{model}: {warning}" for warning in model_warnings)
        model_summaries.append(summary)

    requested_models = extracted.get("requestedModels")
    if isinstance(requested_models, list):
        requested_models = [
            str(item.get("model") or item.get("id")) if isinstance(item, dict) else str(item)
            for item in requested_models
        ]
    else:
        requested_models = extracted.get("statistics", {}).get("models")
    if not isinstance(requested_models, list):
        requested_models = sorted(grouped)
    available_models = sorted(model["model"] for model in model_summaries)
    missing_models = sorted(set(map(str, requested_models)) - set(available_models))
    for model in missing_models:
        warning = f"Requested model {model} was unavailable or produced no decodable guidance."
        warnings.append(warning)
        model_summaries.append(
            {
                "model": model,
                "cycle": window_start.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "status": "unavailable",
                "sourceUrls": [],
                "horizonHours": 0,
                "availableSpanHours": 0,
                "stations": {},
                "neighborhood": {},
                "warnings": [warning],
            }
        )
    completeness_status = (
        "complete"
        if not missing_models and all(model["status"] == "complete" for model in model_summaries)
        else "partial"
    )
    consensus = build_consensus(model_summaries, station_names)
    source_attributions = (
        extracted.get("sourceAttributions")
        if isinstance(extracted.get("sourceAttributions"), dict)
        else {}
    )
    source_notes = (
        extracted.get("sourceNotes") if isinstance(extracted.get("sourceNotes"), list) else []
    )
    payload = {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "input": {
            "path": repo_relative(args.input),
            "sha256": sha256_file(args.input),
            "generatedAt": extracted.get("generatedAt"),
            "issuedAt": issued_at,
            "sourceManifest": extracted.get("sourceManifest"),
            "provenance": extracted.get("provenance"),
        },
        "location": {
            "name": "Danville, Kentucky",
            "timeZone": args.time_zone,
            "points": locations,
            "neighborhoodBounds": extracted.get("neighborhoodBounds"),
        },
        "sourceAttributions": source_attributions,
        "sourceNotes": source_notes,
        "coverage": {
            "start": window_start.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "end": window_end.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "hours": args.hours,
            "forecastDates": [value.isoformat() for value in sorted(forecast_dates)],
        },
        "completeness": {
            "status": completeness_status,
            "modelsRequested": list(map(str, requested_models)),
            "modelsAvailable": available_models,
            "modelsMissing": missing_models,
            "warningCount": len(warnings),
        },
        "models": model_summaries,
        "consensus": consensus,
        "signals": build_signals(model_summaries),
        "proposedForecastBenchmarks": build_benchmarks(
            model_summaries, zone, source_attributions
        ),
        "publicationSafety": {
            "manualReviewRequired": True,
            "writesForecastJson": False,
            "publishesWebsite": False,
            "notice": "Guidance only; publication remains separately approval-gated.",
        },
        "warnings": warnings,
    }
    atomic_write_json(args.output, payload)
    print(
        f"Built {len(model_summaries)}-model Danville guidance summary "
        f"with {len(payload['proposedForecastBenchmarks'])} proposed benchmark(s): {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
