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
  assert.equal(summary.predominantSkyCondition, "clear");
  assert.deepEqual(summary.eventPeriods.fog, ["morning"]);
});

test("converts NWS kilometer-per-hour winds without inflating them", () => {
  const summary = summarizeNwsObservations([
    {
      properties: {
        timestamp: "2026-10-13T18:00:00Z",
        temperature: { value: 20 },
        windSpeed: { value: 16, unitCode: "wmoUnit:km_h-1" },
        windGust: { value: 32, unitCode: "wmoUnit:km_h-1" },
        precipitationLastHour: { value: 12.7, unitCode: "wmoUnit:mm" },
        visibility: { value: 16000 },
        textDescription: "Partly Cloudy",
      },
    },
  ]);
  assert.equal(summary.maximumSustainedWindMph, 9.9);
  assert.equal(summary.maximumGustMph, 19.9);
  assert.equal(summary.maximumHourlyPrecipitationIn, 0.5);
  assert.equal(summary.predominantSkyCondition, "partly-cloudy");
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
  assert.ok(Number.isFinite(result.expanded.scores.visibilityFog.score));
});

test("scores structured wind, visibility, cloud, thunder, hazards, timing, alerts and confidence", () => {
  const forecast = {
    forecastId: "expanded",
    sourceNotes: ["No official alerts were active across Central Kentucky at issuance."],
    regions: {
      kentucky: {
        confidence: "High",
        days: [{
          date: "2026-10-13",
          highF: "70–74",
          lowF: "48–52",
          precipitation: "70%",
          weatherRegime: "frontal-convective",
          verificationTargets: {
            wind: { sustainedRangeMph: [8, 16], gustRangeMph: [18, 28] },
            visibility: { fogProbability: 0.1, minimumVisibilityRangeMiles: null },
            sky: { categories: ["mostly-cloudy"] },
            thunderProbability: 0.7,
            hazards: ["thunder"],
            eventTiming: { rain: ["afternoon"], thunder: ["afternoon"], fog: [], wind: [] },
            confidence: 0.8,
            officialAlerts: { status: "none", checkedAt: "2026-10-13T12:00:00Z", sourceUrl: "https://api.weather.gov/alerts/active" },
          },
        }],
      },
    },
  };
  const result = evaluateRegionCheckpoint({
    forecast,
    regionKey: "kentucky",
    checkpointHours: 24,
    stationObservations: {
      KDVK: {
        highF: 72,
        lowF: 50,
        maximumSustainedWindMph: 14,
        maximumGustMph: 25,
        minimumVisibilityMi: 8,
        maximumHourlyPrecipitationIn: 0.1,
        rainReports: 2,
        thunderReports: 1,
        fogReports: 0,
        predominantSkyCondition: "mostly-cloudy",
        skyConditionCounts: { clear: 0, "mostly-clear": 0, "partly-cloudy": 0, "mostly-cloudy": 4, overcast: 0 },
        eventPeriods: { rain: ["afternoon"], thunder: ["afternoon"], fog: [], wind: [] },
      },
    },
  });
  assert.equal(result.expanded.scores.wind.score, 1);
  assert.equal(result.expanded.scores.cloudCover.score, 1);
  assert.equal(result.expanded.scores.hazards.score, 1);
  assert.equal(result.expanded.scores.eventTiming.score, 1);
  assert.equal(result.expanded.scores.officialAlertHandling.score, 1);
  assert.ok(result.expanded.scores.confidenceCalibration.score > 0.9);
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
      kentucky: { days: Array.from({ length: 3 }, (_, index) => ({ weatherRegime: "dry-high-pressure", date: `2026-10-0${index + 1}`, verificationTargets: verificationTargets() })) },
      upstate: { days: Array.from({ length: 3 }, (_, index) => ({ weatherRegime: "dry-high-pressure", date: `2026-10-0${index + 1}`, verificationTargets: verificationTargets() })) },
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

function verificationTargets() {
  return {
    wind: null,
    visibility: { fogProbability: 0.05, minimumVisibilityRangeMiles: null },
    sky: { categories: ["clear"] },
    thunderProbability: 0.02,
    hazards: [],
    eventTiming: { rain: [], thunder: [], fog: [], wind: [] },
    confidence: 0.8,
    officialAlerts: {
      status: "none",
      checkedAt: "2026-09-29T11:40:00Z",
      sourceUrl: "https://api.weather.gov/alerts/active?point=37.6456,-84.7722",
    },
  };
}
