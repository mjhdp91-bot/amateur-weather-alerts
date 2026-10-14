import { readFileSync, writeFileSync } from "node:fs";
import {
  BIAS_RULES,
  COMPOSITE_CATEGORIES,
  INTERMEDIATE_UPDATE_DEFAULTS,
  MODEL_WEIGHT_RULES,
  RANGE_CALIBRATION_RULES,
  REGION_CONFIG,
  evaluateRegionCheckpoint,
  round,
  recommendedTemperatureRangeWidth,
  recommendModelWeights,
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
    recommendedWeightsByWeatherRegime: Object.fromEntries(
      [...groupBy(samples, (item) => item.weatherRegime)].sort(([left], [right]) => left.localeCompare(right)).map(([regime, regimeSamples]) => [
        regime,
        recommendModelWeights(regimeSamples),
      ]),
    ),
    recommendedWeightsByRegionAndWeatherRegime: Object.fromEntries(
      Object.keys(REGION_CONFIG).map((region) => [
        region,
        Object.fromEntries(
          [...groupBy(samples.filter((item) => item.region === region), (item) => item.weatherRegime)]
            .sort(([left], [right]) => left.localeCompare(right))
            .map(([regime, regimeSamples]) => [regime, recommendModelWeights(regimeSamples)]),
        ),
      ]),
    ),
    weightingRules: MODEL_WEIGHT_RULES,
  };
}

function scoreAggregate(samples) {
  const usable = samples.filter((item) => Number.isFinite(item.score));
  return {
    sampleCount: usable.length,
    meanScore: usable.length ? round(usable.reduce((sum, item) => sum + item.score, 0) / usable.length, 4) : null,
    accuracyPercent: usable.length ? round((usable.reduce((sum, item) => sum + item.score, 0) / usable.length) * 100) : null,
  };
}

