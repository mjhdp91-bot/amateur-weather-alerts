import test from "node:test";
import assert from "node:assert/strict";
import {
  evaluateRegionCheckpoint,
  intermediateUpdateReasons,
  parseProbability,
  parseRange,
  recommendedTemperatureRangeWidth,
  recommendModelWeights,
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
            hazardProbabilities: { fog: 0.1, wind: 0.1, thunder: 0.8, "heavy-rain": 0.2, heat: 0.05, freeze: 0.01 },
            hazardThresholds: { fogVisibilityMiles: 1, windSustainedMph: 25, windGustMph: 35, heavyRainHourlyInches: 0.3, heatHighF: 95, freezeLowF: 32 },
            eventTiming: { rain: ["afternoon"], thunder: ["afternoon"], fog: [], wind: [] },
            eventWindows: [{ event: "thunder", start: "2026-10-13T16:00:00Z", end: "2026-10-13T21:00:00Z", probability: 0.8, toleranceHours: 3 }],
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
        eventTimestamps: { rain: ["2026-10-13T18:00:00Z"], thunder: ["2026-10-13T18:00:00Z"], fog: [], wind: [] },
      },
    },
  });
  assert.equal(result.expanded.scores.wind.score, 1);
  assert.equal(result.expanded.scores.cloudCover.score, 1);
  assert.ok(result.expanded.scores.hazards.score > 0.98);
  assert.equal(result.expanded.scores.eventTiming.score, 0.96);
  assert.equal(result.expanded.scores.eventTiming.method, "timestamped-windows");
  assert.equal(result.expanded.scores.officialAlertHandling.score, 1);
  assert.ok(result.expanded.scores.confidenceCalibration.score > 0.9);
});

test("bias corrections require repeated evidence and remain bounded", () => {
  assert.deepEqual(suggestedBiasAdjustment(3, 3), { status: "monitoring", adjustmentF: 0 });
  assert.deepEqual(suggestedBiasAdjustment(3, 5), { status: "recommended", adjustmentF: 2.5 });
  assert.deepEqual(suggestedBiasAdjustment(-10, 10), { status: "recommended", adjustmentF: -4 });
});

test("turns historical error, model skill and material changes into forecast decisions", () => {
  const ranges = recommendedTemperatureRangeWidth(Array.from({ length: 10 }, (_, index) => ({ errorF: index < 8 ? 3 : 5 })));
  assert.equal(ranges.recommendedWidthF, 6);
  const weights = recommendModelWeights([
    ...Array.from({ length: 3 }, () => ({ model: "HRRR", highErrorF: 1, lowErrorF: 2, precipitationBrierScore: 0.1 })),
    ...Array.from({ length: 3 }, () => ({ model: "GFS", highErrorF: 4, lowErrorF: 4, precipitationBrierScore: 0.3 })),
  ]);
  assert.equal(weights.status, "recommended");
  assert.ok(weights.weights.find((item) => item.model === "HRRR").weight > weights.weights.find((item) => item.model === "GFS").weight);
  assert.deepEqual(intermediateUpdateReasons({ temperatureDeviationF: 4, newOfficialAlert: true }), ["temperature-deviation", "new-official-alert"]);
});

test("strict validation requires model, regime and feedback evidence", () => {
  const issuedAt = "2026-09-29T07:40:00-04:00";
  const forecast = {
    forecastId: "test",
    issuedAt,
    validThrough: "2026-10-02T07:40:00-04:00",
    regions: {
      kentucky: { days: Array.from({ length: 3 }, (_, index) => ({ highF: "70–74", lowF: "50–54", weatherRegime: "dry-high-pressure", date: `2026-10-0${index + 1}`, verificationTargets: verificationTargets("kentucky") })) },
      upstate: { days: Array.from({ length: 3 }, (_, index) => ({ highF: "70–74", lowF: "50–54", weatherRegime: "dry-high-pressure", date: `2026-10-0${index + 1}`, verificationTargets: verificationTargets("upstate") })) },
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
      stationBiasCorrections: [],
      temperatureRangeDecisions: [
        ...[24, 48, 72].map((leadTimeHours) => ({ region: "kentucky", leadTimeHours, decision: "applied", highWidthF: 4, lowWidthF: 4, reason: "Historical width retained." })),
        ...[24, 48, 72].map((leadTimeHours) => ({ region: "upstate", leadTimeHours, decision: "applied", highWidthF: 4, lowWidthF: 4, reason: "Historical width retained." })),
      ],
      modelWeighting: [
        ...[24, 48, 72].map((leadTimeHours) => ({ region: "kentucky", leadTimeHours, decision: "applied", weatherRegime: "dry-high-pressure", weights: [{ model: "HRRR", weight: 1 }], reason: "Only benchmarked model in this fixture." })),
        ...[24, 48, 72].map((leadTimeHours) => ({ region: "upstate", leadTimeHours, decision: "applied", weatherRegime: "dry-high-pressure", weights: [{ model: "HRRR", weight: 1 }], reason: "Only benchmarked model in this fixture." })),
      ],
      learningDecisions: [
        ...["kentucky", "upstate"].flatMap((region) => ["fog", "wind", "hazards", "timing"].map((category) => ({ region, category, decision: "retain", reason: "Fixture decision." }))),
      ],
    },
    intermediateUpdatePolicy: {
      evaluationCadenceHours: 6,
      temperatureDeviationF: 4,
      precipitationProbabilityChange: 0.25,
      timingShiftHours: 3,
      hazardProbabilityChange: 0.25,
      newOfficialAlert: true,
    },
  };
  assert.deepEqual(validateForecastFeedback(forecast, { generatedAt: "2026-09-29T12:00:00Z" }), []);
  const withoutHazardProbabilities = structuredClone(forecast);
  delete withoutHazardProbabilities.regions.kentucky.days[0].verificationTargets.hazardProbabilities;
  assert.ok(validateForecastFeedback(withoutHazardProbabilities, { generatedAt: "2026-09-29T12:00:00Z" }).some((error) => error.includes("hazardProbabilities")));
  delete forecast.accuracyFeedback;
  assert.ok(validateForecastFeedback(forecast, {}).some((error) => error.includes("accuracyFeedback")));
});

function verificationTargets(region) {
  return {
    wind: null,
    visibility: { fogProbability: 0.05, minimumVisibilityRangeMiles: null },
    sky: { categories: ["clear"] },
    thunderProbability: 0.02,
    hazards: [],
    hazardProbabilities: { fog: 0.05, wind: 0.05, thunder: 0.02, "heavy-rain": 0.02, heat: 0.02, freeze: 0.02 },
    hazardThresholds: { fogVisibilityMiles: 1, windSustainedMph: 25, windGustMph: 35, heavyRainHourlyInches: 0.3, heatHighF: 95, freezeLowF: 32 },
    eventTiming: { rain: [], thunder: [], fog: [], wind: [] },
    eventWindows: [],
    confidence: 0.8,
    officialAlerts: {
      status: "none",
      checkedAt: "2026-09-29T11:40:00Z",
      sourceUrl: `https://api.weather.gov/alerts/active?point=${region === "kentucky" ? "37.6456,-84.7722" : "34.6834,-82.8374"}`,
    },
  };
}
