#!/usr/bin/env python3
"""Extract point and neighborhood guidance from staged GRIB2 subsets.

This program is deliberately read-only with respect to forecast.json.  It reads a
fetch manifest, decodes GRIB messages with ecCodes, and atomically writes a small,
reviewable JSON intermediate file for ``build-summary.py``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import (
    PROTECTED_OUTPUT_NAMES,
    repo_relative,
    resolve_guidance_output_path,
    sha256_file,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "grib" / "danville.json"
DEFAULT_MANIFEST_DIR = REPO_ROOT / "grib-data" / "manifests"
DEFAULT_OUTPUT = REPO_ROOT / "grib-data" / "extracted" / "danville-hourly.json"
DEFAULT_LOCATIONS = {
    "Danville": {"latitude": 37.6456, "longitude": -84.7722, "kind": "forecast-point"},
    "KDVK": {"latitude": 37.5779, "longitude": -84.7697, "kind": "station"},
    "KLEX": {"latitude": 38.0365, "longitude": -84.6059, "kind": "station"},
}
INTERVAL_METADATA_KEYS = {
    "precipitationIn": "precipitationInterval",
    "maximumTemperatureF": "maximumTemperatureInterval",
    "minimumTemperatureF": "minimumTemperatureInterval",
}
CONVECTIVE_CAPE_FIELDS = (
    "mostUnstableCapeJkg",
    "cape255To0MbJkg",
    "cape180To0MbJkg",
    "surfaceCapeJkg",
)
PAIRED_CIN_FIELDS = {
    "mostUnstableCapeJkg": "mostUnstableCinJkg",
    "cape255To0MbJkg": "cin255To0MbJkg",
    "cape180To0MbJkg": "cin180To0MbJkg",
    "surfaceCapeJkg": "surfaceCinJkg",
}


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
            f"{error}. GRIB extraction may only write derived files beneath grib-data."
        ) from error


def find_latest_manifest(directory: Path) -> Path:
    candidates = sorted(
        (
            path
            for path in directory.glob("*.json")
            if path.is_file() and path.name.lower() != "latest.json"
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No manifest JSON files found under {directory}")
    return candidates[0]


def _first(mapping: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = mapping.get(name)
        if value not in (None, ""):
            return value
    return None


def manifest_files(manifest: dict[str, Any], manifest_path: Path) -> list[dict[str, Any]]:
    """Normalize known and future fetch-manifest shapes into one file list."""

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(value: Any, inherited: dict[str, Any]) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item, inherited)
            return
        if not isinstance(value, dict):
            return

        context = dict(inherited)
        for key in ("model", "product", "variant", "cycleTime", "cycle", "run", "sourceUrl"):
            if value.get(key) not in (None, ""):
                context[key] = value[key]

        raw_path = _first(value, "localPath", "path", "filePath", "downloadPath")
        if isinstance(raw_path, str) and raw_path.lower().endswith((".grib", ".grib2", ".grb")):
            status = str(value.get("status", "complete")).lower()
            if status not in {"complete", "completed", "success", "downloaded", "available", "ok"}:
                return
            path = Path(raw_path)
            if not path.is_absolute():
                path = (manifest_path.parent / path).resolve()
                if not path.exists():
                    path = (REPO_ROOT / raw_path).resolve()
            key = str(path).lower()
            if key in seen:
                return
            seen.add(key)
            entry = dict(context)
            entry.update(value)
            entry["localPath"] = str(path)
            normalized.append(entry)
            return

        for key, child in value.items():
            if key in {"warnings", "errors", "metadata"}:
                continue
            if isinstance(child, (list, dict)):
                child_context = dict(context)
                if key not in {"files", "downloads", "entries", "models", "runs"} and "model" not in child_context:
                    child_context["model"] = key
                walk(child, child_context)

    preferred = manifest.get("files")
    if isinstance(preferred, list):
        walk(preferred, {})
    else:
        walk(manifest, {})
    return sorted(
        normalized,
        key=lambda item: (
            str(item.get("model", "")),
            str(_first(item, "cycleTime", "cycle", "run") or ""),
            int(item.get("forecastHour", 0) or 0),
            str(item.get("localPath", "")),
        ),
    )


def manifest_model_freshness(
    manifest: dict[str, Any], accepted_models: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """Keep the fetcher's review-facing freshness proof for decoded models."""

    raw_models = manifest.get("models")
    if not isinstance(raw_models, dict):
        return {}
    by_normalized_name = {
        str(name).casefold(): value
        for name, value in raw_models.items()
        if isinstance(value, dict)
    }
    fields = (
        "cycleTime",
        "cycleAgeHours",
        "maximumCycleAgeHours",
        "freshnessStatus",
    )
    result: dict[str, dict[str, Any]] = {}
    for model in sorted({str(value) for value in accepted_models}):
        source = by_normalized_name.get(model.casefold())
        if source is None:
            continue
        result[model] = {field: source.get(field) for field in fields}
    return result


