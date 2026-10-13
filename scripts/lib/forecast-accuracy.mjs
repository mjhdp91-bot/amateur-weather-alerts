const MPH_PER_MPS = 2.2369362921;
const MPH_PER_KPH = 0.6213711922;
const MILES_PER_METER = 0.0006213711922;
const INCHES_PER_METER = 39.37007874;

export const SCORING_VERSION = 2;

export const COMPOSITE_CATEGORIES = [
  "temperature",
  "precipitation",
  "wind",
  "visibilityFog",
  "cloudCover",
  "thunder",
  "hazards",
  "eventTiming",
  "weatherRegimeNarrative",
  "officialAlertHandling",
  "confidenceCalibration",
];

const SKY_INDEX = {
  clear: 0,
  "mostly-clear": 1,
  "partly-cloudy": 2,
  "mostly-cloudy": 3,
  overcast: 4,
};

const PERIODS = ["morning", "afternoon", "evening", "night"];

const OBSERVABLE_HAZARDS = new Set(["fog", "wind", "thunder", "heavy-rain", "heat", "freeze"]);

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

function speedToMph(measurement) {
  const value = measurement?.value;
  if (!Number.isFinite(value)) return null;
  const unit = `${measurement?.unitCode ?? ""}`.toLowerCase();
  if (unit.includes("km_h") || unit.includes("km/h")) return value * MPH_PER_KPH;
  if (unit.includes("mi_h") || unit.includes("mph")) return value;
  return value * MPH_PER_MPS;
}

function precipitationToInches(measurement) {
  const value = measurement?.value;
  if (!Number.isFinite(value)) return null;
  const unit = `${measurement?.unitCode ?? ""}`.toLowerCase();
  if (unit.endsWith(":mm") || unit.includes("millimeter")) return value * 0.03937007874;
  if (unit.endsWith(":cm") || unit.includes("centimeter")) return value * 0.3937007874;
  if (unit.includes("[in_i]") || unit.endsWith(":in") || unit.includes("inch")) return value;
  return value * INCHES_PER_METER;
}

function observationPeriod(timestamp, timeZone = "America/New_York") {
  const date = new Date(timestamp);
  if (!Number.isFinite(date.getTime())) return null;
  const hour = Number(
    new Intl.DateTimeFormat("en-US", { timeZone, hour: "2-digit", hourCycle: "h23" })
      .formatToParts(date)
      .find((part) => part.type === "hour")?.value,
  );
  if (hour >= 6 && hour < 12) return "morning";
  if (hour >= 12 && hour < 17) return "afternoon";
  if (hour >= 17 && hour < 21) return "evening";
  return "night";
}

function classifySky(description = "") {
  const text = description.toLowerCase();
  if (/overcast|\bovc\b|cloudy/.test(text) && !/partly|mostly/.test(text)) return "overcast";
  if (/mostly cloudy|broken|\bbkn\b/.test(text)) return "mostly-cloudy";
  if (/partly cloudy|scattered|\bsct\b/.test(text)) return "partly-cloudy";
  if (/mostly clear|few clouds|\bfew\b/.test(text)) return "mostly-clear";
  if (/clear|sunny|\bclr\b|\bskc\b/.test(text)) return "clear";
  return null;
}

function uniquePeriods(items) {
  return PERIODS.filter((period) => items.includes(period));
}

