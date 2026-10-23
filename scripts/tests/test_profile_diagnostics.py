from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


profiles = load_script(
    "grib_profile_diagnostics", ROOT / "scripts" / "grib" / "profile_diagnostics.py"
)


def record(valid_time: str, value: float) -> dict:
    return {
        "validTime": valid_time,
        "temperature850hPaC": value,
        "relativeHumidity700hPaPct": 40 + value,
        "height500hPaM": 5_700 + value * 2,
        "precipitableWaterIn": 0.5 + value / 100,
        "uWind850hPaMph": 3,
        "vWind850hPaMph": 4,
        "uWind700hPaMph": 5,
        "vWind700hPaMph": 12,
        "verticalVelocity700hPaPaPerS": -value / 100,
    }


class ProfileDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
        self.end = datetime(2026, 10, 24, 6, tzinfo=timezone.utc)

    def test_reports_profile_statistics_tendency_and_explicit_missingness(self):
        result = profiles.build_profile_diagnostics(
            [
                record("2026-10-23T12:00:00Z", 6),
                record("2026-10-23T18:00:00Z", 8),
                record("2026-10-24T00:00:00Z", 10),
                # The end boundary belongs to the following period.
                record("2026-10-24T06:00:00Z", 50),
            ],
            self.start,
            self.end,
        )

        temperature = result["fields"]["temperature850hPaC"]
        self.assertEqual(temperature["count"], 3)
        self.assertEqual(temperature["max"], 10)
        self.assertTrue(temperature["coverageComplete"])
        self.assertEqual(result["fields"]["height500hPaM"]["tendencyM"], 8)
        self.assertEqual(result["fields"]["wind850hPaMph"]["mean"], 5)
        self.assertIn("wind500hPaMph", result["missingFields"])
        self.assertIsNone(result["fields"]["wind500hPaMph"])
        self.assertEqual(result["boundarySemantics"], "start-inclusive/end-exclusive")

    def test_marks_temporal_gaps_and_missing_vector_components_incomplete(self):
        first = record("2026-10-23T12:00:00Z", 6)
        last = record("2026-10-24T00:00:00Z", 10)
        del last["vWind850hPaMph"]
        result = profiles.build_profile_diagnostics(
            [first, last], self.start, self.end
        )

        temperature = result["fields"]["temperature850hPaC"]
        self.assertFalse(temperature["coverageComplete"])
        self.assertEqual(temperature["coverageGaps"][0]["kind"], "internal")
        self.assertFalse(result["fields"]["wind850hPaMph"]["coverageComplete"])
        self.assertIn("wind850hPaMph", result["incompleteFields"])

    def test_consensus_uses_one_complete_representative_per_model(self):
        model_a = profiles.build_profile_diagnostics(
            [
                record("2026-10-23T12:00:00Z", 0),
                record("2026-10-23T18:00:00Z", 0),
                record("2026-10-24T00:00:00Z", 30),
            ],
            self.start,
            self.end,
        )
        model_b = profiles.build_profile_diagnostics(
            [
                record("2026-10-23T12:00:00Z", 20),
                record("2026-10-23T18:00:00Z", 20),
                record("2026-10-24T00:00:00Z", 20),
            ],
            self.start,
            self.end,
        )
        model_c = profiles.build_profile_diagnostics(
            [record("2026-10-23T12:00:00Z", 100)], self.start, self.end
        )

        consensus = profiles.build_profile_consensus(
            [
                ("a", {"atmosphericProfile": model_a}),
                ("b", {"atmosphericProfile": model_b}),
                ("c", {"atmosphericProfile": model_c}),
            ]
        )

        temperature = consensus["fields"]["temperature850hPaC"]
        self.assertEqual(temperature["count"], 2)
        self.assertEqual(temperature["mean"], 15)
        self.assertEqual(
            consensus["fieldContributors"]["temperature850hPaC"], ["a", "b"]
        )
        self.assertEqual(
            consensus["fieldExcludedModels"]["temperature850hPaC"], ["c"]
        )
        self.assertEqual(
            consensus["selectionMethods"]["precipitableWaterIn"],
            "daily-maximum-per-model",
        )
        self.assertTrue(consensus["periodAgreement"])
        self.assertEqual(consensus["periodStart"], "2026-10-23T12:00Z")
        self.assertEqual(consensus["periodEnd"], "2026-10-24T06:00Z")

    def test_profile_coverage_distinguishes_three_hourly_and_six_hourly_cadence(self):
        start = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
        end = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
        hrrr = profiles.build_profile_diagnostics(
            [
                record(
                    (start + timedelta(hours=offset))
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z"),
                    8,
                )
                for offset in range(0, 19, 3)
            ],
            start,
            end,
            model_cadence_hours=3,
        )
        hrrr_temperature = hrrr["fields"]["temperature850hPaC"]
        self.assertEqual(hrrr_temperature["nominalCadenceHours"], 3)
        self.assertEqual(hrrr_temperature["maximumGapHours"], 3.01)
        self.assertFalse(hrrr_temperature["coverageComplete"])
        self.assertEqual(hrrr_temperature["coverageGaps"][-1]["kind"], "trailing")
        self.assertEqual(hrrr_temperature["coverageGaps"][-1]["hours"], 6)

        gefs = profiles.build_profile_diagnostics(
            [
                record(
                    (start + timedelta(hours=offset))
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z"),
                    8,
                )
                for offset in range(0, 19, 6)
            ],
            start,
            end,
            model_cadence_hours=6,
        )
        gefs_temperature = gefs["fields"]["temperature850hPaC"]
        self.assertEqual(gefs_temperature["nominalCadenceHours"], 6)
        self.assertEqual(gefs_temperature["maximumGapHours"], 6.01)
        self.assertTrue(gefs_temperature["coverageComplete"])


if __name__ == "__main__":
    unittest.main()