def load_locations(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    site = config.get("site") if isinstance(config.get("site"), dict) else {}
    raw = (
        config.get("locations")
        or config.get("points")
        or config.get("stations")
        or site.get("points")
    )
    locations: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for name, item in raw.items():
            if not isinstance(item, dict):
                continue
            latitude = _first(item, "latitude", "lat")
            longitude = _first(item, "longitude", "lon", "lng")
            if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
                locations[str(item.get("id") or name)] = {
                    **item,
                    "latitude": float(latitude),
                    "longitude": float(longitude),
                }
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = _first(item, "station", "id", "name")
            latitude = _first(item, "latitude", "lat")
            longitude = _first(item, "longitude", "lon", "lng")
            if name and isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
                canonical_name = str(name).upper() if item.get("station") else str(name)
                if canonical_name.lower() == "danville":
                    canonical_name = "Danville"
                locations[canonical_name] = {
                    **item,
                    "latitude": float(latitude),
                    "longitude": float(longitude),
                }
    return locations or dict(DEFAULT_LOCATIONS)


def load_neighborhood(config: dict[str, Any]) -> dict[str, float]:
    site = config.get("site") if isinstance(config.get("site"), dict) else {}
    raw = (
        config.get("neighborhood")
        or config.get("neighborhoodBounds")
        or config.get("boundingBox")
        or config.get("area")
        or site.get("neighborhoodBounds")
        or site.get("boundingBox")
        or {}
    )
    if isinstance(raw, dict) and isinstance(raw.get("bounds"), dict):
        raw = raw["bounds"]
    defaults = {"west": -84.95, "east": -84.60, "south": 37.48, "north": 37.80}
    aliases = {
        "west": ("west", "left", "leftlon", "minLongitude"),
        "east": ("east", "right", "rightlon", "maxLongitude"),
        "south": ("south", "bottom", "bottomlat", "minLatitude"),
        "north": ("north", "top", "toplat", "maxLatitude"),
    }
    result: dict[str, float] = {}
    for target, names in aliases.items():
        value = _first(raw, *names) if isinstance(raw, dict) else None
        result[target] = float(value) if isinstance(value, (int, float)) else defaults[target]
    return result


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def round_number(value: float | None, digits: int = 3) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def parse_step_range(value: Any, fallback: Any = None) -> dict[str, float | str | None]:
    text = str(value if value not in (None, "") else fallback if fallback is not None else "")
    cleaned = text.lower().replace("hours", "").replace("hour", "").strip()
    numbers: list[float] = []
    for token in cleaned.replace("to", "-").split("-"):
        try:
            numbers.append(float(token.strip()))
        except ValueError:
            continue
    if len(numbers) >= 2:
        start, end = numbers[0], numbers[-1]
    elif len(numbers) == 1:
        start, end = 0.0, numbers[0]
    else:
        start = end = None
    return {
        "stepRange": text or None,
        "startHour": round_number(start),
        "endHour": round_number(end),
        "durationHours": round_number(end - start) if start is not None and end is not None else None,
    }


def _safe_get(eccodes: Any, gid: Any, key: str, default: Any = None) -> Any:
    try:
        return eccodes.codes_get(gid, key)
    except Exception:
        return default


def _valid_time(eccodes: Any, gid: Any) -> tuple[str, str, float]:
    data_date_value = _safe_get(eccodes, gid, "dataDate", 0)
    data_time_value = _safe_get(eccodes, gid, "dataTime", 0)
    data_date = int(0 if data_date_value is None else data_date_value)
    data_time = int(0 if data_time_value is None else data_time_value)
    valid_date_value = _safe_get(eccodes, gid, "validityDate", None)
    if valid_date_value is None:
        valid_date_value = _safe_get(eccodes, gid, "validDate", None)
    valid_time_value = _safe_get(eccodes, gid, "validityTime", None)
    if valid_time_value is None:
        valid_time_value = _safe_get(eccodes, gid, "validTime", None)
    # Midnight is encoded as numeric zero.  Test explicitly for missing values
    # instead of using truthiness, which previously changed 00Z to the cycle hour.
    valid_date = int(data_date if valid_date_value is None else valid_date_value)
    valid_time = int(data_time if valid_time_value is None else valid_time_value)
    cycle = datetime.strptime(f"{data_date:08d}{data_time:04d}", "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    valid = datetime.strptime(f"{valid_date:08d}{valid_time:04d}", "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    lead = (valid - cycle).total_seconds() / 3600
    return (
        cycle.isoformat(timespec="minutes").replace("+00:00", "Z"),
        valid.isoformat(timespec="minutes").replace("+00:00", "Z"),
        lead,
    )


def _pressure_level(type_of_level: str, level: Any) -> int | None:
    if type_of_level.lower() not in {"isobaricinhpa", "isobaricins", "pressurefromgroundlayer"}:
        return None
    try:
        number = int(round(float(level)))
    except (TypeError, ValueError):
        return None
    if number > 2000:
        number //= 100
    return number


def _pressure_layer_depth_mb(type_of_level: str, level: Any) -> int | None:
    """Decode NOAA's N-0 mb-above-ground layer key without calling it surface."""

    if type_of_level.lower() != "pressurefromgroundlayer":
        return None
    try:
        number = int(round(float(level)))
    except (TypeError, ValueError):
        return None
    if number > 2000:
        number //= 100
    return number if number > 0 else None


def _convective_field_name(
    short: str, type_lower: str, level: Any, long_name: str
) -> str | None:
    if short not in {"cape", "cin"}:
        return None
    quantity = "Cape" if short == "cape" else "Cin"
    suffix = "Jkg"
    if type_lower == "surface":
        return f"surface{quantity}{suffix}"
    layer_depth = _pressure_layer_depth_mb(type_lower, level)
    if layer_depth is not None:
        prefix = "cape" if short == "cape" else "cin"
        return f"{prefix}{layer_depth}To0Mb{suffix}"
    if "most" in long_name.lower() or "entire" in type_lower:
        return f"mostUnstable{quantity}{suffix}"
    # Retain backward compatibility for providers that omit a usable level type,
    # while ensuring every recognized pressure layer above is kept separately.
    return f"surface{quantity}{suffix}"


def field_name(short_name: str, type_of_level: str, level: Any, long_name: str = "") -> str | None:
    short = short_name.lower()
    type_lower = type_of_level.lower()
    pressure = _pressure_level(type_of_level, level)
    convective_field = _convective_field_name(short, type_lower, level, long_name)
    if convective_field is not None:
        return convective_field
    if short in {"2t"} or (short in {"t", "tmp"} and "heightaboveground" in type_lower and float(level or 0) == 2):
        return "temperatureF"
    if short in {"2d", "dpt"} or (short in {"d", "dewpoint"} and "heightaboveground" in type_lower):
        return "dewpointF"
    if short in {"mx2t3", "tmax", "maxt"}:
        return "maximumTemperatureF"
    if short in {"mn2t3", "tmin", "mint"}:
        return "minimumTemperatureF"
    if pressure in {925, 850, 700, 500}:
        prefix = {
            "t": "temperature",
            "tmp": "temperature",
            "r": "relativeHumidity",
            "rh": "relativeHumidity",
            "u": "uWind",
            "ugrd": "uWind",
            "v": "vWind",
            "vgrd": "vWind",
            "w": "verticalVelocity",
            "vvel": "verticalVelocity",
            "gh": "height",
            "hgt": "height",
        }.get(short)
        if prefix:
            suffix = {
                "temperature": "C",
                "relativeHumidity": "Pct",
                "uWind": "Mph",
                "vWind": "Mph",
                "verticalVelocity": "PaPerS",
                "height": "M",
            }[prefix]
            return f"{prefix}{pressure}hPa{suffix}"
    if short in {"10u", "u10"} or (short in {"u", "ugrd"} and "heightaboveground" in type_lower):
        return "uWindMph"
    if short in {"10v", "v10"} or (short in {"v", "vgrd"} and "heightaboveground" in type_lower):
        return "vWindMph"
    if short in {"10fg", "gust", "gustsfc"} or "wind gust" in long_name.lower():
        return "gustMph"
    if short in {"tp", "apcp"}:
        return "precipitationIn"
    if short in {"prate", "tprate"}:
        return "precipitationRateInPerHour"
    if short in {"msl", "prmsl", "mslma"}:
        return "seaLevelPressureHpa"
    if short in {"sp", "pres"} and "surface" in type_lower:
        return "surfacePressureHpa"
    if short in {"tcc", "tcdc"}:
        return "cloudCoverPct"
    if short in {"vis", "visibility"}:
        return "visibilityMi"
    if short in {"pwat", "tcwv"}:
        return "precipitableWaterIn"
    if short == "ptype":
        return "precipitationTypeCode"
    return None


def ensemble_spread_field(field: str) -> str:
    replacements = {
        "temperatureF": "temperatureSpreadF",
        "dewpointF": "dewpointSpreadF",
        "maximumTemperatureF": "maximumTemperatureSpreadF",
        "minimumTemperatureF": "minimumTemperatureSpreadF",
        "uWindMph": "uWindSpreadMph",
        "vWindMph": "vWindSpreadMph",
        "gustMph": "gustSpreadMph",
        "precipitationIn": "precipitationSpreadIn",
        "seaLevelPressureHpa": "seaLevelPressureSpreadHpa",
        "cloudCoverPct": "cloudCoverSpreadPct",
        "precipitableWaterIn": "precipitableWaterSpreadIn",
    }
    return replacements.get(field, f"{field}Spread")


def convert_value(field: str, value: Any, units: str) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or abs(number) >= 1e20:
        return None
    unit = units.lower().replace(" ", "")
    if field in {
        "temperatureSpreadF",
        "dewpointSpreadF",
        "maximumTemperatureSpreadF",
        "minimumTemperatureSpreadF",
    }:
        if unit in {"k", "kelvin", "c", "degc", "celsius"} or number < 150:
            number *= 9 / 5
    elif field.endswith("TemperatureF") or field in {"temperatureF", "dewpointF"}:
        if unit in {"k", "kelvin"} or number > 150:
            number = (number - 273.15) * 9 / 5 + 32
        elif unit in {"c", "degc", "celsius"}:
            number = number * 9 / 5 + 32
    elif field.startswith("temperature") and field.endswith("C"):
        if unit in {"k", "kelvin"} or number > 150:
            number -= 273.15
    elif field.endswith("Mph"):
        if "m/s" in units.lower() or "ms**-1" in unit or unit in {"m/s", "ms-1"}:
            number *= 2.2369362921
        elif "km" in unit:
            number *= 0.6213711922
    elif field in {"seaLevelPressureHpa", "surfacePressureHpa", "seaLevelPressureSpreadHpa"}:
        if unit in {"pa", "pascal", "pascals"} or number > 2000:
            number /= 100
    elif field in {
        "precipitationIn",
        "precipitableWaterIn",
        "precipitationSpreadIn",
        "precipitableWaterSpreadIn",
    }:
        if unit in {"m", "metre", "meter"}:
            number *= 39.37007874
        elif "kg" in unit or "mm" in unit:
            number *= 0.03937007874
    elif field == "precipitationRateInPerHour":
        if "kg" in unit or "mm" in unit:
            number *= 3600 * 0.03937007874
        elif unit in {"m/s", "ms-1"}:
            number *= 3600 * 39.37007874
    elif field == "visibilityMi":
        if unit in {"m", "metre", "meter"}:
            number *= 0.0006213711922
        elif "km" in unit:
            number *= 0.6213711922
    elif field in {"cloudCoverPct", "cloudCoverSpreadPct"}:
        if "0-1" in unit or "fraction" in unit:
            number *= 100
        number = max(0.0, min(100.0, number))
    return round_number(number)


def nearest_value(eccodes: Any, gid: Any, latitude: float, longitude: float) -> tuple[float | None, dict[str, float]]:
    nearest = eccodes.codes_grib_find_nearest(gid, latitude, longitude, is_lsm=False, npoints=1)
    if isinstance(nearest, dict):
        nearest = [nearest]
    if not nearest:
        return None, {}
    item = nearest[0]
    return item.get("value"), {
        "gridLatitude": round_number(float(item.get("lat"))) if item.get("lat") is not None else None,
        "gridLongitude": round_number(float(item.get("lon"))) if item.get("lon") is not None else None,
        "distanceKm": round_number(float(item.get("distance"))) if item.get("distance") is not None else None,
    }


def neighborhood_values(eccodes: Any, gid: Any, bounds: dict[str, float]) -> list[float]:
    try:
        latitudes = eccodes.codes_get_array(gid, "latitudes")
        longitudes = eccodes.codes_get_array(gid, "longitudes")
        values = eccodes.codes_get_array(gid, "values")
    except Exception:
        return []
    selected: list[float] = []
    for latitude, longitude, value in zip(latitudes, longitudes, values):
        lon = float(longitude)
        if lon > 180:
            lon -= 360
        number = float(value)
        if (
            bounds["south"] <= float(latitude) <= bounds["north"]
            and bounds["west"] <= lon <= bounds["east"]
            and math.isfinite(number)
            and abs(number) < 1e20
        ):
            selected.append(number)
    return selected


def stats(values: Iterable[float]) -> dict[str, float | int | None]:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    return {
        "count": len(finite),
        "min": round_number(min(finite)) if finite else None,
        "max": round_number(max(finite)) if finite else None,
        "mean": round_number(statistics.fmean(finite)) if finite else None,
        "p10": round_number(percentile(finite, 0.10)),
        "p90": round_number(percentile(finite, 0.90)),
    }


def derive_point_fields(point: dict[str, Any]) -> None:
    u = point.get("uWindMph")
    v = point.get("vWindMph")
    if isinstance(u, (int, float)) and isinstance(v, (int, float)):
        point["windSpeedMph"] = round_number(math.hypot(u, v))
        # Meteorological direction: where wind is coming from.
        point["windDirectionDeg"] = round_number((270 - math.degrees(math.atan2(v, u))) % 360, 1)
    temperature = point.get("temperatureF")
    dewpoint = point.get("dewpointF")
    if isinstance(temperature, (int, float)) and isinstance(dewpoint, (int, float)):
        point["dewpointDepressionF"] = round_number(temperature - dewpoint)
        tc = (temperature - 32) * 5 / 9
        dc = (dewpoint - 32) * 5 / 9
        try:
            rh = 100 * math.exp((17.625 * dc) / (243.04 + dc) - (17.625 * tc) / (243.04 + tc))
            point["relativeHumidity2mPct"] = round_number(max(0.0, min(100.0, rh)))
        except (OverflowError, ZeroDivisionError):
            pass


def _diagnostic_value(value: Any) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, dict):
        mean = value.get("mean")
        if isinstance(mean, (int, float)) and math.isfinite(float(mean)):
            return float(mean)
    return None


def derive_convective_diagnostic(container: dict[str, Any]) -> dict[str, str | None]:
    """Select one review diagnostic without discarding the source variants.

    The maximum available CAPE is a conservative instability diagnostic. CIN is
    taken from the same encoded layer so the two values remain physically paired.
    Every original surface/180-mb/255-mb field stays in ``container``.
    """

    candidates = [
        (field, value)
        for field in CONVECTIVE_CAPE_FIELDS
        if (value := _diagnostic_value(container.get(field))) is not None
    ]
    if not candidates:
        return {"capeSourceField": None, "cinSourceField": None}
    cape_source, _ = max(candidates, key=lambda item: item[1])
    container["forecastCapeJkg"] = container[cape_source]
    container["forecastCapeSourceField"] = cape_source
    cin_source = PAIRED_CIN_FIELDS[cape_source]
    if _diagnostic_value(container.get(cin_source)) is not None:
        container["forecastCinJkg"] = container[cin_source]
        container["forecastCinSourceField"] = cin_source
    else:
        cin_source = None
    return {"capeSourceField": cape_source, "cinSourceField": cin_source}


def extract_file(
    eccodes: Any,
    entry: dict[str, Any],
    locations: dict[str, dict[str, Any]],
    bounds: dict[str, float],
    records: dict[tuple[str, str, str, float], dict[str, Any]],
) -> tuple[int, list[str]]:
    path = Path(str(entry["localPath"]))
    if not path.is_file():
        return 0, [f"Missing staged GRIB file: {path}"]
    decoded = 0
    warnings: list[str] = []
    with path.open("rb") as handle:
        while True:
            gid = eccodes.codes_grib_new_from_file(handle)
            if gid is None:
                break
            try:
                short_name = str(_safe_get(eccodes, gid, "shortName", ""))
                type_of_level = str(_safe_get(eccodes, gid, "typeOfLevel", ""))
                level = _safe_get(eccodes, gid, "level")
                long_name = str(_safe_get(eccodes, gid, "name", ""))
                field = field_name(short_name, type_of_level, level, long_name)
                if not field:
                    continue
                model = str(entry.get("model") or _safe_get(eccodes, gid, "centre", "unknown"))
                variant = str(entry.get("variant") or entry.get("component") or "deterministic")
                if model.lower() == "gefs" and "spread" in variant.lower():
                    field = ensemble_spread_field(field)
                units = str(_safe_get(eccodes, gid, "units", ""))
                cycle, valid_time, lead = _valid_time(eccodes, gid)
                record_key = (model, cycle, valid_time, round(lead, 3))
                record = records.setdefault(
                    record_key,
                    {
                        "model": model,
                        "product": entry.get("product"),
                        "variants": [],
                        "cycle": cycle,
                        "validTime": valid_time,
                        "leadTimeHours": round_number(lead),
                        "sourceUrls": [],
                        "sourceFiles": [],
                        "points": {name: {} for name in locations},
                        "neighborhood": {},
                        "fieldMetadata": {},
                    },
                )
                if variant not in record["variants"]:
                    record["variants"].append(variant)
                source_url = entry.get("sourceUrl")
                if source_url and source_url not in record["sourceUrls"]:
                    record["sourceUrls"].append(source_url)
                relative_path = repo_relative(path)
                if relative_path not in record["sourceFiles"]:
                    record["sourceFiles"].append(relative_path)

                metadata: dict[str, Any] = {
                    "shortName": short_name,
                    "name": long_name,
                    "units": units,
                    "typeOfLevel": type_of_level,
                    "level": level,
                }
                if field in INTERVAL_METADATA_KEYS:
                    step_range_fallback = (
                        _safe_get(eccodes, gid, "endStep", lead)
                        if field == "precipitationIn"
                        else None
                    )
                    metadata.update(
                        parse_step_range(
                            _safe_get(eccodes, gid, "stepRange"),
                            step_range_fallback,
                        )
                    )
                record["fieldMetadata"][field] = metadata

                for name, location in locations.items():
                    raw_value, nearest = nearest_value(
                        eccodes, gid, float(location["latitude"]), float(location["longitude"])
                    )
                    converted = convert_value(field, raw_value, units)
                    if converted is None:
                        continue
                    record["points"][name][field] = converted
                    record["points"][name].setdefault("gridPoint", nearest)
                    interval_key = INTERVAL_METADATA_KEYS.get(field)
                    if interval_key is not None:
                        record["points"][name][interval_key] = {
                            key: metadata.get(key)
                            for key in ("stepRange", "startHour", "endHour", "durationHours")
                        }

                raw_neighborhood = neighborhood_values(eccodes, gid, bounds)
                converted_neighborhood = [
                    converted
                    for value in raw_neighborhood
                    if (converted := convert_value(field, value, units)) is not None
                ]
                if converted_neighborhood:
                    record["neighborhood"][field] = stats(converted_neighborhood)
                decoded += 1
            except Exception as error:
                warnings.append(f"{path.name}: skipped one GRIB message: {error}")
            finally:
                eccodes.codes_release(gid)
    return decoded, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--strict", action="store_true", help="Fail if any staged file is missing or undecodable")
    args = parser.parse_args(argv)
    args.output = ensure_safe_output(args.output)

    config = read_json(args.config)
    manifest_path = args.manifest or find_latest_manifest(DEFAULT_MANIFEST_DIR)
    if manifest_path.name.lower() == "latest.json":
        raise ValueError(
            "--manifest must identify an immutable versioned manifest, not latest.json"
        )
    manifest = read_json(manifest_path)
    files = manifest_files(manifest, manifest_path)
    if not files:
        raise SystemExit(f"No successful GRIB files found in manifest {manifest_path}")

    try:
        import eccodes  # type: ignore
    except ImportError as error:
        raise SystemExit(
            "The eccodes package is required. Run the repository GRIB setup before extraction."
        ) from error

    locations = load_locations(config)
    bounds = load_neighborhood(config)
    records: dict[tuple[str, str, str, float], dict[str, Any]] = {}
    warnings: list[str] = []
    decoded_messages = 0
    failed_files = 0
    for entry in files:
        decoded, file_warnings = extract_file(eccodes, entry, locations, bounds, records)
        decoded_messages += decoded
        warnings.extend(file_warnings)
        if decoded == 0:
            failed_files += 1
            warnings.append(f"No requested fields decoded from {entry['localPath']}")

    ordered_records = sorted(
        records.values(), key=lambda item: (item["model"], item["cycle"], item["validTime"])
    )
    for record in ordered_records:
        point_sources: dict[str, dict[str, str | None]] = {}
        for point in record["points"].values():
            derive_point_fields(point)
        for name, point in record["points"].items():
            point_sources[name] = derive_convective_diagnostic(point)
        neighborhood_sources = derive_convective_diagnostic(record["neighborhood"])
        record["fieldMetadata"]["forecastCapeJkg"] = {
            "derived": True,
            "selectionMethod": "maximum-available-cape-with-paired-cin",
            "candidateFields": list(CONVECTIVE_CAPE_FIELDS),
            "pointSelections": point_sources,
            "neighborhoodSelection": neighborhood_sources,
        }

    models = sorted({str(record["model"]) for record in ordered_records})
    cycles = sorted({str(record["cycle"]) for record in ordered_records})
    payload = {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "issuedAt": manifest.get("issuedAt"),
        "sourceManifest": repo_relative(manifest_path),
        "configPath": repo_relative(args.config),
        "provenance": {
            "sourceManifest": {
                "path": repo_relative(manifest_path),
                "sha256": sha256_file(manifest_path),
            },
            "configuration": {
                "path": repo_relative(args.config),
                "sha256": sha256_file(args.config),
            },
        },
        "sourceAttributions": manifest.get("sourceAttributions", {}),
        "sourceNotes": manifest.get("sourceNotes", []),
        "requestedModels": manifest.get("requestedModels", models),
        "modelResults": manifest.get("modelResults", manifest.get("models", [])),
        "modelFreshness": manifest_model_freshness(manifest, models),
        "locations": locations,
        "neighborhoodBounds": bounds,
        "statistics": {
            "manifestFileCount": len(files),
            "failedFileCount": failed_files,
            "decodedMessageCount": decoded_messages,
            "recordCount": len(ordered_records),
            "models": models,
            "cycles": cycles,
        },
        "records": ordered_records,
        "warnings": warnings,
    }
    if args.strict and (warnings or failed_files or not ordered_records):
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        return 2
    atomic_write_json(args.output, payload)
    print(f"Extracted {decoded_messages} messages into {len(ordered_records)} records: {args.output}")
    if warnings:
        print(f"Completed with {len(warnings)} warning(s).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