export function summarizeNwsObservations(features = []) {
  const usable = features
    .map((feature) => feature?.properties ?? feature)
    .filter(Boolean)
    .map((properties) => {
      const temperatureC = properties.temperature?.value;
      const precipitationM = properties.precipitationLastHour?.value;
      const visibilityM = properties.visibility?.value;
      const description = `${properties.textDescription ?? ""} ${JSON.stringify(properties.presentWeather ?? [])}`;
      const sustainedWindMph = speedToMph(properties.windSpeed);
      const gustMph = speedToMph(properties.windGust);
      const period = observationPeriod(properties.timestamp);
      const rain = (Number.isFinite(precipitationM) && precipitationM > 0.000001) || /rain|drizzle|shower/i.test(description);
      const thunder = /thunder/i.test(description);
      const fog = /fog|mist/i.test(description) || (Number.isFinite(visibilityM) && visibilityM < 1609.344);
      return {
        timestamp: properties.timestamp ?? null,
        temperatureF: round(celsiusToFahrenheit(temperatureC)),
        sustainedWindMph: round(sustainedWindMph),
        gustMph: round(gustMph),
        precipitationIn: round(precipitationToInches(properties.precipitationLastHour), 3),
        visibilityMi: round(Number.isFinite(visibilityM) ? visibilityM * MILES_PER_METER : null),
        rain,
        thunder,
        fog,
        strongWind: (Number.isFinite(sustainedWindMph) && sustainedWindMph >= 25) || (Number.isFinite(gustMph) && gustMph >= 35),
        sky: classifySky(description),
        period,
      };
    });

  const temperatures = usable.map((item) => item.temperatureF).filter(Number.isFinite);
  const winds = usable.map((item) => item.sustainedWindMph).filter(Number.isFinite);
  const gusts = usable.map((item) => item.gustMph).filter(Number.isFinite);
  const hourlyPrecipitation = usable.map((item) => item.precipitationIn).filter(Number.isFinite);
  const visibilities = usable.map((item) => item.visibilityMi).filter(Number.isFinite);
  const skyConditionCounts = Object.fromEntries(
    Object.keys(SKY_INDEX).map((condition) => [condition, usable.filter((item) => item.sky === condition).length]),
  );
  const predominantSkyCondition = Object.entries(skyConditionCounts)
    .filter(([, count]) => count > 0)
    .sort((left, right) => right[1] - left[1] || SKY_INDEX[left[0]] - SKY_INDEX[right[0]])[0]?.[0] ?? null;
  return {
    observationCount: usable.length,
    temperatureObservationCount: temperatures.length,
    firstObservedAt: usable.map((item) => item.timestamp).filter(Boolean).sort()[0] ?? null,
    lastObservedAt: usable.map((item) => item.timestamp).filter(Boolean).sort().at(-1) ?? null,
    highF: temperatures.length ? round(Math.max(...temperatures)) : null,
    lowF: temperatures.length ? round(Math.min(...temperatures)) : null,
    maximumSustainedWindMph: winds.length ? round(Math.max(...winds)) : null,
    maximumGustMph: gusts.length ? round(Math.max(...gusts)) : null,
    maximumHourlyPrecipitationIn: hourlyPrecipitation.length ? round(Math.max(...hourlyPrecipitation), 3) : null,
    minimumVisibilityMi: visibilities.length ? round(Math.min(...visibilities)) : null,
    rainReports: usable.filter((item) => item.rain).length,
    thunderReports: usable.filter((item) => item.thunder).length,
    fogReports: usable.filter((item) => item.fog).length,
    skyConditionCounts,
    predominantSkyCondition,
    eventPeriods: {
      rain: uniquePeriods(usable.filter((item) => item.rain).map((item) => item.period).filter(Boolean)),
      thunder: uniquePeriods(usable.filter((item) => item.thunder).map((item) => item.period).filter(Boolean)),
      fog: uniquePeriods(usable.filter((item) => item.fog).map((item) => item.period).filter(Boolean)),
      wind: uniquePeriods(usable.filter((item) => item.strongWind).map((item) => item.period).filter(Boolean)),
    },
  };
}

function normalizeRange(value) {
  if (Array.isArray(value) && value.length === 2 && value.every(Number.isFinite)) {
    return { min: Math.min(...value), max: Math.max(...value) };
  }
  return parseRange(value);
}

function extractPeriods(text = "") {
  const normalized = text.toLowerCase();
  return PERIODS.filter((period) => new RegExp(`\\b${period}\\b`).test(normalized));
}

