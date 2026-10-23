from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "scripts" / "tests" / "fixtures" / "grib" / "extracted-guidance.json"


def load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_summary = load_script("grib_build_summary", ROOT / "scripts" / "grib" / "build-summary.py")
extract_guidance = load_script("grib_extract_guidance", ROOT / "scripts" / "grib" / "extract-guidance.py")


class GribProcessingTests(unittest.TestCase):
    def test_convective_layers_remain_distinct_and_diagnostic_is_traceable(self):
        self.assertEqual(
            extract_guidance.field_name("cape", "surface", 0),
            "surfaceCapeJkg",
        )
        self.assertEqual(
            extract_guidance.field_name("cape", "pressureFromGroundLayer", 18000),
            "cape180To0MbJkg",
        )
        self.assertEqual(
            extract_guidance.field_name("cape", "pressureFromGroundLayer", 25500),
            "cape255To0MbJkg",
        )
        self.assertEqual(
            extract_guidance.field_name("cin", "pressureFromGroundLayer", 25500),
            "cin255To0MbJkg",
        )

        point = {
            "surfaceCapeJkg": 500,
            "surfaceCinJkg": -25,
            "cape180To0MbJkg": 750,
            "cin180To0MbJkg": -40,
            "cape255To0MbJkg": 600,
            "cin255To0MbJkg": -10,
        }
        source = extract_guidance.derive_convective_diagnostic(point)
        self.assertEqual(point["forecastCapeJkg"], 750)
        self.assertEqual(point["forecastCinJkg"], -40)
        self.assertEqual(source["capeSourceField"], "cape180To0MbJkg")
        self.assertEqual(source["cinSourceField"], "cin180To0MbJkg")
        self.assertEqual(point["surfaceCapeJkg"], 500)
        self.assertEqual(point["cape255To0MbJkg"], 600)

    def test_local_neighborhood_is_separate_from_download_crop(self):
        config = {
            "site": {
                "boundingBox": {
                    "west": -85.8,
                    "east": -83.75,
                    "south": 36.6,
                    "north": 38.7,
                },
                "neighborhoodBounds": {
                    "west": -84.95,
                    "east": -84.6,
                    "south": 37.48,
                    "north": 37.8,
                },
            }
        }
        self.assertEqual(
            extract_guidance.load_neighborhood(config),
            config["site"]["neighborhoodBounds"],
        )

    def test_extractor_refuses_every_protected_output_name(self):
        for name in extract_guidance.PROTECTED_OUTPUT_NAMES:
            with self.subTest(name=name), self.assertRaises(SystemExit):
                extract_guidance.ensure_safe_output(ROOT / "grib-data" / "safe" / ".." / name)

    def test_both_processors_confine_outputs_to_grib_data(self):
        allowed = ROOT / "grib-data" / "tests" / "derived.json"
        self.assertEqual(extract_guidance.ensure_safe_output(allowed), allowed.resolve())
        self.assertEqual(build_summary.ensure_safe_output(allowed), allowed.resolve())
        for guard in (extract_guidance.ensure_safe_output, build_summary.ensure_safe_output):
            with self.subTest(guard=guard.__module__), self.assertRaises(SystemExit):
                guard(ROOT / "outside-guidance.json")
            with self.subTest(guard=guard.__module__), self.assertRaises(SystemExit):
                guard(ROOT / "grib-data" / "forecast.json")

    def test_ecmwf_benchmark_retains_structured_attribution(self):
        attribution = {
            "provider": "ECMWF Open Data",
            "statement": "Contains modified ECMWF Open Data.",
            "licenseName": "Creative Commons Attribution 4.0 International (CC BY 4.0)",
            "licenseUrl": "https://creativecommons.org/licenses/by/4.0/",
            "modificationNotice": "Selected GRIB messages were processed locally.",
        }
        model = {
            "model": "ecmwf",
            "cycle": "2026-10-23T00:00:00Z",
            "sourceUrls": ["https://data.ecmwf.int/forecasts/example.grib2"],
            "stations": {
                "KDVK": {
                    "days": [{
                        "date": "2026-10-23",
                        "highF": 70,
                        "lowF": 45,
                        "highFEligible": True,
                        "lowFEligible": True,
                        "qpfComplete": False,
                    }]
                }
            },
        }
        benchmarks = build_summary.build_benchmarks(
            [model], ZoneInfo("America/New_York"), {"ecmwf": attribution}
        )
        self.assertEqual(len(benchmarks), 1)
        self.assertEqual(benchmarks[0]["attribution"], attribution)
        self.assertIn(attribution["provider"], benchmarks[0]["note"])
        self.assertIn(attribution["licenseUrl"], benchmarks[0]["note"])
        self.assertIn(attribution["modificationNotice"], benchmarks[0]["note"])

    def test_daily_extremes_use_explicit_and_instantaneous_union(self):
        day = build_summary.summarize_day(
            date(2026, 10, 23),
            [
                {
                    "validTime": "2026-10-23T12:00:00Z",
                    "temperatureF": 40,
                    "maximumTemperatureF": 60,
                    "minimumTemperatureF": 50,
                },
                {"validTime": "2026-10-23T18:00:00Z", "temperatureF": 70},
            ],
        )
        self.assertEqual(day["highF"], 70)
        self.assertEqual(day["lowF"], 40)

    def test_boundary_crossing_explicit_extrema_are_excluded(self):
        zone = build_summary.ZoneInfo("America/New_York")
        day = build_summary.summarize_day(
            date(2026, 10, 23),
            [
                {
                    "validTime": "2026-10-23T12:00:00Z",
                    "leadTimeHours": 6,
                    "maximumTemperatureF": 90,
                    "maximumTemperatureInterval": {
                        "startHour": 0,
                        "endHour": 6,
                        "durationHours": 6,
                    },
                },
                {
                    "validTime": "2026-10-23T20:00:00Z",
                    "leadTimeHours": 14,
                    "maximumTemperatureF": 70,
                    "maximumTemperatureInterval": {
                        "startHour": 12,
                        "endHour": 14,
                        "durationHours": 2,
                    },
                },
                {
                    "validTime": "2026-10-23T21:00:00Z",
                    "leadTimeHours": 15,
                    "maximumTemperatureF": 85,
                },
            ],
            following_night_items=[
                {
                    "validTime": "2026-10-24T00:00:00Z",
                    "leadTimeHours": 18,
                    "minimumTemperatureF": 20,
                    "minimumTemperatureInterval": {
                        "startHour": 12,
                        "endHour": 18,
                        "durationHours": 6,
                    },
                },
                {
                    "validTime": "2026-10-24T06:00:00Z",
                    "leadTimeHours": 24,
                    "minimumTemperatureF": 42,
                    "minimumTemperatureInterval": {
                        "startHour": 18,
                        "endHour": 24,
                        "durationHours": 6,
                    },
                },
                {
                    "validTime": "2026-10-24T10:00:00Z",
                    "leadTimeHours": 28,
                    "minimumTemperatureF": 40,
                    "minimumTemperatureInterval": {
                        "startHour": 24,
                        "endHour": 28,
                        "durationHours": 4,
                    },
                },
            ],
            zone=zone,
            forecast_window_start=datetime(2026, 10, 23, 11, tzinfo=timezone.utc),
            forecast_window_end=datetime(2026, 10, 26, 11, tzinfo=timezone.utc),
        )
        self.assertEqual(day["highF"], 70)
        self.assertEqual(day["lowF"], 40)
        self.assertTrue(day["highFEligible"])
        self.assertTrue(day["lowFEligible"])
        self.assertEqual(
            day["fieldEligibility"]["highF"]["explicitExtremaIntervals"],
            {
                "candidateCount": 3,
                "acceptedCount": 1,
                "excludedBoundaryCrossingCount": 1,
                "unknownIntervalCount": 1,
                "excludedUnknownIntervalCount": 1,
            },
        )
        self.assertEqual(
            day["fieldEligibility"]["lowF"]["explicitExtremaIntervals"]
            ["excludedBoundaryCrossingCount"],
            1,
        )

    def test_consensus_excludes_zero_sample_model_days(self):
        populated = {
            "sampleCount": 2,
            "highF": 70,
            "lowF": 45,
            "highFEligible": True,
            "lowFEligible": True,
            "qpfIn": None,
            "qpfComplete": False,
            "maximumGustMph": 20,
            "meanCloudCoverPct": 40,
            "minimumVisibilityMi": 8,
            "fieldEligibility": {
                "maximumWindMph": {"eligible": False},
                "maximumGustMph": {"eligible": True},
                "meanCloudCoverPct": {"eligible": True},
                "minimumVisibilityMi": {"eligible": True},
            },
            "weatherRegime": "dry-high-pressure",
            "signals": {"fog": False, "wind": True, "precipitation": False},
        }
        empty = {
            "sampleCount": 0,
            "highF": None,
            "lowF": None,
            "highFEligible": False,
            "lowFEligible": False,
            "qpfComplete": False,
            "signals": {"fog": False, "wind": False, "precipitation": False},
        }
        consensus = build_summary._consensus_day(
            "2026-10-23", [("gfs", populated), ("hrrr", empty)]
        )
        self.assertEqual(consensus["modelCount"], 1)
        self.assertEqual(consensus["models"], ["gfs"])
        self.assertEqual(consensus["excludedZeroSampleModels"], ["hrrr"])
        self.assertEqual(consensus["eventSupport"]["wind"]["fraction"], 1)
        self.assertEqual(consensus["eventSupport"]["wind"]["contributors"], 1)
        self.assertEqual(consensus["fieldContributorCounts"]["highF"], 1)

        missing = build_summary._consensus_day("2026-10-24", [("hrrr", empty)])
        self.assertEqual(missing["modelCount"], 0)
        self.assertEqual(missing["confidence"], "unavailable")
        self.assertIn("highF", missing["missingFields"])

    def test_fog_event_support_uses_only_assessable_source_days(self):
        assessable = {
            "sampleCount": 5,
            "highF": 70,
            "lowF": 45,
            "highFEligible": True,
            "lowFEligible": False,
            "qpfIn": None,
            "qpfComplete": False,
            "fieldEligibility": {"fogSignal": {"eligible": True}},
            "signals": {"fog": True, "wind": False, "precipitation": False},
        }
        unavailable = {
            "sampleCount": 5,
            "highF": 71,
            "lowF": 46,
            "highFEligible": True,
            "lowFEligible": True,
            "qpfIn": None,
            "qpfComplete": False,
            "fieldEligibility": {"fogSignal": {"eligible": False}},
            "signals": {"fog": False, "wind": False, "precipitation": False},
        }
        consensus = build_summary._consensus_day(
            "2026-10-23", [("gfs", assessable), ("gefs", unavailable)]
        )
        fog = consensus["eventSupport"]["fog"]
        self.assertEqual(fog["contributors"], 1)
        self.assertEqual(fog["contributorModels"], ["gfs"])
        self.assertEqual(fog["models"], 1)
        self.assertEqual(fog["supportingModels"], ["gfs"])
        self.assertEqual(fog["fraction"], 1)

    def test_partial_horizon_surface_fields_do_not_inflate_consensus(self):
        complete = {
            "sampleCount": 9,
            "highF": 70,
            "lowF": 45,
            "highFEligible": True,
            "lowFEligible": True,
            "qpfIn": 0,
            "qpfComplete": True,
            "maximumWindMph": 12,
            "maximumGustMph": 18,
            "meanCloudCoverPct": 30,
            "minimumVisibilityMi": 9,
            "fieldEligibility": {
                "maximumWindMph": {"eligible": True},
                "maximumGustMph": {"eligible": True},
                "meanCloudCoverPct": {"eligible": True},
                "minimumVisibilityMi": {"eligible": True},
            },
            "signals": {"fog": False, "wind": False, "precipitation": False},
        }
        partial = {
            **complete,
            "sampleCount": 1,
            "highFEligible": False,
            "lowFEligible": False,
            "maximumGustMph": 42,
            "meanCloudCoverPct": 100,
            "minimumVisibilityMi": 1,
            "fieldEligibility": {
                field: {"eligible": False}
                for field in (
                    "maximumWindMph",
                    "maximumGustMph",
                    "meanCloudCoverPct",
                    "minimumVisibilityMi",
                )
            },
        }
        consensus = build_summary._consensus_day(
            "2026-10-25", [("gfs", complete), ("hrrr", partial)]
        )
        self.assertEqual(consensus["fieldContributorCounts"]["maximumGustMph"], 1)
        self.assertEqual(consensus["fieldContributors"]["maximumGustMph"], ["gfs"])
        self.assertEqual(consensus["maximumGustMph"]["max"], 18)
        self.assertEqual(consensus["meanCloudCoverPct"]["mean"], 30)
        self.assertEqual(consensus["minimumVisibilityMi"]["min"], 9)
        self.assertEqual(consensus["fieldConfidence"]["maximumGustMph"], "low")

    def test_partial_day_temperature_values_do_not_enter_consensus(self):
        complete = {
            "sampleCount": 10,
            "highF": 70,
            "lowF": 45,
            "highFEligible": True,
            "lowFEligible": True,
            "qpfIn": 0,
            "qpfComplete": True,
            "signals": {"fog": False, "wind": False, "precipitation": False},
        }
        morning_only = {
            "sampleCount": 3,
            "highF": 51.5,
            "lowF": 49.0,
            "highFEligible": False,
            "lowFEligible": False,
            "qpfIn": 0,
            "qpfComplete": True,
            "signals": {"fog": False, "wind": False, "precipitation": False},
        }
        consensus = build_summary._consensus_day(
            "2026-10-25", [("gfs", complete), ("rap", morning_only)]
        )
        self.assertEqual(consensus["models"], ["gfs"])
        self.assertEqual(consensus["modelCount"], 1)
        self.assertEqual(consensus["excludedPartialDayModels"], ["rap"])
        self.assertEqual(consensus["fieldContributorCounts"]["highF"], 1)
        self.assertEqual(consensus["fieldContributorCounts"]["lowF"], 1)
        self.assertEqual(consensus["highF"]["mean"], 70)
        self.assertEqual(consensus["lowF"]["mean"], 45)

    def test_three_day_bucket_uses_trailing_night_through_valid_time(self):
        zone = build_summary.ZoneInfo("America/New_York")
        items = [
            {"validTime": "2026-10-25T16:00:00Z", "temperatureF": 65},
            {"validTime": "2026-10-25T20:00:00Z", "temperatureF": 75},
            {"validTime": "2026-10-25T22:00:00Z", "temperatureF": 60},
            {"validTime": "2026-10-26T04:00:00Z", "temperatureF": 50},
            {
                "validTime": "2026-10-26T10:00:00Z",
                "temperatureF": 42,
                "dewpointF": 42,
                "relativeHumidity2mPct": 100,
                "windSpeedMph": 1,
            },
            {"validTime": "2026-10-26T11:00:00Z", "temperatureF": 43},
        ]
        days = build_summary.group_days(
            items,
            {date(2026, 10, 23), date(2026, 10, 24), date(2026, 10, 25)},
            zone,
        )
        self.assertEqual(len(days), 3)
        final_day = days[2]
        self.assertEqual(final_day["date"], "2026-10-25")
        self.assertEqual(final_day["lastValidTime"], "2026-10-26T11:00:00Z")
        self.assertEqual(final_day["lowF"], 42)
        self.assertTrue(final_day["highFEligible"])
        self.assertTrue(final_day["lowFEligible"])
        self.assertIn("2026-10-26T10:00:00Z", final_day["signals"]["fogTimes"])

    def test_global_signal_windows_deduplicate_overlapping_day_buckets(self):
        summary = build_summary.build_signals(
            [
                {
                    "model": "gfs",
                    "stations": {
                        "Danville": {
                            "days": [
                                {
                                    "signals": {
                                        "fogTimes": ["2026-10-24T10:00:00Z"],
                                        "windTimes": [],
                                        "precipitationTimes": [],
                                    }
                                },
                                {
                                    "signals": {
                                        "fogTimes": [
                                            "2026-10-24T10:00:00Z",
                                            "2026-10-24T12:00:00Z",
                                        ],
                                        "windTimes": [],
                                        "precipitationTimes": [],
                                    }
                                },
                            ]
                        }
                    },
                }
            ]
        )
        self.assertEqual(
            summary["fogWindows"],
            [
                {
                    "model": "gfs",
                    "station": "Danville",
                    "start": "2026-10-24T10:00:00Z",
                    "end": "2026-10-24T12:00:00Z",
                    "samples": 2,
                }
            ],
        )

    def test_daily_signals_use_nonoverlapping_boundaries_and_include_only_final_endpoint(self):
        zone = build_summary.ZoneInfo("America/New_York")
        window_start = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
        window_end = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)

        def event(timestamp: str) -> dict:
            return {
                "validTime": timestamp,
                "temperatureF": 50,
                "dewpointF": 50,
                "visibilityMi": 1,
                "windSpeedMph": 25,
                "precipitationRateInPerHour": 0.1,
            }

        first = build_summary.summarize_day(
            date(2026, 10, 23),
            [],
            following_night_items=[event("2026-10-24T12:00:00Z")],
            zone=zone,
            forecast_window_start=window_start,
            forecast_window_end=window_end,
        )
        second = build_summary.summarize_day(
            date(2026, 10, 24),
            [event("2026-10-24T12:00:00Z")],
            zone=zone,
            forecast_window_start=window_start,
            forecast_window_end=window_end,
        )
        final = build_summary.summarize_day(
            date(2026, 10, 25),
            [],
            following_night_items=[event("2026-10-26T12:00:00Z")],
            zone=zone,
            forecast_window_start=window_start,
            forecast_window_end=window_end,
        )

        for key in ("fogTimes", "windTimes", "precipitationTimes"):
            self.assertEqual(first["signals"][key], [])
            self.assertEqual(second["signals"][key], ["2026-10-24T12:00:00Z"])
            self.assertEqual(final["signals"][key], ["2026-10-26T12:00:00Z"])
        self.assertEqual(
            first["signals"]["boundarySemantics"],
            "start-inclusive/end-exclusive",
        )
        self.assertEqual(
            final["signals"]["boundarySemantics"],
            "start-inclusive/end-inclusive",
        )

    def test_surface_coverage_uses_model_and_field_cadence(self):
        start = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
        end = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)

        hrrr_items = [
            {
                "validTime": (start + timedelta(hours=offset))
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                "gustMph": 20,
            }
            for offset in range(0, 19, 3)
        ]
        hrrr = build_summary._instantaneous_field_eligibility(
            hrrr_items, "gustMph", start, end, 3
        )
        self.assertEqual(hrrr["nominalCadenceHours"], 3)
        self.assertEqual(hrrr["maximumGapHours"], 3.01)
        self.assertFalse(hrrr["eligible"])
        self.assertEqual(hrrr["gaps"][-1]["hours"], 6)

        gefs_items = [
            {
                "validTime": (start + timedelta(hours=offset))
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                "gustMph": 20,
            }
            for offset in range(0, 19, 6)
        ]
        gefs = build_summary._instantaneous_field_eligibility(
            gefs_items, "gustMph", start, end, 6
        )
        self.assertEqual(gefs["nominalCadenceHours"], 6)
        self.assertEqual(gefs["maximumGapHours"], 6.01)
        self.assertTrue(gefs["eligible"])

    def test_fog_coverage_requires_at_least_one_usable_diagnostic(self):
        start = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
        end = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
        timestamps = [start + timedelta(hours=offset) for offset in range(0, 25, 6)]
        temperature_only = [
            {
                "validTime": timestamp.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "temperatureF": 50,
            }
            for timestamp in timestamps
        ]
        diagnostic = build_summary._predicate_eligibility(
            temperature_only,
            build_summary._fog_diagnostic_available,
            start,
            end,
            6,
        )
        self.assertFalse(diagnostic["eligible"])
        self.assertEqual(diagnostic["sampleCount"], 0)

        for item in temperature_only:
            item["visibilityMi"] = 10
        diagnostic = build_summary._predicate_eligibility(
            temperature_only,
            build_summary._fog_diagnostic_available,
            start,
            end,
            6,
        )
        self.assertTrue(diagnostic["eligible"])

    def test_partial_day_eligibility_requires_daytime_and_following_night_coverage(self):
        zone = build_summary.ZoneInfo("America/New_York")
        partial = build_summary.summarize_day(
            date(2026, 10, 25),
            [{"validTime": "2026-10-25T14:00:00Z", "temperatureF": 51.5}],
            following_night_items=[
                {"validTime": "2026-10-26T09:00:00Z", "temperatureF": 49}
            ],
            zone=zone,
        )
        self.assertFalse(partial["highFEligible"])
        self.assertFalse(partial["lowFEligible"])

        complete = build_summary.summarize_day(
            date(2026, 10, 25),
            [{"validTime": "2026-10-25T20:00:00Z", "temperatureF": 70}],
            following_night_items=[
                {"validTime": "2026-10-26T10:00:00Z", "temperatureF": 45}
            ],
            zone=zone,
        )
        self.assertTrue(complete["highFEligible"])
        self.assertTrue(complete["lowFEligible"])

    def test_cadence_gap_and_extractor_warning_prevent_complete_status(self):
        self.assertTrue(
            build_summary.extraction_is_degraded(
                {"statistics": {"failedFileCount": 1}, "warnings": []}
            )
        )
        self.assertTrue(
            build_summary.extraction_is_degraded(
                {"statistics": {"failedFileCount": 0}, "warnings": ["decode warning"]}
            )
        )
        self.assertFalse(
            build_summary.extraction_is_degraded(
                {"statistics": {"failedFileCount": 0}, "warnings": []}
            )
        )
        start = datetime(2026, 10, 23, tzinfo=timezone.utc)
        end = start + timedelta(hours=72)

        def records(hours):
            return [
                {
                    "model": "gfs",
                    "cycle": "2026-10-23T00:00:00Z",
                    "validTime": (start + timedelta(hours=hour)).isoformat().replace("+00:00", "Z"),
                    "leadTimeHours": hour,
                    "sourceUrls": ["https://nomads.ncep.noaa.gov/fixture"],
                    "points": {"KDVK": {"temperatureF": 50 + hour / 24}},
                    "neighborhood": {},
                }
                for hour in hours
            ]

        gapped, warnings = build_summary.build_model_summary(
            "gfs",
            records([0, 6, 18, 24, 30, 36, 42, 48, 54, 60, 66, 72]),
            ["KDVK"],
            {date(2026, 10, 23), date(2026, 10, 24), date(2026, 10, 25)},
            start,
            end,
            build_summary.ZoneInfo("America/New_York"),
        )
        self.assertEqual(gapped["status"], "partial")
        self.assertEqual(len(gapped["cadenceGaps"]), 1)
        self.assertTrue(any("cadence gap" in warning for warning in warnings))

        degraded, warnings = build_summary.build_model_summary(
            "gfs",
            records(range(0, 73, 6)),
            ["KDVK"],
            {date(2026, 10, 23), date(2026, 10, 24), date(2026, 10, 25)},
            start,
            end,
            build_summary.ZoneInfo("America/New_York"),
            extraction_degraded=True,
        )
        self.assertEqual(degraded["status"], "partial")
        self.assertTrue(any("extraction reported" in warning for warning in warnings))

    def test_redundant_accumulation_windows_form_one_exact_nonoverlapping_path(self):
        start = datetime(2026, 10, 23, 0, tzinfo=timezone.utc)

        def accumulation(end_hour: int, amount: float, step_start: int) -> dict:
            return {
                "validTime": (start + timedelta(hours=end_hour))
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
                "precipitationIn": amount,
                "precipitationInterval": {
                    "stepRange": f"{step_start}-{end_hour}",
                    "startHour": step_start,
                    "endHour": end_hour,
                },
            }

        # NAM/RAP commonly provide overlapping 1-, 2- and 3-hour windows.
        # Both the short and long exact paths below total 0.23 inch.
        items = [
            accumulation(2, 0.10, 0),
            accumulation(3, 0.15, 0),
            accumulation(4, 0.02, 3),
            accumulation(5, 0.04, 3),
            accumulation(6, 0.08, 3),
        ]
        self.assertEqual(build_summary.add_precip_increments(items), [])
        amount, complete, flags, coverage = build_summary._qpf_summary(
            items, start, start + timedelta(hours=6)
        )
        self.assertTrue(complete)
        self.assertEqual(flags, [])
        self.assertAlmostEqual(amount, 0.23, places=3)
        self.assertEqual(coverage["selectionMethod"], "exact-non-overlapping-interval-path")
        self.assertEqual(coverage["selectedIntervalCount"], 2)
        self.assertGreaterEqual(coverage["candidateIntervalCount"], 6)

    def test_conflicting_exact_qpf_paths_are_withheld(self):
        start = datetime(2026, 10, 23, 0, tzinfo=timezone.utc)
        end = start + timedelta(hours=1)

        def candidate(amount: float) -> dict:
            return {
                "validTime": "2026-10-23T01:00:00Z",
                "precipitationIn": amount,
                "precipitationIncrementCandidates": [
                    {
                        "startTime": "2026-10-23T00:00:00Z",
                        "endTime": "2026-10-23T01:00:00Z",
                        "amountIn": amount,
                        "method": "fixture",
                        "sourceStepRange": "0-1",
                    }
                ],
            }

        amount, complete, flags, _ = build_summary._qpf_summary(
            [candidate(0.1), candidate(0.2)], start, end
        )
        self.assertFalse(complete)
        self.assertIsNone(amount)
        self.assertTrue(any("conflicting" in flag for flag in flags))

    def test_cumulative_decrease_is_withheld_but_new_step_start_is_a_valid_reset(self):
        start = datetime(2026, 10, 23, 0, tzinfo=timezone.utc)
        decreased = [
            {
                "validTime": "2026-10-23T01:00:00Z",
                "precipitationIn": 0.2,
                "precipitationInterval": {"stepRange": "0-1", "startHour": 0, "endHour": 1},
            },
            {
                "validTime": "2026-10-23T02:00:00Z",
                "precipitationIn": 0.1,
                "precipitationInterval": {"stepRange": "0-2", "startHour": 0, "endHour": 2},
            },
        ]
        warnings = build_summary.add_precip_increments(decreased)
        self.assertTrue(any("decreased" in warning for warning in warnings))
        amount, complete, flags, _ = build_summary._qpf_summary(
            decreased, start, start + timedelta(hours=2)
        )
        self.assertFalse(complete)
        self.assertIsNone(amount)
        self.assertTrue(any("decreased" in flag for flag in flags))

        reset = [
            {
                "validTime": "2026-10-23T03:00:00Z",
                "precipitationIn": 0.3,
                "precipitationInterval": {"stepRange": "0-3", "startHour": 0, "endHour": 3},
            },
            {
                "validTime": "2026-10-23T04:00:00Z",
                "precipitationIn": 0.1,
                "precipitationInterval": {"stepRange": "3-4", "startHour": 3, "endHour": 4},
            },
        ]
        self.assertEqual(build_summary.add_precip_increments(reset), [])
        amount, complete, flags, _ = build_summary._qpf_summary(
            reset, start, start + timedelta(hours=4)
        )
        self.assertTrue(complete)
        self.assertEqual(flags, [])
        self.assertAlmostEqual(amount, 0.4, places=3)

    def test_high_resolution_qpf_sources_request_every_available_hour(self):
        config = json.loads((ROOT / "config" / "grib" / "danville.json").read_text())
        self.assertEqual(config["models"]["hrrr"]["sampling"]["baseIntervalHours"], 1)
        self.assertEqual(config["models"]["rap"]["sampling"]["baseIntervalHours"], 1)
        self.assertEqual(config["models"]["nam"]["sampling"]["baseIntervalHours"], 3)
        self.assertEqual(
            config["models"]["nam_conusnest"]["sampling"]["baseIntervalHours"], 3
        )

    def test_qpf_excludes_pre_window_increment_and_rejects_positive_boundary_crossing(self):
        zone = build_summary.ZoneInfo("America/New_York")
        window_start = datetime(2026, 10, 23, 11, tzinfo=timezone.utc)
        window_end = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
        exact = build_summary.summarize_day(
            date(2026, 10, 23),
            [
                {
                    "validTime": "2026-10-23T11:00:00Z",
                    "precipitationIn": 0.25,
                    "precipitationIncrementIn": 0.25,
                    "precipitationIncrementStartTime": "2026-10-23T10:00:00Z",
                    "precipitationIncrementEndTime": "2026-10-23T11:00:00Z",
                },
                {
                    "validTime": "2026-10-23T12:00:00Z",
                    "precipitationIn": 0.35,
                    "precipitationIncrementIn": 0.1,
                    "precipitationIncrementStartTime": "2026-10-23T11:00:00Z",
                    "precipitationIncrementEndTime": "2026-10-23T12:00:00Z",
                },
            ],
            zone=zone,
            forecast_window_start=window_start,
            forecast_window_end=window_end,
        )
        self.assertTrue(exact["qpfComplete"])
        self.assertAlmostEqual(exact["qpfIn"], 0.1, places=3)

        crossing = build_summary.summarize_day(
            date(2026, 10, 23),
            [
                {
                    "validTime": "2026-10-23T12:00:00Z",
                    "precipitationIn": 0.2,
                    "precipitationIncrementIn": 0.2,
                    "precipitationIncrementStartTime": "2026-10-23T10:00:00Z",
                    "precipitationIncrementEndTime": "2026-10-23T12:00:00Z",
                }
            ],
            zone=zone,
            forecast_window_start=window_start,
            forecast_window_end=window_end,
        )
        self.assertFalse(crossing["qpfComplete"])
        self.assertIsNone(crossing["qpfIn"])
        self.assertTrue(any("not apportioned" in flag for flag in crossing["qpfQualityFlags"]))

    def test_positive_boundary_alternative_does_not_poison_an_exact_qpf_path(self):
        start = datetime(2026, 10, 23, 11, tzinfo=timezone.utc)
        end = start + timedelta(hours=1)
        items = [
            {
                "validTime": "2026-10-23T12:00:00Z",
                "precipitationIn": 0.1,
                "precipitationIncrementIn": 0.1,
                "precipitationIncrementStartTime": "2026-10-23T11:00:00Z",
                "precipitationIncrementEndTime": "2026-10-23T12:00:00Z",
            },
            {
                "validTime": "2026-10-23T12:00:00Z",
                "precipitationIn": 0.2,
                "precipitationIncrementIn": 0.2,
                "precipitationIncrementStartTime": "2026-10-23T10:00:00Z",
                "precipitationIncrementEndTime": "2026-10-23T12:00:00Z",
            },
        ]
        amount, complete, flags, coverage = build_summary._qpf_summary(items, start, end)
        self.assertTrue(complete)
        self.assertEqual(flags, [])
        self.assertAlmostEqual(amount, 0.1, places=3)
        self.assertEqual(coverage["excludedPositiveBoundaryCrossingCount"], 1)

    def test_qpf_requires_following_night_coverage(self):
        zone = build_summary.ZoneInfo("America/New_York")
        window_start = datetime(2026, 10, 23, 11, tzinfo=timezone.utc)
        window_end = datetime(2026, 10, 26, 11, tzinfo=timezone.utc)
        final_day = build_summary.summarize_day(
            date(2026, 10, 25),
            [
                {
                    "validTime": "2026-10-25T18:00:00Z",
                    "precipitationIn": 0,
                    "precipitationIncrementIn": 0,
                    "precipitationIncrementStartTime": "2026-10-25T12:00:00Z",
                    "precipitationIncrementEndTime": "2026-10-25T18:00:00Z",
                }
            ],
            zone=zone,
            forecast_window_start=window_start,
            forecast_window_end=window_end,
        )
        self.assertFalse(final_day["qpfComplete"])
        self.assertIsNone(final_day["qpfIn"])
        self.assertEqual(final_day["qpfCoverage"]["requiredEnd"], "2026-10-26T11:00:00Z")
        self.assertEqual(final_day["qpfCoverage"]["coveredEnd"], "2026-10-25T18:00:00Z")
        self.assertTrue(final_day["qpfCoverage"]["gaps"])

    def test_daily_qpf_windows_are_non_overlapping(self):
        zone = build_summary.ZoneInfo("America/New_York")
        window_start = datetime(2026, 10, 23, 11, tzinfo=timezone.utc)
        window_end = datetime(2026, 10, 26, 11, tzinfo=timezone.utc)
        items = [
            {
                "validTime": "2026-10-24T12:00:00Z",
                "precipitationIn": 0.1,
                "precipitationIncrementIn": 0.1,
                "precipitationIncrementStartTime": "2026-10-23T11:00:00Z",
                "precipitationIncrementEndTime": "2026-10-24T12:00:00Z",
            },
            {
                "validTime": "2026-10-25T12:00:00Z",
                "precipitationIn": 0.2,
                "precipitationIncrementIn": 0.2,
                "precipitationIncrementStartTime": "2026-10-24T12:00:00Z",
                "precipitationIncrementEndTime": "2026-10-25T12:00:00Z",
            },
            {
                "validTime": "2026-10-26T11:00:00Z",
                "precipitationIn": 0.3,
                "precipitationIncrementIn": 0.3,
                "precipitationIncrementStartTime": "2026-10-25T12:00:00Z",
                "precipitationIncrementEndTime": "2026-10-26T11:00:00Z",
            },
        ]
        days = build_summary.group_days(
            items,
            {date(2026, 10, 23), date(2026, 10, 24), date(2026, 10, 25)},
            zone,
            forecast_window_start=window_start,
            forecast_window_end=window_end,
        )
        self.assertEqual([day["qpfIn"] for day in days], [0.1, 0.2, 0.3])
        self.assertAlmostEqual(sum(day["qpfIn"] for day in days), 0.6, places=3)
        self.assertEqual(days[0]["qpfCoverage"]["requiredEnd"], days[1]["qpfCoverage"]["requiredStart"])
        self.assertEqual(days[1]["qpfCoverage"]["requiredEnd"], days[2]["qpfCoverage"]["requiredStart"])

    def test_final_day_counts_every_sample_through_exact_72_hour_end(self):
        zone = build_summary.ZoneInfo("America/New_York")
        window_start = datetime(2026, 10, 23, 16, 15, 48, tzinfo=timezone.utc)
        window_end = datetime(2026, 10, 26, 16, 15, 48, tzinfo=timezone.utc)
        items = [
            {"validTime": "2026-10-25T20:00:00Z", "temperatureF": 70},
            {"validTime": "2026-10-25T23:00:00Z", "temperatureF": 60},
            {"validTime": "2026-10-26T06:00:00Z", "temperatureF": 40},
            {"validTime": "2026-10-26T12:00:00Z", "temperatureF": 42},
            {"validTime": "2026-10-26T15:00:00Z", "temperatureF": 10},
            {"validTime": "2026-10-26T18:00:00Z", "temperatureF": 0},
        ]
        for is_neighborhood in (False, True):
            with self.subTest(is_neighborhood=is_neighborhood):
                days = build_summary.group_days(
                    items,
                    {date(2026, 10, 23), date(2026, 10, 24), date(2026, 10, 25)},
                    zone,
                    is_neighborhood=is_neighborhood,
                    forecast_window_start=window_start,
                    forecast_window_end=window_end,
                )

                final_day = days[2]
                self.assertEqual(final_day["calendarSampleCount"], 2)
                self.assertEqual(final_day["followingNightSampleCount"], 4)
                self.assertEqual(final_day["sampleCount"], 5)
                self.assertEqual(final_day["lastValidTime"], "2026-10-26T15:00:00Z")
                self.assertEqual(final_day["lowF"], 40)
                self.assertEqual(final_day["signals"]["periodEnd"], "2026-10-26T12:00:00Z")

    def test_rejected_precipitation_is_incomplete_and_not_benchmarked(self):
        day = build_summary.summarize_day(
            date(2026, 10, 23),
            [
                {
                    "validTime": "2026-10-23T20:00:00Z",
                    "temperatureF": 70,
                    "precipitationIn": 0,
                    "precipitationIncrementIn": None,
                    "precipitationQualityFlag": "overlapping precipitation intervals were not summed",
                }
            ],
            following_night_items=[
                {"validTime": "2026-10-24T10:00:00Z", "temperatureF": 45}
            ],
            zone=build_summary.ZoneInfo("America/New_York"),
        )
        self.assertFalse(day["qpfComplete"])
        self.assertIsNone(day["qpfIn"])
        self.assertIn("qpfIn", day["missingFields"])
        self.assertEqual(
            day["qpfQualityFlags"],
            ["overlapping precipitation intervals were not summed"],
        )
        benchmarks = build_summary.build_benchmarks(
            [
                {
                    "model": "nam",
                    "cycle": "2026-10-23T00:00:00Z",
                    "sourceUrls": ["https://nomads.ncep.noaa.gov/fixture"],
                    "stations": {"KDVK": {"days": [day]}},
                }
            ],
            build_summary.ZoneInfo("America/New_York"),
        )
        self.assertEqual(len(benchmarks), 1)
        self.assertNotIn("precipitationIn", benchmarks[0])

    def test_benchmarks_use_canonical_forecast_model_names(self):
        day = {
            "date": "2026-10-23",
            "highF": 70,
            "lowF": 45,
            "highFEligible": True,
            "lowFEligible": True,
            "qpfComplete": False,
            "weatherRegime": "dry-high-pressure",
        }
        models = [
            {
                "model": model,
                "cycle": "2026-10-23T00:00:00Z",
                "sourceUrls": ["https://example.test/guidance.grib2"],
                "stations": {"KDVK": {"days": [day]}},
            }
            for model in build_summary.FORECAST_MODEL_NAMES
        ]
        benchmarks = build_summary.build_benchmarks(
            models, build_summary.ZoneInfo("America/New_York")
        )
        self.assertEqual(
            {benchmark["model"] for benchmark in benchmarks},
            set(build_summary.FORECAST_MODEL_NAMES.values()),
        )

    def test_step_range_is_preserved(self):
        self.assertEqual(
            extract_guidance.parse_step_range("12-15"),
            {"stepRange": "12-15", "startHour": 12.0, "endHour": 15.0, "durationHours": 3.0},
        )
        self.assertEqual(
            extract_guidance.INTERVAL_METADATA_KEYS,
            {
                "precipitationIn": "precipitationInterval",
                "maximumTemperatureF": "maximumTemperatureInterval",
                "minimumTemperatureF": "minimumTemperatureInterval",
            },
        )

    def test_midnight_valid_time_is_not_replaced_by_cycle_hour(self):
        try:
            import eccodes
        except ImportError:
            self.skipTest("eccodes is not installed")
        gid = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
        try:
            eccodes.codes_set(gid, "dataDate", 20261023)
            eccodes.codes_set(gid, "dataTime", 600)
            eccodes.codes_set(gid, "forecastTime", 18)
            cycle, valid, lead = extract_guidance._valid_time(eccodes, gid)
        finally:
            eccodes.codes_release(gid)
        self.assertEqual(cycle, "2026-10-23T06:00Z")
        self.assertEqual(valid, "2026-10-24T00:00Z")
        self.assertEqual(lead, 18)

    def test_manifest_contract_is_normalized(self):
        (ROOT / "grib-data").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "grib-data") as temporary:
            directory = Path(temporary)
            staged = directory / "sample.grib2"
            staged.write_bytes(b"GRIBfixture7777")
            manifest_path = directory / "latest.json"
            manifest = {
                "schemaVersion": 1,
                "files": [
                    {
                        "model": "gfs",
                        "variant": "deterministic",
                        "cycleTime": "2026-10-23T00:00:00Z",
                        "forecastHour": 12,
                        "localPath": "sample.grib2",
                        "status": "complete",
                    },
                    {"model": "hrrr", "localPath": "failed.grib2", "status": "failed"},
                ],
            }
            files = extract_guidance.manifest_files(manifest, manifest_path)
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0]["model"], "gfs")
            self.assertEqual(Path(files[0]["localPath"]), staged)

    def test_extractor_rejects_mutable_latest_manifest_alias(self):
        (ROOT / "grib-data").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "grib-data") as temporary:
            directory = Path(temporary)
            manifest_path = directory / "latest.json"
            manifest_path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "immutable versioned manifest"):
                extract_guidance.main(
                    [
                        "--config",
                        str(ROOT / "config" / "grib" / "danville.json"),
                        "--manifest",
                        str(manifest_path),
                        "--output",
                        str(directory / "extracted.json"),
                    ]
                )

    def test_eccodes_sample_reaches_point_extractor(self):
        try:
            import eccodes
        except ImportError:
            self.skipTest("eccodes is not installed")
        (ROOT / "grib-data").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "grib-data") as temporary:
            directory = Path(temporary)
            grib_path = directory / "sample.grib2"
            gid = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
            try:
                eccodes.codes_set(gid, "Ni", 5)
                eccodes.codes_set(gid, "Nj", 5)
                eccodes.codes_set(gid, "latitudeOfFirstGridPointInDegrees", 36.6)
                eccodes.codes_set(gid, "longitudeOfFirstGridPointInDegrees", 274.2)
                eccodes.codes_set(gid, "latitudeOfLastGridPointInDegrees", 38.7)
                eccodes.codes_set(gid, "longitudeOfLastGridPointInDegrees", 276.25)
                eccodes.codes_set(gid, "iDirectionIncrementInDegrees", 0.5125)
                eccodes.codes_set(gid, "jDirectionIncrementInDegrees", 0.525)
                eccodes.codes_set(gid, "jScansPositively", 1)
                eccodes.codes_set(gid, "paramId", 167)  # 2 m temperature
                eccodes.codes_set(gid, "dataDate", 20261023)
                eccodes.codes_set(gid, "dataTime", 0)
                eccodes.codes_set(gid, "forecastTime", 12)
                eccodes.codes_set_values(gid, [280.0] * 25)
                with grib_path.open("wb") as handle:
                    eccodes.codes_write(gid, handle)
            finally:
                eccodes.codes_release(gid)

            manifest_path = directory / "fetch-20261023T100000Z.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "issuedAt": "2026-10-23T10:00:00Z",
                        "requestedModels": ["gfs"],
                        "models": {
                            "gfs": {
                                "cycleTime": "2026-10-23T00:00:00Z",
                                "cycleAgeHours": 10,
                                "maximumCycleAgeHours": 12,
                                "freshnessStatus": "fresh",
                            }
                        },
                        "files": [
                            {
                                "model": "gfs",
                                "variant": "deterministic",
                                "cycleTime": "2026-10-23T00:00:00Z",
                                "forecastHour": 12,
                                "validTime": "2026-10-23T12:00:00Z",
                                "sourceUrl": "https://nomads.ncep.noaa.gov/fixture",
                                "localPath": str(grib_path),
                                "status": "complete",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = directory / "extracted.json"
            code = extract_guidance.main(
                [
                    "--config",
                    str(ROOT / "config" / "grib" / "danville.json"),
                    "--manifest",
                    str(manifest_path),
                    "--output",
                    str(output),
                    "--strict",
                ]
            )
            self.assertEqual(code, 0)
            extracted = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(extracted["requestedModels"], ["gfs"])
            self.assertEqual(extracted["issuedAt"], "2026-10-23T10:00:00Z")
            self.assertEqual(
                extracted["modelFreshness"]["gfs"],
                {
                    "cycleTime": "2026-10-23T00:00:00Z",
                    "cycleAgeHours": 10,
                    "maximumCycleAgeHours": 12,
                    "freshnessStatus": "fresh",
                },
            )
            self.assertEqual(extracted["statistics"]["decodedMessageCount"], 1)
            self.assertEqual(
                extracted["sourceManifest"], manifest_path.relative_to(ROOT).as_posix()
            )
            self.assertEqual(extracted["configPath"], "config/grib/danville.json")
            self.assertEqual(
                extracted["provenance"]["sourceManifest"]["sha256"],
                extract_guidance.sha256_file(manifest_path),
            )
            self.assertEqual(
                extracted["provenance"]["configuration"]["sha256"],
                extract_guidance.sha256_file(ROOT / "config" / "grib" / "danville.json"),
            )
            self.assertEqual(
                extracted["records"][0]["sourceFiles"],
                [grib_path.relative_to(ROOT).as_posix()],
            )
            self.assertNotIn(str(ROOT), json.dumps(extracted))
            point = extracted["records"][0]["points"]["KDVK"]
            self.assertAlmostEqual(point["temperatureF"], 44.33, places=2)

    def test_summary_is_72_hours_and_manual_only(self):
        forecast_before = (ROOT / "forecast.json").read_bytes()
        (ROOT / "grib-data").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "grib-data") as temporary:
            output = Path(temporary) / "danville-guidance.json"
            exit_code = build_summary.main(
                [
                    "--input",
                    str(FIXTURE),
                    "--output",
                    str(output),
                    "--start",
                    "2026-10-23T10:00:00Z",
                    "--hours",
                    "72",
                ]
            )
            self.assertEqual(exit_code, 0)
            result = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result["coverage"]["hours"], 72)
        self.assertEqual(result["coverage"]["start"], "2026-10-23T10:00:00Z")
        self.assertEqual(result["coverage"]["end"], "2026-10-26T10:00:00Z")
        self.assertEqual(
            result["input"]["path"], "scripts/tests/fixtures/grib/extracted-guidance.json"
        )
        self.assertEqual(result["input"]["sha256"], build_summary.sha256_file(FIXTURE))
        self.assertEqual(result["input"]["issuedAt"], "2026-10-23T10:00:00Z")
        self.assertEqual(
            result["models"][0]["freshness"],
            {
                "issuedAt": "2026-10-23T10:00:00Z",
                "cycleTime": "2026-10-23T00:00:00Z",
                "cycleAgeHours": 10,
                "maximumCycleAgeHours": 12,
                "freshnessStatus": "fresh",
            },
        )
        self.assertNotIn(str(ROOT), json.dumps(result))
        self.assertFalse(result["publicationSafety"]["writesForecastJson"])
        self.assertTrue(result["publicationSafety"]["manualReviewRequired"])
        self.assertEqual((ROOT / "forecast.json").read_bytes(), forecast_before)

        benchmarks = result["proposedForecastBenchmarks"]
        self.assertTrue(all(item["station"] in {"KDVK", "KLEX"} for item in benchmarks))
        self.assertNotIn("Danville", {item["station"] for item in benchmarks})
        self.assertTrue(all(item["region"] == "kentucky" for item in benchmarks))

        kdvk_days = result["models"][0]["stations"]["KDVK"]["days"]
        self.assertEqual(len(kdvk_days), 3)
        self.assertTrue(all(day["qpfIn"] is None for day in kdvk_days))
        self.assertTrue(all(day["qpfComplete"] is False for day in kdvk_days))
        self.assertTrue(all(day["qpfCoverage"]["gaps"] for day in kdvk_days))
        self.assertTrue(all(isinstance(day["missingFields"], list) for day in kdvk_days))
        self.assertTrue(
            all(isinstance(day["missingFields"], list) for day in result["consensus"]["days"])
        )
        self.assertTrue(
            all(
                isinstance(day["missingFields"], list)
                for day in result["models"][0]["neighborhood"]["days"]
            )
        )


if __name__ == "__main__":
    unittest.main()
