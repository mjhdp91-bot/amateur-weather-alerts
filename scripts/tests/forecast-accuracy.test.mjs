import test from "node:test";
import assert from "node:assert/strict";
import {
  evaluateRegionCheckpoint,
  parseProbability,
  parseRange,
  suggestedBiasAdjustment,
  summarizeNwsObservations,
  validateForecastFeedback,
} from "../lib/forecast-accuracy.mjs";

test("parses forecast ranges and precipitation probabilities", () => {
  assert.deepEqual(parseRange("72–76"), { min: 72, max: 76 });
  assert.equal(parseProbability("5% or less"), 0.05);
  assert.equal(parseProbability("60–70% through noon; under 10% tonight"), 0.65);
});

test("summarizes official NWS observation units", () => {
  const summary = summarizeNwsObservations([
    {
      properties: {
        timestamp: "2026-09-29T12:00:00Z",
        temperature: { value: 10 },
        windSpeed: { value: 5 },
        windGust: { value: 8 },
        precipitationLastHour: { value: 0.001 },
        visibility: { value: 800 },
        textDescription: "Light Rain and Fog",
      },
    },
    {
      properties: {
        timestamp: "2026-09-29T18:00:00Z",
        temperature: { value: 20 },
        windSpeed: { value: 2 },
        windGust: { value: null },
        precipitationLastHour: { value: 0 },
        visibility: { value: 16000 },
        textDescription: "Clear",
      },
    },
  ]);
  assert.equal(summary.highF, 68);
  assert.equal(summary.lowF, 50);
  assert.equal(summary.rainReports, 1);
  assert.equal(summary.fogReports, 1);
  assert.equal(summary.minimumVisibilityMi, 0.5);
});

test("scores ranges and Brier probability at a checkpoint", () => {
  const forecast = {
    forecastId: "test",
    regions: {
      kentucky: {
        days: [{ date: "2026-09-29", highF: "70–74", lowF: "48–52", precipitation: "20%", weatherRegime: "dry-high-pressure" }],
      },
    },
  };
  const result = evaluateRegionCheckpoint({
    forecast,
    regionKey: "kentucky",
    checkpointHours: 24,
    stationObservations: {
      KDVK: { highF: 73, lowF: 47, rainReports: 0 },
      KLEX: { highF: 75, lowF: 50, rainReports: 0 },
    },
  });
  assert.equal(result.stationScores.KDVK.highTemperature.withinRange, true);
  assert.equal(result.stationScores.KDVK.lowTemperature.withinRange, false);
  assert.equal(result.precipitation.brierScore, 0.04);
});

test("bias corrections require repeated evidence and remain bounded", () => {
  assert.deepEqual(suggestedBiasAdjustment(3, 3), { status: "monitoring", adjustmentF: 0 });
  assert.deepEqual(suggestedBiasAdjustment(3, 5), { status: "recommended", adjustmentF: 2.5 });
  assert.deepEqual(suggestedBiasAdjustment(-10, 10), { status: "recommended", adjustmentF: -4 });
});

test("strict validation requires model, regime and feedback evidence", () => {
  const issuedAt = "2026-09-29T07:40:00-04:00";
  const forecast = {
    forecastId: "test",
    issuedAt,
    validThrough: "2026-10-02T07:40:00-04:00",
    regions: {
      kentucky: { days: Array.from({ length: 3 }, (_, index) => ({ weatherRegime: "dry-high-pressure", date: `2026-10-0${index + 1}` })) },
      upstate: { days: Array.from({ length: 3 }, (_, index) => ({ weatherRegime: "dry-high-pressure", date: `2026-10-0${index + 1}` })) },
    },
    verification: { checkpoints: [{ hours: 24 }, { hours: 48 }, { hours: 72 }] },
    modelStatus: [{ model: "HRRR" }],
    modelBenchmarks: [{ model: "HRRR", run: "2026-09-29 12Z", region: "kentucky", targetDate: "2026-10-01", leadTimeHours: 24, highF: 80 }],
    accuracyFeedback: {
      source: "forecast-accuracy.json",
      summaryGeneratedAt: "2026-09-29T12:00:00Z",
      consultedForecastIds: ["prior"],
      appliedAdjustments: [
        { region: "kentucky", decision: "no-adjustment", reason: "insufficient sample" },
        { region: "upstate", decision: "no-adjustment", reason: "insufficient sample" },
      ],
    },
  };
  assert.deepEqual(validateForecastFeedback(forecast, { generatedAt: "2026-09-29T12:00:00Z" }), []);
  delete forecast.accuracyFeedback;
  assert.ok(validateForecastFeedback(forecast, {}).some((error) => error.includes("accuracyFeedback")));
});
