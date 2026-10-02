import { readFileSync, writeFileSync } from "node:fs";
import {
  BIAS_RULES,
  REGION_CONFIG,
  evaluateRegionCheckpoint,
  round,
  statistics,
  suggestedBiasAdjustment,
} from "./lib/forecast-accuracy.mjs";

const [historyPath = "forecast-history.json", currentPath = "forecast.json", ledgerPath = "forecast-verifications.json", outputPath = "forecast-accuracy.json"] = process.argv.slice(2);

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new Error(`Unable to read valid JSON from ${path}: ${error.message}`);
  }
}

function groupBy(items, keyFunction) {
  const groups = new Map();
  for (const item of items) {
    const key = keyFunction(item);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }
  return groups;
}

function temperatureAggregate(samples) {
  const errors = samples.map((item) => item.errorF);
  const stats = statistics(errors);
  const usable = samples.filter((item) => Number.isFinite(item.errorF));
  return {
    sampleCount: stats.sampleCount,
    meanErrorF: stats.mean,
    meanAbsoluteErrorF: stats.meanAbsolute,
    rmseF: stats.rmse,
    rangeCoveragePercent: usable.length
      ? round((usable.filter((item) => item.withinRange).length / usable.length) * 100)
      : null,
    averageRangeWidthF: usable.length
      ? round(usable.reduce((sum, item) => sum + item.rangeWidthF, 0) / usable.length)
      : null,
  };
}

function keyedAggregates(samples, keyFunction) {
  return Object.fromEntries(
    [...groupBy(samples, keyFunction)].sort(([left], [right]) => left.localeCompare(right)).map(([key, values]) => [key, temperatureAggregate(values)]),
  );
}

function normalizeCheckpoint(forecast, checkpointKey, checkpoint) {
  const checkpointHours = Number(checkpointKey.replace("h", ""));
  if (checkpoint.regions) return checkpoint;
  const regions = {};
  for (const [regionKey, config] of Object.entries(REGION_CONFIG)) {
    const legacy = checkpoint[config.historyKey];
    if (!legacy?.stations) continue;
    regions[regionKey] = evaluateRegionCheckpoint({
      forecast,
      regionKey,
      checkpointHours,
      stationObservations: legacy.stations,
    });
  }
  return { ...checkpoint, checkpointHours, regions, modelScores: checkpoint.modelScores ?? [] };
}

function precipitationSummary(samples) {
  const usable = samples.filter((item) => Number.isFinite(item.forecastProbability));
  const bins = [
    { label: "0–20%", min: 0, max: 0.2 },
    { label: "21–40%", min: 0.2, max: 0.4 },
    { label: "41–60%", min: 0.4, max: 0.6 },
    { label: "61–80%", min: 0.6, max: 0.8 },
    { label: "81–100%", min: 0.8, max: 1.000001 },
  ];
  return {
    sampleCount: usable.length,
    brierScore: usable.length ? round(usable.reduce((sum, item) => sum + item.brierScore, 0) / usable.length, 4) : null,
    reliability: bins.map((bin, index) => {
      const members = usable.filter((item) => item.forecastProbability >= bin.min && (index === 0 ? item.forecastProbability <= bin.max : item.forecastProbability > bin.min && item.forecastProbability <= bin.max));
      return {
        bin: bin.label,
        sampleCount: members.length,
        meanForecastProbability: members.length ? round(members.reduce((sum, item) => sum + item.forecastProbability, 0) / members.length, 3) : null,
        observedFrequency: members.length ? round(members.filter((item) => item.observed).length / members.length, 3) : null,
      };
    }),
  };
}

function modelSummary(samples) {
  const byModel = {};
  for (const [model, values] of groupBy(samples, (item) => item.model)) {
    const high = statistics(values.map((item) => item.highErrorF));
    const low = statistics(values.map((item) => item.lowErrorF));
    const brierValues = values.map((item) => item.precipitationBrierScore).filter(Number.isFinite);
    byModel[model] = {
      sampleCount: values.length,
      highTemperatureMeanAbsoluteErrorF: high.meanAbsolute,
      lowTemperatureMeanAbsoluteErrorF: low.meanAbsolute,
      precipitationBrierScore: brierValues.length ? round(brierValues.reduce((sum, value) => sum + value, 0) / brierValues.length, 4) : null,
      byWeatherRegime: Object.fromEntries(
        [...groupBy(values, (item) => item.weatherRegime)].map(([regime, regimeValues]) => [
          regime,
          {
            sampleCount: regimeValues.length,
            highTemperatureMeanAbsoluteErrorF: statistics(regimeValues.map((item) => item.highErrorF)).meanAbsolute,
            lowTemperatureMeanAbsoluteErrorF: statistics(regimeValues.map((item) => item.lowErrorF)).meanAbsolute,
          },
        ]),
      ),
    };
  }
  return {
    sampleCount: samples.length,
    availability: samples.length
      ? "Model benchmarks are being scored against regional official-observation means."
      : "No issued forecast yet contains modelBenchmarks; model scoring begins with the first forecast that passes the new validator.",
    byModel,
  };
}