function balancedComposite(samples) {
  const byCategory = Object.fromEntries(
    COMPOSITE_CATEGORIES.map((category) => [category, scoreAggregate(samples.filter((item) => item.category === category))]),
  );
  const available = Object.entries(byCategory).filter(([, aggregate]) => aggregate.sampleCount > 0);
  const overall = available.length
    ? available.reduce((sum, [, aggregate]) => sum + aggregate.accuracyPercent, 0) / available.length
    : null;
  return {
    overallAccuracyPercent: round(overall),
    categoryCoverage: `${available.length}/${COMPOSITE_CATEGORIES.length}`,
    availableCategoryCount: available.length,
    totalCategoryCount: COMPOSITE_CATEGORIES.length,
    weighting: "Each scored category receives equal weight; missing categories are excluded and disclosed through categoryCoverage.",
    byCategory,
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
const allCategorySamples = [];
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
          allCategorySamples.push({
            category: "temperature",
            forecastId,
            region: regionKey,
            leadTimeHours,
            score: Number(score.withinRange),
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
      if (Number.isFinite(region.precipitation?.brierScore)) {
        allCategorySamples.push({
          category: "precipitation",
          forecastId,
          region: regionKey,
          leadTimeHours,
          score: 1 - region.precipitation.brierScore,
        });
      }
      for (const [category, result] of Object.entries(region.expanded?.scores ?? {})) {
        if (!Number.isFinite(result?.score)) continue;
        allCategorySamples.push({ category, forecastId, region: regionKey, leadTimeHours, score: result.score });
      }
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

const temperatureRangeRecommendations = Object.fromEntries(
  [24, 48, 72].map((leadTimeHours) => {
    const samples = temperatureSamples.filter((item) => item.leadTimeHours === leadTimeHours);
    return [String(leadTimeHours), {
      all: recommendedTemperatureRangeWidth(samples),
      high: recommendedTemperatureRangeWidth(samples.filter((item) => item.metric === "high")),
      low: recommendedTemperatureRangeWidth(samples.filter((item) => item.metric === "low")),
    }];
  }),
);

const regionalTemperatureRangeRecommendations = Object.fromEntries(
  Object.keys(REGION_CONFIG).map((region) => [
    region,
    Object.fromEntries(
      [24, 48, 72].map((leadTimeHours) => {
        const samples = temperatureSamples.filter((item) => item.region === region && item.leadTimeHours === leadTimeHours);
        return [String(leadTimeHours), {
          high: recommendedTemperatureRangeWidth(samples.filter((item) => item.metric === "high")),
          low: recommendedTemperatureRangeWidth(samples.filter((item) => item.metric === "low")),
        }];
      }),
    ),
  ]),
);
const modelAccuracySummary = modelSummary(modelSamples);

const summary = {
  schemaVersion: 2,
  generatedAt: ledger.generatedAt ?? new Date().toISOString(),
  source: "forecast-verifications.json",
  methodology: {
    temperatureError: "Observed official-station temperature minus the midpoint of the issued range.",
    precipitation: "Brier score for occurrence at either official station in the region; lower is better.",
    rangeCalibration: "Coverage is the percentage of official-station observations inside the issued range.",
    models: "Per-model scores require modelBenchmarks captured at issuance; models are never reconstructed after the fact.",
    weatherRegime: "Explicit day-level weatherRegime is required for new issues; legacy issues use documented text inference.",
    compositeAccuracy: "A category-balanced percentage covering temperature, precipitation, wind, fog/visibility, cloud cover, thunder, hazards, event timing, weather-regime narrative, official-alert documentation and confidence calibration. Each available category receives equal weight; missing categories are disclosed rather than counted as failures.",
    componentRules: {
      temperature: "Binary credit for each official-station high and low inside the issued range.",
      precipitation: "Probability accuracy is 1 minus the Brier score for regional occurrence.",
      wind: "Full credit inside the issued sustained/gust range, tapering to zero over 10 mph for sustained wind and 15 mph for gusts.",
      visibilityFog: "Brier-style probability accuracy for fog or sub-mile visibility.",
      cloudCover: "Ordinal agreement across clear, mostly clear, partly cloudy, mostly cloudy and overcast categories.",
      thunder: "Brier-style probability accuracy for an official-station thunder report.",
      hazards: "Mean Brier-style probability accuracy for observable fog, wind, thunder, heavy-rain, heat and freeze hazards against explicit measurable thresholds; legacy forecasts use Jaccard agreement.",
      eventTiming: "Brier-style accuracy for timestamped event windows with an issued tolerance; legacy forecasts use morning/afternoon/evening/night overlap.",
      weatherRegimeNarrative: "Deterministic outcome rule appropriate to the issued weatherRegime.",
      officialAlertHandling: "Checks that the issuance-time alert status is explicit and backed by a timestamped official source.",
      confidenceCalibration: "One minus squared error between issued confidence and realized component accuracy.",
    },
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
  allCategoryAccuracy: {
    ...balancedComposite(allCategorySamples),
    byRegion: Object.fromEntries(
      Object.keys(REGION_CONFIG).map((region) => [region, balancedComposite(allCategorySamples.filter((item) => item.region === region))]),
    ),
    byLeadTimeHours: Object.fromEntries(
      [24, 48, 72].map((hours) => [String(hours), balancedComposite(allCategorySamples.filter((item) => item.leadTimeHours === hours))]),
    ),
  },
  models: modelAccuracySummary,
  biasCorrection: {
    rules: BIAS_RULES,
    signConvention: "Positive means observations were warmer than the issued midpoint and future guidance may be adjusted upward.",
    candidates: biasCandidates.sort((left, right) => left.region.localeCompare(right.region) || left.station.localeCompare(right.station) || left.metric.localeCompare(right.metric)),
  },
  forecastDecisionSupport: {
    temperatureRanges: {
      rules: RANGE_CALIBRATION_RULES,
      byLeadTimeHours: temperatureRangeRecommendations,
      byRegionAndLeadTimeHours: regionalTemperatureRangeRecommendations,
      instruction: "Use at least the recommended empirical width unless current ensemble spread or a documented meteorological reason supports a wider range. A narrower range requires an explicit override reason.",
    },
    modelWeighting: {
      rules: MODEL_WEIGHT_RULES,
      recommendationsByWeatherRegime: modelAccuracySummary.recommendedWeightsByWeatherRegime,
      recommendationsByRegionAndWeatherRegime: modelAccuracySummary.recommendedWeightsByRegionAndWeatherRegime,
      instruction: "Use regime-specific weights when available. When samples are insufficient, document the operational blend and retain at least two independent model families.",
    },
    intermediateUpdates: {
      defaults: INTERMEDIATE_UPDATE_DEFAULTS,
      instruction: "Issue an intermediate candidate only when an observation or guidance change crosses a threshold; a new official alert always triggers review.",
    },
  },
  futureForecastRequirement: {
    summaryMustBeConsulted: true,
    requiredFields: ["weatherRegime on every day", "probabilistic verificationTargets on every day", "timestamped eventWindows", "modelBenchmarks", "accuracyFeedback with station/range/model/learning decisions", "intermediateUpdatePolicy"],
    noAdjustmentDecisionMustBeDocumented: true,
  },
};

writeFileSync(outputPath, `${JSON.stringify(summary, null, 2)}\n`, "utf8");
console.log(`Wrote ${outputPath} from ${summary.coverage.verifiedCheckpoints} verified checkpoints.`);
