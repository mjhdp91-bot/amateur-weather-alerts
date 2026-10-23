"""Offline tests for the Danville GRIB source planner."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse


REPO_ROOT = Path(__file__).resolve().parents[2]
GRIB_SCRIPT_DIR = REPO_ROOT / "scripts" / "grib"
sys.path.insert(0, str(GRIB_SCRIPT_DIR))

from common import (  # noqa: E402
    candidate_cycles,
    maximum_lead,
    resolve_guidance_root_path,
    select_leads,
)

_SPEC = importlib.util.spec_from_file_location(
    "fetch_guidance", GRIB_SCRIPT_DIR / "fetch-guidance.py"
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("Unable to load fetch-guidance.py")
fetch_guidance = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = fetch_guidance
_SPEC.loader.exec_module(fetch_guidance)


UTC = timezone.utc


class CommonPlannerTests(unittest.TestCase):
    def test_dependency_lock_is_complete_and_contains_direct_requirements(self) -> None:
        direct = {
            line.strip()
            for line in (REPO_ROOT / "requirements-grib.txt").read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        locked = {
            line.strip()
            for line in (REPO_ROOT / "requirements-grib-lock.txt").read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertTrue(direct.issubset(locked))
        self.assertGreater(len(locked), len(direct))
        self.assertTrue(all(line.count("==") == 1 for line in locked))

    def test_runtime_root_is_confined_to_grib_data(self) -> None:
        allowed = (REPO_ROOT / "grib-data").resolve()
        self.assertEqual(resolve_guidance_root_path("grib-data"), allowed)
        self.assertEqual(
            resolve_guidance_root_path("grib-data/test-cache"),
            allowed / "test-cache",
        )
        with self.assertRaisesRegex(ValueError, "must remain beneath"):
            resolve_guidance_root_path(".")
        with self.assertRaisesRegex(ValueError, "must remain beneath"):
            resolve_guidance_root_path("config")

    def test_versioned_manifests_are_content_addressed_and_never_overwritten(self) -> None:
        parent = REPO_ROOT / "grib-data" / "test-manifests"
        parent.mkdir(parents=True, exist_ok=True)
        issued_at = datetime(2026, 10, 23, 11, tzinfo=UTC)
        with tempfile.TemporaryDirectory(dir=parent) as temporary:
            manifest_dir = Path(temporary)
            first_value = {
                "schemaVersion": 1,
                "generatedAt": "2026-10-23T11:01:00Z",
                "status": "complete",
            }
            first_path = fetch_guidance.write_immutable_manifest(
                manifest_dir, issued_at, "cache-only", first_value
            )
            first_bytes = first_path.read_bytes()
            first_hash = fetch_guidance.sha256_file(first_path)

            second_value = {
                **first_value,
                "generatedAt": "2026-10-23T11:02:00Z",
            }
            second_path = fetch_guidance.write_immutable_manifest(
                manifest_dir, issued_at, "cache-only", second_value
            )

            self.assertNotEqual(second_path, first_path)
            self.assertEqual(first_path.read_bytes(), first_bytes)
            self.assertEqual(fetch_guidance.sha256_file(first_path), first_hash)
            self.assertIn(first_hash, first_path.name)
            self.assertIn(fetch_guidance.sha256_file(second_path), second_path.name)

            desired_value = {**first_value, "status": "planned"}
            desired_payload = fetch_guidance._manifest_json_bytes(desired_value)
            desired_hash = fetch_guidance.hashlib.sha256(desired_payload).hexdigest()
            collision_path = manifest_dir / fetch_guidance.manifest_filename(
                issued_at, "cache-only", desired_hash
            )
            collision_path.write_bytes(b"different bytes")
            with self.assertRaisesRegex(
                fetch_guidance.IntegrityError,
                "Refusing to mutate immutable manifest",
            ):
                fetch_guidance.write_immutable_manifest(
                    manifest_dir, issued_at, "cache-only", desired_value
                )
            self.assertEqual(collision_path.read_bytes(), b"different bytes")

    def test_cache_only_is_a_mutually_exclusive_fetch_mode(self) -> None:
        self.assertTrue(fetch_guidance.parse_args(["--cache-only"]).cache_only)
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            fetch_guidance.parse_args(["--cache-only", "--dry-run"])

    def test_candidate_cycles_are_newest_first_and_never_future(self) -> None:
        reference = datetime(2026, 10, 23, 11, 20, tzinfo=UTC)
        result = candidate_cycles(reference, [0, 6, 12, 18], 4)
        self.assertEqual(
            result,
            [
                datetime(2026, 10, 23, 6, tzinfo=UTC),
                datetime(2026, 10, 23, 0, tzinfo=UTC),
                datetime(2026, 10, 22, 18, tzinfo=UTC),
                datetime(2026, 10, 22, 12, tzinfo=UTC),
            ],
        )

    def test_explicit_issuance_is_the_default_availability_cutoff(self) -> None:
        issued, as_of = fetch_guidance.resolve_request_times(
            "2026-10-23T11:00:00Z", None
        )
        self.assertEqual(issued, datetime(2026, 10, 23, 11, tzinfo=UTC))
        self.assertEqual(as_of, issued)

    def test_availability_cutoff_cannot_look_past_issuance(self) -> None:
        with self.assertRaisesRegex(ValueError, "look-ahead data"):
            fetch_guidance.resolve_request_times(
                "2026-10-23T11:00:00Z", "2026-10-23T12:00:00Z"
            )

    def test_earlier_explicit_availability_cutoff_is_allowed(self) -> None:
        issued, as_of = fetch_guidance.resolve_request_times(
            "2026-10-23T11:00:00Z", "2026-10-23T10:30:00Z"
        )
        self.assertEqual(issued, datetime(2026, 10, 23, 11, tzinfo=UTC))
        self.assertEqual(as_of, datetime(2026, 10, 23, 10, 30, tzinfo=UTC))

    def test_extended_hrrr_cycle_is_preferred_for_long_coverage(self) -> None:
        config = json.loads((REPO_ROOT / "config/grib/danville.json").read_text())
        model = config["models"]["hrrr"]
        as_of = datetime(2026, 10, 23, 11, tzinfo=UTC)
        valid_through = datetime(2026, 10, 26, 11, tzinfo=UTC)
        ordered = fetch_guidance.ordered_cycle_candidates(model, as_of, valid_through)
        self.assertEqual(ordered[0].hour, 6)
        self.assertEqual(maximum_lead(model, ordered[0]), 48)

    def test_cycle_freshness_is_bounded_and_configuration_is_validated(self) -> None:
        issued = datetime(2026, 10, 23, 18, tzinfo=UTC)
        allowed, age, reason = fetch_guidance.cycle_freshness_allows(
            {"maximumCycleAgeHours": 12},
            issued,
            datetime(2026, 10, 23, 6, tzinfo=UTC),
        )
        self.assertTrue(allowed)
        self.assertEqual(age, 12)
        self.assertIsNone(reason)

        allowed, age, reason = fetch_guidance.cycle_freshness_allows(
            {"maximumCycleAgeHours": 12},
            issued,
            datetime(2026, 10, 23, 5, tzinfo=UTC),
        )
        self.assertFalse(allowed)
        self.assertEqual(age, 13)
        self.assertIn("exceeds configured maximum", reason or "")

        with self.assertRaisesRegex(ValueError, "must be positive"):
            fetch_guidance.maximum_cycle_age_hours({"maximumCycleAgeHours": 0})

    def test_every_enabled_model_has_an_explicit_cycle_age_limit(self) -> None:
        config = json.loads((REPO_ROOT / "config/grib/danville.json").read_text())
        enabled = [model for model in config["models"].values() if model.get("enabled")]
        self.assertTrue(enabled)
        self.assertTrue(all(model.get("maximumCycleAgeHours") == 12 for model in enabled))

    def test_every_enabled_noaa_model_declares_required_download_fields(self) -> None:
        config = json.loads((REPO_ROOT / "config/grib/danville.json").read_text())
        noaa = [
            model
            for model in config["models"].values()
            if model.get("enabled") and model.get("provider") == "noaa-nomads"
        ]
        self.assertTrue(noaa)
        for model in noaa:
            required = fetch_guidance.required_noaa_short_names(model)
            self.assertTrue(required)

    def test_http_download_rejects_a_short_content_length_response(self) -> None:
        class Headers:
            def get(self, name: str, default: object = None) -> object:
                values = {"Content-Type": "application/octet-stream", "Content-Length": "12"}
                return values.get(name, default)

        class Response:
            headers = Headers()

            def __init__(self) -> None:
                self.remaining = b"GRIB7777"

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, _size: int) -> bytes:
                payload, self.remaining = self.remaining, b""
                return payload

        parent = REPO_ROOT / "grib-data" / "test-download"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temporary:
            target = Path(temporary) / "short.grib2"
            client = fetch_guidance.HttpClient(user_agent="offline-test")
            with (
                mock.patch.object(client, "_open", return_value=Response()),
                self.assertRaisesRegex(fetch_guidance.IntegrityError, "Incomplete response"),
            ):
                client.download("https://example.invalid/short.grib2", target)
            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.glob("*.part")), [])

    def test_partial_horizon_is_explicit(self) -> None:
        config = json.loads((REPO_ROOT / "config/grib/danville.json").read_text())
        model = config["models"]["hrrr"]
        cycle = datetime(2026, 10, 23, 6, tzinfo=UTC)
        issued = datetime(2026, 10, 23, 11, tzinfo=UTC)
        leads, partial = select_leads(
            model, cycle, issued, datetime(2026, 10, 26, 11, tzinfo=UTC)
        )
        self.assertTrue(partial)
        self.assertEqual(max(leads), 48)


class SourcePlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = json.loads(
            (REPO_ROOT / "config/grib/danville.json").read_text()
        )
        cls.cycle = datetime(2026, 10, 23, 6, tzinfo=UTC)
        cls.issued = datetime(2026, 10, 23, 11, tzinfo=UTC)
        cls.valid = datetime(2026, 10, 26, 11, tzinfo=UTC)

    def test_nomads_filter_uses_bbox_and_selected_fields(self) -> None:
        model = self.config["models"]["gfs"]
        component = fetch_guidance.noaa_components(model)[0]
        url = fetch_guidance.noaa_filter_url(
            model,
            self.cycle,
            6,
            component,
            self.config["site"]["boundingBox"],
        )
        parsed = urlparse(url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        self.assertEqual(parsed.path, "/cgi-bin/filter_gfs_0p25.pl")
        self.assertEqual(query["leftlon"], ["-85.8"])
        self.assertEqual(query["rightlon"], ["-83.75"])
        self.assertEqual(query["toplat"], ["38.7"])
        self.assertEqual(query["bottomlat"], ["36.6"])
        self.assertEqual(query["var_TMP"], ["on"])
        self.assertEqual(query["lev_2_m_above_ground"], ["on"])
        self.assertEqual(query["subregion"], [""])

    def test_gefs_has_mean_and_spread_for_each_selected_step(self) -> None:
        plan = fetch_guidance.build_noaa_plan(
            "gefs",
            self.config["models"]["gefs"],
            self.cycle,
            self.issued,
            self.valid,
            self.config["site"]["boundingBox"],
        )
        grouped: dict[int, set[str]] = {}
        for item in plan.files:
            grouped.setdefault(item.forecast_hour, set()).add(item.variant)
        self.assertTrue(grouped)
        self.assertTrue(all(value == {"mean", "spread"} for value in grouped.values()))

    def test_full_range_plans_bracket_the_72_hour_endpoint(self) -> None:
        expected_last_leads = {
            "nam": 78,
            "gfs": 77,
            "gefs": 78,
            "ecmwf": 78,
        }
        for key, expected_last in expected_last_leads.items():
            model = self.config["models"][key]
            if model["provider"] == "ecmwf-open-data":
                plan = fetch_guidance.build_ecmwf_plan(
                    key, model, self.cycle, self.issued, self.valid
                )
            else:
                plan = fetch_guidance.build_noaa_plan(
                    key,
                    model,
                    self.cycle,
                    self.issued,
                    self.valid,
                    self.config["site"]["boundingBox"],
                )
            with self.subTest(model=key):
                self.assertFalse(plan.partial_horizon)
                self.assertEqual(
                    max(item.forecast_hour for item in plan.files), expected_last
                )
                self.assertGreaterEqual(
                    max(item.valid_time for item in plan.files), self.valid
                )

        midnight_cycle = datetime(2026, 10, 23, 0, tzinfo=UTC)
        for key in ("nam", "ecmwf"):
            model = self.config["models"][key]
            if model["provider"] == "ecmwf-open-data":
                plan = fetch_guidance.build_ecmwf_plan(
                    key, model, midnight_cycle, self.issued, self.valid
                )
            else:
                plan = fetch_guidance.build_noaa_plan(
                    key,
                    model,
                    midnight_cycle,
                    self.issued,
                    self.valid,
                    self.config["site"]["boundingBox"],
                )
            with self.subTest(model=key, cycle="00Z"):
                self.assertFalse(plan.partial_horizon)
                self.assertEqual(max(item.forecast_hour for item in plan.files), 84)

    def test_short_range_plans_stop_at_cap_and_remain_partial(self) -> None:
        cases = (
            ("hrrr", self.cycle, 48),
            ("rap", datetime(2026, 10, 23, 9, tzinfo=UTC), 51),
            ("nam_conusnest", self.cycle, 60),
        )
        for key, cycle, expected_cap in cases:
            model = self.config["models"][key]
            plan = fetch_guidance.build_noaa_plan(
                key,
                model,
                cycle,
                self.issued,
                self.valid,
                self.config["site"]["boundingBox"],
            )
            with self.subTest(model=key):
                self.assertTrue(plan.partial_horizon)
                self.assertEqual(
                    max(item.forecast_hour for item in plan.files), expected_cap
                )
                self.assertLess(max(item.valid_time for item in plan.files), self.valid)

    def test_gefs_filter_omits_unsupported_255_mb_layer(self) -> None:
        model = self.config["models"]["gefs"]
        component = fetch_guidance.noaa_components(model)[0]
        url = fetch_guidance.noaa_filter_url(
            model,
            self.cycle,
            0,
            component,
            self.config["site"]["boundingBox"],
        )
        query = parse_qs(urlparse(url).query, keep_blank_values=True)

        self.assertNotIn("lev_255-0_mb_above_ground", query)
        self.assertIn("lev_entire_atmosphere", query)
        self.assertIn(
            "lev_entire_atmosphere_(considered_as_a_single_layer)", query
        )
        self.assertIn("var_TMP", query)

    def test_nam_parent_filter_omits_unsupported_form_keys(self) -> None:
        model = self.config["models"]["nam"]
        component = fetch_guidance.noaa_components(model)[0]
        url = fetch_guidance.noaa_filter_url(
            model,
            self.cycle,
            6,
            component,
            self.config["site"]["boundingBox"],
        )
        query = parse_qs(urlparse(url).query, keep_blank_values=True)

        self.assertNotIn("var_PRATE", query)
        self.assertNotIn("lev_entire_atmosphere", query)
        self.assertNotIn("lev_255-0_mb_above_ground", query)
        self.assertIn(
            "lev_entire_atmosphere_(considered_as_a_single_layer)", query
        )
        self.assertIn("var_APCP", query)

    def test_nam_nest_filter_keeps_supported_parenthesized_layer_only(self) -> None:
        model = self.config["models"]["nam_conusnest"]
        component = fetch_guidance.noaa_components(model)[0]
        url = fetch_guidance.noaa_filter_url(
            model,
            self.cycle,
            6,
            component,
            self.config["site"]["boundingBox"],
        )
        query = parse_qs(urlparse(url).query, keep_blank_values=True)

        self.assertNotIn("lev_entire_atmosphere", query)
        self.assertIn(
            "lev_entire_atmosphere_(considered_as_a_single_layer)", query
        )
        self.assertIn("var_PRATE", query)
        self.assertIn("lev_255-0_mb_above_ground", query)

    def test_ecmwf_uses_indexed_products_not_a_whole_run(self) -> None:
        plan = fetch_guidance.build_ecmwf_plan(
            "ecmwf",
            self.config["models"]["ecmwf"],
            self.cycle,
            self.issued,
            self.valid,
        )
        self.assertTrue(plan.files)
        self.assertTrue(all(item.request["parameters"] for item in plan.files))
        self.assertTrue(all(item.source_url.endswith(".grib2") for item in plan.files))
        self.assertTrue(
            any(item.product.endswith("-pressure") for item in plan.files)
        )

    def test_ecmwf_client_output_is_redirected_away_from_json_stdout(self) -> None:
        class FakeClient:
            def __init__(self, **_kwargs: object) -> None:
                pass

            def retrieve(self, **request: object) -> None:
                print("third-party ECMWF licence notice")
                Path(str(request["target"])).write_bytes(b"GRIBfixture7777")

        plan = fetch_guidance.FilePlan(
            model="ecmwf",
            product="oper-surface",
            variant="surface",
            cycle=self.cycle,
            forecast_hour=6,
            source_url="https://data.ecmwf.int/example.grib2",
            filename="ecmwf.grib2",
            provider="ecmwf-open-data",
            request={
                "stream": "oper",
                "type": "fc",
                "levtype": "sfc",
                "parameters": ["2t"],
                "requiredParameters": ["2t"],
                "sourceCandidates": ["ecmwf"],
                "model": "ifs",
                "resolution": "0p25",
            },
        )
        parent = REPO_ROOT / "grib-data" / "test-download"
        parent.mkdir(parents=True, exist_ok=True)
        standard_output = StringIO()
        standard_error = StringIO()
        with tempfile.TemporaryDirectory(dir=parent) as temporary:
            target = Path(temporary) / plan.filename
            with (
                mock.patch.object(fetch_guidance, "inject_windows_truststore"),
                mock.patch.object(
                    fetch_guidance,
                    "grib_inventory",
                    return_value={"shortNames": ["2t"]},
                ),
                mock.patch("ecmwf.opendata.Client", FakeClient),
                mock.patch("sys.stdout", standard_output),
                mock.patch("sys.stderr", standard_error),
            ):
                fetch_guidance.fetch_ecmwf_file(plan, target, verbose=False)

        self.assertEqual(standard_output.getvalue(), "")
        self.assertIn("third-party ECMWF licence notice", standard_error.getvalue())

    def test_ecmwf_attribution_is_structured_for_downstream_outputs(self) -> None:
        attributions = fetch_guidance.source_attributions(self.config, ["gfs", "ecmwf"])
        self.assertEqual(set(attributions), {"ecmwf"})
        attribution = attributions["ecmwf"]
        self.assertEqual(attribution["provider"], "ECMWF Open Data")
        self.assertEqual(
            attribution["licenseUrl"],
            "https://creativecommons.org/licenses/by/4.0/",
        )
        self.assertIn("locally", attribution["modificationNotice"].lower())

    def test_nam_lifecycle_stops_after_official_end(self) -> None:
        model = self.config["models"]["nam"]
        allowed, reason = fetch_guidance.lifecycle_allows(
            model, datetime(2026, 11, 3, 12, tzinfo=UTC)
        )
        self.assertFalse(allowed)
        self.assertIn("RRFS/REFS", reason or "")


class CacheLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        parent = REPO_ROOT / "grib-data" / "test-cache"
        parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=parent)
        self.output_root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_cycle(
        self,
        model: str,
        cycle: str,
        payload_bytes: int = 128,
        *,
        status: str = "complete",
        manifest_model: str | None = None,
    ) -> Path:
        path = self.output_root / "raw" / model / cycle
        path.mkdir(parents=True)
        data_path = path / "subset.grib2"
        data_path.write_bytes(b"x" * payload_bytes)
        cycle_time = datetime.strptime(cycle, "%Y%m%dT%HZ").replace(tzinfo=UTC)
        manifest = {
            "schemaVersion": 1,
            "model": manifest_model or model,
            "cycleTime": cycle_time.isoformat().replace("+00:00", "Z"),
            "status": status,
            "files": [{"status": status, "localPath": data_path.as_posix()}],
        }
        (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_limit_prunes_oldest_completed_cycle_across_models(self) -> None:
        oldest_gfs = self.make_cycle("gfs", "20261023T00Z")
        same_time_hrrr = self.make_cycle("hrrr", "20261023T00Z")
        current = self.make_cycle("gfs", "20261023T06Z")
        before = fetch_guidance.directory_size(self.output_root / "raw")
        limit = before - fetch_guidance.directory_size(oldest_gfs)

        report = fetch_guidance.enforce_cache_limit(
            self.output_root, limit, {("gfs", "20261023T06Z")}
        )

        self.assertEqual(report["status"], "within-limit")
        self.assertFalse(oldest_gfs.exists())
        self.assertTrue(same_time_hrrr.exists())
        self.assertTrue(current.exists())
        self.assertEqual(report["prunedForLimit"][0]["model"], "gfs")
        self.assertLessEqual(report["afterLimitPruningBytes"], limit)

    def test_current_cycle_is_protected_and_overage_is_reported(self) -> None:
        current = self.make_cycle("gfs", "20261023T06Z", payload_bytes=512)
        size = fetch_guidance.directory_size(self.output_root / "raw")

        report = fetch_guidance.enforce_cache_limit(
            self.output_root, size - 1, {("gfs", "20261023T06Z")}
        )

        self.assertEqual(report["status"], "limit-exceeded")
        self.assertEqual(report["excessBytes"], 1)
        self.assertEqual(report["prunedForLimit"], [])
        self.assertTrue(current.exists())
        self.assertIn("were not deleted", report["error"])

    def test_incomplete_and_staging_directories_are_never_deleted(self) -> None:
        incomplete = self.make_cycle(
            "rap", "20261023T06Z", payload_bytes=256, status="failed"
        )
        staging = self.output_root / "raw" / "rap" / ".staging" / "active"
        staging.mkdir(parents=True)
        (staging / "download.part").write_bytes(b"y" * 256)

        report = fetch_guidance.enforce_cache_limit(self.output_root, 1, set())

        self.assertEqual(report["status"], "limit-exceeded")
        self.assertTrue(incomplete.exists())
        self.assertTrue(staging.exists())
        self.assertEqual(report["prunedForLimit"], [])

    def test_normal_retention_keeps_an_explicitly_protected_old_cycle(self) -> None:
        oldest = self.make_cycle("nam", "20261023T00Z")
        middle = self.make_cycle("nam", "20261023T06Z")
        newest = self.make_cycle("nam", "20261023T12Z")

        removed = fetch_guidance.prune_cycles(
            "nam",
            self.output_root,
            1,
            protected_cycles=["20261023T00Z"],
        )

        self.assertTrue(oldest.exists())
        self.assertFalse(middle.exists())
        self.assertTrue(newest.exists())
        self.assertEqual(len(removed), 1)

    def test_manifest_model_mismatch_is_not_an_eligible_deletion(self) -> None:
        mismatched = self.make_cycle(
            "gfs", "20261023T00Z", manifest_model="not-gfs"
        )

        report = fetch_guidance.enforce_cache_limit(self.output_root, 1, set())

        self.assertEqual(report["status"], "limit-exceeded")
        self.assertTrue(mismatched.exists())
        self.assertEqual(report["prunedForLimit"], [])


class ReuseUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        parent = REPO_ROOT / "grib-data" / "test-reuse"
        parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=parent)
        self.output_root = Path(self.temporary.name)
        self.cycle = datetime(2026, 10, 23, 6, tzinfo=UTC)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def file_plan(self, lead: int) -> object:
        return fetch_guidance.FilePlan(
            model="gfs",
            product="pgrb2-0p25",
            variant="deterministic",
            cycle=self.cycle,
            forecast_hour=lead,
            source_url=f"https://example.invalid/gfs-f{lead:03d}",
            filename=f"gfs-f{lead:03d}.grib2",
            provider="NOAA/NCEP NOMADS Grib Filter",
            request={"archiveIndexUrl": f"https://example.invalid/gfs-f{lead:03d}.idx"},
        )

    def model_plan(self, leads: list[int]) -> object:
        return fetch_guidance.ModelPlan(
            model="gfs",
            cycle=self.cycle,
            partial_horizon=False,
            files=[self.file_plan(lead) for lead in leads],
        )

    def write_cached_plan(self, plan: object, prefix: bytes = b"cached-") -> Path:
        final_dir = (
            self.output_root
            / "raw"
            / plan.model
            / fetch_guidance.cycle_id(plan.cycle)
        )
        final_dir.mkdir(parents=True)
        entries = []
        for item in plan.files:
            path = final_dir / item.filename
            payload = prefix + item.filename.encode("ascii")
            path.write_bytes(payload)
            entries.append(
                fetch_guidance.manifest_entry(
                    item,
                    path,
                    status="complete",
                    sha256=fetch_guidance.sha256_file(path),
                    byte_size=len(payload),
                    inventory={"messageCount": 1},
                )
            )
        manifest = {
            "schemaVersion": 1,
            "model": plan.model,
            "cycleTime": fetch_guidance.iso_z(plan.cycle),
            "planFingerprint": fetch_guidance.model_plan_fingerprint(plan),
            "status": "complete",
            "files": entries,
        }
        (final_dir / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return final_dir

    @staticmethod
    def fake_fetch(item: object, path: Path, client: object, *, verbose: bool):
        payload = b"replacement-" + item.filename.encode("ascii")
        path.write_bytes(payload)
        return (
            fetch_guidance.sha256_file(path),
            len(payload),
            {"messageCount": 1},
            None,
        )

    def test_short_cache_is_atomically_upgraded_for_long_plan(self) -> None:
        short_plan = self.model_plan([0])
        long_plan = self.model_plan([0, 3])
        final_dir = self.write_cached_plan(short_plan)
        self.assertIsNotNone(
            fetch_guidance.verify_reusable_cycle(
                final_dir / "manifest.json", short_plan, final_dir
            )
        )
        self.assertIsNone(
            fetch_guidance.verify_reusable_cycle(
                final_dir / "manifest.json", long_plan, final_dir
            )
        )

        with mock.patch.object(
            fetch_guidance, "fetch_noaa_file", side_effect=self.fake_fetch
        ):
            entries, _ = fetch_guidance.fetch_cycle(
                long_plan,
                {"provider": "noaa-nomads"},
                self.output_root,
                object(),
                verbose=False,
            )

        self.assertEqual(len(entries), 2)
        self.assertEqual((final_dir / "gfs-f000.grib2").read_bytes(), b"replacement-gfs-f000.grib2")
        self.assertTrue((final_dir / "gfs-f003.grib2").is_file())
        self.assertIsNotNone(
            fetch_guidance.verify_reusable_cycle(
                final_dir / "manifest.json", long_plan, final_dir
            )
        )

    def test_extra_cached_files_prevent_reuse(self) -> None:
        short_plan = self.model_plan([0])
        long_plan = self.model_plan([0, 3])
        final_dir = self.write_cached_plan(long_plan)

        self.assertIsNone(
            fetch_guidance.verify_reusable_cycle(
                final_dir / "manifest.json", short_plan, final_dir
            )
        )

    def test_cache_reuse_rejects_inventory_missing_required_noaa_fields(self) -> None:
        plan = self.model_plan([0])
        final_dir = self.write_cached_plan(plan)
        manifest_path = final_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["inventory"] = {
            "messageCount": 1,
            "shortNames": ["vis"],
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        self.assertIsNone(
            fetch_guidance.verify_reusable_cycle(
                manifest_path,
                plan,
                final_dir,
                required_short_names={"2t", "10u", "10v"},
            )
        )

    def test_clean_message_boundary_truncation_is_rejected_after_download(self) -> None:
        plan = self.model_plan([0])

        def truncated_fetch(
            item: object, path: Path, client: object, *, verbose: bool
        ) -> tuple[str, int, dict[str, object], None]:
            # A transport can end cleanly after one complete GRIB message.  The
            # framing, cycle and valid time can all be valid while core fields
            # from later messages are absent.
            payload = b"one-complete-grib-message"
            path.write_bytes(payload)
            return (
                fetch_guidance.sha256_file(path),
                len(payload),
                {"messageCount": 1, "shortNames": ["vis"]},
                None,
            )

        with mock.patch.object(
            fetch_guidance, "fetch_noaa_file", side_effect=truncated_fetch
        ):
            with self.assertRaisesRegex(
                fetch_guidance.IntegrityError,
                "lacks required fields",
            ):
                fetch_guidance.fetch_cycle(
                    plan,
                    {
                        "provider": "noaa-nomads",
                        "requiredShortNames": ["2t", "10u", "10v"],
                    },
                    self.output_root,
                    object(),
                    verbose=False,
                )

        final_dir = (
            self.output_root
            / "raw"
            / plan.model
            / fetch_guidance.cycle_id(plan.cycle)
        )
        self.assertFalse(final_dir.exists())
        staging_root = self.output_root / "raw" / "gfs" / ".staging"
        self.assertFalse(staging_root.exists() and any(staging_root.iterdir()))

    def test_cache_only_selects_one_exact_sha_verified_cycle(self) -> None:
        plan = self.model_plan([0, 3])
        self.write_cached_plan(plan)

        with mock.patch.object(fetch_guidance, "build_plan", return_value=plan):
            selected, entries, failures = fetch_guidance.select_verified_cached_plan(
                "gfs",
                {},
                self.cycle,
                self.cycle.replace(hour=9),
                self.cycle,
                {},
                self.output_root,
                verbose=False,
            )

        self.assertIs(selected, plan)
        self.assertEqual(len(entries), 2)
        self.assertEqual(failures, [])
        self.assertEqual(selected.discovery["status"], "verified-cache-only")

    def test_cache_only_run_does_not_construct_a_network_client(self) -> None:
        plan = self.model_plan([0, 3])
        args = fetch_guidance.parse_args(
            [
                "--models",
                "gfs",
                "--hours",
                "3",
                "--issued-at",
                "2026-10-23T06:00:00Z",
                "--output-root",
                fetch_guidance.repo_relative(self.output_root),
                "--cache-only",
            ]
        )
        entries = fetch_guidance.planned_entries(plan, self.output_root, "complete")
        with (
            mock.patch.object(
                fetch_guidance,
                "make_client",
                side_effect=AssertionError("network client must not be constructed"),
            ),
            mock.patch.object(
                fetch_guidance,
                "select_verified_cached_plan",
                return_value=(plan, entries, []),
            ),
        ):
            manifest, status = fetch_guidance.run(args)

        self.assertEqual(status, 0)
        self.assertEqual(manifest["mode"], "cache-only")
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["availabilityCutoff"], manifest["issuedAt"])
        self.assertEqual(manifest["models"]["gfs"]["cycleAgeHours"], 0)
        self.assertEqual(manifest["models"]["gfs"]["maximumCycleAgeHours"], 12)
        self.assertEqual(manifest["models"]["gfs"]["freshnessStatus"], "fresh")

    def test_manifest_cycle_age_preserves_issuance_second_precision(self) -> None:
        plan = self.model_plan([0, 3])
        args = fetch_guidance.parse_args(
            [
                "--models",
                "gfs",
                "--hours",
                "3",
                "--issued-at",
                "2026-10-23T06:02:59Z",
                "--output-root",
                fetch_guidance.repo_relative(self.output_root),
                "--cache-only",
            ]
        )
        entries = fetch_guidance.planned_entries(plan, self.output_root, "complete")
        with (
            mock.patch.object(
                fetch_guidance,
                "make_client",
                side_effect=AssertionError("network client must not be constructed"),
            ),
            mock.patch.object(
                fetch_guidance,
                "select_verified_cached_plan",
                return_value=(plan, entries, []),
            ),
        ):
            manifest, status = fetch_guidance.run(args)

        self.assertEqual(status, 0)
        self.assertEqual(
            manifest["models"]["gfs"]["cycleAgeHours"],
            round(179 / 3600, 6),
        )

    def test_cache_only_rejects_a_corrupt_or_future_cycle(self) -> None:
        plan = self.model_plan([0, 3])
        final_dir = self.write_cached_plan(plan)
        (final_dir / "gfs-f003.grib2").write_bytes(b"tampered")

        with mock.patch.object(fetch_guidance, "build_plan", return_value=plan):
            with self.assertRaisesRegex(
                fetch_guidance.GuidanceError,
                "No exact verified cached gfs cycle found",
            ):
                fetch_guidance.select_verified_cached_plan(
                    "gfs",
                    {},
                    self.cycle,
                    self.cycle.replace(hour=9),
                    self.cycle,
                    {},
                    self.output_root,
                    verbose=False,
                )

            with self.assertRaisesRegex(
                fetch_guidance.GuidanceError,
                "no completed cached cycles",
            ):
                fetch_guidance.select_verified_cached_plan(
                    "gfs",
                    {},
                    self.cycle,
                    self.cycle.replace(hour=9),
                    self.cycle.replace(hour=0),
                    {},
                    self.output_root,
                    verbose=False,
                )

    def test_cache_only_rejects_an_exact_but_stale_cycle(self) -> None:
        plan = self.model_plan([0, 3])
        self.write_cached_plan(plan)
        issued = self.cycle + timedelta(hours=13)

        with mock.patch.object(fetch_guidance, "build_plan", return_value=plan):
            with self.assertRaisesRegex(
                fetch_guidance.GuidanceError,
                "exceeds configured maximum",
            ):
                fetch_guidance.select_verified_cached_plan(
                    "gfs",
                    {"maximumCycleAgeHours": 12},
                    issued,
                    issued + timedelta(hours=3),
                    issued,
                    {},
                    self.output_root,
                    verbose=False,
                )

    def test_keyboard_interrupt_cleans_stage_and_preserves_old_cache(self) -> None:
        short_plan = self.model_plan([0])
        long_plan = self.model_plan([0, 3])
        final_dir = self.write_cached_plan(short_plan)
        old_manifest = (final_dir / "manifest.json").read_bytes()
        old_payload = (final_dir / "gfs-f000.grib2").read_bytes()

        def interrupted_fetch(item: object, path: Path, client: object, *, verbose: bool):
            if item.forecast_hour == 3:
                raise KeyboardInterrupt()
            return self.fake_fetch(item, path, client, verbose=verbose)

        with mock.patch.object(
            fetch_guidance, "fetch_noaa_file", side_effect=interrupted_fetch
        ):
            with self.assertRaises(KeyboardInterrupt):
                fetch_guidance.fetch_cycle(
                    long_plan,
                    {"provider": "noaa-nomads"},
                    self.output_root,
                    object(),
                    verbose=False,
                )

        self.assertEqual((final_dir / "manifest.json").read_bytes(), old_manifest)
        self.assertEqual((final_dir / "gfs-f000.grib2").read_bytes(), old_payload)
        staging_root = self.output_root / "raw" / "gfs" / ".staging"
        self.assertFalse(staging_root.exists() and any(staging_root.iterdir()))
        backup_root = self.output_root / "raw" / "gfs" / ".replacement-backups"
        self.assertFalse(backup_root.exists())


if __name__ == "__main__":
    unittest.main()