const history = readJson(historyPath);
const current = readJson(currentPath);
const ledger = readJson(ledgerPath);
const forecasts = new Map(history.issues.map((issue) => [issue.forecastId, issue.originalForecast]));
forecasts.set(current.forecastId, current);

const temperatureSamples = [];
const precipitationSamples = [];
const modelSamples = [];
const verifiedForecastIds = new Set();
const checkpointStatuses = [];

for (const [forecastId, record] of Object.entries(ledger.forecasts ?? {})) {
  const forecast = forecasts.get(forecastId);
  if (!forecast) continue;
  for (const [checkpointKey, rawCheckpoint] of Object.entries(record.checkpoints ?? {})) {
    checkpointStatuses.push(rawCheckpoint.status ?? "pending");
    if (rawCheckpoint.status !== "verified") continue;
    verifiedForecastIds.add(forecastId);
    const checkpoint = normalizeCheckpoint(forecast, checkpointKey, rawCheckpoint);
    const leadTimeHours = checkpoint.checkpointHours ?? Number(checkpointKey.replace("h", ""));
    for (const [regionKey, region] of Object.entries(checkpoint.regions ?? {})) {
      for (const [station, stationScore] of Object.entries(region.stationScores ?? {})) {
        for (const [metric, score] of Object.entries({ high: stationScore.highTemperature, low: stationScore.lowTemperature })) {
          if (!score) continue;
          temperatureSamples.push({
            forecastId,
            region: regionKey,
            station,
            metric,
            leadTimeHours,
            weatherRegime: region.weatherRegime,
            errorF: score.errorF,
            absoluteErrorF: score.absoluteErrorF,
            withinRange: score.withinRange,
            rangeWidthF: score.rangeWidthF,
          });
        }
      }
      precipitationSamples.push({
        forecastId,
        region: regionKey,
        leadTimeHours,
        weatherRegime: region.weatherRegime,
        ...region.precipitation,
      });
    }
    modelSamples.push(...(checkpoint.modelScores ?? []));
  }
}

const biasCandidates = [];
for (const [key, samples] of groupBy(temperatureSamples, (item) => `${item.region}|${item.station}|${item.metric}`)) {
  const [region, station, metric] = key.split("|");
  const stats = statistics(samples.map((item) => item.errorF));
  const recommendation = suggestedBiasAdjustment(stats.mean ?? 0, stats.sampleCount);
  biasCandidates.push({
    region,
    station,
    metric,
    sampleCount: stats.sampleCount,
    meanErrorF: stats.mean,
    meanAbsoluteErrorF: stats.meanAbsolute,
    status: recommendation.status,
    suggestedAdjustmentF: recommendation.adjustmentF,
  });
}

const summary = {
  schemaVersion: 1,
  generatedAt: ledger.generatedAt ?? new Date().toISOString(),
  source: "forecast-verifications.json",
  methodology: {
    temperatureError: "Observed official-station temperature minus the midpoint of the issued range.",
    precipitation: "Brier score for occurrence at either official station in the region; lower is better.",
    rangeCalibration: "Coverage is the percentage of official-station observations inside the issued range.",
    models: "Per-model scores require modelBenchmarks captured at issuance; models are never reconstructed after the fact.",
    weatherRegime: "Explicit day-level weatherRegime is required for new issues; legacy issues use documented text inference.",
    upstateTempestExcluded: true,
  },
  coverage: {
    forecastCount: Object.keys(ledger.forecasts ?? {}).length,
    checkpointCount: checkpointStatuses.length,
    verifiedCheckpoints: checkpointStatuses.filter((status) => status === "verified").length,
    pendingCheckpoints: checkpointStatuses.filter((status) => status === "pending").length,
    deferredCheckpoints: checkpointStatuses.filter((status) => status === "deferred").length,
    verifiedForecastIds: [...verifiedForecastIds].sort(),
  },
  temperature: {
    overall: temperatureAggregate(temperatureSamples),
    byRegion: keyedAggregates(temperatureSamples, (item) => item.region),
    byStation: keyedAggregates(temperatureSamples, (item) => item.station),
    byLeadTimeHours: keyedAggregates(temperatureSamples, (item) => String(item.leadTimeHours)),
    byWeatherRegime: keyedAggregates(temperatureSamples, (item) => item.weatherRegime),
  },
  precipitation: precipitationSummary(precipitationSamples),
  models: modelSummary(modelSamples),
  biasCorrection: {
    rules: BIAS_RULES,
    signConvention: "Positive means observations were warmer than the issued midpoint and future guidance may be adjusted upward.",
    candidates: biasCandidates.sort((left, right) => left.region.localeCompare(right.region) || left.station.localeCompare(right.station) || left.metric.localeCompare(right.metric)),
  },
  futureForecastRequirement: {
    summaryMustBeConsulted: true,
    requiredFields: ["weatherRegime on every day", "modelBenchmarks", "accuracyFeedback"],
    noAdjustmentDecisionMustBeDocumented: true,
  },
};

writeFileSync(outputPath, `${JSON.stringify(summary, null, 2)}\n`, "utf8");
console.log(`Wrote ${outputPath} from ${summary.coverage.verifiedCheckpoints} verified checkpoints.`);
