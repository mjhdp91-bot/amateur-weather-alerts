#!/usr/bin/env python3
"""Fetch small, auditable model subsets for Danville forecast research.

The fetcher never reads or writes ``forecast.json``.  It selects one complete
cycle per model, downloads only configured fields, validates each GRIB file,
then atomically promotes the cycle and writes a machine-readable manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlencode

from common import (
    REPO_ROOT,
    UTC,
    DownloadError,
    GuidanceError,
    HttpClient,
    IntegrityError,
    atomic_write_json,
    candidate_cycles,
    cycle_id,
    iso_z,
    load_json,
    maximum_lead,
    parse_utc,
    repo_relative,
    resolve_guidance_root_path,
    resolve_repo_path,
    safe_rmtree,
    select_leads,
    sha256_file,
    utc_now,
)


NOAA_FILTER_ROOT = "https://nomads.ncep.noaa.gov/cgi-bin"
NOAA_ARCHIVE_ROOT = "https://nomads.ncep.noaa.gov/pub/data/nccf/com"
COMPLETE_STATUS = "complete"


@dataclass(frozen=True)
class FilePlan:
    model: str
    product: str
    variant: str
    cycle: datetime
    forecast_hour: int
    source_url: str
    filename: str
    provider: str
    request: Mapping[str, Any] = field(default_factory=dict)

    @property
    def valid_time(self) -> datetime:
        return self.cycle + timedelta(hours=self.forecast_hour)


@dataclass
class ModelPlan:
    model: str
    cycle: datetime
    partial_horizon: bool
    files: list[FilePlan]
    discovery: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CompletedCycle:
    """A validated, safely removable raw model-cycle directory."""

    model: str
    cycle: str
    cycle_time: datetime
    path: Path
    byte_size: int


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch official GRIB guidance subsets without publishing a forecast."
    )
    parser.add_argument(
        "--config",
        default="config/grib/danville.json",
        help="Repository-relative GRIB configuration path.",
    )
    parser.add_argument(
        "--models",
        help="Comma-separated configured model keys; default is every enabled model.",
    )
    parser.add_argument(
        "--hours",
        type=int,
        help="Guidance horizon from issued time; default comes from configuration.",
    )
    parser.add_argument(
        "--issued-at",
        help="ISO-8601 forecast issuance time; default is the current UTC time.",
    )
    parser.add_argument(
        "--as-of",
        help=(
            "ISO-8601 availability cutoff used for cycle discovery; defaults to "
            "the issuance time and may not be later than it."
        ),
    )
    parser.add_argument(
        "--output-root",
        help="Cache root beneath grib-data; default comes from configuration.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan nominal requests without any network access.",
    )
    mode.add_argument(
        "--discover-only",
        action="store_true",
        help="Validate available cycles/indexes without downloading GRIB data.",
    )
    mode.add_argument(
        "--cache-only",
        action="store_true",
        help=(
            "Regenerate the fetch manifest from exact SHA-256-verified cached "
            "cycles without network access or downloads."
        ),
    )
    parser.add_argument(
        "--keep-raw",
        action="store_true",
        help="Do not prune completed raw cycles beyond the configured retention count.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print source discovery and download progress to stderr.",
    )
    return parser.parse_args(argv)


def log(message: str, *, verbose: bool = True) -> None:
    if verbose:
        print(message, file=sys.stderr, flush=True)


def choose_models(config: Mapping[str, Any], requested: str | None) -> list[str]:
    models = config.get("models")
    if not isinstance(models, dict):
        raise ValueError("Configuration must contain a models object")
    if requested:
        selected = [item.strip() for item in requested.split(",") if item.strip()]
        unknown = [item for item in selected if item not in models]
        if unknown:
            raise ValueError(f"Unknown model(s): {', '.join(unknown)}")
        disabled = [item for item in selected if not models[item].get("enabled", False)]
        if disabled:
            raise ValueError(f"Requested model(s) are disabled: {', '.join(disabled)}")
        return list(dict.fromkeys(selected))
    return [key for key, value in models.items() if value.get("enabled", False)]


def resolve_request_times(
    issued_at_value: str | None,
    as_of_value: str | None,
) -> tuple[datetime, datetime]:
    """Resolve an issuance and data cutoff without permitting look-ahead data."""

    reference = utc_now()
    issued_at = parse_utc(issued_at_value) if issued_at_value else reference
    as_of = parse_utc(as_of_value) if as_of_value else issued_at
    if as_of > issued_at:
        raise ValueError(
            "--as-of may not be later than --issued-at; model guidance initialized "
            "after issuance would introduce look-ahead data."
        )
    return issued_at, as_of


def ordered_cycle_candidates(
    model: Mapping[str, Any], as_of: datetime, valid_through: datetime
) -> list[datetime]:
    candidates = candidate_cycles(
        as_of,
        model["cycles"],
        int(model.get("lookbackCycles", 8)),
    )
    # Prefer the cycle that reaches farthest into the requested period, then the
    # newest cycle.  This selects extended HRRR/RAP cycles over a newer short run.
    return sorted(
        candidates,
        key=lambda value: (
            min(value + timedelta(hours=maximum_lead(model, value)), valid_through),
            value,
        ),
        reverse=True,
    )


def lifecycle_allows(model: Mapping[str, Any], cycle: datetime) -> tuple[bool, str | None]:
    lifecycle = model.get("lifecycle")
    if not isinstance(lifecycle, dict) or not lifecycle.get("scheduledEnd"):
        return True, None
    end = parse_utc(str(lifecycle["scheduledEnd"]))
    if cycle >= end:
        replacement = lifecycle.get("replacementFamily", "the replacement system")
        return False, f"source ended at {iso_z(end)}; use {replacement} after validation"
    return True, None


def maximum_cycle_age_hours(model: Mapping[str, Any]) -> float:
    """Return the oldest model cycle that may support a new forecast issuance."""

    value = float(model.get("maximumCycleAgeHours", 12))
    if value <= 0:
        raise ValueError("maximumCycleAgeHours must be positive")
    return value


def cycle_freshness_allows(
    model: Mapping[str, Any], issued_at: datetime, cycle: datetime
) -> tuple[bool, float, str | None]:
    """Reject look-ahead and silently stale cycles before any source access."""

    age_hours = (issued_at - cycle).total_seconds() / 3600
    maximum_age = maximum_cycle_age_hours(model)
    if age_hours < 0:
        return False, age_hours, "cycle begins after forecast issuance"
    if age_hours > maximum_age:
        return (
            False,
            age_hours,
            f"cycle age {age_hours:.1f}h exceeds configured maximum {maximum_age:.1f}h",
        )
    return True, age_hours, None


def format_template(template: str, cycle: datetime, lead: int) -> str:
    return template.format(
        date=cycle.strftime("%Y%m%d"),
        hour=cycle.hour,
        forecastHour=lead,
    )


def noaa_components(model: Mapping[str, Any]) -> list[dict[str, Any]]:
    components = model.get("components")
    if isinstance(components, list) and components:
        return [dict(component) for component in components]
    return [
        {
            "variant": str(model.get("variant", "deterministic")),
            "fileTemplate": str(model["fileTemplate"]),
        }
    ]


def noaa_archive_url(
    model: Mapping[str, Any], cycle: datetime, lead: int, component: Mapping[str, Any]
) -> str:
    directory = format_template(str(model["directoryTemplate"]), cycle, lead)
    filename = format_template(str(component["fileTemplate"]), cycle, lead)
    return (
        f"{NOAA_ARCHIVE_ROOT}/{model['archiveModel']}/prod"
        f"{directory}/{filename}"
    )


def noaa_filter_url(
    model: Mapping[str, Any],
    cycle: datetime,
    lead: int,
    component: Mapping[str, Any],
    bounding_box: Mapping[str, Any],
) -> str:
    directory = format_template(str(model["directoryTemplate"]), cycle, lead)
    filename = format_template(str(component["fileTemplate"]), cycle, lead)
    pairs: list[tuple[str, str]] = [("file", filename)]
    pairs.extend((f"var_{variable}", "on") for variable in model["variables"])
    pairs.extend((f"lev_{level}", "on") for level in model["levels"])
    pairs.extend(
        [
            ("subregion", ""),
            ("leftlon", str(bounding_box["west"])),
            ("rightlon", str(bounding_box["east"])),
            ("toplat", str(bounding_box["north"])),
            ("bottomlat", str(bounding_box["south"])),
            ("dir", directory),
        ]
    )
    return f"{NOAA_FILTER_ROOT}/{model['filter']}?{urlencode(pairs)}"


def ecmwf_file_url(model: Mapping[str, Any], cycle: datetime, lead: int) -> str:
    cycle_stamp = cycle.strftime("%Y%m%d%H") + "0000"
    filename = f"{cycle_stamp}-{lead}h-{model['stream']}-{model['type']}.grib2"
    return (
        f"{model['indexRoot']}/{cycle:%Y%m%d}/{cycle:%H}z/"
        f"{model['model']}/{model['resolution']}/{model['stream']}/{filename}"
    )


def build_noaa_plan(
    key: str,
    model: Mapping[str, Any],
    cycle: datetime,
    issued_at: datetime,
    valid_through: datetime,
    bounding_box: Mapping[str, Any],
) -> ModelPlan:
    leads, partial = select_leads(model, cycle, issued_at, valid_through)
    files: list[FilePlan] = []
    for lead in leads:
        for component in noaa_components(model):
            remote_name = format_template(str(component["fileTemplate"]), cycle, lead)
            local_name = remote_name if remote_name.endswith(".grib2") else f"{remote_name}.grib2"
            files.append(
                FilePlan(
                    model=key,
                    product=str(model["product"]),
                    variant=str(component["variant"]),
                    cycle=cycle,
                    forecast_hour=lead,
                    source_url=noaa_filter_url(
                        model, cycle, lead, component, bounding_box
                    ),
                    filename=local_name,
                    provider="NOAA/NCEP NOMADS Grib Filter",
                    request={
                        "archiveIndexUrl": f"{noaa_archive_url(model, cycle, lead, component)}.idx"
                    },
                )
            )
    return ModelPlan(model=key, cycle=cycle, partial_horizon=partial, files=files)


def build_ecmwf_plan(
    key: str,
    model: Mapping[str, Any],
    cycle: datetime,
    issued_at: datetime,
    valid_through: datetime,
) -> ModelPlan:
    leads, partial = select_leads(model, cycle, issued_at, valid_through)
    files: list[FilePlan] = []
    for lead in leads:
        for product in model["products"]:
            interval = int(product.get("intervalHours", 3))
            if lead % interval != 0:
                continue
            name = str(product["name"])
            files.append(
                FilePlan(
                    model=key,
                    product=f"{model['product']}-{name}",
                    variant=str(model.get("variant", "deterministic")),
                    cycle=cycle,
                    forecast_hour=lead,
                    source_url=ecmwf_file_url(model, cycle, lead),
                    filename=(
                        f"ecmwf_ifs_{model['resolution']}_{name}."
                        f"t{cycle:%H}z.f{lead:03d}.grib2"
                    ),
                    provider="ECMWF Open Data",
                    request={
                        "sourceCandidates": list(model["sources"]),
                        "model": model["model"],
                        "resolution": model["resolution"],
                        "stream": model["stream"],
                        "type": model["type"],
                        "levtype": product["levtype"],
                        "parameters": list(product["parameters"]),
                        "requiredParameters": list(product["requiredParameters"]),
                        "levels": list(product.get("levels", [])),
                    },
                )
            )
    return ModelPlan(model=key, cycle=cycle, partial_horizon=partial, files=files)


def build_plan(
    key: str,
    model: Mapping[str, Any],
    cycle: datetime,
    issued_at: datetime,
    valid_through: datetime,
    bounding_box: Mapping[str, Any],
) -> ModelPlan:
    provider = model["provider"]
    if provider == "noaa-nomads":
        return build_noaa_plan(
            key, model, cycle, issued_at, valid_through, bounding_box
        )
    if provider == "ecmwf-open-data":
        return build_ecmwf_plan(key, model, cycle, issued_at, valid_through)
    raise ValueError(f"Unsupported provider for {key}: {provider}")


def validate_index_text(text: str, patterns: Iterable[str], url: str) -> None:
    stripped = text.lstrip()
    if not stripped or stripped.startswith("<"):
        raise IntegrityError(f"Index response is empty or HTML: {url}")
    missing = [pattern for pattern in patterns if pattern not in text]
    if missing:
        raise IntegrityError(
            f"Index lacks required fields at {url}: {', '.join(missing)}"
        )


def parse_ecmwf_index(text: str, url: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise IntegrityError(
                f"Invalid ECMWF JSON-lines index at {url}, line {line_number}"
            ) from error
        if isinstance(value, dict):
            records.append(value)
    if not records:
        raise IntegrityError(f"Empty ECMWF index: {url}")
    return records


def index_parameter(record: Mapping[str, Any]) -> str:
    for key in ("param", "_param", "shortName"):
        if record.get(key) is not None:
            return str(record[key])
    return ""


def discover_noaa_plan(
    plan: ModelPlan, model: Mapping[str, Any], client: HttpClient
) -> dict[str, Any]:
    if not plan.files:
        raise IntegrityError(f"No usable forecast steps for {plan.model}")
    checked: list[str] = []
    # A cycle is accepted only if the last requested file for every component
    # exists and exposes the required core fields.
    by_variant: dict[str, list[FilePlan]] = {}
    for item in plan.files:
        by_variant.setdefault(item.variant, []).append(item)
    for files in by_variant.values():
        item = max(files, key=lambda value: value.forecast_hour)
        url = str(item.request["archiveIndexUrl"])
        text = client.get_text(url)
        validate_index_text(text, model.get("requiredIndexPatterns", []), url)
        checked.append(url)
    return {"checkedIndexes": checked}


def discover_ecmwf_plan(
    plan: ModelPlan, model: Mapping[str, Any], client: HttpClient
) -> dict[str, Any]:
    if not plan.files:
        raise IntegrityError(f"No usable forecast steps for {plan.model}")
    checked: list[str] = []
    counts: dict[str, int] = {}
    by_product: dict[str, list[FilePlan]] = {}
    for item in plan.files:
        by_product.setdefault(item.product, []).append(item)
    for product, files in by_product.items():
        item = max(files, key=lambda value: value.forecast_hour)
        url = f"{item.source_url.removesuffix('.grib2')}.index"
        records = parse_ecmwf_index(client.get_text(url), url)
        available = {index_parameter(record) for record in records}
        required = {str(value) for value in item.request["requiredParameters"]}
        missing = sorted(required - available)
        if missing:
            raise IntegrityError(
                f"ECMWF index lacks required {product} parameters at {url}: "
                f"{', '.join(missing)}"
            )
        checked.append(url)
        counts[product] = len(records)
    return {"checkedIndexes": checked, "indexRecordCounts": counts}


def discover_plan(
    plan: ModelPlan, model: Mapping[str, Any], client: HttpClient
) -> dict[str, Any]:
    provider = model["provider"]
    if provider == "noaa-nomads":
        return discover_noaa_plan(plan, model, client)
    if provider == "ecmwf-open-data":
        return discover_ecmwf_plan(plan, model, client)
    raise ValueError(f"Unsupported provider: {provider}")


def select_complete_plan(
    key: str,
    model: Mapping[str, Any],
    issued_at: datetime,
    valid_through: datetime,
    as_of: datetime,
    bounding_box: Mapping[str, Any],
    client: HttpClient,
    *,
    dry_run: bool,
    verbose: bool,
) -> tuple[ModelPlan, list[dict[str, str]]]:
    failures: list[dict[str, str]] = []
    candidates = ordered_cycle_candidates(model, as_of, valid_through)
    for cycle in candidates:
        fresh, _, freshness_reason = cycle_freshness_allows(model, issued_at, cycle)
        if not fresh:
            failures.append(
                {"cycleTime": iso_z(cycle), "error": freshness_reason or "stale cycle"}
            )
            continue
        allowed, lifecycle_reason = lifecycle_allows(model, cycle)
        if not allowed:
            failures.append({"cycleTime": iso_z(cycle), "error": lifecycle_reason or "ended"})
            continue
        plan = build_plan(
            key, model, cycle, issued_at, valid_through, bounding_box
        )
        if dry_run:
            plan.discovery = {"status": "not-checked-dry-run"}
            return plan, failures
        try:
            log(f"Discovering {key} cycle {iso_z(cycle)}", verbose=verbose)
            plan.discovery = discover_plan(plan, model, client)
            return plan, failures
        except (DownloadError, IntegrityError) as error:
            failures.append({"cycleTime": iso_z(cycle), "error": str(error)})
            log(f"Rejected {key} cycle {iso_z(cycle)}: {error}", verbose=verbose)
    detail = failures[-1]["error"] if failures else "no candidate cycles"
    raise GuidanceError(f"No complete {key} cycle found: {detail}")


def structural_grib_check(path: Path) -> None:
    size = path.stat().st_size
    if size < 16:
        raise IntegrityError(f"GRIB file is too small: {path}")
    with path.open("rb") as handle:
        if handle.read(4) != b"GRIB":
            raise IntegrityError(f"File does not begin with GRIB framing: {path}")
        handle.seek(-4, os.SEEK_END)
        if handle.read(4) != b"7777":
            raise IntegrityError(f"File does not end with GRIB framing: {path}")


def grib_inventory(path: Path, expected_cycle: datetime, expected_valid: datetime) -> dict[str, Any]:
    """Decode enough metadata to reject wrong cycles, leads and HTML payloads."""

    structural_grib_check(path)
    try:
        from eccodes import codes_get, codes_grib_new_from_file, codes_release
    except ImportError as error:
        raise GuidanceError(
            "The eccodes package is required for downloaded-file validation"
        ) from error

    count = 0
    short_names: set[str] = set()
    grid_types: set[str] = set()
    cycle_values: set[tuple[int, int]] = set()
    valid_values: set[tuple[int, int]] = set()
    with path.open("rb") as handle:
        while True:
            message = codes_grib_new_from_file(handle)
            if message is None:
                break
            try:
                count += 1
                short_names.add(str(codes_get(message, "shortName")))
                grid_types.add(str(codes_get(message, "gridType")))
                cycle_values.add(
                    (
                        int(codes_get(message, "dataDate")),
                        int(codes_get(message, "dataTime")),
                    )
                )
                valid_values.add(
                    (
                        int(codes_get(message, "validityDate")),
                        int(codes_get(message, "validityTime")),
                    )
                )
            finally:
                codes_release(message)
    if count == 0:
        raise IntegrityError(f"No decodable GRIB messages in {path}")
    wanted_cycle = (int(expected_cycle.strftime("%Y%m%d")), expected_cycle.hour * 100)
    wanted_valid = (int(expected_valid.strftime("%Y%m%d")), expected_valid.hour * 100)
    if cycle_values != {wanted_cycle}:
        raise IntegrityError(
            f"Unexpected model cycle in {path}: {sorted(cycle_values)}; wanted {wanted_cycle}"
        )
    if valid_values != {wanted_valid}:
        raise IntegrityError(
            f"Unexpected valid time in {path}: {sorted(valid_values)}; wanted {wanted_valid}"
        )
    return {
        "messageCount": count,
        "shortNames": sorted(short_names),
        "gridTypes": sorted(grid_types),
    }


def required_noaa_short_names(model: Mapping[str, Any]) -> set[str]:
    """Return configured core fields that every downloaded NOAA file must contain."""

    if model.get("provider") != "noaa-nomads":
        return set()
    configured = model.get("requiredShortNames")
    if configured is None:
        if model.get("enabled", False):
            raise ValueError(
                "Enabled NOAA models must configure requiredShortNames for "
                "post-download integrity validation"
            )
        return set()
    if (
        not isinstance(configured, list)
        or not configured
        or any(not isinstance(value, str) or not value.strip() for value in configured)
    ):
        raise ValueError("requiredShortNames must be a non-empty string array")
    return {value.strip() for value in configured}


def validate_required_short_names(
    inventory: Mapping[str, Any],
    required: Iterable[str],
    context: str,
) -> None:
    """Reject cleanly framed but incomplete multi-message GRIB responses."""

    expected = {str(value) for value in required}
    if not expected:
        return
    short_names = inventory.get("shortNames")
    available = (
        {str(value) for value in short_names}
        if isinstance(short_names, list)
        else set()
    )
    missing = sorted(expected - available)
    if missing:
        raise IntegrityError(
            f"GRIB inventory lacks required fields for {context}: {', '.join(missing)}"
        )


def manifest_entry(
    plan: FilePlan,
    final_path: Path,
    *,
    status: str,
    sha256: str | None = None,
    byte_size: int = 0,
    error: str | None = None,
    inventory: Mapping[str, Any] | None = None,
    retrieval_source: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "model": plan.model,
        "product": plan.product,
        "variant": plan.variant,
        "cycleTime": iso_z(plan.cycle),
        "forecastHour": plan.forecast_hour,
        "validTime": iso_z(plan.valid_time),
        "sourceUrl": plan.source_url,
        "localPath": repo_relative(final_path),
        "expectedFilename": plan.filename,
        "requestFingerprint": file_plan_fingerprint(plan),
        "sha256": sha256,
        "byteSize": byte_size,
        "status": status,
    }
    if error:
        result["error"] = error
    if inventory:
        result["inventory"] = dict(inventory)
    if retrieval_source:
        result["retrievalSource"] = retrieval_source
    return result


def file_plan_fingerprint(plan: FilePlan) -> str:
    """Fingerprint every source-selection detail that determines file contents."""

    identity = {
        "model": plan.model,
        "product": plan.product,
        "variant": plan.variant,
        "cycleTime": iso_z(plan.cycle),
        "forecastHour": plan.forecast_hour,
        "validTime": iso_z(plan.valid_time),
        "sourceUrl": plan.source_url,
        "filename": plan.filename,
        "provider": plan.provider,
        "request": plan.request,
    }
    encoded = json.dumps(
        identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def model_plan_fingerprint(plan: ModelPlan) -> str:
    identities = sorted(file_plan_fingerprint(item) for item in plan.files)
    encoded = json.dumps(
        {
            "model": plan.model,
            "cycleTime": iso_z(plan.cycle),
            "files": identities,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def fetch_noaa_file(
    plan: FilePlan,
    staging_path: Path,
    client: HttpClient,
    *,
    verbose: bool,
) -> tuple[str, int, dict[str, Any], str | None]:
    log(
        f"Downloading {plan.model} {plan.variant} f{plan.forecast_hour:03d}",
        verbose=verbose,
    )
    result = client.download(plan.source_url, staging_path)
    try:
        inventory = grib_inventory(staging_path, plan.cycle, plan.valid_time)
    except GuidanceError:
        raise
    except Exception as error:
        raise IntegrityError(f"ecCodes rejected {staging_path}: {error}") from error
    return result.sha256, result.byte_size, inventory, None


_WINDOWS_TRUSTSTORE_INJECTED = False


def inject_windows_truststore() -> bool:
    """Make Requests use Windows' trusted roots without weakening TLS checks."""

    global _WINDOWS_TRUSTSTORE_INJECTED
    if sys.platform != "win32":
        return False
    if _WINDOWS_TRUSTSTORE_INJECTED:
        return True
    try:
        import truststore
    except ImportError as error:
        raise GuidanceError(
            "Windows ECMWF retrieval requires truststore; install the pinned "
            "GRIB requirements instead of disabling TLS certificate verification"
        ) from error
    try:
        truststore.inject_into_ssl()
    except Exception as error:
        raise GuidanceError(
            f"Unable to activate the Windows system TLS trust store: {error}"
        ) from error
    _WINDOWS_TRUSTSTORE_INJECTED = True
    return True


