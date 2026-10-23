import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

test("builds a category-balanced all-parts accuracy percentage", () => {
  const directory = mkdtempSync(join(tmpdir(), "forecast-accuracy-summary-"));
  const historyPath = join(directory, "history.json");
  const currentPath = join(directory, "current.json");
  const ledgerPath = join(directory, "ledger.json");
  const outputPath = join(directory, "summary.json");
  const forecast = {
    forecastId: "historical",
    regions: { kentucky: { days: [{ date: "2026-10-01" }] } },
  };
  writeFileSync(historyPath, JSON.stringify({ issues: [{ forecastId: "historical", originalForecast: forecast }] }));
  writeFileSync(currentPath, JSON.stringify({ forecastId: "current" }));
  writeFileSync(ledgerPath, JSON.stringify({
    generatedAt: "2026-10-13T12:00:00Z",
    forecasts: {
      historical: {
        checkpoints: {
          "24h": {
            status: "verified",
            checkpointHours: 24,
            regions: {
              kentucky: {
                weatherRegime: "dry-high-pressure",
                stationScores: {
                  KDVK: {
                    highTemperature: { errorF: 1, absoluteErrorF: 1, withinRange: true, rangeWidthF: 4 },
                    lowTemperature: { errorF: -4, absoluteErrorF: 4, withinRange: false, rangeWidthF: 4 },
                  },
                },
                precipitation: { forecastProbability: 0.5, observed: false, brierScore: 0.25 },
                expanded: {
                  scores: {
                    wind: { score: 0.8 },
                    visibilityFog: { score: 0.7 },
                    cloudCover: { score: 0.6 },
                    thunder: { score: 0.5 },
                    hazards: { score: 0.4 },
                    eventTiming: { score: 0.3 },
                    weatherRegimeNarrative: { score: 0.2 },
                    officialAlertHandling: { score: 0.1 },
                    confidenceCalibration: { score: 0.9 },
                  },
                },
              },
            },
            modelScores: [
              {
                model: "HRRR",
                region: "kentucky",
                station: "KDVK",
                leadTimeHours: 24,
                weatherRegime: "dry-high-pressure",
                highErrorF: 1,
                lowErrorF: -2,
                precipitationBrierScore: 0.04,
              },
              {
                model: "HRRR",
                region: "kentucky",
                station: null,
                leadTimeHours: 48,
                weatherRegime: "dry-high-pressure",
                highErrorF: 3,
                lowErrorF: -4,
                precipitationBrierScore: 0.16,
              },
            ],
          },
        },
      },
    },
  }));

  execFileSync(process.execPath, [
    "scripts/build-accuracy-summary.mjs",
    historyPath,
    currentPath,
    ledgerPath,
    outputPath,
  ], { cwd: new URL("../..", import.meta.url), stdio: "pipe" });
  const summary = JSON.parse(readFileSync(outputPath, "utf8"));
  assert.equal(summary.allCategoryAccuracy.categoryCoverage, "11/11");
  assert.equal(summary.allCategoryAccuracy.overallAccuracyPercent, 52.3);
  assert.equal(summary.allCategoryAccuracy.byCategory.temperature.accuracyPercent, 50);
  assert.equal(summary.allCategoryAccuracy.byCategory.precipitation.accuracyPercent, 75);
  assert.equal(summary.schemaVersion, 2);
  assert.equal(summary.forecastDecisionSupport.temperatureRanges.byLeadTimeHours["24"].high.status, "insufficient-samples");
  assert.equal(summary.forecastDecisionSupport.intermediateUpdates.defaults.timingShiftHours, 3);
  assert.equal(summary.models.byModel.HRRR.byStation.KDVK.highTemperatureMeanAbsoluteErrorF, 1);
  assert.equal(summary.models.byModel.HRRR.byStation["regional-mean"].lowTemperatureMeanAbsoluteErrorF, 4);
  assert.equal(summary.models.byModel.HRRR.byLeadTimeHours["48"].sampleCount, 1);
  assert.equal(summary.models.byStation.KDVK.precipitationBrierScore, 0.04);
});
