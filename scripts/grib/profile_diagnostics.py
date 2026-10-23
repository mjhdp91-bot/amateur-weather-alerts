"""Conservative atmospheric-profile diagnostics for the GRIB review summary.

This module only summarizes fields already present in extracted point records.
It does not interpolate missing pressure levels, fill temporal gaps, or infer a
field from a different variable.  Those constraints keep the output suitable
for human forecast discussion without making partial guidance look complete.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from typing import Any, Callable, Iterable


PROFILE_FIELD_NAMES = (
    "temperature850hPaC",
    "relativeHumidity700hPaPct",
    "height500hPaM",
    "precipitableWaterIn",
    "wind850hPaMph",
    "wind700hPaMph",
    "wind500hPaMph",
    "verticalVelocity850hPaPaPerS",
    "verticalVelocity700hPaPaPerS",
    "verticalVelocity500hPaPaPerS",
)

CONSENSUS_PROFILE_FIELD_NAMES = PROFILE_FIELD_NAMES + ("height500hPaTendencyM",)

PROFILE_FIELD_DIGITS = {
    "temperature850hPaC": 1,
    "relativeHumidity700hPaPct": 0,
    "height500hPaM": 0,
    "precipitableWaterIn": 2,
    "wind850hPaMph": 1,
    "wind700hPaMph": 1,
    "wind500hPaMph": 1,
    "verticalVelocity850hPaPaPerS": 3,
    "verticalVelocity700hPaPaPerS": 3,
    "verticalVelocity500hPaPaPerS": 3,
    "height500hPaTendencyM": 0,
}

# A per-model daily representative is deliberately chosen before calculating
# the consensus, so a high-frequency model does not receive more weight.
CONSENSUS_SELECTION_METHODS = {
    "temperature850hPaC": "daily-mean-per-model",
    "relativeHumidity700hPaPct": "daily-mean-per-model",
    "height500hPaM": "daily-mean-per-model",
    "height500hPaTendencyM": "period-end-minus-start-per-model",
    "precipitableWaterIn": "daily-maximum-per-model",
    "wind850hPaMph": "daily-maximum-per-model",
    "wind700hPaMph": "daily-maximum-per-model",
    "wind500hPaMph": "daily-maximum-per-model",
    "verticalVelocity850hPaPaPerS": "daily-minimum-per-model",
    "verticalVelocity700hPaPaPerS": "daily-minimum-per-model",
    "verticalVelocity500hPaPaPerS": "daily-minimum-per-model",
}


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="minutes").replace(
        "+00:00", "Z"
    )


def _finite(value: Any) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, dict):
        mean = value.get("mean")
        if isinstance(mean, (int, float)) and math.isfinite(float(mean)):
            return float(mean)
    return None


def _direct(field: str) -> Callable[[dict[str, Any]], float | None]:
    return lambda item: _finite(item.get(field))


def _wind(level: int) -> Callable[[dict[str, Any]], float | None]:
    def calculate(item: dict[str, Any]) -> float | None:
        u_value = _finite(item.get(f"uWind{level}hPaMph"))
        v_value = _finite(item.get(f"vWind{level}hPaMph"))
        if u_value is None or v_value is None:
            return None
        return math.hypot(u_value, v_value)

    return calculate


PROFILE_EXTRACTORS: dict[str, Callable[[dict[str, Any]], float | None]] = {
    "temperature850hPaC": _direct("temperature850hPaC"),
    "relativeHumidity700hPaPct": _direct("relativeHumidity700hPaPct"),
    "height500hPaM": _direct("height500hPaM"),
    "precipitableWaterIn": _direct("precipitableWaterIn"),
    "wind850hPaMph": _wind(850),
    "wind700hPaMph": _wind(700),
    "wind500hPaMph": _wind(500),
    "verticalVelocity850hPaPaPerS": _direct("verticalVelocity850hPaPaPerS"),
    "verticalVelocity700hPaPaPerS": _direct("verticalVelocity700hPaPaPerS"),
    "verticalVelocity500hPaPaPerS": _direct("verticalVelocity500hPaPaPerS"),
}


def _rounded(value: float, digits: int) -> float:
    return round(value, digits)


def derive_nominal_cadence_hours(
    timestamps: Iterable[datetime],
    fallback_hours: float | None = None,
    maximum_hours: float = 6.0,
) -> float:
    """Infer the slowest repeatedly observed supported cadence.

    HRRR legitimately transitions from hourly to three-hourly output, while
    GEFS is six-hourly throughout.  A single larger gap is therefore treated as
    missing guidance rather than silently redefining the nominal cadence.
    """

    ordered = sorted(set(timestamps))
    deltas = [
        round((right - left).total_seconds() / 3600, 3)
        for left, right in zip(ordered, ordered[1:])
        if right > left
        and (right - left).total_seconds() / 3600 <= maximum_hours + 0.01
    ]
    counts = {delta: deltas.count(delta) for delta in set(deltas)}
    repeated = [delta for delta, count in counts.items() if count >= 2]
    if repeated:
        return min(max(repeated), maximum_hours)
    if isinstance(fallback_hours, (int, float)) and math.isfinite(float(fallback_hours)):
        if float(fallback_hours) > 0:
            return min(float(fallback_hours), maximum_hours)
    if deltas:
        return min(min(deltas), maximum_hours)
    return maximum_hours


def _gap(
    kind: str, start: datetime, end: datetime, maximum_gap_hours: float
) -> dict[str, Any] | None:
    hours = (end - start).total_seconds() / 3600
    if hours <= maximum_gap_hours:
        return None
    return {
        "kind": kind,
        "start": _iso(start),
        "end": _iso(end),
        "hours": round(hours, 1),
    }


def _field_summary(
    samples: list[tuple[datetime, float]],
    period_start: datetime,
    period_end: datetime,
    digits: int,
    fallback_cadence_hours: float | None,
    maximum_cadence_hours: float,
    *,
    include_tendency: bool = False,
) -> dict[str, Any] | None:
    if not samples:
        return None
    ordered = sorted(samples)
    nominal_cadence_hours = derive_nominal_cadence_hours(
        (timestamp for timestamp, _ in ordered),
        fallback_cadence_hours,
        maximum_cadence_hours,
    )
    maximum_gap_hours = nominal_cadence_hours + 0.01
    gaps: list[dict[str, Any]] = []
    leading = _gap("leading", period_start, ordered[0][0], maximum_gap_hours)
    if leading:
        gaps.append(leading)
    for left, right in zip(ordered, ordered[1:]):
        internal = _gap("internal", left[0], right[0], maximum_gap_hours)
        if internal:
            gaps.append(internal)
    trailing = _gap("trailing", ordered[-1][0], period_end, maximum_gap_hours)
    if trailing:
        gaps.append(trailing)

    values = [value for _, value in ordered]
    result: dict[str, Any] = {
        "count": len(values),
        "min": _rounded(min(values), digits),
        "max": _rounded(max(values), digits),
        "mean": _rounded(statistics.fmean(values), digits),
        "median": _rounded(statistics.median(values), digits),
        "spread": _rounded(max(values) - min(values), digits),
        "firstValue": _rounded(values[0], digits),
        "lastValue": _rounded(values[-1], digits),
        "firstValidTime": _iso(ordered[0][0]),
        "lastValidTime": _iso(ordered[-1][0]),
        "nominalCadenceHours": nominal_cadence_hours,
        "maximumGapHours": maximum_gap_hours,
        "coverageComplete": not gaps,
        "coverageGaps": gaps,
    }
    if include_tendency:
        result["tendencyM"] = (
            _rounded(values[-1] - values[0], digits) if len(values) >= 2 else None
        )
    return result


def build_profile_diagnostics(
    items: Iterable[dict[str, Any]],
    period_start: datetime,
    period_end: datetime,
    *,
    include_period_end: bool = False,
    model_cadence_hours: float | None = None,
    maximum_cadence_hours: float = 6.0,
) -> dict[str, Any]:
    """Summarize extracted profile fields inside one non-overlapping period.

    The normal boundary is start-inclusive and end-exclusive.  The caller may
    include the end only for the last forecast period, where no following day
    can duplicate the boundary sample.
    """

    if period_start.tzinfo is None or period_end.tzinfo is None:
        raise ValueError("profile periods must be timezone-aware")
    start = period_start.astimezone(timezone.utc)
    end = period_end.astimezone(timezone.utc)
    if end <= start:
        raise ValueError("profile period end must be after its start")

    parsed_items: list[tuple[datetime, dict[str, Any]]] = []
    for item in items:
        valid_time = _parse_time(item.get("validTime"))
        if valid_time is None or valid_time < start:
            continue
        if valid_time > end or (valid_time == end and not include_period_end):
            continue
        parsed_items.append((valid_time, item))

    fields: dict[str, dict[str, Any] | None] = {}
    for field in PROFILE_FIELD_NAMES:
        extractor = PROFILE_EXTRACTORS[field]
        samples = [
            (valid_time, value)
            for valid_time, item in parsed_items
            if (value := extractor(item)) is not None
        ]
        fields[field] = _field_summary(
            samples,
            start,
            end,
            PROFILE_FIELD_DIGITS[field],
            model_cadence_hours,
            maximum_cadence_hours,
            include_tendency=field == "height500hPaM",
        )

    available = [field for field, value in fields.items() if value is not None]
    complete = [
        field
        for field, value in fields.items()
        if value is not None and value.get("coverageComplete") is True
    ]
    return {
        "periodStart": _iso(start),
        "periodEnd": _iso(end),
        "boundarySemantics": (
            "start-inclusive/end-inclusive"
            if include_period_end
            else "start-inclusive/end-exclusive"
        ),
        "maximumGapHours": maximum_cadence_hours + 0.01,
        "modelCadenceHours": model_cadence_hours,
        "cadenceMethod": "slowest-repeated-field-cadence-capped-at-six-hours",
        "availableFields": available,
        "coverageCompleteFields": complete,
        "incompleteFields": sorted(set(available) - set(complete)),
        "missingFields": sorted(set(PROFILE_FIELD_NAMES) - set(available)),
        "fields": fields,
        "verticalVelocitySignConvention": "negative-ascent-positive-descent",
    }


def _summary_stats(values: Iterable[float], digits: int) -> dict[str, Any] | None:
    numbers = [float(value) for value in values if math.isfinite(float(value))]
    if not numbers:
        return None
    return {
        "count": len(numbers),
        "min": _rounded(min(numbers), digits),
        "max": _rounded(max(numbers), digits),
        "mean": _rounded(statistics.fmean(numbers), digits),
        "median": _rounded(statistics.median(numbers), digits),
        "spread": _rounded(max(numbers) - min(numbers), digits),
    }


def _representative_value(field: str, value: dict[str, Any]) -> float | None:
    if field == "height500hPaTendencyM":
        source = value.get("tendencyM")
    elif field == "precipitableWaterIn" or field.startswith("wind"):
        source = value.get("max")
    elif field.startswith("verticalVelocity"):
        # Negative omega indicates ascent; retain the strongest modeled ascent.
        source = value.get("min")
    else:
        source = value.get("mean")
    return _finite(source)


def _confidence(count: int) -> str:
    if count >= 4:
        return "high"
    if count >= 2:
        return "medium"
    if count == 1:
        return "low"
    return "unavailable"


def build_profile_consensus(
    model_days: Iterable[tuple[str, dict[str, Any]]]
) -> dict[str, Any]:
    """Build equal-weight consensus from temporally complete model-day fields."""

    days = list(model_days)
    periods = sorted(
        {
            (str(profile.get("periodStart")), str(profile.get("periodEnd")))
            for _, day in days
            if isinstance((profile := day.get("atmosphericProfile")), dict)
            and profile.get("periodStart")
            and profile.get("periodEnd")
        }
    )
    candidate_models = sorted(
        {
            model
            for model, day in days
            if isinstance(day.get("atmosphericProfile"), dict)
        }
    )
    fields: dict[str, dict[str, Any] | None] = {}
    contributors: dict[str, list[str]] = {}
    excluded: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    confidence: dict[str, str] = {}

    for field in CONSENSUS_PROFILE_FIELD_NAMES:
        source_field = "height500hPaM" if field == "height500hPaTendencyM" else field
        model_values: list[tuple[str, float]] = []
        excluded_models: list[str] = []
        for model, day in days:
            profile = day.get("atmosphericProfile")
            source = profile.get("fields", {}).get(source_field) if isinstance(profile, dict) else None
            if not isinstance(source, dict) or source.get("coverageComplete") is not True:
                excluded_models.append(model)
                continue
            representative = _representative_value(field, source)
            if representative is None:
                excluded_models.append(model)
                continue
            model_values.append((model, representative))

        model_values.sort()
        contributor_models = [model for model, _ in model_values]
        fields[field] = _summary_stats(
            (value for _, value in model_values), PROFILE_FIELD_DIGITS[field]
        )
        contributors[field] = contributor_models
        excluded[field] = sorted(set(excluded_models))
        counts[field] = len(contributor_models)
        confidence[field] = _confidence(len(contributor_models))

    contributing_models = sorted(
        {model for model_list in contributors.values() for model in model_list}
    )
    return {
        "periodStart": periods[0][0] if len(periods) == 1 else None,
        "periodEnd": periods[0][1] if len(periods) == 1 else None,
        "periodAgreement": len(periods) == 1,
        "models": contributing_models,
        "candidateModels": candidate_models,
        "modelCount": len(contributing_models),
        "requiresCompleteCoverage": True,
        "selectionMethods": dict(CONSENSUS_SELECTION_METHODS),
        "fieldContributorCounts": counts,
        "fieldConfidence": confidence,
        "fieldContributors": contributors,
        "fieldExcludedModels": excluded,
        "missingFields": [field for field, value in fields.items() if value is None],
        "fields": fields,
        "verticalVelocitySignConvention": "negative-ascent-positive-descent",
    }