def fetch_ecmwf_file(
    plan: FilePlan,
    staging_path: Path,
    *,
    verbose: bool,
) -> tuple[str, int, dict[str, Any], str | None]:
    inject_windows_truststore()
    try:
        from ecmwf.opendata import Client
    except ImportError as error:
        raise GuidanceError(
            "The ecmwf-opendata package is required for the ECMWF adapter"
        ) from error

    request: dict[str, Any] = {
        "date": plan.cycle.strftime("%Y%m%d"),
        "time": plan.cycle.hour,
        "step": plan.forecast_hour,
        "stream": plan.request["stream"],
        "type": plan.request["type"],
        "levtype": plan.request["levtype"],
        "param": list(plan.request["parameters"]),
        "target": str(staging_path),
    }
    if plan.request.get("levels"):
        request["levelist"] = list(plan.request["levels"])
    errors: list[str] = []
    used_source: str | None = None
    for source in plan.request["sourceCandidates"]:
        staging_path.unlink(missing_ok=True)
        try:
            log(
                f"Downloading ECMWF {plan.product} f{plan.forecast_hour:03d} from {source}",
                verbose=verbose,
            )
            client = Client(
                source=source,
                model=plan.request["model"],
                resol=plan.request["resolution"],
            )
            client.retrieve(**request)
            used_source = str(source)
            break
        except Exception as error:  # official client has backend-specific errors
            errors.append(f"{source}: {error}")
    if used_source is None:
        raise DownloadError(
            f"All ECMWF Open Data sources failed for {plan.filename}: "
            + " | ".join(errors)
        )
    try:
        inventory = grib_inventory(staging_path, plan.cycle, plan.valid_time)
    except GuidanceError:
        raise
    except Exception as error:
        raise IntegrityError(f"ecCodes rejected {staging_path}: {error}") from error
    required = {str(value) for value in plan.request["requiredParameters"]}
    available = set(inventory["shortNames"])
    missing = sorted(required - available)
    if missing:
        raise IntegrityError(
            f"ECMWF subset {staging_path} lacks required fields: {', '.join(missing)}"
        )
    return (
        sha256_file(staging_path),
        staging_path.stat().st_size,
        inventory,
        used_source,
    )


