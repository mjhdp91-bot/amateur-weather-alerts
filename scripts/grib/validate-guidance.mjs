import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const DEFAULT_PATH = "grib-data/summary/danville-guidance.json";
const REPOSITORY_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const OFFICIAL_STATIONS = new Set(["KDVK", "KLEX"]);
const SUMMARY_STATIONS = ["KDVK", "KLEX", "Danville"];
const MODEL_STATUSES = new Set(["complete", "partial", "partial-horizon", "unavailable", "failed", "skipped"]);
const COMPLETENESS_STATUSES = new Set(["complete", "partial", "unavailable"]);
const ATTRIBUTION_FIELDS = ["provider", "statement", "licenseName", "licenseUrl", "modificationNotice"];
const PROFILE_FIELDS = [
  "temperature850hPaC",
  "relativeHumidity700hPaPct",
  "height500hPaM",
  "precipitableWaterIn",
  "wind850hPaMph",
  "wind700hPaMph",
  "wind500hPaMph",
  "verticalVelocity850hPaPaPerS",
  "verticalVelocity700hPaPaPerS",
  "verticalVelocity500hPaPaPerS",
];
const CONSENSUS_PROFILE_FIELDS = [...PROFILE_FIELDS, "height500hPaTendencyM"];
const PROFILE_BOUNDARY_SEMANTICS = new Set([
  "start-inclusive/end-exclusive",
  "start-inclusive/end-inclusive",
]);
const PROFILE_CONFIDENCE = new Set(["unavailable", "low", "medium", "high"]);
const ADAPTIVE_CADENCE_METHOD = "slowest-repeated-field-cadence-capped-at-six-hours";
const QPF_SELECTION_METHOD = "exact-non-overlapping-interval-path";
const QPF_CONFLICT_TOLERANCE_IN = 0.001;
const SURFACE_CONSENSUS_FIELDS = [
  "highF",
  "lowF",
  "qpfIn",
  "maximumGustMph",
  "meanCloudCoverPct",
  "minimumVisibilityMi",
];
const DAILY_COVERAGE_FIELDS = [
  "maximumWindMph",
  "maximumGustMph",
  "meanCloudCoverPct",
  "minimumVisibilityMi",
  "fogSignal",
];
const DAILY_NUMERIC_COVERAGE_FIELDS = new Set([
  "maximumWindMph",
  "maximumGustMph",
  "meanCloudCoverPct",
  "minimumVisibilityMi",
]);

function isObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function validIso(value) {
  return typeof value === "string" && Number.isFinite(Date.parse(value));
}

function validDate(value) {
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)
    && Number.isFinite(Date.parse(`${value}T00:00:00Z`));
}

