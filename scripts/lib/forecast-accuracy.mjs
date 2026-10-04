const MPH_PER_MPS = 2.2369362921;
const MILES_PER_METER = 0.0006213711922;
const INCHES_PER_METER = 39.37007874;

export const REGION_CONFIG = {
  kentucky: {
    label: "Central Kentucky",
    historyKey: "centralKentucky",
    stations: ["KDVK", "KLEX"],
    alertPoint: "37.6456,-84.7722",
  },
  upstate: {
    label: "Upstate South Carolina",
    historyKey: "upstateSouthCarolina",
    stations: ["KCEU", "KLQK"],
    alertPoint: "34.6834,-82.8374",
  },
};

export const BIAS_RULES = {
  minimumSamples: 4,
  minimumAbsoluteMeanBiasF: 1.5,
  shrinkageFactor: 0.75,
  maximumAbsoluteAdjustmentF: 4,
};

export function round(value, digits = 1) {
  if (!Number.isFinite(value)) return null;
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

export function parseRange(value) {
  if (Number.isFinite(value)) return { min: Number(value), max: Number(value) };
  if (typeof value !== "string") return null;
  const normalized = value.replace(/[−–—]/g, "-");
  const range = normalized.match(/(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)/);
  if (range) {
    const left = Number(range[1]);
    const right = Number(range[2]);
    return { min: Math.min(left, right), max: Math.max(left, right) };
  }
  const single = normalized.match(/-?\d+(?:\.\d+)?/);
  if (!single) return null;
  const number = Number(single[0]);
  return { min: number, max: number };
}

export function parseProbability(value) {
  if (Number.isFinite(value)) {
    const probability = Number(value) > 1 ? Number(value) / 100 : Number(value);
    return Math.max(0, Math.min(1, probability));
  }
  if (typeof value !== "string") return null;
  const normalized = value.replace(/[−–—]/g, "-");
  const candidates = [];
  const consumed = [];
  for (const match of normalized.matchAll(/(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*%/g)) {
    candidates.push((Number(match[1]) + Number(match[2])) / 200);
    consumed.push([match.index, match.index + match[0].length]);
  }
  for (const match of normalized.matchAll(/(\d+(?:\.\d+)?)\s*%/g)) {
    const insideRange = consumed.some(([start, end]) => match.index >= start && match.index < end);
    if (!insideRange) candidates.push(Number(match[1]) / 100);
  }
  if (!candidates.length) return null;
  return Math.max(0, Math.min(1, Math.max(...candidates)));
}

export function inferWeatherRegime(day = {}) {
  if (day.weatherRegime) return day.weatherRegime;
  const text = `${day.forecast ?? ""} ${day.hazard ?? ""}`.toLowerCase();
  if (/wedge|cold[- ]air damming|northeast flow/.test(text)) return "cold-air-damming";
  if (/thunder|storm|convect/.test(text)) return "convective";
  if (/front|frontal/.test(text)) return "frontal";
  if (/fog|low cloud|stratus/.test(text)) return "fog-low-cloud";
  if (/rain|shower|drizzle/.test(text)) return "stratiform-precipitation";
  if (/sunny|clear|high pressure|dry/.test(text)) return "dry-high-pressure";
  return "mixed-or-unspecified";
}

function celsiusToFahrenheit(value) {
  return Number.isFinite(value) ? (value * 9) / 5 + 32 : null;
}

export function summarizeNwsObservations(features = []) {
  const usable = features
    .map((feature) => feature?.properties ?? feature)
    .filter(Boolean)
    .map((properties) => {
      const temperatureC = properties.temperature?.value;
      const windMps = properties.windSpeed?.value;
      const gustMps = properties.windGust?.value;
      const precipitationM = properties.precipitationLastHour?.value;
      const visibilityM = properties.visibility?.value;
      const description = `${properties.textDescription ?? ""} ${JSON.stringify(properties.presentWeather ?? [])}`;
      return {
        timestamp: properties.timestamp ?? null,
        temperatureF: round(celsiusToFahrenheit(temperatureC)),
        sustainedWindMph: round(Number.isFinite(windMps) ? windMps * MPH_PER_MPS : null),
        gustMph: round(Number.isFinite(gustMps) ? gustMps * MPH_PER_MPS : null),
        precipitationIn: round(Number.isFinite(precipitationM) ? precipitationM * INCHES_PER_METER : null, 3),
        visibilityMi: round(Number.isFinite(visibilityM) ? visibilityM * MILES_PER_METER : null),
        rain: (Number.isFinite(precipitationM) && precipitationM > 0.000001) || /rain|drizzle|shower/i.test(description),
        thunder: /thunder/i.test(description),
        fog: /fog|mist/i.test(description) || (Number.isFinite(visibilityM) && visibilityM < 1609.344),
      };
    });

  const temperatures = usable.map((item) => item.temperatureF).filter(Number.isFinite);
  const winds = usable.map((item) => item.sustainedWindMph).filter(Number.isFinite);
  const gusts = usable.map((item) => item.gustMph).filter(Number.isFinite);
  const visibilities = usable.map((item) => item.visibilityMi).filter(Number.isFinite);
  return {
    observationCount: usable.length,
    temperatureObservationCount: temperatures.length,
    firstObservedAt: usable.map((item) => item.timestamp).filter(Boolean).sort()[0] ?? null,
    lastObservedAt: usable.map((item) => item.timestamp).filter(Boolean).sort().at(-1) ?? null,
    highF: temperatures.length ? round(Math.max(...temperatures)) : null,
    lowF: temperatures.length ? round(Math.min(...temperatures)) : null,
    maximumSustainedWindMph: winds.length ? round(Math.max(...winds)) : null,
    maximumGustMph: gusts.length ? round(Math.max(...gusts)) : null,
    minimumVisibilityMi: visibilities.length ? round(Math.min(...visibilities)) : null,
    rainReports: usable.filter((item) => item.rain).length,
    thunderReports: usable.filter((item) => item.thunder).length,
    fogReports: usable.filter((item) => item.fog).length,
  };
}

function temperatureScore(range, observedF) {
  if (!range || !Number.isFinite(observedF)) return null;
  const midpoint = (range.min + range.max) / 2;
  const error = observedF - midpoint;
  return {
    forecastRangeF: [range.min, range.max],
    forecastMidpointF: round(midpoint),
    observedF: round(observedF),
    errorF: round(error),
    absoluteErrorF: round(Math.abs(error)),
    withinRange: observedF >= range.min && observedF <= range.max,
    rangeWidthF: round(range.max - range.min),
  };
}

export function evaluateRegionCheckpoint({
  forecast,
  regionKey,
  checkpointHours,
  stationObservations,
}) {
  const region = forecast.regions?.[regionKey];
  const dayIndex = checkpointHours / 24 - 1;
  const day = region?.days?.[dayIndex];
  if (!day) throw new Error(`${forecast.forecastId} has no ${checkpointHours}h day for ${regionKey}`);
  const highRange = parseRange(day.highF);
  const lowRange = parseRange(day.lowF);
  const precipitationProbability = parseProbability(day.precipitation);
  const stationScores = {};
  for (const [station, observed] of Object.entries(stationObservations)) {
    stationScores[station] = {
      observed,
      highTemperature: temperatureScore(highRange, observed.highF),
      lowTemperature: temperatureScore(lowRange, observed.lowF),
    };
  }
  const precipitationObserved = Object.values(stationObservations).some((item) => item.rainReports > 0);
  const precipitationBrierScore = Number.isFinite(precipitationProbability)
    ? (precipitationProbability - Number(precipitationObserved)) ** 2
    : null;
  return {
    forecastDay: {
      date: day.date,
      highF: day.highF,
      lowF: day.lowF,
      precipitation: day.precipitation,
    },
    weatherRegime: inferWeatherRegime(day),
    stationScores,
    precipitation: {
      forecastProbability: round(precipitationProbability, 3),
      observed: precipitationObserved,
      brierScore: round(precipitationBrierScore, 4),
    },
  };
}

export function scoreModelBenchmarks({ forecast, checkpointHours, regionKey, regionResult }) {
  const dayIndex = checkpointHours / 24 - 1;
  const day = forecast.regions?.[regionKey]?.days?.[dayIndex];
  const benchmarks = Array.isArray(forecast.modelBenchmarks) ? forecast.modelBenchmarks : [];
  const regionObservations = Object.values(regionResult.stationScores)
    .map((item) => item.observed)
    .filter(Boolean);
  const highs = regionObservations.map((item) => item.highF).filter(Number.isFinite);
  const lows = regionObservations.map((item) => item.lowF).filter(Number.isFinite);
  const observedHighF = highs.length ? highs.reduce((sum, value) => sum + value, 0) / highs.length : null;
  const observedLowF = lows.length ? lows.reduce((sum, value) => sum + value, 0) / lows.length : null;
  return benchmarks
    .filter((item) => item.region === regionKey && item.targetDate === day?.date)
    .map((item) => {
      const probability = parseProbability(item.precipitationProbability);
      const precipitationObserved = regionResult.precipitation.observed;
      return {
        model: item.model,
        run: item.run,
        region: regionKey,
        targetDate: item.targetDate,
        leadTimeHours: item.leadTimeHours ?? checkpointHours,
        weatherRegime: item.weatherRegime ?? regionResult.weatherRegime,
        highErrorF: Number.isFinite(item.highF) && Number.isFinite(observedHighF) ? round(observedHighF - item.highF) : null,
        lowErrorF: Number.isFinite(item.lowF) && Number.isFinite(observedLowF) ? round(observedLowF - item.lowF) : null,
        precipitationBrierScore: Number.isFinite(probability)
          ? round((probability - Number(precipitationObserved)) ** 2, 4)
          : null,
      };
    });
}

export function statistics(values) {
  const usable = values.filter(Number.isFinite);
  if (!usable.length) return { sampleCount: 0, mean: null, meanAbsolute: null, rmse: null };
  const mean = usable.reduce((sum, value) => sum + value, 0) / usable.length;
  const meanAbsolute = usable.reduce((sum, value) => sum + Math.abs(value), 0) / usable.length;
  const rmse = Math.sqrt(usable.reduce((sum, value) => sum + value ** 2, 0) / usable.length);
  return {
    sampleCount: usable.length,
    mean: round(mean),
    meanAbsolute: round(meanAbsolute),
    rmse: round(rmse),
  };
}

export function suggestedBiasAdjustment(meanErrorF, sampleCount, rules = BIAS_RULES) {
  const qualified = sampleCount >= rules.minimumSamples && Math.abs(meanErrorF) >= rules.minimumAbsoluteMeanBiasF;
  if (!qualified) return { status: "monitoring", adjustmentF: 0 };
  const raw = meanErrorF * rules.shrinkageFactor;
  const bounded = Math.max(-rules.maximumAbsoluteAdjustmentF, Math.min(rules.maximumAbsoluteAdjustmentF, raw));
  return { status: "recommended", adjustmentF: Math.round(bounded * 2) / 2 };
}

export function validateForecastFeedback(forecast, accuracySummary, { allowLegacy = false } = {}) {
  const errors = [];
  const issuedAt = Date.parse(forecast.issuedAt);
  const validThrough = Date.parse(forecast.validThrough);
  if (!Number.isFinite(issuedAt) || !Number.isFinite(validThrough) || validThrough - issuedAt !== 72 * 60 * 60 * 1000) {
    errors.push("validThrough must be exactly 72 hours after issuedAt");
  }
  for (const regionKey of Object.keys(REGION_CONFIG)) {
    const days = forecast.regions?.[regionKey]?.days;
    if (!Array.isArray(days) || days.length !== 3) errors.push(`${regionKey} must contain exactly three forecast days`);
    for (const [index, day] of (days ?? []).entries()) {
      if (!day.weatherRegime && !allowLegacy) errors.push(`${regionKey} day ${index + 1} is missing weatherRegime`);
    }
  }
  const checkpoints = forecast.verification?.checkpoints;
  if (!Array.isArray(checkpoints) || checkpoints.map((item) => item.hours).join(",") !== "24,48,72") {
    errors.push("verification checkpoints must be 24, 48 and 72 hours");
  }
  if (!allowLegacy) {
    if (!Array.isArray(forecast.modelBenchmarks) || !forecast.modelBenchmarks.length) {
      errors.push("modelBenchmarks are required for individual-model scoring");
    } else {
      for (const [index, benchmark] of forecast.modelBenchmarks.entries()) {
        for (const field of ["model", "run", "region", "targetDate", "leadTimeHours"]) {
          if (benchmark[field] == null || benchmark[field] === "") errors.push(`modelBenchmarks[${index}] is missing ${field}`);
        }
        if (!["kentucky", "upstate"].includes(benchmark.region)) errors.push(`modelBenchmarks[${index}] has an invalid region`);
        if (![benchmark.highF, benchmark.lowF, benchmark.precipitationProbability].some(Number.isFinite)) {
          errors.push(`modelBenchmarks[${index}] must contain a numeric highF, lowF or precipitationProbability`);
        }
      }
      const benchmarkedModels = new Set(forecast.modelBenchmarks.map((item) => item.model));
      const excludedModels = new Set((forecast.modelBenchmarkExclusions ?? []).map((item) => item.model));
      for (const model of forecast.modelStatus ?? []) {
        if (!benchmarkedModels.has(model.model) && !excludedModels.has(model.model)) {
          errors.push(`${model.model} needs a model benchmark or a documented modelBenchmarkExclusion`);
        }
      }
    }
    const feedback = forecast.accuracyFeedback;
    if (!feedback || feedback.source !== "forecast-accuracy.json") {
      errors.push("accuracyFeedback.source must be forecast-accuracy.json");
    }
    if (!Array.isArray(feedback?.consultedForecastIds) || !feedback.consultedForecastIds.length) {
      errors.push("accuracyFeedback.consultedForecastIds must identify the verification history used");
    }
    if (!Array.isArray(feedback?.appliedAdjustments) || !feedback.appliedAdjustments.length) {
      errors.push("accuracyFeedback.appliedAdjustments must document adjustments or no-adjustment decisions");
    } else {
      for (const regionKey of Object.keys(REGION_CONFIG)) {
        if (!feedback.appliedAdjustments.some((item) => item.region === regionKey)) {
          errors.push(`accuracyFeedback.appliedAdjustments must document the ${regionKey} decision`);
        }
      }
    }
    if (accuracySummary?.generatedAt && feedback?.summaryGeneratedAt !== accuracySummary.generatedAt) {
      errors.push("accuracyFeedback.summaryGeneratedAt must match forecast-accuracy.json");
    }
    for (const forecastId of accuracySummary?.coverage?.verifiedForecastIds ?? []) {
      if (!feedback?.consultedForecastIds?.includes(forecastId)) {
        errors.push(`accuracyFeedback.consultedForecastIds must include verified forecast ${forecastId}`);
      }
    }
  }
  return errors;
}