_REUSE_IDENTITY_FIELDS = (
    "model",
    "product",
    "variant",
    "cycleTime",
    "forecastHour",
    "validTime",
    "sourceUrl",
    "localPath",
    "expectedFilename",
    "requestFingerprint",
)


def _entry_identity(entry: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(entry.get(field) for field in _REUSE_IDENTITY_FIELDS)


def verify_reusable_cycle(
    manifest_path: Path,
    plan: ModelPlan,
    final_dir: Path,
    *,
    physical_dir: Path | None = None,
    required_short_names: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    """Return a cache only when it exactly satisfies the newly planned request."""

    if not manifest_path.is_file():
        return None
    try:
        manifest = load_json(manifest_path)
        entries = manifest["files"]
        if (
            manifest.get("status") != COMPLETE_STATUS
            or manifest.get("model") != plan.model
            or manifest.get("cycleTime") != iso_z(plan.cycle)
            or manifest.get("planFingerprint") != model_plan_fingerprint(plan)
            or not isinstance(entries, list)
            or len(entries) != len(plan.files)
            or any(not isinstance(entry, dict) for entry in entries)
        ):
            return None
        expected_entries = [
            manifest_entry(
                item,
                final_dir / item.filename,
                status=COMPLETE_STATUS,
            )
            for item in plan.files
        ]
        expected = {_entry_identity(entry) for entry in expected_entries}
        actual = {_entry_identity(entry) for entry in entries}
        if len(expected) != len(expected_entries) or len(actual) != len(entries):
            return None
        if actual != expected:
            return None
        storage_dir = (physical_dir or final_dir).resolve()
        for entry in entries:
            if (
                entry.get("status") != COMPLETE_STATUS
                or not entry.get("sha256")
                or int(entry.get("byteSize", 0)) < 1
            ):
                return None
            expected_filename = str(entry["expectedFilename"])
            path = (storage_dir / expected_filename).resolve()
            if path.parent != storage_dir:
                return None
            if not path.is_file() or path.stat().st_size != int(entry["byteSize"]):
                return None
            if sha256_file(path) != entry["sha256"]:
                return None
            try:
                validate_required_short_names(
                    entry.get("inventory", {}),
                    required_short_names,
                    str(entry.get("localPath", path)),
                )
            except IntegrityError:
                return None
        return entries, manifest
    except (KeyError, OSError, ValueError, TypeError):
        return None


def select_verified_cached_plan(
    key: str,
    model: Mapping[str, Any],
    issued_at: datetime,
    valid_through: datetime,
    as_of: datetime,
    bounding_box: Mapping[str, Any],
    output_root: Path,
    *,
    verbose: bool,
) -> tuple[ModelPlan, list[dict[str, Any]], list[dict[str, str]]]:
    """Select one exact, integrity-checked cached cycle without network access.

    Every candidate is rebuilt from the current request and configuration before
    its per-cycle manifest, file set, byte sizes and SHA-256 digests are checked.
    Rejected candidates are retained in the returned audit trail; files from
    different cycles are never combined.
    """

    failures: list[dict[str, str]] = []
    candidates = sorted(
        (
            candidate
            for candidate in completed_cycles(output_root)
            if candidate.model == key and candidate.cycle_time <= as_of
        ),
        key=lambda candidate: candidate.cycle_time,
        reverse=True,
    )
    for candidate in candidates:
        fresh, _, freshness_reason = cycle_freshness_allows(
            model, issued_at, candidate.cycle_time
        )
        if not fresh:
            failures.append(
                {
                    "cycleTime": iso_z(candidate.cycle_time),
                    "error": freshness_reason or "stale cached cycle",
                }
            )
            continue
        allowed, lifecycle_reason = lifecycle_allows(model, candidate.cycle_time)
        if not allowed:
            failures.append(
                {
                    "cycleTime": iso_z(candidate.cycle_time),
                    "error": lifecycle_reason or "cycle is outside its configured lifecycle",
                }
            )
            continue
        plan = build_plan(
            key,
            model,
            candidate.cycle_time,
            issued_at,
            valid_through,
            bounding_box,
        )
        verified = verify_reusable_cycle(
            candidate.path / "manifest.json",
            plan,
            candidate.path,
            required_short_names=required_noaa_short_names(model),
        )
        if verified is None:
            failures.append(
                {
                    "cycleTime": iso_z(candidate.cycle_time),
                    "error": "cached cycle does not exactly match the request or failed integrity checks",
                }
            )
            continue
        entries, _ = verified
        plan.discovery = {
            "status": "verified-cache-only",
            "cacheManifest": repo_relative(candidate.path / "manifest.json"),
        }
        log(
            f"Using exact verified cached {key} cycle {candidate.cycle}",
            verbose=verbose,
        )
        return plan, entries, failures

    detail = failures[-1]["error"] if failures else "no completed cached cycles"
    raise GuidanceError(f"No exact verified cached {key} cycle found: {detail}")


def promote_staged_cycle(staging_dir: Path, final_dir: Path, model_root: Path) -> None:
    """Promote a verified stage while keeping an old cache recoverable."""

    model_resolved = model_root.resolve()
    final_resolved = final_dir.resolve()
    if final_resolved.parent != model_resolved:
        raise ValueError(f"Final cycle path is outside model root: {final_resolved}")
    staging_root = (model_resolved / ".staging").resolve()
    staging_resolved = staging_dir.resolve()
    try:
        staging_resolved.relative_to(staging_root)
    except ValueError as error:
        raise ValueError(f"Staging path is outside staging root: {staging_resolved}") from error

    backup_root = model_resolved / ".replacement-backups"
    backup_dir: Path | None = None
    if final_dir.exists():
        backup_root.mkdir(parents=True, exist_ok=True)
        backup_dir = backup_root / f"{final_dir.name}-{uuid.uuid4().hex}"
        os.replace(final_dir, backup_dir)
    try:
        os.replace(staging_dir, final_dir)
    except BaseException:
        if backup_dir is not None and backup_dir.exists() and not final_dir.exists():
            os.replace(backup_dir, final_dir)
        raise
    if backup_dir is not None and backup_dir.exists():
        try:
            safe_rmtree(backup_dir, backup_root)
        except OSError:
            # The new cache is already fully verified and promoted.  Leaving the
            # recoverable old backup is safer than treating the new cycle as a
            # failed download and attempting a different model cycle.
            pass


def fetch_cycle(
    plan: ModelPlan,
    model: Mapping[str, Any],
    output_root: Path,
    client: HttpClient,
    *,
    verbose: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    model_root = output_root / "raw" / plan.model
    final_dir = model_root / cycle_id(plan.cycle)
    required_short_names = required_noaa_short_names(model)
    existing = verify_reusable_cycle(
        final_dir / "manifest.json",
        plan,
        final_dir,
        required_short_names=required_short_names,
    )
    if existing:
        log(f"Reusing verified {plan.model} cycle {cycle_id(plan.cycle)}", verbose=verbose)
        return existing
    replacing_existing = final_dir.exists()
    if replacing_existing:
        log(
            f"Cached {plan.model} cycle {cycle_id(plan.cycle)} does not exactly "
            "match this request; staging a complete replacement",
            verbose=verbose,
        )

    staging_root = model_root / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(prefix=f"{cycle_id(plan.cycle)}-", dir=staging_root)
    )
    entries: list[dict[str, Any]] = []
    try:
        for file_plan in plan.files:
            staging_path = staging_dir / file_plan.filename
            final_path = final_dir / file_plan.filename
            if model["provider"] == "noaa-nomads":
                digest, byte_size, inventory, retrieval_source = fetch_noaa_file(
                    file_plan, staging_path, client, verbose=verbose
                )
                validate_required_short_names(
                    inventory,
                    required_short_names,
                    repo_relative(final_path),
                )
            else:
                digest, byte_size, inventory, retrieval_source = fetch_ecmwf_file(
                    file_plan, staging_path, verbose=verbose
                )
            entries.append(
                manifest_entry(
                    file_plan,
                    final_path,
                    status=COMPLETE_STATUS,
                    sha256=digest,
                    byte_size=byte_size,
                    inventory=inventory,
                    retrieval_source=retrieval_source,
                )
            )
        cycle_manifest = {
            "schemaVersion": 1,
            "generatedAt": iso_z(utc_now()),
            "model": plan.model,
            "cycleTime": iso_z(plan.cycle),
            "partialHorizon": plan.partial_horizon,
            "planFingerprint": model_plan_fingerprint(plan),
            "status": COMPLETE_STATUS,
            "files": entries,
        }
        atomic_write_json(staging_dir / "manifest.json", cycle_manifest)
        staged = verify_reusable_cycle(
            staging_dir / "manifest.json",
            plan,
            final_dir,
            physical_dir=staging_dir,
            required_short_names=required_short_names,
        )
        if staged is None:
            raise IntegrityError(
                f"Staged {plan.model} cycle failed exact plan verification"
            )
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        promote_staged_cycle(staging_dir, final_dir, model_root)
        return entries, cycle_manifest
    except BaseException:
        if staging_dir.exists():
            try:
                safe_rmtree(staging_dir, staging_root)
            except Exception:
                pass
        raise


def planned_entries(plan: ModelPlan, output_root: Path, status: str) -> list[dict[str, Any]]:
    final_dir = output_root / "raw" / plan.model / cycle_id(plan.cycle)
    return [
        manifest_entry(item, final_dir / item.filename, status=status)
        for item in plan.files
    ]


def failed_entries(
    plan: ModelPlan | None, output_root: Path, error: str
) -> list[dict[str, Any]]:
    if plan and plan.files:
        return [
            manifest_entry(
                plan.files[0],
                output_root / "raw" / plan.model / cycle_id(plan.cycle) / plan.files[0].filename,
                status="failed",
                error=error,
            )
        ]
    return []


_CYCLE_DIRECTORY = re.compile(r"^\d{8}T\d{2}Z$")


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def directory_size(path: Path) -> int:
    """Count stored bytes without following symbolic links or junctions."""

    if not path.exists():
        return 0
    total = 0
    for root, directories, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        directories[:] = [
            name
            for name in directories
            if not _is_link_or_junction(root_path / name)
        ]
        for name in files:
            file_path = root_path / name
            try:
                total += file_path.lstat().st_size
            except FileNotFoundError:
                # A concurrent cleanup can remove a file between walk and stat.
                continue
    return total


def completed_cycles(output_root: Path) -> list[CompletedCycle]:
    """Inventory completed immediate model/cycle children under raw storage."""

    raw_root = (output_root / "raw").resolve()
    if not raw_root.is_dir() or _is_link_or_junction(raw_root):
        return []
    cycles: list[CompletedCycle] = []
    for model_root in sorted(raw_root.iterdir(), key=lambda value: value.name.casefold()):
        if (
            not model_root.is_dir()
            or _is_link_or_junction(model_root)
            or model_root.parent.resolve() != raw_root
        ):
            continue
        for path in sorted(model_root.iterdir(), key=lambda value: value.name):
            if (
                not path.is_dir()
                or _is_link_or_junction(path)
                or path.parent.resolve() != model_root.resolve()
                or not _CYCLE_DIRECTORY.fullmatch(path.name)
            ):
                continue
            try:
                manifest = load_json(path / "manifest.json")
                if manifest.get("status") != COMPLETE_STATUS:
                    continue
                if manifest.get("model") != model_root.name:
                    continue
                parsed_cycle = parse_utc(str(manifest["cycleTime"]))
                if cycle_id(parsed_cycle) != path.name:
                    continue
                files = manifest.get("files")
                if not isinstance(files, list) or not files:
                    continue
                if any(entry.get("status") != COMPLETE_STATUS for entry in files):
                    continue
            except (KeyError, OSError, TypeError, ValueError):
                continue
            cycles.append(
                CompletedCycle(
                    model=model_root.name,
                    cycle=path.name,
                    cycle_time=parsed_cycle,
                    path=path.resolve(),
                    byte_size=directory_size(path),
                )
            )
    return sorted(
        cycles,
        key=lambda value: (
            value.cycle_time,
            value.model.casefold(),
            value.cycle,
        ),
    )


def prune_cycles(
    model_key: str,
    output_root: Path,
    retain: int,
    *,
    protected_cycles: Iterable[str] = (),
) -> list[str]:
    if retain < 1:
        return []
    model_root = (output_root / "raw" / model_key).resolve()
    if not model_root.is_dir():
        return []
    candidates = sorted(
        [cycle for cycle in completed_cycles(output_root) if cycle.model == model_key],
        key=lambda value: value.cycle_time,
        reverse=True,
    )
    protected = {str(value) for value in protected_cycles}
    keep = {cycle.cycle for cycle in candidates[:retain]} | protected
    removed: list[str] = []
    for cycle in candidates:
        if cycle.cycle in keep:
            continue
        safe_rmtree(cycle.path, model_root)
        removed.append(repo_relative(cycle.path))
    return removed


def cache_report_without_enforcement(output_root: Path, limit_bytes: int) -> dict[str, Any]:
    current = directory_size(output_root / "raw")
    return {
        "status": "not-enforced",
        "limitBytes": limit_bytes,
        "beforeLimitPruningBytes": current,
        "afterLimitPruningBytes": current,
        "excessBytes": max(current - limit_bytes, 0),
        "protectedCurrentCycles": [],
        "prunedForLimit": [],
    }


def enforce_cache_limit(
    output_root: Path,
    limit_bytes: int,
    protected_cycles: Iterable[tuple[str, str]],
) -> dict[str, Any]:
    """Prune oldest completed cycles across all models to a hard byte ceiling."""

    if limit_bytes < 1:
        raise ValueError("storage.maxCacheBytes must be a positive integer")
    raw_root = (output_root / "raw").resolve()
    output_resolved = output_root.resolve()
    try:
        raw_root.relative_to(output_resolved)
    except ValueError as error:
        raise ValueError(f"Raw cache is outside output root: {raw_root}") from error
    protected = {(str(model), str(cycle)) for model, cycle in protected_cycles}
    before = directory_size(raw_root)
    current = before
    removed: list[dict[str, Any]] = []
    candidates = completed_cycles(output_root)
    for candidate in candidates:
        if current <= limit_bytes:
            break
        if (candidate.model, candidate.cycle) in protected:
            continue
        model_root = (raw_root / candidate.model).resolve()
        if candidate.path.parent.resolve() != model_root:
            continue
        # Recheck completion immediately before the destructive operation.
        still_complete = {
            (value.model, value.cycle): value for value in completed_cycles(output_root)
        }.get((candidate.model, candidate.cycle))
        if still_complete is None or still_complete.path != candidate.path:
            continue
        safe_rmtree(candidate.path, model_root)
        removed.append(
            {
                "model": candidate.model,
                "cycleTime": iso_z(candidate.cycle_time),
                "localPath": repo_relative(candidate.path),
                "byteSize": candidate.byte_size,
            }
        )
        current = directory_size(raw_root)

    after = directory_size(raw_root)
    excess = max(after - limit_bytes, 0)
    report: dict[str, Any] = {
        "status": "within-limit" if excess == 0 else "limit-exceeded",
        "limitBytes": limit_bytes,
        "beforeLimitPruningBytes": before,
        "afterLimitPruningBytes": after,
        "excessBytes": excess,
        "protectedCurrentCycles": [
            {"model": model, "cycle": cycle}
            for model, cycle in sorted(protected, key=lambda value: (value[0], value[1]))
        ],
        "prunedForLimit": removed,
    }
    if excess:
        report["error"] = (
            f"Raw GRIB cache remains {excess} bytes above the {limit_bytes}-byte "
            "ceiling after every eligible completed old cycle was pruned; current "
            "requested cycles and non-completed/staging data were not deleted"
        )
    return report


def manifest_filename(issued_at: datetime, mode: str, content_sha256: str) -> str:
    stamp = issued_at.strftime("%Y%m%dT%H%M%SZ")
    suffix = "" if mode == "download" else f"-{mode}"
    if not re.fullmatch(r"[0-9a-f]{64}", content_sha256):
        raise ValueError("Manifest content digest must be a lowercase SHA-256 value")
    return f"fetch-{stamp}{suffix}-{content_sha256}.json"


def _manifest_json_bytes(manifest: Mapping[str, Any]) -> bytes:
    """Serialize exactly as ``atomic_write_json`` does for stable addressing."""

    return (json.dumps(manifest, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def write_immutable_manifest(
    manifest_dir: Path,
    issued_at: datetime,
    mode: str,
    manifest: Mapping[str, Any],
) -> Path:
    """Create a content-addressed manifest without ever replacing an old one.

    A sibling temporary file is hard-linked into its final content-addressed
    name.  Linking is atomic and fails when the destination already exists.  An
    existing identical object is reusable; different bytes at the same digest
    are treated as an integrity failure and are never overwritten.
    """

    payload = _manifest_json_bytes(manifest)
    digest = hashlib.sha256(payload).hexdigest()
    manifest_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_dir / manifest_filename(issued_at, mode, digest)
    temporary = manifest_dir / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            existing = path.read_bytes()
            if existing != payload:
                raise IntegrityError(
                    f"Refusing to mutate immutable manifest with different bytes: {path}"
                )
        except OSError as error:
            # Hard links can be unavailable on a non-NTFS/network filesystem.
            # Preserve the no-overwrite contract with exclusive creation.
            try:
                descriptor = os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                    0o600,
                )
            except FileExistsError:
                existing = path.read_bytes()
                if existing != payload:
                    raise IntegrityError(
                        f"Refusing to mutate immutable manifest with different bytes: {path}"
                    ) from error
            else:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
        if path.read_bytes() != payload:
            raise IntegrityError(f"Immutable manifest verification failed: {path}")
        return path
    finally:
        temporary.unlink(missing_ok=True)


def make_client(config: Mapping[str, Any]) -> HttpClient:
    request = config["request"]
    return HttpClient(
        user_agent=str(request["userAgent"]),
        timeout_seconds=float(request.get("timeoutSeconds", 90)),
        attempts=int(request.get("attempts", 3)),
        backoff_seconds=float(request.get("backoffSeconds", 3)),
        nomads_minimum_interval_seconds=float(
            request.get("nomadsMinimumIntervalSeconds", 10)
        ),
    )


def source_attributions(
    config: Mapping[str, Any], requested_models: Iterable[str]
) -> dict[str, dict[str, str]]:
    """Return structured attribution required by requested licensed sources."""

    result: dict[str, dict[str, str]] = {}
    models = config.get("models")
    if not isinstance(models, Mapping):
        return result
    for key in requested_models:
        model = models.get(key)
        if not isinstance(model, Mapping):
            continue
        raw = model.get("attribution")
        if not isinstance(raw, Mapping):
            continue
        required = (
            "provider",
            "statement",
            "licenseName",
            "licenseUrl",
            "modificationNotice",
        )
        if any(not isinstance(raw.get(field), str) or not raw[field].strip() for field in required):
            raise ValueError(f"models.{key}.attribution must define {', '.join(required)}")
        result[key] = {field: str(raw[field]).strip() for field in required}
    return result


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    config_path = resolve_repo_path(args.config)
    config = load_json(config_path)
    requested_models = choose_models(config, args.models)
    issued_at, as_of = resolve_request_times(args.issued_at, args.as_of)
    horizon = int(
        args.hours
        if args.hours is not None
        else config["request"]["defaultHorizonHours"]
    )
    if horizon < 1 or horizon > 384:
        raise ValueError("--hours must be from 1 through 384")
    valid_through = issued_at + timedelta(hours=horizon)
    output_value = args.output_root or config["storage"]["root"]
    output_root = resolve_guidance_root_path(output_value)
    output_root.mkdir(parents=True, exist_ok=True)
    cache_limit_bytes = int(config["storage"]["maxCacheBytes"])
    if cache_limit_bytes < 1:
        raise ValueError("storage.maxCacheBytes must be a positive integer")
    client = None if args.cache_only else make_client(config)
    mode = (
        "dry-run"
        if args.dry_run
        else "discovery"
        if args.discover_only
        else "cache-only"
        if args.cache_only
        else "download"
    )

    manifest: dict[str, Any] = {
        "schemaVersion": 1,
        "generatedAt": iso_z(utc_now()),
        "mode": mode,
        "site": config["site"]["id"],
        "issuedAt": iso_z(issued_at),
        "availabilityCutoff": iso_z(as_of),
        "validThrough": iso_z(valid_through),
        "requestedModels": requested_models,
        "sourceAttributions": source_attributions(config, requested_models),
        "files": [],
        "models": {},
        "sourceNotes": [
            "NOAA/NCEP data are locally subset and are not an official NOAA forecast.",
            "ECMWF Open Data are locally selected and processed under CC BY 4.0; licence: https://creativecommons.org/licenses/by/4.0/.",
            "A complete model entry uses one cycle only; cycles are never mixed within a model.",
        ],
    }
    if args.cache_only:
        manifest["sourceNotes"].append(
            "Cache-only mode performed no discovery or download; every selected cycle and file was matched to the current request and SHA-256 verified."
        )
    required_failures: list[str] = []
    required_system_failures: list[str] = []
    protected_current_cycles: set[tuple[str, str]] = set()
    for key in requested_models:
        model = config["models"][key]
        plan: ModelPlan | None = None
        attempts: list[dict[str, str]] = []
        try:
            if args.cache_only:
                plan, entries, attempts = select_verified_cached_plan(
                    key,
                    model,
                    issued_at,
                    valid_through,
                    as_of,
                    config["site"]["boundingBox"],
                    output_root,
                    verbose=args.verbose,
                )
                status = COMPLETE_STATUS
                removed: list[str] = []
                protected_current_cycles.add((key, cycle_id(plan.cycle)))
            else:
                plan, attempts = select_complete_plan(
                    key,
                    model,
                    issued_at,
                    valid_through,
                    as_of,
                    config["site"]["boundingBox"],
                    client,
                    dry_run=args.dry_run,
                    verbose=args.verbose,
                )
                if args.dry_run:
                    entries = planned_entries(plan, output_root, "planned")
                    status = "planned"
                elif args.discover_only:
                    entries = planned_entries(plan, output_root, "available")
                    status = "available"
                else:
                    # If a discovered cycle fails while downloading, try the next
                    # discoverable cycle as a whole; never fill holes from another run.
                    candidates = ordered_cycle_candidates(model, as_of, valid_through)
                    start_index = candidates.index(plan.cycle)
                    entries = []
                    last_error: Exception | None = None
                    for candidate in candidates[start_index:]:
                        fresh, _, freshness_reason = cycle_freshness_allows(
                            model, issued_at, candidate
                        )
                        if not fresh:
                            attempts.append(
                                {
                                    "cycleTime": iso_z(candidate),
                                    "error": freshness_reason or "stale cycle",
                                }
                            )
                            continue
                        allowed, _ = lifecycle_allows(model, candidate)
                        if not allowed:
                            continue
                        candidate_plan = build_plan(
                            key,
                            model,
                            candidate,
                            issued_at,
                            valid_through,
                            config["site"]["boundingBox"],
                        )
                        try:
                            if candidate != plan.cycle:
                                candidate_plan.discovery = discover_plan(candidate_plan, model, client)
                            entries, _ = fetch_cycle(
                                candidate_plan,
                                model,
                                output_root,
                                client,
                                verbose=args.verbose,
                            )
                            plan = candidate_plan
                            last_error = None
                            break
                        except (GuidanceError, OSError, ValueError) as error:
                            last_error = error
                            attempts.append(
                                {"cycleTime": iso_z(candidate), "error": str(error)}
                            )
                            log(
                                f"Download rejected for {key} {iso_z(candidate)}: {error}",
                                verbose=True,
                            )
                    if last_error is not None or not entries:
                        raise GuidanceError(
                            f"No {key} cycle downloaded completely: {last_error}"
                        )
                    status = COMPLETE_STATUS
                    protected_current_cycles.add((key, cycle_id(plan.cycle)))
                    if not args.keep_raw:
                        removed = prune_cycles(
                            key,
                            output_root,
                            int(config["storage"].get("retainCyclesPerModel", 2)),
                            protected_cycles=[cycle_id(plan.cycle)],
                        )
                    else:
                        removed = []
            manifest["files"].extend(entries)
            _, cycle_age, _ = cycle_freshness_allows(model, issued_at, plan.cycle)
            manifest["models"][key] = {
                "status": status,
                "required": bool(model.get("required", False)),
                "cycleTime": iso_z(plan.cycle),
                "cycleAgeHours": round(cycle_age, 1),
                "maximumCycleAgeHours": maximum_cycle_age_hours(model),
                "freshnessStatus": "fresh",
                "partialHorizon": plan.partial_horizon,
                "coverageThrough": (
                    iso_z(max(item.valid_time for item in plan.files))
                    if plan.files
                    else None
                ),
                "attemptsRejected": attempts,
                "discovery": plan.discovery,
            }
            if not args.dry_run and not args.discover_only:
                manifest["models"][key]["prunedCycles"] = removed
        except Exception as error:
            message = str(error)
            manifest["models"][key] = {
                "status": "failed",
                "required": bool(model.get("required", False)),
                "error": message,
                "attemptsRejected": attempts,
            }
            manifest["files"].extend(failed_entries(plan, output_root, message))
            if model.get("required", False) or args.cache_only:
                required_failures.append(key)
            log(f"{key} failed: {message}", verbose=True)

    if args.dry_run or args.discover_only or args.cache_only:
        manifest["cache"] = cache_report_without_enforcement(
            output_root, cache_limit_bytes
        )
    else:
        try:
            manifest["cache"] = enforce_cache_limit(
                output_root, cache_limit_bytes, protected_current_cycles
            )
            if manifest["cache"]["status"] == "limit-exceeded":
                required_system_failures.append("cache-limit")
                log(str(manifest["cache"]["error"]), verbose=True)
        except (OSError, ValueError) as error:
            required_system_failures.append("cache-enforcement")
            manifest["cache"] = {
                "status": "enforcement-failed",
                "limitBytes": cache_limit_bytes,
                "error": str(error),
                "protectedCurrentCycles": [
                    {"model": model, "cycle": cycle}
                    for model, cycle in sorted(protected_current_cycles)
                ],
                "prunedForLimit": [],
            }
            log(f"Cache enforcement failed: {error}", verbose=True)

    if required_failures or required_system_failures:
        manifest["status"] = "incomplete"
    elif args.dry_run:
        manifest["status"] = "planned"
    elif args.discover_only:
        manifest["status"] = "available"
    else:
        manifest["status"] = "complete"
    manifest["requiredModelFailures"] = required_failures
    manifest["requiredSystemFailures"] = required_system_failures
    manifest_dir = output_root / "manifests"
    path = write_immutable_manifest(manifest_dir, issued_at, mode, manifest)
    atomic_write_json(manifest_dir / "latest.json", manifest)
    manifest["manifestPath"] = repo_relative(path)
    return manifest, 0 if not required_failures and not required_system_failures else 2


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        manifest, status = run(args)
        summary = {
            "status": manifest["status"],
            "mode": manifest["mode"],
            "manifestPath": manifest["manifestPath"],
            "requestedModels": manifest["requestedModels"],
            "modelStatus": {
                key: value["status"] for key, value in manifest["models"].items()
            },
            "fileCount": len(manifest["files"]),
            "cache": manifest["cache"],
        }
        print(json.dumps(summary, indent=2))
        return status
    except (GuidanceError, OSError, ValueError, KeyError) as error:
        print(f"GRIB fetch failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