function inferWindTarget(text) {
  const normalized = text.replace(/[−–—]/g, "-");
  const gust = normalized.match(/gust(?:s|ing)?[^.]{0,45}?(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*mph/i)
    ?? normalized.match(/gust(?:s|ing)?[^.]{0,45}?(?:near|around|up to)?\s*(\d+(?:\.\d+)?)\s*mph/i);
  const sustained = normalized.match(/winds?[^.]{0,45}?(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*mph/i)
    ?? normalized.match(/winds?[^.]{0,45}?(?:near|around|up to)?\s*(\d+(?:\.\d+)?)\s*mph/i);
  const toRange = (match) => {
    if (!match) return null;
    const left = Number(match[1]);
    const right = Number(match[2] ?? match[1]);
    return [Math.min(left, right), Math.max(left, right)];
  };
  if (gust || sustained) return { sustainedRangeMph: toRange(sustained), gustRangeMph: toRange(gust) };
  if (/calm|nearly calm/.test(normalized)) return { sustainedRangeMph: [0, 6], gustRangeMph: null };
  if (/light winds?/.test(normalized)) return { sustainedRangeMph: [0, 12], gustRangeMph: null };
  if (/breezy/.test(normalized)) return { sustainedRangeMph: [10, 25], gustRangeMph: null };
  return null;
}

function inferSkyTargets(text) {
  const normalized = text.toLowerCase();
  const targets = [];
  if (/sunny|clear/.test(normalized)) targets.push("clear");
  if (/mostly sunny|mostly clear/.test(normalized)) targets.push("mostly-clear");
  if (/partly sunny|partly cloudy|variable clouds/.test(normalized)) targets.push("partly-cloudy");
  if (/mostly cloudy|considerable cloud/.test(normalized)) targets.push("mostly-cloudy");
  if (/overcast|cloudy/.test(normalized) && !targets.length) targets.push("overcast");
  return [...new Set(targets)];
}

function eventProbability(text, baseProbability, event) {
  const normalized = text.toLowerCase();
  if (event === "thunder") {
    if (!/thunder|storm/.test(normalized)) return 0.02;
    if (/widespread|likely|numerous/.test(normalized)) return Math.max(baseProbability ?? 0, 0.7);
    if (/scattered|a few/.test(normalized)) return Math.max(baseProbability ?? 0, 0.4);
    if (/isolated|possible/.test(normalized)) return Math.max(Math.min(baseProbability ?? 0.25, 0.35), 0.2);
    return Math.max(baseProbability ?? 0, 0.5);
  }
  if (event === "fog") {
    if (/fog[^.]{0,50}(not expected|not.*concern)|no (?:widespread )?fog/.test(normalized)) return 0.1;
    if (!/fog|mist|low visibility/.test(normalized)) return 0.05;
    if (/dense fog|advisory/.test(normalized)) return 0.85;
    if (/patchy|possible|may develop/.test(normalized)) return 0.25;
    return 0.65;
  }
  return baseProbability;
}

function inferHazards(text) {
  const normalized = text.toLowerCase();
  if (/no significant weather hazards expected/.test(normalized)) return [];
  const hazards = [];
  if (/fog|visibility/.test(normalized) && !/not expected|not.*concern/.test(normalized)) hazards.push("fog");
  if (/strong wind|gust|wind advisory|breezy/.test(normalized)) hazards.push("wind");
  if (/thunder|storm|severe/.test(normalized)) hazards.push("thunder");
  if (/heavy rain|downpour|flood|ponding/.test(normalized)) hazards.push("heavy-rain");
  if (/heat advisory|excessive heat|dangerous heat/.test(normalized)) hazards.push("heat");
  if (/freeze|freezing|frost|extreme cold/.test(normalized)) hazards.push("freeze");
  return [...new Set(hazards)].filter((hazard) => OBSERVABLE_HAZARDS.has(hazard));
}

function inferAlertTarget(day, forecast, regionKey) {
  const text = `${day.hazard ?? ""}`.toLowerCase();
  const regionName = REGION_CONFIG[regionKey]?.label.toLowerCase();
  const relevantNotes = (forecast.sourceNotes ?? []).filter((note) => `${note}`.toLowerCase().includes(regionName?.split(" ")[0] ?? regionKey));
  const hasAlertSource = (forecast.sources ?? []).some((source) => /\/alerts\//.test(source.url ?? ""));
  if (/no official nws|no official (?:alert|watch|warning)|no (?:nws )?alerts? were active/.test(text)) {
    return { status: "none", documented: relevantNotes.some((note) => /no official|no alert/.test(`${note}`.toLowerCase())) };
  }
  if (/official nws|advisory|watch|warning/.test(text)) return { status: "active", documented: hasAlertSource };
  return { status: "unknown", documented: false };
}

function inferConfidence(region = {}) {
  const text = `${region.confidence ?? ""}`.toLowerCase();
  if (/very high/.test(text)) return 0.95;
  if (/moderate-high|moderately high/.test(text)) return 0.75;
  if (/high/.test(text)) return 0.85;
  if (/moderate-low|moderately low/.test(text)) return 0.45;
  if (/moderate/.test(text)) return 0.65;
  if (/low/.test(text)) return 0.35;
  return null;
}

export function forecastVerificationTargets(forecast, regionKey, day) {
  if (day.verificationTargets) {
    const targets = structuredClone(day.verificationTargets);
    if (targets.officialAlerts) {
      targets.officialAlerts.documented = Boolean(
        ["none", "active"].includes(targets.officialAlerts.status)
        && targets.officialAlerts.checkedAt
        && targets.officialAlerts.sourceUrl,
      );
    }
    return targets;
  }
  const region = forecast.regions?.[regionKey] ?? {};
  const text = `${day.forecast ?? ""} ${day.hazard ?? ""}`;
  const precipitationProbability = parseProbability(day.precipitation);
  const timing = {};
  for (const event of ["rain", "thunder", "fog", "wind"]) {
    const sentences = text.split(/(?<=[.!?;])\s+/).filter((sentence) => {
      if (event === "rain") return /rain|shower|drizzle|precip/.test(sentence.toLowerCase());
      if (event === "thunder") return /thunder|storm/.test(sentence.toLowerCase());
      if (event === "fog") return /fog|mist|visibility/.test(sentence.toLowerCase());
      return /wind|gust|breez/.test(sentence.toLowerCase());
    });
    timing[event] = uniquePeriods(sentences.flatMap(extractPeriods));
  }
  return {
    inferredFromText: true,
    wind: inferWindTarget(text),
    visibility: {
      fogProbability: eventProbability(text, null, "fog"),
      minimumVisibilityRangeMiles: null,
    },
    sky: { categories: inferSkyTargets(text) },
    thunderProbability: eventProbability(text, precipitationProbability, "thunder"),
    hazards: inferHazards(day.hazard ?? text),
    eventTiming: timing,
    confidence: inferConfidence(region),
    officialAlerts: inferAlertTarget(day, forecast, regionKey),
  };
}

function boundedScore(value) {
  return Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : null;
}

function rangeAccuracy(observed, value, tolerance = 10) {
  const range = normalizeRange(value);
  if (!range || !Number.isFinite(observed)) return null;
  if (observed >= range.min && observed <= range.max) return 1;
  const distance = observed < range.min ? range.min - observed : observed - range.max;
  return boundedScore(1 - distance / tolerance);
}

function aggregateRegionObservations(stationObservations) {
  const observations = Object.values(stationObservations);
  const numbers = (key) => observations.map((item) => item[key]).filter(Number.isFinite);
  const maximum = (key) => {
    const values = numbers(key);
    return values.length ? Math.max(...values) : null;
  };
  const minimum = (key) => {
    const values = numbers(key);
    return values.length ? Math.min(...values) : null;
  };
  const skyCounts = Object.fromEntries(Object.keys(SKY_INDEX).map((condition) => [
    condition,
    observations.reduce((sum, item) => sum + (item.skyConditionCounts?.[condition] ?? 0), 0),
  ]));
  const predominantSkyCondition = Object.entries(skyCounts)
    .filter(([, count]) => count > 0)
    .sort((left, right) => right[1] - left[1] || SKY_INDEX[left[0]] - SKY_INDEX[right[0]])[0]?.[0] ?? null;
  const eventPeriods = Object.fromEntries(["rain", "thunder", "fog", "wind"].map((event) => [
    event,
    uniquePeriods(observations.flatMap((item) => item.eventPeriods?.[event] ?? [])),
  ]));
  return {
    maximumSustainedWindMph: maximum("maximumSustainedWindMph"),
    maximumGustMph: maximum("maximumGustMph"),
    maximumHourlyPrecipitationIn: maximum("maximumHourlyPrecipitationIn"),
    minimumVisibilityMi: minimum("minimumVisibilityMi"),
    highF: maximum("highF"),
    lowF: minimum("lowF"),
    rain: observations.some((item) => item.rainReports > 0),
    thunder: observations.some((item) => item.thunderReports > 0),
    fog: observations.some((item) => item.fogReports > 0) || minimum("minimumVisibilityMi") < 1,
    predominantSkyCondition,
    skyConditionCounts: skyCounts,
    eventPeriods,
  };
}

function skyAccuracy(targets, observed) {
  const usable = (targets ?? []).filter((target) => target in SKY_INDEX);
  if (!usable.length || !(observed in SKY_INDEX)) return null;
  const distance = Math.min(...usable.map((target) => Math.abs(SKY_INDEX[target] - SKY_INDEX[observed])));
  return boundedScore(1 - distance / 4);
}

function hazardAccuracy(expected, observed) {
  const forecastSet = new Set((expected ?? []).filter((item) => OBSERVABLE_HAZARDS.has(item)));
  const observedSet = new Set(observed);
  const union = new Set([...forecastSet, ...observedSet]);
  if (!union.size) return 1;
  const intersection = [...forecastSet].filter((item) => observedSet.has(item)).length;
  return intersection / union.size;
}

function timingScores(targets, observedPeriods) {
  const scores = [];
  for (const event of ["rain", "thunder", "fog", "wind"]) {
    const expected = (targets?.[event] ?? []).filter((period) => PERIODS.includes(period));
    if (!expected.length) continue;
    const observed = observedPeriods?.[event] ?? [];
    if (!observed.length) continue;
    scores.push({ event, expectedPeriods: expected, observedPeriods: observed, score: observed.some((period) => expected.includes(period)) ? 1 : 0 });
  }
  return scores;
}

function weatherRegimeAccuracy(regime, observed) {
  if (!regime || regime === "mixed-or-unspecified") return null;
  if (["dry-high-pressure", "warm-ridge", "post-frontal-high-pressure", "pre-frontal-warm-sector"].includes(regime)) {
    return observed.rain ? 0 : 1;
  }
  if (["convective", "frontal-convective"].includes(regime)) return observed.rain || observed.thunder ? 1 : 0;
  if (["stratiform-precipitation", "post-frontal-showers", "tropical-remnant-precipitation", "tropical-remnant-stratiform", "post-tropical-showers"].includes(regime)) {
    return observed.rain ? 1 : 0;
  }
  if (["fog-low-cloud", "cold-air-damming", "wedge-erosion"].includes(regime)) {
    return observed.fog || ["mostly-cloudy", "overcast"].includes(observed.predominantSkyCondition) ? 1 : 0;
  }
  if (["frontal", "post-tropical-frontal-passage"].includes(regime)) return observed.rain || observed.maximumSustainedWindMph >= 15 ? 1 : 0;
  return null;
}

export function evaluateExpandedComponents({ forecast, regionKey, day, stationObservations, temperatureScores, precipitation }) {
  const targets = forecastVerificationTargets(forecast, regionKey, day);
  const observed = aggregateRegionObservations(stationObservations);
  const windParts = [];
  if (targets.wind?.sustainedRangeMph) windParts.push(rangeAccuracy(observed.maximumSustainedWindMph, targets.wind.sustainedRangeMph, 10));
  if (targets.wind?.gustRangeMph) windParts.push(rangeAccuracy(observed.maximumGustMph, targets.wind.gustRangeMph, 15));
  const usableWind = windParts.filter(Number.isFinite);
  const windScore = usableWind.length ? usableWind.reduce((sum, value) => sum + value, 0) / usableWind.length : null;
  const fogProbability = targets.visibility?.fogProbability;
  const visibilityParts = [];
  if (Number.isFinite(fogProbability)) visibilityParts.push(1 - (fogProbability - Number(observed.fog)) ** 2);
  if (targets.visibility?.minimumVisibilityRangeMiles) {
    visibilityParts.push(rangeAccuracy(observed.minimumVisibilityMi, targets.visibility.minimumVisibilityRangeMiles, 2));
  }
  const usableVisibility = visibilityParts.filter(Number.isFinite);
  const fogScore = usableVisibility.length ? usableVisibility.reduce((sum, value) => sum + value, 0) / usableVisibility.length : null;
  const thunderProbability = targets.thunderProbability;
  const thunderScore = Number.isFinite(thunderProbability) ? 1 - (thunderProbability - Number(observed.thunder)) ** 2 : null;
  const observedHazards = [];
  if (observed.fog) observedHazards.push("fog");
  if (observed.maximumSustainedWindMph >= 25 || observed.maximumGustMph >= 35) observedHazards.push("wind");
  if (observed.thunder) observedHazards.push("thunder");
  if (observed.maximumHourlyPrecipitationIn >= 0.3) observedHazards.push("heavy-rain");
  if (observed.highF >= 95) observedHazards.push("heat");
  if (observed.lowF <= 32) observedHazards.push("freeze");
  const timing = timingScores(targets.eventTiming, observed.eventPeriods);
  const temperatureValues = Object.values(temperatureScores).flatMap((score) => [score.highTemperature, score.lowTemperature])
    .filter(Boolean)
    .map((score) => Number(score.withinRange));
  const componentOutcomes = [
    ...temperatureValues,
    Number.isFinite(precipitation?.brierScore) ? 1 - precipitation.brierScore : null,
    windScore,
    fogScore,
    skyAccuracy(targets.sky?.categories, observed.predominantSkyCondition),
    thunderScore,
    hazardAccuracy(targets.hazards, observedHazards),
    ...timing.map((item) => item.score),
    weatherRegimeAccuracy(inferWeatherRegime(day), observed),
  ].filter(Number.isFinite);
  const realizedAccuracy = componentOutcomes.length
    ? componentOutcomes.reduce((sum, value) => sum + value, 0) / componentOutcomes.length
    : null;
  const confidence = targets.confidence;
  const confidenceScore = Number.isFinite(confidence) && Number.isFinite(realizedAccuracy)
    ? 1 - (confidence - realizedAccuracy) ** 2
    : null;
  return {
    targets,
    observed,
    scores: {
      wind: { score: round(windScore, 4), maximumSustainedWindMph: round(observed.maximumSustainedWindMph), maximumGustMph: round(observed.maximumGustMph) },
      visibilityFog: { score: round(fogScore, 4), forecastProbability: round(fogProbability, 3), forecastMinimumVisibilityRangeMiles: targets.visibility?.minimumVisibilityRangeMiles ?? null, observed: observed.fog, minimumVisibilityMi: round(observed.minimumVisibilityMi) },
      cloudCover: { score: round(skyAccuracy(targets.sky?.categories, observed.predominantSkyCondition), 4), forecastCategories: targets.sky?.categories ?? [], observedCategory: observed.predominantSkyCondition },
      thunder: { score: round(thunderScore, 4), forecastProbability: round(thunderProbability, 3), observed: observed.thunder },
      hazards: { score: round(hazardAccuracy(targets.hazards, observedHazards), 4), forecast: targets.hazards ?? [], observed: observedHazards },
      eventTiming: { score: timing.length ? round(timing.reduce((sum, item) => sum + item.score, 0) / timing.length, 4) : null, events: timing },
      weatherRegimeNarrative: { score: round(weatherRegimeAccuracy(inferWeatherRegime(day), observed), 4), forecastRegime: inferWeatherRegime(day) },
      officialAlertHandling: { score: targets.officialAlerts?.status === "unknown" ? null : Number(Boolean(targets.officialAlerts?.documented)), ...targets.officialAlerts },
      confidenceCalibration: { score: round(confidenceScore, 4), forecastConfidence: round(confidence, 3), realizedAccuracy: round(realizedAccuracy, 4) },
    },
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
  const precipitation = {
    forecastProbability: round(precipitationProbability, 3),
    observed: precipitationObserved,
    brierScore: round(precipitationBrierScore, 4),
  };
  const expanded = evaluateExpandedComponents({
    forecast,
    regionKey,
    day,
    stationObservations,
    temperatureScores: stationScores,
    precipitation,
  });
  return {
    forecastDay: {
      date: day.date,
      highF: day.highF,
      lowF: day.lowF,
      precipitation: day.precipitation,
      hazard: day.hazard,
      forecast: day.forecast,
    },
    weatherRegime: inferWeatherRegime(day),
    stationScores,
    precipitation,
    expanded,
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
      if (!allowLegacy) {
        const targets = day.verificationTargets;
        if (!targets) {
          errors.push(`${regionKey} day ${index + 1} is missing verificationTargets`);
          continue;
        }
        if (!Number.isFinite(targets.thunderProbability) || targets.thunderProbability < 0 || targets.thunderProbability > 1) {
          errors.push(`${regionKey} day ${index + 1} verificationTargets.thunderProbability must be between 0 and 1`);
        }
        if (!Number.isFinite(targets.visibility?.fogProbability) || targets.visibility.fogProbability < 0 || targets.visibility.fogProbability > 1) {
          errors.push(`${regionKey} day ${index + 1} verificationTargets.visibility.fogProbability must be between 0 and 1`);
        }
        if (targets.visibility?.minimumVisibilityRangeMiles != null && !normalizeRange(targets.visibility.minimumVisibilityRangeMiles)) {
          errors.push(`${regionKey} day ${index + 1} verificationTargets.visibility.minimumVisibilityRangeMiles must be null or a two-value range`);
        }
        if (!Number.isFinite(targets.confidence) || targets.confidence < 0 || targets.confidence > 1) {
          errors.push(`${regionKey} day ${index + 1} verificationTargets.confidence must be between 0 and 1`);
        }
        if (!Array.isArray(targets.sky?.categories) || !targets.sky.categories.length || !targets.sky.categories.every((item) => item in SKY_INDEX)) {
          errors.push(`${regionKey} day ${index + 1} verificationTargets.sky.categories contains an invalid category`);
        }
        if (!Array.isArray(targets.hazards) || !targets.hazards.every((item) => OBSERVABLE_HAZARDS.has(item))) {
          errors.push(`${regionKey} day ${index + 1} verificationTargets.hazards contains an invalid hazard`);
        }
        for (const event of ["rain", "thunder", "fog", "wind"]) {
          if (!Array.isArray(targets.eventTiming?.[event])) {
            errors.push(`${regionKey} day ${index + 1} verificationTargets.eventTiming.${event} must be an array`);
          }
        }
        for (const [event, periods] of Object.entries(targets.eventTiming ?? {})) {
          if (!["rain", "thunder", "fog", "wind"].includes(event) || !Array.isArray(periods) || !periods.every((period) => PERIODS.includes(period))) {
            errors.push(`${regionKey} day ${index + 1} verificationTargets.eventTiming contains an invalid event or period`);
          }
        }
        for (const [metric, range] of Object.entries(targets.wind ?? {})) {
          if (!["sustainedRangeMph", "gustRangeMph"].includes(metric) || (range != null && !normalizeRange(range))) {
            errors.push(`${regionKey} day ${index + 1} verificationTargets.wind contains an invalid range`);
          }
        }
        if (!['none', 'active'].includes(targets.officialAlerts?.status)
          || !Number.isFinite(Date.parse(targets.officialAlerts?.checkedAt))
          || !/^https:\/\/api\.weather\.gov\/alerts\//.test(targets.officialAlerts?.sourceUrl ?? "")) {
          errors.push(`${regionKey} day ${index + 1} verificationTargets.officialAlerts must include status, checkedAt and sourceUrl`);
        }
      }
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