function validSha256(value) {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function validateProvenanceLink(value, path, errors) {
  if (!isObject(value)) {
    errors.push(`${path} must be an object`);
    return;
  }
  if (typeof value.path !== "string" || !value.path.trim()
    || value.path.startsWith("/") || /^[A-Za-z]:[\\/]/.test(value.path)) {
    errors.push(`${path}.path must be a repository-relative path`);
  }
  if (!validSha256(value.sha256)) errors.push(`${path}.sha256 must be a lowercase SHA-256 digest`);
}

function repositoryFilePath(repositoryRoot, relativePath, label, errors) {
  if (typeof relativePath !== "string" || !relativePath.trim() || isAbsolute(relativePath)) {
    errors.push(`${label}.path must identify a repository-relative file`);
    return null;
  }
  const absolutePath = resolve(repositoryRoot, relativePath);
  const repositoryRelative = relative(repositoryRoot, absolutePath);
  if (!repositoryRelative || repositoryRelative.startsWith("..") || isAbsolute(repositoryRelative)) {
    errors.push(`${label}.path must remain inside the repository`);
    return null;
  }
  return absolutePath;
}

function sha256Path(path) {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

function validateOnDiskProvenance(summary, repositoryRoot) {
  const errors = [];
  const sourceManifest = summary.input?.provenance?.sourceManifest;
  const configuration = summary.input?.provenance?.configuration;
  const inputLink = { path: summary.input?.path, sha256: summary.input?.sha256 };
  if (typeof sourceManifest?.path === "string"
    && /(?:^|[\\/])latest\.json$/i.test(sourceManifest.path)) {
    errors.push("input.provenance.sourceManifest.path must use an immutable versioned manifest, not latest.json");
  }
  const immutableManifestMatch = typeof sourceManifest?.path === "string"
    ? /^grib-data\/manifests\/fetch-\d{8}T\d{6}Z(?:-cache-only)?-([0-9a-f]{64})\.json$/.exec(sourceManifest.path)
    : null;
  if (typeof sourceManifest?.path === "string" && !immutableManifestMatch) {
    errors.push("input.provenance.sourceManifest.path must identify a content-addressed download or cache-only fetch manifest");
  } else if (immutableManifestMatch && sourceManifest.sha256 !== immutableManifestMatch[1]) {
    errors.push("input.provenance.sourceManifest.path content digest must match sourceManifest.sha256");
  }
  if (summary.input?.sourceManifest !== sourceManifest?.path) {
    errors.push("input.sourceManifest must match input.provenance.sourceManifest.path");
  }

  const links = [
    ["input", inputLink],
    ["input.provenance.sourceManifest", sourceManifest],
    ["input.provenance.configuration", configuration],
  ];
  const resolved = new Map();
  for (const [label, link] of links) {
    if (!link || typeof link.path !== "string" || !validSha256(link.sha256)) continue;
    const filePath = repositoryFilePath(repositoryRoot, link.path, label, errors);
    if (!filePath) continue;
    try {
      const actual = sha256Path(filePath);
      if (actual !== link.sha256) errors.push(`${label}.sha256 does not match the referenced on-disk file`);
      resolved.set(label, filePath);
    } catch (error) {
      errors.push(`${label}.path cannot be read: ${error.message}`);
    }
  }

  const inputPath = resolved.get("input");
  if (inputPath) {
    try {
      const extracted = JSON.parse(readFileSync(inputPath, "utf8"));
      if (extracted.sourceManifest !== sourceManifest?.path) {
        errors.push("extracted sourceManifest must match the summary's immutable manifest link");
      }
      for (const [field, expected] of [
        ["sourceManifest", sourceManifest],
        ["configuration", configuration],
      ]) {
        const actual = extracted.provenance?.[field];
        if (actual?.path !== expected?.path || actual?.sha256 !== expected?.sha256) {
          errors.push(`extracted provenance.${field} must match input.provenance.${field}`);
        }
      }
    } catch (error) {
      errors.push(`input.path cannot be read as extracted guidance JSON: ${error.message}`);
    }
  }
  return errors;
}

function uniqueStrings(values) {
  return Array.isArray(values)
    && values.every((value) => typeof value === "string" && value.trim())
    && new Set(values).size === values.length;
}

function validateDailyFieldEligibility(metadata, path, errors) {
  if (!isObject(metadata)) {
    errors.push(`${path} must be an object`);
    return;
  }
  if (typeof metadata.eligible !== "boolean") errors.push(`${path}.eligible must be boolean`);
  if (!validIso(metadata.requiredStart) || !validIso(metadata.requiredEnd)) {
    errors.push(`${path} must define requiredStart and requiredEnd timestamps`);
  } else if (Date.parse(metadata.requiredEnd) <= Date.parse(metadata.requiredStart)) {
    errors.push(`${path}.requiredEnd must be after requiredStart`);
  }
  for (const field of ["coveredStart", "coveredEnd"]) {
    if (metadata[field] != null && !validIso(metadata[field])) {
      errors.push(`${path}.${field} must be a timestamp or null`);
    }
  }
  if (!Number.isInteger(metadata.sampleCount) || metadata.sampleCount < 0) {
    errors.push(`${path}.sampleCount must be a nonnegative integer`);
  }
  if (!Number.isFinite(metadata.nominalCadenceHours)
    || metadata.nominalCadenceHours <= 0 || metadata.nominalCadenceHours > 6) {
    errors.push(`${path}.nominalCadenceHours must be positive and no greater than six`);
  }
  if (!Number.isFinite(metadata.maximumGapHours) || metadata.maximumGapHours <= 0) {
    errors.push(`${path}.maximumGapHours must be positive`);
  } else if (Number.isFinite(metadata.nominalCadenceHours)
    && Math.abs(metadata.maximumGapHours - metadata.nominalCadenceHours - 0.01) > 0.001) {
    errors.push(`${path}.maximumGapHours must equal nominalCadenceHours plus the numeric epsilon`);
  }
  if (metadata.cadenceMethod !== ADAPTIVE_CADENCE_METHOD) {
    errors.push(`${path}.cadenceMethod must document the adaptive cadence rule`);
  }
  if (!Array.isArray(metadata.gaps)) {
    errors.push(`${path}.gaps must be an array`);
  } else {
    for (const [index, gap] of metadata.gaps.entries()) {
      if (!isObject(gap) || !validIso(gap.start) || !validIso(gap.end)
        || !Number.isFinite(gap.hours) || gap.hours <= 0) {
        errors.push(`${path}.gaps[${index}] must be a positive timestamped gap`);
      }
    }
    if (metadata.eligible === true && metadata.gaps.length) {
      errors.push(`${path}.gaps must be empty when eligible is true`);
    }
  }
  if (metadata.eligible === true
    && (!Number.isInteger(metadata.sampleCount) || metadata.sampleCount <= 0
      || !validIso(metadata.coveredStart) || !validIso(metadata.coveredEnd))) {
    errors.push(`${path} must preserve sampled coverage when eligible is true`);
  }
}

function validHttps(value) {
  try {
    return new URL(value).protocol === "https:";
  } catch {
    return false;
  }
}

function validateAttribution(value, path, errors) {
  if (!isObject(value)) {
    errors.push(`${path} must be an object`);
    return;
  }
  for (const field of ATTRIBUTION_FIELDS) {
    if (typeof value[field] !== "string" || !value[field].trim()) {
      errors.push(`${path}.${field} must be a non-empty string`);
    }
  }
  if (!validHttps(value.licenseUrl)) errors.push(`${path}.licenseUrl must be HTTPS`);
}

function normalizedModel(value) {
  const normalized = `${value ?? ""}`.trim().toLowerCase().replace(/[\s_-]+/g, "");
  return ({
    ecmwfifs: "ecmwf",
    nam3kmnest: "namconusnest",
  })[normalized] ?? normalized;
}

function validSummaryStatistic(value) {
  return isObject(value)
    && Number.isFinite(value.min)
    && Number.isFinite(value.max)
    && Number.isFinite(value.mean)
    && value.min <= value.max;
}

function validateProfileStatistics(value, path, errors, { temporal = false } = {}) {
  if (!isObject(value)) {
    errors.push(`${path} must be a statistics object or null`);
    return;
  }
  if (!Number.isInteger(value.count) || value.count <= 0) {
    errors.push(`${path}.count must be a positive integer`);
  }
  for (const field of ["min", "max", "mean", "median", "spread"]) {
    if (!Number.isFinite(value[field])) errors.push(`${path}.${field} must be numeric`);
  }
  if (Number.isFinite(value.min) && Number.isFinite(value.max) && value.min > value.max) {
    errors.push(`${path}.min cannot exceed max`);
  }
  if (Number.isFinite(value.mean) && Number.isFinite(value.min) && Number.isFinite(value.max)
    && (value.mean < value.min || value.mean > value.max)) {
    errors.push(`${path}.mean must fall within min and max`);
  }
  if (Number.isFinite(value.median) && Number.isFinite(value.min) && Number.isFinite(value.max)
    && (value.median < value.min || value.median > value.max)) {
    errors.push(`${path}.median must fall within min and max`);
  }
  if (Number.isFinite(value.spread) && value.spread < 0) {
    errors.push(`${path}.spread must be nonnegative`);
  }
  if (!temporal) return;
  for (const field of ["firstValue", "lastValue"]) {
    if (!Number.isFinite(value[field])) errors.push(`${path}.${field} must be numeric`);
  }
  if (!validIso(value.firstValidTime) || !validIso(value.lastValidTime)) {
    errors.push(`${path}.firstValidTime and lastValidTime must be timestamps`);
  } else if (Date.parse(value.lastValidTime) < Date.parse(value.firstValidTime)) {
    errors.push(`${path}.lastValidTime cannot precede firstValidTime`);
  }
  if (typeof value.coverageComplete !== "boolean") {
    errors.push(`${path}.coverageComplete must be boolean`);
  }
  if (!Number.isFinite(value.nominalCadenceHours)
    || value.nominalCadenceHours <= 0 || value.nominalCadenceHours > 6) {
    errors.push(`${path}.nominalCadenceHours must be positive and no greater than six`);
  }
  if (!Number.isFinite(value.maximumGapHours) || value.maximumGapHours <= 0) {
    errors.push(`${path}.maximumGapHours must be positive`);
  } else if (Number.isFinite(value.nominalCadenceHours)
    && Math.abs(value.maximumGapHours - value.nominalCadenceHours - 0.01) > 0.001) {
    errors.push(`${path}.maximumGapHours must equal nominalCadenceHours plus the numeric epsilon`);
  }
  if (!Array.isArray(value.coverageGaps)) {
    errors.push(`${path}.coverageGaps must be an array`);
  } else {
    for (const [index, gap] of value.coverageGaps.entries()) {
      if (!isObject(gap) || !["leading", "internal", "trailing"].includes(gap.kind)
        || !validIso(gap.start) || !validIso(gap.end)
        || !Number.isFinite(gap.hours) || gap.hours <= 0) {
        errors.push(`${path}.coverageGaps[${index}] must be a positive timestamped gap`);
      }
    }
    if (value.coverageComplete === true && value.coverageGaps.length) {
      errors.push(`${path}.coverageGaps must be empty when coverageComplete is true`);
    }
    if (value.coverageComplete === false && !value.coverageGaps.length) {
      errors.push(`${path}.coverageGaps must explain incomplete coverage`);
    }
  }
}

function validateProfileFieldLists(profile, fields, path, errors) {
  const listNames = ["availableFields", "coverageCompleteFields", "incompleteFields", "missingFields"];
  for (const listName of listNames) {
    if (!uniqueStrings(profile[listName] ?? [])) {
      errors.push(`${path}.${listName} must contain unique non-empty field names`);
      continue;
    }
    for (const field of profile[listName] ?? []) {
      if (!fields.includes(field)) errors.push(`${path}.${listName} contains unknown field ${field}`);
    }
  }
}

function validateSourceAtmosphericProfile(profile, path, errors) {
  if (!isObject(profile)) {
    errors.push(`${path} must be an object when present`);
    return;
  }
  if (!validIso(profile.periodStart) || !validIso(profile.periodEnd)
    || Date.parse(profile.periodEnd) <= Date.parse(profile.periodStart)) {
    errors.push(`${path}.periodStart and periodEnd must define a positive timestamped period`);
  }
  if (!PROFILE_BOUNDARY_SEMANTICS.has(profile.boundarySemantics)) {
    errors.push(`${path}.boundarySemantics is invalid`);
  }
  if (!Number.isFinite(profile.maximumGapHours) || profile.maximumGapHours <= 0) {
    errors.push(`${path}.maximumGapHours must be positive`);
  }
  if (!Number.isFinite(profile.modelCadenceHours)
    || profile.modelCadenceHours <= 0 || profile.modelCadenceHours > 6) {
    errors.push(`${path}.modelCadenceHours must be positive and no greater than six`);
  }
  if (profile.cadenceMethod !== ADAPTIVE_CADENCE_METHOD) {
    errors.push(`${path}.cadenceMethod must document the adaptive cadence rule`);
  }
  if (profile.verticalVelocitySignConvention !== "negative-ascent-positive-descent") {
    errors.push(`${path}.verticalVelocitySignConvention must document the omega sign`);
  }
  validateProfileFieldLists(profile, PROFILE_FIELDS, path, errors);
  if (!isObject(profile.fields)) {
    errors.push(`${path}.fields must be an object`);
    return;
  }

  const available = new Set(profile.availableFields ?? []);
  const complete = new Set(profile.coverageCompleteFields ?? []);
  const incomplete = new Set(profile.incompleteFields ?? []);
  const missing = new Set(profile.missingFields ?? []);
  for (const field of PROFILE_FIELDS) {
    const value = profile.fields[field];
    const fieldPath = `${path}.fields.${field}`;
    if (value == null) {
      if (!missing.has(field)) errors.push(`${fieldPath} must be listed in missingFields when null`);
      if (available.has(field) || complete.has(field) || incomplete.has(field)) {
        errors.push(`${fieldPath} cannot be listed as available when null`);
      }
      continue;
    }
    validateProfileStatistics(value, fieldPath, errors, { temporal: true });
    if (!available.has(field) || missing.has(field)) {
      errors.push(`${fieldPath} must be listed as available and not missing`);
    }
    if (value.coverageComplete === true) {
      if (!complete.has(field) || incomplete.has(field)) {
        errors.push(`${fieldPath} complete-coverage lists contradict coverageComplete`);
      }
    } else if (!incomplete.has(field) || complete.has(field)) {
      errors.push(`${fieldPath} incomplete-coverage lists contradict coverageComplete`);
    }
    if (validIso(profile.periodStart) && validIso(value.firstValidTime)
      && Date.parse(value.firstValidTime) < Date.parse(profile.periodStart)) {
      errors.push(`${fieldPath}.firstValidTime precedes the profile period`);
    }
    if (validIso(profile.periodEnd) && validIso(value.lastValidTime)
      && Date.parse(value.lastValidTime) > Date.parse(profile.periodEnd)) {
      errors.push(`${fieldPath}.lastValidTime exceeds the profile period`);
    }
    if (field === "height500hPaM"
      && value.tendencyM != null && !Number.isFinite(value.tendencyM)) {
      errors.push(`${fieldPath}.tendencyM must be numeric or null`);
    }
    if (field === "height500hPaM" && value.count >= 2 && !Number.isFinite(value.tendencyM)) {
      errors.push(`${fieldPath}.tendencyM is required when at least two height samples exist`);
    }
    if (field === "height500hPaM" && value.count < 2 && value.tendencyM != null) {
      errors.push(`${fieldPath}.tendencyM must be null when fewer than two height samples exist`);
    }
  }
}

function validateConsensusAtmosphericProfile(profile, path, errors) {
  if (!isObject(profile)) {
    errors.push(`${path} must be an object when present`);
    return;
  }
  if (profile.requiresCompleteCoverage !== true) {
    errors.push(`${path}.requiresCompleteCoverage must be true`);
  }
  if (profile.verticalVelocitySignConvention !== "negative-ascent-positive-descent") {
    errors.push(`${path}.verticalVelocitySignConvention must document the omega sign`);
  }
  if (typeof profile.periodAgreement !== "boolean") {
    errors.push(`${path}.periodAgreement must be boolean`);
  } else if (profile.periodAgreement) {
    if (!validIso(profile.periodStart) || !validIso(profile.periodEnd)
      || Date.parse(profile.periodEnd) <= Date.parse(profile.periodStart)) {
      errors.push(`${path} must expose one agreed positive profile period`);
    }
  } else if (profile.periodStart != null || profile.periodEnd != null) {
    errors.push(`${path}.periodStart and periodEnd must be null without period agreement`);
  }
  for (const listName of ["models", "candidateModels", "missingFields"]) {
    if (!uniqueStrings(profile[listName] ?? [])) {
      errors.push(`${path}.${listName} must contain unique non-empty field or model names`);
    }
  }
  if (!Number.isInteger(profile.modelCount) || profile.modelCount < 0
    || profile.modelCount !== (profile.models?.length ?? -1)) {
    errors.push(`${path}.modelCount must match models.length`);
  }
  for (const property of [
    "fields",
    "selectionMethods",
    "fieldContributorCounts",
    "fieldConfidence",
    "fieldContributors",
    "fieldExcludedModels",
  ]) {
    if (!isObject(profile[property])) errors.push(`${path}.${property} must be an object`);
  }
  if (!isObject(profile.fields)) return;

  const missing = new Set(profile.missingFields ?? []);
  for (const field of missing) {
    if (!CONSENSUS_PROFILE_FIELDS.includes(field)) {
      errors.push(`${path}.missingFields contains unknown field ${field}`);
    }
  }
  for (const field of CONSENSUS_PROFILE_FIELDS) {
    const fieldPath = `${path}.fields.${field}`;
    const value = profile.fields[field];
    if (value == null) {
      if (!missing.has(field)) errors.push(`${fieldPath} must be listed in missingFields when null`);
    } else {
      validateProfileStatistics(value, fieldPath, errors);
      if (missing.has(field)) errors.push(`${fieldPath} cannot also be listed in missingFields`);
    }
    if (typeof profile.selectionMethods?.[field] !== "string"
      || !profile.selectionMethods[field].trim()) {
      errors.push(`${path}.selectionMethods.${field} must be documented`);
    }
    const count = profile.fieldContributorCounts?.[field];
    if (!Number.isInteger(count) || count < 0) {
      errors.push(`${path}.fieldContributorCounts.${field} must be a nonnegative integer`);
    }
    if (Number.isInteger(value?.count) && count !== value.count) {
      errors.push(`${path}.fieldContributorCounts.${field} must match the statistic count`);
    }
    if (value == null && count !== 0) {
      errors.push(`${path}.fieldContributorCounts.${field} must be zero when missing`);
    }
    if (!PROFILE_CONFIDENCE.has(profile.fieldConfidence?.[field])) {
      errors.push(`${path}.fieldConfidence.${field} is invalid`);
    } else {
      const expectedConfidence = count >= 4 ? "high" : count >= 2 ? "medium" : count === 1 ? "low" : "unavailable";
      if (profile.fieldConfidence[field] !== expectedConfidence) {
        errors.push(`${path}.fieldConfidence.${field} must match its contributor count`);
      }
    }
    const contributors = profile.fieldContributors?.[field];
    const excluded = profile.fieldExcludedModels?.[field];
    if (!uniqueStrings(contributors ?? [])) {
      errors.push(`${path}.fieldContributors.${field} must contain unique model names`);
    } else if (Number.isInteger(count) && contributors.length !== count) {
      errors.push(`${path}.fieldContributors.${field} must match its contributor count`);
    }
    if (!uniqueStrings(excluded ?? [])) {
      errors.push(`${path}.fieldExcludedModels.${field} must contain unique model names`);
    }
    if (Array.isArray(contributors) && Array.isArray(excluded)
      && contributors.some((model) => excluded.includes(model))) {
      errors.push(`${path}.${field} cannot list a model as both contributor and excluded`);
    }
  }
}

function validateSourceSignals(signals, path, errors) {
  if (!isObject(signals)) {
    errors.push(`${path} must be an object`);
    return;
  }
  if (!validIso(signals.periodStart) || !validIso(signals.periodEnd)
    || Date.parse(signals.periodEnd) <= Date.parse(signals.periodStart)) {
    errors.push(`${path}.periodStart and periodEnd must define a positive timestamped period`);
  }
  if (!PROFILE_BOUNDARY_SEMANTICS.has(signals.boundarySemantics)) {
    errors.push(`${path}.boundarySemantics is invalid`);
  }
  const start = Date.parse(signals.periodStart);
  const end = Date.parse(signals.periodEnd);
  const includesEnd = signals.boundarySemantics === "start-inclusive/end-inclusive";
  for (const [signal, timesField] of [
    ["fog", "fogTimes"],
    ["wind", "windTimes"],
    ["precipitation", "precipitationTimes"],
  ]) {
    const times = signals[timesField];
    if (typeof signals[signal] !== "boolean") {
      errors.push(`${path}.${signal} must be boolean`);
    }
    if (!uniqueStrings(times)) {
      errors.push(`${path}.${timesField} must contain unique timestamps`);
      continue;
    }
    if (typeof signals[signal] === "boolean" && signals[signal] !== (times.length > 0)) {
      errors.push(`${path}.${signal} must match whether ${timesField} contains events`);
    }
    for (const [index, timestamp] of times.entries()) {
      if (!validIso(timestamp)) {
        errors.push(`${path}.${timesField}[${index}] must be a timestamp`);
        continue;
      }
      const value = Date.parse(timestamp);
      if (Number.isFinite(start) && value < start) {
        errors.push(`${path}.${timesField}[${index}] precedes periodStart`);
      }
      if (Number.isFinite(end) && (value > end || (value === end && !includesEnd))) {
        errors.push(`${path}.${timesField}[${index}] violates the documented end boundary`);
      }
    }
  }
}

function validateDay(day, path, errors, {
  allowStatistics = false,
  requireSamples = false,
  consensus = false,
  sourceDay = false,
} = {}) {
  if (!isObject(day)) {
    errors.push(`${path} must be an object`);
    return;
  }
  if (!validDate(day.date)) errors.push(`${path}.date must use YYYY-MM-DD`);
  const missingFields = Array.isArray(day.missingFields) ? day.missingFields : [];
  const validHigh = Number.isFinite(day.highF) || (allowStatistics && validSummaryStatistic(day.highF));
  const validLow = Number.isFinite(day.lowF) || (allowStatistics && validSummaryStatistic(day.lowF));
  if (!validHigh && !(day.highF == null && missingFields.includes("highF"))) {
    errors.push(`${path}.highF must be numeric${allowStatistics ? " or a valid statistics object" : ""}, or flagged as missing`);
  }
  if (!validLow && !(day.lowF == null && missingFields.includes("lowF"))) {
    errors.push(`${path}.lowF must be numeric${allowStatistics ? " or a valid statistics object" : ""}, or flagged as missing`);
  }
  if (Number.isFinite(day.highF) && Number.isFinite(day.lowF) && day.lowF > day.highF) {
    errors.push(`${path}.lowF cannot exceed highF`);
  }
  if (!Array.isArray(day.missingFields) || day.missingFields.some((field) => typeof field !== "string" || !field)) {
    errors.push(`${path}.missingFields must be an array of field names (empty when complete)`);
  }
  if (day.sampleCount != null && (!Number.isInteger(day.sampleCount) || day.sampleCount < 0)) {
    errors.push(`${path}.sampleCount must be a nonnegative integer`);
  }
  if (requireSamples && (!Number.isInteger(day.sampleCount) || day.sampleCount <= 0)) {
    errors.push(`${path} cannot have zero samples when its model is marked complete`);
  }
  if (consensus && (!Number.isInteger(day.modelCount) || day.modelCount <= 0)) {
    errors.push(`${path}.modelCount must be positive`);
  }
  if (sourceDay) {
    validateSourceSignals(day.signals, `${path}.signals`, errors);
    if (day.atmosphericProfile != null) {
      validateSourceAtmosphericProfile(day.atmosphericProfile, `${path}.atmosphericProfile`, errors);
    }
    for (const field of ["highFEligible", "lowFEligible", "qpfComplete"]) {
      if (typeof day[field] !== "boolean") errors.push(`${path}.${field} must be boolean`);
    }
    if (!isObject(day.fieldEligibility)) {
      errors.push(`${path}.fieldEligibility must be an object`);
    } else {
      for (const [field, flag] of [["highF", "highFEligible"], ["lowF", "lowFEligible"]]) {
        const metadata = day.fieldEligibility[field];
        if (!isObject(metadata) || typeof metadata.eligible !== "boolean") {
          errors.push(`${path}.fieldEligibility.${field}.eligible must be boolean`);
          continue;
        }
        if (typeof day[flag] === "boolean" && metadata.eligible !== day[flag]) {
          errors.push(`${path}.fieldEligibility.${field}.eligible must match ${flag}`);
        }
        if (!Object.hasOwn(metadata, "lastQualifyingSample")
          || (metadata.lastQualifyingSample != null && !validIso(metadata.lastQualifyingSample))) {
          errors.push(`${path}.fieldEligibility.${field}.lastQualifyingSample must be a timestamp or null`);
        }
        const requiredHourField = field === "highF" ? "requiredThroughLocalHour" : "requiredThroughFollowingLocalHour";
        if (!Number.isInteger(metadata[requiredHourField])
          || metadata[requiredHourField] < 0 || metadata[requiredHourField] > 23) {
          errors.push(`${path}.fieldEligibility.${field}.${requiredHourField} must be a local hour from 0 through 23`);
        }
        if (day[flag] === true && !validIso(metadata.lastQualifyingSample)) {
          errors.push(`${path}.fieldEligibility.${field}.lastQualifyingSample is required when eligible`);
        }
      }
      for (const field of DAILY_COVERAGE_FIELDS) {
        const metadata = day.fieldEligibility[field];
        validateDailyFieldEligibility(metadata, `${path}.fieldEligibility.${field}`, errors);
        if (DAILY_NUMERIC_COVERAGE_FIELDS.has(field)
          && metadata?.eligible === true && !Number.isFinite(day[field])) {
          errors.push(`${path}.${field} must be numeric when its source-day coverage is eligible`);
        }
      }
    }
    if (day.highFEligible === true && !Number.isFinite(day.highF)) errors.push(`${path}.highF must be numeric when eligible`);
    if (day.lowFEligible === true && !Number.isFinite(day.lowF)) errors.push(`${path}.lowF must be numeric when eligible`);
    if (day.qpfComplete === false && Number.isFinite(day.qpfIn)) errors.push(`${path}.qpfIn must be null when qpfComplete is false`);
    if (day.qpfComplete === true && !Number.isFinite(day.qpfIn)) errors.push(`${path}.qpfIn must be numeric when qpfComplete is true`);
    if (day.qpfCoverage != null) {
      const coverage = day.qpfCoverage;
      if (!isObject(coverage)
        || !validIso(coverage.requiredStart)
        || !validIso(coverage.requiredEnd)
        || !Array.isArray(coverage.gaps)) {
        errors.push(`${path}.qpfCoverage must define requiredStart, requiredEnd, and gaps`);
      } else {
        if (Date.parse(coverage.requiredEnd) <= Date.parse(coverage.requiredStart)) {
          errors.push(`${path}.qpfCoverage requiredEnd must be after requiredStart`);
        }
        if (coverage.coveredStart != null && !validIso(coverage.coveredStart)) {
          errors.push(`${path}.qpfCoverage.coveredStart must be a timestamp or null`);
        }
        if (coverage.coveredEnd != null && !validIso(coverage.coveredEnd)) {
          errors.push(`${path}.qpfCoverage.coveredEnd must be a timestamp or null`);
        }
        for (const [index, gap] of coverage.gaps.entries()) {
          if (!isObject(gap) || !validIso(gap.start) || !validIso(gap.end)
            || !Number.isFinite(gap.hours) || gap.hours <= 0) {
            errors.push(`${path}.qpfCoverage.gaps[${index}] must be a positive timestamped gap`);
          }
        }
        if (day.qpfComplete === true) {
          if (coverage.gaps.length) errors.push(`${path}.qpfCoverage.gaps must be empty when qpfComplete is true`);
          if (!validIso(coverage.coveredStart)
            || Date.parse(coverage.coveredStart) > Date.parse(coverage.requiredStart)) {
            errors.push(`${path}.qpfCoverage must begin by requiredStart when qpfComplete is true`);
          }
          if (!validIso(coverage.coveredEnd)
            || Date.parse(coverage.coveredEnd) < Date.parse(coverage.requiredEnd)) {
            errors.push(`${path}.qpfCoverage must extend through requiredEnd when qpfComplete is true`);
          }
        }

        const exactPathFields = [
          "selectionMethod",
          "sourceItemCount",
          "candidateIntervalCount",
          "excludedPositiveBoundaryCrossingCount",
          "selectedIntervalCount",
          "selectedIntervals",
          "exactPathTotalsIn",
        ];
        const hasExactPathMetadata = exactPathFields.some((field) => Object.hasOwn(coverage, field));
        if (hasExactPathMetadata) {
          if (coverage.selectionMethod !== QPF_SELECTION_METHOD) {
            errors.push(`${path}.qpfCoverage.selectionMethod must document exact non-overlapping interval selection`);
          }
          for (const field of [
            "sourceItemCount",
            "candidateIntervalCount",
            "excludedPositiveBoundaryCrossingCount",
            "selectedIntervalCount",
          ]) {
            if (!Number.isInteger(coverage[field]) || coverage[field] < 0) {
              errors.push(`${path}.qpfCoverage.${field} must be a nonnegative integer`);
            }
          }
          if (!Array.isArray(coverage.selectedIntervals)) {
            errors.push(`${path}.qpfCoverage.selectedIntervals must be an array`);
          } else {
            if (Number.isInteger(coverage.selectedIntervalCount)
              && coverage.selectedIntervalCount !== coverage.selectedIntervals.length) {
              errors.push(`${path}.qpfCoverage.selectedIntervalCount must match selectedIntervals`);
            }
            if (Number.isInteger(coverage.candidateIntervalCount)
              && coverage.candidateIntervalCount < coverage.selectedIntervals.length) {
              errors.push(`${path}.qpfCoverage.candidateIntervalCount cannot be smaller than the selected path`);
            }
            let previousEnd = null;
            let selectedTotal = 0;
            let selectedIntervalsValid = true;
            for (const [index, interval] of coverage.selectedIntervals.entries()) {
              const intervalPath = `${path}.qpfCoverage.selectedIntervals[${index}]`;
              if (!isObject(interval)
                || !validIso(interval.start)
                || !validIso(interval.end)
                || Date.parse(interval.end) <= Date.parse(interval.start)
                || !Number.isFinite(interval.amountIn)
                || interval.amountIn < 0
                || typeof interval.method !== "string"
                || !interval.method.trim()
                || (interval.sourceStepRange != null && typeof interval.sourceStepRange !== "string")
                || typeof interval.boundaryClipped !== "boolean") {
                errors.push(`${intervalPath} must define a positive timestamped interval, nonnegative amount, method, source step range, and boundary flag`);
                selectedIntervalsValid = false;
                continue;
              }
              if (previousEnd != null && Date.parse(interval.start) !== previousEnd) {
                errors.push(`${intervalPath}.start must exactly continue the preceding selected interval`);
              }
              previousEnd = Date.parse(interval.end);
              selectedTotal += interval.amountIn;
            }
            if (coverage.selectedIntervals.length && selectedIntervalsValid) {
              const first = coverage.selectedIntervals[0];
              const last = coverage.selectedIntervals.at(-1);
              if (Date.parse(first.start) !== Date.parse(coverage.requiredStart)
                || Date.parse(last.end) !== Date.parse(coverage.requiredEnd)) {
                errors.push(`${path}.qpfCoverage.selectedIntervals must exactly span requiredStart through requiredEnd`);
              }
              if (day.qpfComplete === true
                && Number.isFinite(day.qpfIn)
                && Math.abs(selectedTotal - day.qpfIn) > QPF_CONFLICT_TOLERANCE_IN + 0.0001) {
                errors.push(`${path}.qpfIn must equal the selected interval total within rounding tolerance`);
              }
            }
            if (day.qpfComplete === true && coverage.selectedIntervals.length === 0) {
              errors.push(`${path}.qpfCoverage.selectedIntervals cannot be empty when qpfComplete is true`);
            }
          }
          if (!Array.isArray(coverage.exactPathTotalsIn)
            || coverage.exactPathTotalsIn.some((value) => !Number.isFinite(value) || value < 0)) {
            errors.push(`${path}.qpfCoverage.exactPathTotalsIn must contain only nonnegative numeric totals`);
          } else {
            if (day.qpfComplete === true && coverage.exactPathTotalsIn.length === 0) {
              errors.push(`${path}.qpfCoverage.exactPathTotalsIn cannot be empty when qpfComplete is true`);
            }
            if (day.qpfComplete === true && coverage.exactPathTotalsIn.length > 1
              && Math.max(...coverage.exactPathTotalsIn) - Math.min(...coverage.exactPathTotalsIn)
                > QPF_CONFLICT_TOLERANCE_IN) {
              errors.push(`${path}.qpfCoverage.exactPathTotalsIn contains conflicting complete-path totals`);
            }
          }
        }
      }
    }
  }
  if (allowStatistics && day.atmosphericProfile != null) {
    validateConsensusAtmosphericProfile(day.atmosphericProfile, `${path}.atmosphericProfile`, errors);
  }
}

function localDate(timestamp, timeZone) {
  if (!validIso(timestamp)) return null;
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(new Date(timestamp)).map((part) => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function validateFollowingNight(container, path, coverage, timeZone, errors) {
  if (!isObject(container) || !Array.isArray(container.hourly) || !Array.isArray(container.days)) return;
  const finalDate = coverage.forecastDates?.[2];
  if (!validDate(finalDate) || !validIso(coverage.end)) return;
  const trailing = container.hourly.filter((record) => {
    const timestamp = record?.validTime ?? record?.timestamp;
    return validIso(timestamp)
      && Date.parse(timestamp) <= Date.parse(coverage.end)
      && localDate(timestamp, timeZone) > finalDate;
  });
  if (!trailing.length) return;
  const finalDay = container.days.find((day) => day?.date === finalDate);
  if (!isObject(finalDay)) {
    errors.push(`${path} must retain the third forecast day when following-night samples exist`);
    return;
  }
  if (!Number.isInteger(finalDay.followingNightSampleCount)
    || finalDay.followingNightSampleCount < trailing.length) {
    errors.push(`${path} third day must count all following-night samples within the 72-hour window`);
  }
  const latestTrailing = Math.max(...trailing.map((record) => Date.parse(record.validTime ?? record.timestamp)));
  if (!validIso(finalDay.lastValidTime) || Date.parse(finalDay.lastValidTime) < latestTrailing) {
    errors.push(`${path} third day must extend through its latest following-night sample`);
  }
  if (Number.isInteger(finalDay.calendarSampleCount)
    && (!Number.isInteger(finalDay.sampleCount)
      || finalDay.sampleCount < finalDay.calendarSampleCount + trailing.length)) {
    errors.push(`${path} third-day sampleCount must incorporate following-night samples`);
  }
}

function provenanceFailures(summary) {
  const failures = [];
  const containers = [
    ["input", summary.input],
    ["input.statistics", summary.input?.statistics],
    ["input.provenance", summary.input?.provenance],
    ["provenance", summary.provenance],
  ].filter(([, value]) => isObject(value));
  const countFields = ["failedFileCount", "missingFileCount", "gapCount", "failedDownloadCount"];
  const listFields = ["failedFiles", "missingFiles", "gaps", "failedDownloads"];
  for (const [label, container] of containers) {
    for (const field of countFields) {
      if (Number.isFinite(container[field]) && container[field] > 0) failures.push(`${label}.${field}`);
    }
    for (const field of listFields) {
      if (Array.isArray(container[field]) && container[field].length) failures.push(`${label}.${field}`);
    }
    for (const result of container.modelResults ?? []) {
      if (["failed", "error", "missing", "incomplete"].includes(`${result?.status ?? ""}`.toLowerCase())) {
        failures.push(`${label}.modelResults`);
        break;
      }
    }
  }
  for (const [index, model] of (summary.models ?? []).entries()) {
    for (const field of countFields) {
      if (Number.isFinite(model?.[field]) && model[field] > 0) failures.push(`models[${index}].${field}`);
    }
    for (const field of listFields) {
      if (Array.isArray(model?.[field]) && model[field].length) failures.push(`models[${index}].${field}`);
    }
  }
  return [...new Set(failures)];
}

function eligibleSurfaceModels(models, station, date) {
  const eligible = Object.fromEntries(SURFACE_CONSENSUS_FIELDS.map((field) => [field, new Set()]));
  eligible.fogSignal = new Set();
  eligible.windSignal = new Set();
  eligible.precipitationSignal = eligible.qpfIn;
  eligible.fogSupport = new Set();
  eligible.windSupport = new Set();
  eligible.precipitationSupport = new Set();
  for (const model of models ?? []) {
    const day = model?.stations?.[station]?.days?.find((item) => item?.date === date);
    if (!day || !(day.sampleCount > 0)) continue;
    const key = normalizedModel(model.model);
    if (day.highFEligible === true && Number.isFinite(day.highF)) eligible.highF.add(key);
    if (day.lowFEligible === true && Number.isFinite(day.lowF)) eligible.lowF.add(key);
    if (day.qpfComplete === true && Number.isFinite(day.qpfIn)) eligible.qpfIn.add(key);
    for (const field of ["maximumGustMph", "meanCloudCoverPct", "minimumVisibilityMi"]) {
      if (day.fieldEligibility?.[field]?.eligible === true && Number.isFinite(day[field])) {
        eligible[field].add(key);
      }
    }
    if (day.fieldEligibility?.fogSignal?.eligible === true) {
      eligible.fogSignal.add(key);
      if (day.signals?.fog === true) eligible.fogSupport.add(key);
    }
    const hasCompleteWind = day.fieldEligibility?.maximumWindMph?.eligible === true
      && Number.isFinite(day.maximumWindMph);
    const hasCompleteGust = day.fieldEligibility?.maximumGustMph?.eligible === true
      && Number.isFinite(day.maximumGustMph);
    if (hasCompleteWind || hasCompleteGust) {
      eligible.windSignal.add(key);
      if (day.signals?.wind === true) eligible.windSupport.add(key);
    }
    if (day.qpfComplete === true && Number.isFinite(day.qpfIn) && day.qpfIn > 0.001) {
      eligible.precipitationSupport.add(key);
    }
  }
  eligible.temperatureUnion = new Set([...eligible.highF, ...eligible.lowF]);
  return eligible;
}

function profileRepresentative(field, source) {
  if (!isObject(source) || source.coverageComplete !== true) return null;
  if (field === "height500hPaTendencyM") return source.tendencyM;
  if (field === "precipitableWaterIn" || field.startsWith("wind")) return source.max;
  if (field.startsWith("verticalVelocity")) return source.min;
  return source.mean;
}

function eligibleProfileModels(models, station, date) {
  const eligible = Object.fromEntries(CONSENSUS_PROFILE_FIELDS.map((field) => [field, new Set()]));
  for (const model of models ?? []) {
    const day = model?.stations?.[station]?.days?.find((item) => item?.date === date);
    if (!day || !(day.sampleCount > 0)) continue;
    const key = normalizedModel(model.model);
    for (const field of CONSENSUS_PROFILE_FIELDS) {
      const sourceField = field === "height500hPaTendencyM" ? "height500hPaM" : field;
      const source = day.atmosphericProfile?.fields?.[sourceField];
      if (Number.isFinite(profileRepresentative(field, source))) eligible[field].add(key);
    }
  }
  return eligible;
}

function normalizedStringSet(values) {
  return new Set((values ?? []).map(normalizedModel));
}

function setsEqual(left, right) {
  return left.size === right.size && [...left].every((value) => right.has(value));
}

function validateEventSupport(day, path, eligible, errors) {
  if (!isObject(day.eventSupport)) {
    errors.push(`${path}.eventSupport must be an object`);
    return;
  }
  const definitions = {
    fog: [eligible.fogSignal, eligible.fogSupport],
    wind: [eligible.windSignal, eligible.windSupport],
    precipitation: [eligible.precipitationSignal, eligible.precipitationSupport],
  };
  for (const [event, [eligibleModels, supportingModels]] of Object.entries(definitions)) {
    const support = day.eventSupport[event];
    const supportPath = `${path}.eventSupport.${event}`;
    if (!isObject(support)) {
      errors.push(`${supportPath} must be an object`);
      continue;
    }
    if (!uniqueStrings(support.contributorModels ?? [])) {
      errors.push(`${supportPath}.contributorModels must contain unique model names`);
    }
    if (!uniqueStrings(support.supportingModels ?? [])) {
      errors.push(`${supportPath}.supportingModels must contain unique model names`);
    }
    const contributors = normalizedStringSet(support.contributorModels);
    const supporters = normalizedStringSet(support.supportingModels);
    if (!setsEqual(contributors, eligibleModels)) {
      errors.push(`${supportPath}.contributorModels must exactly match eligible source days`);
    }
    if (!setsEqual(supporters, supportingModels)) {
      errors.push(`${supportPath}.supportingModels must exactly match eligible supporting source days`);
    }
    if (!Number.isInteger(support.contributors) || support.contributors < 0
      || support.contributors !== contributors.size) {
      errors.push(`${supportPath}.contributors must match contributorModels.length`);
    }
    if (!Number.isInteger(support.models) || support.models < 0 || support.models !== supporters.size) {
      errors.push(`${supportPath}.models must match supportingModels.length`);
    }
    if ([...supporters].some((model) => !contributors.has(model))) {
      errors.push(`${supportPath}.supportingModels must be a subset of contributorModels`);
    }
    const expectedFraction = contributors.size
      ? Math.round((supporters.size / contributors.size) * 100) / 100
      : null;
    if (expectedFraction == null) {
      if (support.fraction != null) errors.push(`${supportPath}.fraction must be null without contributors`);
    } else if (!Number.isFinite(support.fraction)
      || Math.abs(support.fraction - expectedFraction) > 0.001) {
      errors.push(`${supportPath}.fraction must match eligible supporting-model support`);
    }
  }
}

function minimumContributorsForConfidence(value) {
  return { unavailable: 0, low: 1, medium: 2, high: 4 }[value] ?? null;
}

function confidenceForContributorCount(count) {
  return count >= 4 ? "high" : count >= 2 ? "medium" : count === 1 ? "low" : "unavailable";
}

function validateConsensusSemantics(day, path, station, models, errors) {
  if (!isObject(day) || !validDate(day.date)) return;
  const eligible = eligibleSurfaceModels(models, station, day.date);
  if (!isObject(day.fieldContributorCounts)) {
    errors.push(`${path}.fieldContributorCounts must be an object`);
  } else {
    for (const field of SURFACE_CONSENSUS_FIELDS) {
      const maximum = eligible[field].size;
      const count = day.fieldContributorCounts[field];
      if (!Number.isInteger(count) || count < 0) {
        errors.push(`${path}.fieldContributorCounts.${field} must be a nonnegative integer`);
      } else if (count > maximum) {
        errors.push(`${path}.fieldContributorCounts.${field} exceeds eligible source days`);
      }
      const statisticCount = day[field]?.count;
      if (Number.isInteger(statisticCount) && statisticCount > maximum) {
        errors.push(`${path}.${field}.count exceeds eligible source days`);
      }
    }
  }
  if (!isObject(day.fieldContributors)) {
    errors.push(`${path}.fieldContributors must be an object`);
  } else {
    for (const field of SURFACE_CONSENSUS_FIELDS) {
      const contributors = day.fieldContributors[field];
      if (!uniqueStrings(contributors)) {
        errors.push(`${path}.fieldContributors.${field} must contain unique model names`);
        continue;
      }
      if (Number.isInteger(day.fieldContributorCounts?.[field])
        && contributors.length !== day.fieldContributorCounts[field]) {
        errors.push(`${path}.fieldContributors.${field} must match its contributor count`);
      }
      for (const model of contributors) {
        if (!eligible[field].has(normalizedModel(model))) {
          errors.push(`${path}.fieldContributors.${field} includes an ineligible source day: ${model}`);
        }
      }
    }
  }
  if (Number.isInteger(day.modelCount) && day.modelCount > eligible.temperatureUnion.size) {
    errors.push(`${path}.modelCount exceeds models with eligible temperature source days`);
  }
  if (!isObject(day.fieldConfidence)) {
    errors.push(`${path}.fieldConfidence must be an object`);
  } else {
    for (const field of SURFACE_CONSENSUS_FIELDS) {
      const maximum = eligible[field].size;
      const minimum = minimumContributorsForConfidence(day.fieldConfidence[field]);
      if (minimum == null) {
        errors.push(`${path}.fieldConfidence.${field} is invalid`);
      } else if (minimum > maximum) {
        errors.push(`${path}.fieldConfidence.${field} overstates eligible source-day support`);
      }
      const count = day.fieldContributorCounts?.[field];
      if (Number.isInteger(count) && day.fieldConfidence[field] !== confidenceForContributorCount(count)) {
        errors.push(`${path}.fieldConfidence.${field} must reflect its eligible contributor count`);
      }
    }
  }
  const overallMinimum = minimumContributorsForConfidence(day.confidence);
  if (overallMinimum == null) {
    errors.push(`${path}.confidence is invalid`);
  } else if (overallMinimum > Math.min(eligible.highF.size, eligible.lowF.size)) {
    errors.push(`${path}.confidence overstates eligible temperature source-day support`);
  }
  validateEventSupport(day, path, eligible, errors);

  if (isObject(day.atmosphericProfile)) {
    const eligibleProfiles = eligibleProfileModels(models, station, day.date);
    for (const field of CONSENSUS_PROFILE_FIELDS) {
      const contributors = day.atmosphericProfile.fieldContributors?.[field];
      if (!Array.isArray(contributors)) continue;
      for (const model of contributors) {
        if (!eligibleProfiles[field].has(normalizedModel(model))) {
          errors.push(`${path}.atmosphericProfile.fieldContributors.${field} includes an ineligible source-day profile: ${model}`);
        }
      }
      if (contributors.length > eligibleProfiles[field].size) {
        errors.push(`${path}.atmosphericProfile.fieldContributorCounts.${field} exceeds eligible source-day profiles`);
      }
    }
  }
}

function validateHourly(records, path, modelCycle, coverage, errors) {
  if (!Array.isArray(records)) {
    errors.push(`${path} must be an array`);
    return;
  }
  let prior = -Infinity;
  for (const [index, record] of records.entries()) {
    const itemPath = `${path}[${index}]`;
    if (!isObject(record)) {
      errors.push(`${itemPath} must be an object`);
      continue;
    }
    const validTime = record.validTime ?? record.timestamp;
    if (!validIso(validTime)) {
      errors.push(`${itemPath}.validTime must be a valid timestamp`);
      continue;
    }
    const epoch = Date.parse(validTime);
    if (epoch <= prior) errors.push(`${path} must be strictly increasing by valid time`);
    prior = epoch;
    if (validIso(coverage.start) && epoch < Date.parse(coverage.start)) errors.push(`${itemPath} precedes coverage.start`);
    if (validIso(coverage.end) && epoch > Date.parse(coverage.end)) errors.push(`${itemPath} exceeds coverage.end`);
    if (Number.isFinite(record.leadTimeHours)) {
      const expected = (epoch - Date.parse(modelCycle)) / 3_600_000;
      if (Math.abs(record.leadTimeHours - expected) > 0.02) errors.push(`${itemPath}.leadTimeHours is inconsistent with the model cycle`);
    }
    const recordCycle = record.cycleTime ?? record.runTime ?? record.cycle;
    if (recordCycle != null && (!validIso(recordCycle) || Date.parse(recordCycle) !== Date.parse(modelCycle))) {
      errors.push(`${itemPath} mixes a different model cycle`);
    }
  }
}

function validateSignalWindows(windows, path, errors) {
  if (!Array.isArray(windows)) {
    errors.push(`${path} must be an array`);
    return;
  }
  const identities = new Set();
  for (const [index, window] of windows.entries()) {
    const itemPath = `${path}[${index}]`;
    if (!isObject(window)) {
      errors.push(`${itemPath} must be an object`);
      continue;
    }
    if (typeof window.model !== "string" || !window.model.trim()) {
      errors.push(`${itemPath}.model is required`);
    }
    if (typeof window.station !== "string" || !window.station.trim()) {
      errors.push(`${itemPath}.station is required`);
    }
    if (!validIso(window.start) || !validIso(window.end)) {
      errors.push(`${itemPath}.start and end must be timestamps`);
      continue;
    }
    const start = Date.parse(window.start);
    const end = Date.parse(window.end);
    if (end < start) errors.push(`${itemPath}.end cannot precede start`);
    if (!Number.isInteger(window.samples) || window.samples <= 0) {
      errors.push(`${itemPath}.samples must be a positive integer`);
    }
    if (window.samples > 1 && end <= start) {
      errors.push(`${itemPath} cannot collapse multiple samples onto one timestamp`);
    }
    const identity = `${normalizedModel(window.model)}\u0000${window.station}\u0000${window.start}\u0000${window.end}`;
    if (identities.has(identity)) errors.push(`${itemPath} duplicates another signal window`);
    identities.add(identity);
  }
}

function validateLocation(location, errors) {
  if (!isObject(location)) {
    errors.push("location must be an object");
    return;
  }
  const point = location.points?.Danville ?? location.points?.danville ?? location;
  const latitude = point.latitude ?? point.lat;
  const longitude = point.longitude ?? point.lon;
  if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90) errors.push("location latitude is invalid");
  if (!Number.isFinite(longitude) || longitude < -180 || longitude > 180) errors.push("location longitude is invalid");
}

function validateModelFreshness(model, path, issuedAt, errors) {
  const freshness = model.freshness;
  if (!isObject(freshness)) {
    errors.push(`${path}.freshness must preserve the accepted fetch-cycle freshness proof`);
    return;
  }
  if (!validIso(freshness.issuedAt)) {
    errors.push(`${path}.freshness.issuedAt must be a valid timestamp`);
  } else if (validIso(issuedAt) && Date.parse(freshness.issuedAt) !== Date.parse(issuedAt)) {
    errors.push(`${path}.freshness.issuedAt must match input.issuedAt`);
  }
  if (!validIso(freshness.cycleTime)) {
    errors.push(`${path}.freshness.cycleTime must be a valid timestamp`);
  } else if (validIso(model.cycle) && Date.parse(freshness.cycleTime) !== Date.parse(model.cycle)) {
    errors.push(`${path}.freshness.cycleTime must match the accepted model cycle`);
  }
  if (!Number.isFinite(freshness.cycleAgeHours) || freshness.cycleAgeHours < 0) {
    errors.push(`${path}.freshness.cycleAgeHours must be nonnegative`);
  }
  if (!Number.isFinite(freshness.maximumCycleAgeHours) || freshness.maximumCycleAgeHours <= 0) {
    errors.push(`${path}.freshness.maximumCycleAgeHours must be positive`);
  }
  if (freshness.freshnessStatus !== "fresh") {
    errors.push(`${path}.freshness.freshnessStatus must be fresh for accepted guidance`);
  }
  if (validIso(freshness.issuedAt) && validIso(freshness.cycleTime)) {
    const expectedAge = (Date.parse(freshness.issuedAt) - Date.parse(freshness.cycleTime)) / 3_600_000;
    if (expectedAge < -0.02) {
      errors.push(`${path}.freshness cannot use a model cycle later than issuance`);
    }
    if (Number.isFinite(freshness.cycleAgeHours)
      && Math.abs(freshness.cycleAgeHours - expectedAge) > 0.02) {
      errors.push(`${path}.freshness.cycleAgeHours must match issuedAt minus cycleTime`);
    }
  }
  if (Number.isFinite(freshness.cycleAgeHours)
    && Number.isFinite(freshness.maximumCycleAgeHours)
    && freshness.cycleAgeHours - freshness.maximumCycleAgeHours > 0.02) {
    errors.push(`${path}.freshness cannot claim fresh beyond maximumCycleAgeHours`);
  }
}

export function validateGuidance(summary) {
  const errors = [];
  if (!isObject(summary)) return ["guidance summary must be a JSON object"];
  if (summary.schemaVersion !== 1) errors.push("schemaVersion must be 1");
  if (!validIso(summary.generatedAt)) errors.push("generatedAt must be a valid timestamp");
  if (!isObject(summary.input) || typeof summary.input.path !== "string" || !summary.input.path) {
    errors.push("input.path must identify the extracted guidance input");
  }
  if (!validSha256(summary.input?.sha256)) errors.push("input.sha256 must be a lowercase SHA-256 digest");
  if (!isObject(summary.input?.provenance)) {
    errors.push("input.provenance must preserve manifest and configuration hash links");
  } else {
    validateProvenanceLink(summary.input.provenance.sourceManifest, "input.provenance.sourceManifest", errors);
    validateProvenanceLink(summary.input.provenance.configuration, "input.provenance.configuration", errors);
  }
  if (summary.input?.generatedAt != null && !validIso(summary.input.generatedAt)) errors.push("input.generatedAt must be a valid timestamp");
  if (!validIso(summary.input?.issuedAt)) errors.push("input.issuedAt must be a valid timestamp");
  validateLocation(summary.location, errors);
  const timeZone = summary.timeZone ?? summary.location?.timeZone;
  if (timeZone !== "America/New_York") errors.push("timeZone must be America/New_York");

  const coverage = isObject(summary.coverage) ? summary.coverage : {};
  if (!isObject(summary.coverage)) errors.push("coverage must be an object");
  if (!validIso(coverage.start) || !validIso(coverage.end)) {
    errors.push("coverage.start and coverage.end must be valid timestamps");
  } else if (Date.parse(coverage.end) <= Date.parse(coverage.start)) {
    errors.push("coverage.end must be later than coverage.start");
  } else if (coverage.hours !== 72) {
    errors.push("coverage.hours must be exactly 72");
  } else {
    const actualHours = (Date.parse(coverage.end) - Date.parse(coverage.start)) / 3_600_000;
    if (Math.abs(actualHours - coverage.hours) > 0.02) errors.push("coverage.hours must match start-to-end duration");
  }
  if (validIso(summary.input?.issuedAt) && validIso(coverage.start)
    && Date.parse(summary.input.issuedAt) !== Date.parse(coverage.start)) {
    errors.push("coverage.start must match input.issuedAt");
  }
  if (!Array.isArray(coverage.forecastDates) || coverage.forecastDates.length !== 3
    || !coverage.forecastDates.every(validDate) || new Set(coverage.forecastDates).size !== 3) {
    errors.push("coverage.forecastDates must contain exactly three unique YYYY-MM-DD dates");
  }

  const completeness = isObject(summary.completeness) ? summary.completeness : {};
  if (!isObject(summary.completeness)) errors.push("completeness must be an object");
  if (!COMPLETENESS_STATUSES.has(completeness.status)) errors.push("completeness.status is invalid");
  for (const key of ["modelsRequested", "modelsAvailable", "modelsMissing"]) {
    if (!uniqueStrings(completeness[key])) errors.push(`completeness.${key} must contain unique non-empty model identifiers`);
  }
  if (!Number.isInteger(completeness.warningCount) || completeness.warningCount < 0) errors.push("completeness.warningCount must be a nonnegative integer");
  const requested = new Set(completeness.modelsRequested ?? []);
  const available = new Set(completeness.modelsAvailable ?? []);
  const missing = new Set(completeness.modelsMissing ?? []);
  for (const model of available) {
    if (!requested.has(model)) errors.push(`available model ${model} was not requested`);
    if (missing.has(model)) errors.push(`model ${model} cannot be both available and missing`);
  }
  for (const model of missing) if (!requested.has(model)) errors.push(`missing model ${model} was not requested`);
  if (completeness.status === "complete" && missing.size) errors.push("complete guidance cannot list missing models");

  if (!Array.isArray(summary.models) || !summary.models.length) {
    errors.push("models must contain at least one model result");
  }
  const modelKeys = new Set();
  for (const [index, model] of (summary.models ?? []).entries()) {
    const path = `models[${index}]`;
    if (!isObject(model)) {
      errors.push(`${path} must be an object`);
      continue;
    }
    const key = normalizedModel(model.model);
    if (!key) errors.push(`${path}.model is required`);
    if (modelKeys.has(key)) errors.push(`${path}.model duplicates another model result`);
    modelKeys.add(key);
    if (!validIso(model.cycle)) errors.push(`${path}.cycle must be a valid timestamp`);
    if (!MODEL_STATUSES.has(model.status)) errors.push(`${path}.status is invalid`);
    if (!Array.isArray(model.warnings)) errors.push(`${path}.warnings must be an array`);
    if (completeness.status === "complete" && model.status !== "complete") errors.push(`${path} must be complete when completeness.status is complete`);
    if (completeness.status === "complete" && model.warnings?.length) errors.push(`${path} cannot contain warnings when completeness.status is complete`);
    if (!Array.isArray(model.sourceUrls) || model.sourceUrls.some((url) => !validHttps(url))) errors.push(`${path}.sourceUrls must contain HTTPS source URLs`);
    if (!Number.isFinite(model.horizonHours) || model.horizonHours < 0) errors.push(`${path}.horizonHours must be nonnegative`);
    const hasData = model.status === "complete" || model.status === "partial" || model.status === "partial-horizon";
    if (hasData) {
      validateModelFreshness(model, path, summary.input?.issuedAt, errors);
      if (!Number.isFinite(model.coverageCadenceHours)
        || model.coverageCadenceHours <= 0 || model.coverageCadenceHours > 6) {
        errors.push(`${path}.coverageCadenceHours must be positive and no greater than six`);
      }
      if (!model.sourceUrls.length) errors.push(`${path} must retain at least one source URL`);
      if (!isObject(model.stations)) {
        errors.push(`${path}.stations must be an object`);
      } else {
        for (const station of SUMMARY_STATIONS) {
          const stationData = model.stations[station];
          if (!isObject(stationData)) {
            errors.push(`${path}.stations.${station} is required`);
            continue;
          }
          validateHourly(stationData.hourly, `${path}.stations.${station}.hourly`, model.cycle, coverage, errors);
          if (!Array.isArray(stationData.days) || stationData.days.length !== 3) errors.push(`${path}.stations.${station}.days must contain exactly three daily summaries`);
          for (const [dayIndex, day] of (stationData.days ?? []).entries()) {
            validateDay(day, `${path}.stations.${station}.days[${dayIndex}]`, errors, {
              requireSamples: model.status === "complete",
              sourceDay: true,
            });
          }
          validateFollowingNight(stationData, `${path}.stations.${station}`, coverage, timeZone, errors);
        }
      }
      if (!isObject(model.neighborhood)) {
        errors.push(`${path}.neighborhood is required`);
      } else {
        validateHourly(model.neighborhood.hourly, `${path}.neighborhood.hourly`, model.cycle, coverage, errors);
        if (!Array.isArray(model.neighborhood.days) || model.neighborhood.days.length !== 3) errors.push(`${path}.neighborhood.days must contain exactly three daily summaries`);
        for (const [dayIndex, day] of (model.neighborhood.days ?? []).entries()) {
          validateDay(day, `${path}.neighborhood.days[${dayIndex}]`, errors, {
            requireSamples: model.status === "complete",
            sourceDay: true,
          });
        }
        validateFollowingNight(model.neighborhood, `${path}.neighborhood`, coverage, timeZone, errors);
      }
    }
  }
  for (const model of requested) {
    if (!modelKeys.has(normalizedModel(model))) errors.push(`requested model ${model} has no model result`);
  }

  const hasEcmwf = (summary.models ?? []).some((model) => normalizedModel(model?.model) === "ecmwf"
    && ["complete", "partial", "partial-horizon"].includes(model?.status));
  if (summary.sourceAttributions != null && !isObject(summary.sourceAttributions)) {
    errors.push("sourceAttributions must be an object");
  }
  if (summary.sourceNotes != null && !uniqueStrings(summary.sourceNotes)) {
    errors.push("sourceNotes must contain unique non-empty strings");
  }
  if (hasEcmwf) {
    validateAttribution(summary.sourceAttributions?.ecmwf, "sourceAttributions.ecmwf", errors);
    const licenceUrl = summary.sourceAttributions?.ecmwf?.licenseUrl;
    if (typeof licenceUrl !== "string" || !Array.isArray(summary.sourceNotes)
      || !summary.sourceNotes.some((note) => typeof note === "string" && note.includes(licenceUrl))) {
      errors.push("sourceNotes must retain the ECMWF licence URL");
    }
  }

  if (!isObject(summary.consensus) || !isObject(summary.consensus.stations)) {
    errors.push("consensus.stations must be an object");
  } else {
    for (const station of SUMMARY_STATIONS) {
      const stationData = summary.consensus.stations[station];
      if (!isObject(stationData) || !Array.isArray(stationData.days) || stationData.days.length !== 3) {
        errors.push(`consensus.stations.${station}.days must contain exactly three daily summaries`);
        continue;
      }
      for (const [index, day] of stationData.days.entries()) {
        const path = `consensus.stations.${station}.days[${index}]`;
        validateDay(day, path, errors, { allowStatistics: true, consensus: completeness.status === "complete" });
        validateConsensusSemantics(day, path, station, summary.models, errors);
      }
    }
  }
  if (!Array.isArray(summary.consensus?.days) || summary.consensus.days.length !== 3) errors.push("consensus.days must contain exactly three daily summaries");
  for (const [index, day] of (summary.consensus?.days ?? []).entries()) {
    const path = `consensus.days[${index}]`;
    validateDay(day, path, errors, { allowStatistics: true, consensus: completeness.status === "complete" });
    validateConsensusSemantics(day, path, "Danville", summary.models, errors);
  }

  if (!isObject(summary.signals)) {
    errors.push("signals must be an object");
  } else {
    for (const key of ["fogWindows", "windWindows", "precipitationWindows"]) {
      validateSignalWindows(summary.signals[key], `signals.${key}`, errors);
    }
  }

  if (!Array.isArray(summary.proposedForecastBenchmarks)) {
    errors.push("proposedForecastBenchmarks must be an array");
  }
  const benchmarkKeys = new Set();
  for (const [index, benchmark] of (summary.proposedForecastBenchmarks ?? []).entries()) {
    const path = `proposedForecastBenchmarks[${index}]`;
    if (!isObject(benchmark)) {
      errors.push(`${path} must be an object`);
      continue;
    }
    for (const key of ["model", "run", "region", "station", "targetDate", "leadTimeHours", "sourceUrl", "note"]) {
      if (benchmark[key] == null || benchmark[key] === "") errors.push(`${path}.${key} is required`);
    }
    if (benchmark.region !== "kentucky") errors.push(`${path}.region must be kentucky`);
    if (!OFFICIAL_STATIONS.has(benchmark.station)) errors.push(`${path}.station must be KDVK or KLEX`);
    if (!validDate(benchmark.targetDate)) errors.push(`${path}.targetDate must use YYYY-MM-DD`);
    if (!Number.isFinite(benchmark.leadTimeHours) || benchmark.leadTimeHours < 0) errors.push(`${path}.leadTimeHours must be nonnegative`);
    if (![benchmark.highF, benchmark.lowF].some(Number.isFinite)) errors.push(`${path} must contain a numeric highF or lowF`);
    if (!validHttps(benchmark.sourceUrl)) errors.push(`${path}.sourceUrl must be HTTPS`);
    if (typeof benchmark.note !== "string" || !benchmark.note.trim()) errors.push(`${path}.note must document local processing`);
    if (!modelKeys.has(normalizedModel(benchmark.model))) errors.push(`${path}.model has no matching model result`);
    const sourceModel = (summary.models ?? []).find((model) => normalizedModel(model?.model) === normalizedModel(benchmark.model));
    const sourceDay = sourceModel?.stations?.[benchmark.station]?.days?.find((day) => day?.date === benchmark.targetDate);
    if (!sourceDay) {
      errors.push(`${path} has no corresponding source model, station and date day`);
    } else {
      for (const [field, eligibility] of [
        ["highF", "highFEligible"],
        ["lowF", "lowFEligible"],
        ["precipitationIn", "qpfComplete"],
      ]) {
        if (!Object.hasOwn(benchmark, field)) continue;
        if (!Number.isFinite(benchmark[field])) {
          errors.push(`${path}.${field} must be numeric when present`);
        } else if (sourceDay[eligibility] !== true) {
          errors.push(`${path}.${field} requires an eligible and complete source day`);
        } else {
          const sourceField = field === "precipitationIn" ? "qpfIn" : field;
          if (!Number.isFinite(sourceDay[sourceField]) || benchmark[field] !== sourceDay[sourceField]) {
            errors.push(`${path}.${field} must match the eligible source-day value`);
          }
        }
      }
    }
    if (sourceModel && validIso(sourceModel.cycle)
      && (!validIso(benchmark.run) || Date.parse(benchmark.run) !== Date.parse(sourceModel.cycle))) {
      errors.push(`${path}.run must match the source model cycle`);
    }
    if (normalizedModel(benchmark.model) === "ecmwf") {
      validateAttribution(benchmark.attribution, `${path}.attribution`, errors);
      for (const field of ATTRIBUTION_FIELDS) {
        if (benchmark.attribution?.[field] !== summary.sourceAttributions?.ecmwf?.[field]) {
          errors.push(`${path}.attribution.${field} must match sourceAttributions.ecmwf`);
        }
      }
      for (const requiredText of [
        benchmark.attribution?.provider,
        benchmark.attribution?.licenseUrl,
        benchmark.attribution?.modificationNotice,
      ]) {
        if (typeof requiredText === "string" && !benchmark.note.includes(requiredText)) {
          errors.push(`${path}.note must retain ECMWF provider, licence and modification disclosure`);
          break;
        }
      }
    }
    const key = [normalizedModel(benchmark.model), benchmark.run, benchmark.station, benchmark.targetDate, benchmark.leadTimeHours].join("|");
    if (benchmarkKeys.has(key)) errors.push(`${path} duplicates another benchmark`);
    benchmarkKeys.add(key);
  }

  if (!Array.isArray(summary.warnings)) errors.push("warnings must be an array");
  if (Array.isArray(summary.warnings) && Number.isInteger(completeness.warningCount)
    && completeness.warningCount !== summary.warnings.length) {
    errors.push("completeness.warningCount must match warnings.length");
  }
  if ((completeness.status === "partial" || completeness.status === "unavailable") && !(summary.warnings?.length > 0)) {
    errors.push("partial or unavailable guidance must explain the limitation in warnings");
  }
  if (completeness.status === "complete" && summary.warnings?.length) {
    errors.push("complete guidance cannot contain warnings");
  }
  const failedProvenance = provenanceFailures(summary);
  if (completeness.status === "complete" && failedProvenance.length) {
    errors.push(`complete guidance cannot contain failed-file or gap provenance: ${failedProvenance.join(", ")}`);
  }
  if (!isObject(summary.publicationSafety)
    || summary.publicationSafety.manualReviewRequired !== true
    || summary.publicationSafety.writesForecastJson !== false
    || summary.publicationSafety.publishesWebsite !== false) {
    errors.push("publicationSafety must require manual review and prohibit forecast.json or website publication");
  }
  return errors;
}

export function validateGuidanceFile(path = DEFAULT_PATH, repositoryRoot = REPOSITORY_ROOT) {
  let summary;
  try {
    summary = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    return [`Unable to read valid JSON from ${path}: ${error.message}`];
  }
  return [...validateGuidance(summary), ...validateOnDiskProvenance(summary, resolve(repositoryRoot))];
}

const invokedPath = process.argv[1] ? pathToFileURL(process.argv[1]).href : null;
if (invokedPath === import.meta.url) {
  const path = process.argv[2] ?? DEFAULT_PATH;
  const errors = validateGuidanceFile(path);
  if (errors.length) {
    console.error(`GRIB guidance validation failed (${errors.length} error${errors.length === 1 ? "" : "s"}):`);
    for (const error of errors) console.error(`- ${error}`);
    process.exitCode = 1;
  } else {
    console.log(`Validated ${path}.`);
  }
}
