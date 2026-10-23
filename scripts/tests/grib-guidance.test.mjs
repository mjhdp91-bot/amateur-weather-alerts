import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { validateGuidance, validateGuidanceFile } from "../grib/validate-guidance.mjs";

function dailyCoverage(date, nextDateText) {
  return {
    eligible: true,
    requiredStart: `${date}T12:00:00Z`,
    requiredEnd: `${nextDateText}T12:00:00Z`,
    coveredStart: `${date}T12:00:00Z`,
    coveredEnd: `${nextDateText}T12:00:00Z`,
    sampleCount: 5,
    nominalCadenceHours: 6,
    maximumGapHours: 6.01,
    cadenceMethod: "slowest-repeated-field-cadence-capped-at-six-hours",
    gaps: [],
  };
}

function sourceDay(date, highF, lowF, overrides = {}) {
  const nextDate = new Date(`${date}T00:00:00Z`);
  nextDate.setUTCDate(nextDate.getUTCDate() + 1);
  const nextDateText = nextDate.toISOString().slice(0, 10);
  return {
    date,
    highF,
    lowF,
    sampleCount: 1,
    calendarSampleCount: 1,
    followingNightSampleCount: 0,
    firstValidTime: `${date}T16:00:00Z`,
    lastValidTime: `${date}T20:00:00Z`,
    highFEligible: true,
    lowFEligible: true,
    fieldEligibility: {
      highF: { eligible: true, requiredThroughLocalHour: 16, lastQualifyingSample: `${date}T20:00:00Z` },
      lowF: { eligible: true, requiredThroughFollowingLocalHour: 6, lastQualifyingSample: `${nextDateText}T10:00:00Z` },
      maximumWindMph: dailyCoverage(date, nextDateText),
      maximumGustMph: dailyCoverage(date, nextDateText),
      meanCloudCoverPct: dailyCoverage(date, nextDateText),
      minimumVisibilityMi: dailyCoverage(date, nextDateText),
      fogSignal: dailyCoverage(date, nextDateText),
    },
    qpfIn: 0,
    qpfComplete: true,
    maximumWindMph: 12,
    maximumGustMph: 18,
    meanCloudCoverPct: 25,
    minimumVisibilityMi: 10,
    signals: {
      periodStart: `${date}T12:00:00Z`,
      periodEnd: `${nextDateText}T12:00:00Z`,
      boundarySemantics: "start-inclusive/end-exclusive",
      fog: false,
      wind: false,
      precipitation: false,
      fogTimes: [],
      windTimes: [],
      precipitationTimes: [],
    },
    missingFields: [],
    ...overrides,
  };
}

function statistic(value) {
  return { count: 1, min: value, max: value, mean: value, median: value, spread: 0 };
}

function consensusDay(date, highF, lowF) {
  const contributors = ["HRRR"];
  return {
    date,
    models: ["HRRR"],
    modelCount: 1,
    fieldContributorCounts: {
      highF: 1,
      lowF: 1,
      qpfIn: 1,
      maximumGustMph: 1,
      meanCloudCoverPct: 1,
      minimumVisibilityMi: 1,
    },
    fieldContributors: {
      highF: contributors,
      lowF: contributors,
      qpfIn: contributors,
      maximumGustMph: contributors,
      meanCloudCoverPct: contributors,
      minimumVisibilityMi: contributors,
    },
    fieldConfidence: {
      highF: "low",
      lowF: "low",
      qpfIn: "low",
      maximumGustMph: "low",
      meanCloudCoverPct: "low",
      minimumVisibilityMi: "low",
    },
    highF: statistic(highF),
    lowF: statistic(lowF),
    qpfIn: statistic(0),
    maximumGustMph: statistic(18),
    meanCloudCoverPct: statistic(25),
    minimumVisibilityMi: statistic(10),
    eventSupport: {
      fog: {
        models: 0,
        contributors: 1,
        contributorModels: contributors,
        supportingModels: [],
        fraction: 0,
      },
      wind: {
        models: 0,
        contributors: 1,
        contributorModels: contributors,
        supportingModels: [],
        fraction: 0,
      },
      precipitation: {
        models: 0,
        contributors: 1,
        contributorModels: contributors,
        supportingModels: [],
        fraction: 0,
      },
    },
    confidence: "low",
    missingFields: [],
  };
}

const profileFields = [
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
const consensusProfileFields = [...profileFields, "height500hPaTendencyM"];

function emptySourceProfile() {
  return {
    periodStart: "2026-10-23T12:00:00Z",
    periodEnd: "2026-10-24T12:00:00Z",
    boundarySemantics: "start-inclusive/end-exclusive",
    maximumGapHours: 6.01,
    modelCadenceHours: 6,
    cadenceMethod: "slowest-repeated-field-cadence-capped-at-six-hours",
    availableFields: [],
    coverageCompleteFields: [],
    incompleteFields: [],
    missingFields: [...profileFields],
    fields: Object.fromEntries(profileFields.map((field) => [field, null])),
    verticalVelocitySignConvention: "negative-ascent-positive-descent",
  };
}

function emptyConsensusProfile() {
  return {
    periodStart: "2026-10-23T12:00:00Z",
    periodEnd: "2026-10-24T12:00:00Z",
    periodAgreement: true,
    models: [],
    candidateModels: ["HRRR"],
    modelCount: 0,
    requiresCompleteCoverage: true,
    selectionMethods: Object.fromEntries(
      consensusProfileFields.map((field) => [field, "fixture-selection-method"]),
    ),
    fieldContributorCounts: Object.fromEntries(
      consensusProfileFields.map((field) => [field, 0]),
    ),
    fieldConfidence: Object.fromEntries(
      consensusProfileFields.map((field) => [field, "unavailable"]),
    ),
    fieldContributors: Object.fromEntries(
      consensusProfileFields.map((field) => [field, []]),
    ),
    fieldExcludedModels: Object.fromEntries(
      consensusProfileFields.map((field) => [field, ["HRRR"]]),
    ),
    missingFields: [...consensusProfileFields],
    fields: Object.fromEntries(consensusProfileFields.map((field) => [field, null])),
    verticalVelocitySignConvention: "negative-ascent-positive-descent",
  };
}

function validSummary() {
  const sourceDays = [
    sourceDay("2026-10-23", 71, 48),
    sourceDay("2026-10-24", 74, 50),
    sourceDay("2026-10-25", 76, 53, {
      sampleCount: 2,
      calendarSampleCount: 1,
      followingNightSampleCount: 1,
      lastValidTime: "2026-10-26T12:00:00Z",
    }),
  ];
  const consensusDays = [
    consensusDay("2026-10-23", 71, 48),
    consensusDay("2026-10-24", 74, 50),
    consensusDay("2026-10-25", 76, 53),
  ];
  const hourly = [
    { validTime: "2026-10-23T12:00:00Z", leadTimeHours: 0, cycleTime: "2026-10-23T12:00:00Z", temperatureF: 55 },
    { validTime: "2026-10-26T12:00:00Z", leadTimeHours: 72, cycleTime: "2026-10-23T12:00:00Z", temperatureF: 70 },
  ];
  const station = () => ({ hourly: structuredClone(hourly), days: structuredClone(sourceDays) });
  return {
    schemaVersion: 1,
    generatedAt: "2026-10-23T13:00:00Z",
    input: {
      path: "grib-data/extracted/danville.json",
      sha256: "a".repeat(64),
      generatedAt: "2026-10-23T12:59:00Z",
      issuedAt: "2026-10-23T12:00:00Z",
      sourceManifest: "grib-data/manifests/fetch-20261023T120000Z.json",
      provenance: {
        sourceManifest: { path: "grib-data/manifests/fetch-20261023T120000Z.json", sha256: "b".repeat(64) },
        configuration: { path: "config/grib/danville.json", sha256: "c".repeat(64) },
      },
    },
    location: { name: "Danville, Kentucky", latitude: 37.6456, longitude: -84.7722 },
    timeZone: "America/New_York",
    coverage: {
      start: "2026-10-23T12:00:00Z",
      end: "2026-10-26T12:00:00Z",
      hours: 72,
      forecastDates: ["2026-10-23", "2026-10-24", "2026-10-25"],
    },
    completeness: {
      status: "complete",
      modelsRequested: ["hrrr"],
      modelsAvailable: ["hrrr"],
      modelsMissing: [],
      warningCount: 0,
    },
    models: [{
      model: "HRRR",
      cycle: "2026-10-23T12:00:00Z",
      status: "complete",
      freshness: {
        issuedAt: "2026-10-23T12:00:00Z",
        cycleTime: "2026-10-23T12:00:00Z",
        cycleAgeHours: 0,
        maximumCycleAgeHours: 12,
        freshnessStatus: "fresh",
      },
      sourceUrls: ["https://nomads.ncep.noaa.gov/example.grib2"],
      horizonHours: 72,
      coverageCadenceHours: 6,
      stations: { KDVK: station(), KLEX: station(), Danville: station() },
      neighborhood: station(),
      warnings: [],
    }],
    consensus: {
      stations: {
        KDVK: { days: structuredClone(consensusDays) },
        KLEX: { days: structuredClone(consensusDays) },
        Danville: { days: structuredClone(consensusDays) },
      },
      days: structuredClone(consensusDays),
    },
    signals: { fogWindows: [], windWindows: [], precipitationWindows: [] },
    proposedForecastBenchmarks: [{
      model: "HRRR",
      run: "2026-10-23T12:00:00Z",
      region: "kentucky",
      station: "KDVK",
      targetDate: "2026-10-24",
      leadTimeHours: 24,
      highF: 74,
      lowF: 50,
      sourceUrl: "https://nomads.ncep.noaa.gov/example.grib2",
      note: "Locally extracted from the cited official NOAA model file.",
    }],
    publicationSafety: {
      manualReviewRequired: true,
      writesForecastJson: false,
      publishesWebsite: false,
    },
    warnings: [],
  };
}

test("accepts a complete, traceable Danville guidance summary", () => {
  assert.deepEqual(validateGuidance(validSummary()), []);
});

test("requires a portable SHA-256 provenance chain", () => {
  const summary = validSummary();
  summary.input.sha256 = "not-a-digest";
  summary.input.provenance.sourceManifest.path = "C:\\Users\\example\\latest.json";
  summary.input.provenance.configuration.sha256 = "A".repeat(64);
  const errors = validateGuidance(summary);
  assert.ok(errors.some((error) => error.includes("input.sha256")));
  assert.ok(errors.some((error) => error.includes("sourceManifest.path")));
  assert.ok(errors.some((error) => error.includes("configuration.sha256")));
});

test("file validation recomputes immutable on-disk provenance", () => {
  const root = mkdtempSync(join(tmpdir(), "grib-provenance-"));
  const digest = (value) => createHash("sha256").update(value).digest("hex");
  const writeRelative = (path, value) => {
    const absolute = join(root, ...path.split("/"));
    mkdirSync(join(absolute, ".."), { recursive: true });
    writeFileSync(absolute, value);
    return absolute;
  };
  try {
    const configRelative = "config/grib/danville.json";
    const extractedRelative = "grib-data/extracted/danville.json";
    const summaryRelative = "grib-data/summary/danville-guidance.json";
    const manifestText = JSON.stringify({ status: "complete", files: [] });
    const manifestRelative = `grib-data/manifests/fetch-20261023T120000Z-${digest(manifestText)}.json`;
    const configText = JSON.stringify({ site: { id: "danville-ky" } });
    writeRelative(manifestRelative, manifestText);
    writeRelative(configRelative, configText);
    const extracted = {
      sourceManifest: manifestRelative,
      provenance: {
        sourceManifest: { path: manifestRelative, sha256: digest(manifestText) },
        configuration: { path: configRelative, sha256: digest(configText) },
      },
    };
    let extractedText = JSON.stringify(extracted);
    writeRelative(extractedRelative, extractedText);
    const summary = validSummary();
    summary.input.path = extractedRelative;
    summary.input.sha256 = digest(extractedText);
    summary.input.sourceManifest = manifestRelative;
    summary.input.provenance = structuredClone(extracted.provenance);
    const summaryPath = writeRelative(summaryRelative, JSON.stringify(summary));
    assert.deepEqual(validateGuidanceFile(summaryPath, root), []);

    writeRelative(manifestRelative, `${manifestText}\n`);
    assert.ok(validateGuidanceFile(summaryPath, root).some((error) => (
      error.includes("sourceManifest.sha256 does not match")
    )));
    writeRelative(manifestRelative, manifestText);

    const aliasRelative = "grib-data/manifests/latest.json";
    writeRelative(aliasRelative, manifestText);
    extracted.sourceManifest = aliasRelative;
    extracted.provenance.sourceManifest.path = aliasRelative;
    extractedText = JSON.stringify(extracted);
    writeRelative(extractedRelative, extractedText);
    summary.input.sha256 = digest(extractedText);
    summary.input.sourceManifest = aliasRelative;
    summary.input.provenance.sourceManifest.path = aliasRelative;
    writeRelative(summaryRelative, JSON.stringify(summary));
    assert.ok(validateGuidanceFile(summaryPath, root).some((error) => (
      error.includes("immutable versioned manifest")
    )));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("requires accepted-model freshness to match issue and cycle times", () => {
  const summary = validSummary();
  summary.models[0].freshness.cycleTime = "2026-10-23T06:00:00Z";
  summary.models[0].freshness.cycleAgeHours = 13;
  summary.models[0].freshness.maximumCycleAgeHours = 12;
  summary.models[0].freshness.freshnessStatus = "stale";
  const errors = validateGuidance(summary);
  assert.ok(errors.some((error) => error.includes("cycleTime must match the accepted model cycle")));
  assert.ok(errors.some((error) => error.includes("cycleAgeHours must match issuedAt minus cycleTime")));
  assert.ok(errors.some((error) => error.includes("cannot claim fresh beyond maximumCycleAgeHours")));
  assert.ok(errors.some((error) => error.includes("freshnessStatus must be fresh")));
});

test("matches canonical forecast-facing model names to internal guidance IDs", () => {
  for (const [internal, canonical] of [["ecmwf", "ECMWF IFS"], ["nam_conusnest", "NAM 3-km Nest"]]) {
    const summary = validSummary();
    summary.completeness.modelsRequested = [internal];
    summary.completeness.modelsAvailable = [internal];
    summary.models[0].model = internal;
    for (const station of Object.values(summary.consensus.stations)) {
      for (const day of station.days) {
        day.models = [internal];
        day.fieldContributors = Object.fromEntries(
          Object.keys(day.fieldContributors).map((field) => [field, [internal]]),
        );
        for (const support of Object.values(day.eventSupport)) {
          support.contributorModels = [internal];
        }
      }
    }
    for (const day of summary.consensus.days) {
      day.models = [internal];
      day.fieldContributors = Object.fromEntries(
        Object.keys(day.fieldContributors).map((field) => [field, [internal]]),
      );
      for (const support of Object.values(day.eventSupport)) {
        support.contributorModels = [internal];
      }
    }
    summary.proposedForecastBenchmarks[0].model = canonical;
    if (internal === "ecmwf") {
      const attribution = {
        provider: "ECMWF Open Data",
        statement: "Contains modified ECMWF Open Data.",
        licenseName: "Creative Commons Attribution 4.0 International (CC BY 4.0)",
        licenseUrl: "https://creativecommons.org/licenses/by/4.0/",
        modificationNotice: "Selected GRIB messages were processed locally.",
      };
      summary.models[0].sourceUrls = ["https://data.ecmwf.int/forecasts/example.grib2"];
      summary.sourceAttributions = { ecmwf: structuredClone(attribution) };
      summary.sourceNotes = [`ECMWF licence: ${attribution.licenseUrl}`];
      summary.proposedForecastBenchmarks[0].sourceUrl = summary.models[0].sourceUrls[0];
      summary.proposedForecastBenchmarks[0].attribution = structuredClone(attribution);
      summary.proposedForecastBenchmarks[0].note = `${attribution.statement} ${attribution.provider}; ${attribution.licenseUrl}; ${attribution.modificationNotice}`;
    }
    assert.deepEqual(validateGuidance(summary), []);
  }
});

test("requires complete ECMWF attribution in the summary and its benchmarks", () => {
  const summary = validSummary();
  const attribution = {
    provider: "ECMWF Open Data",
    statement: "Contains modified ECMWF Open Data.",
    licenseName: "Creative Commons Attribution 4.0 International (CC BY 4.0)",
    licenseUrl: "https://creativecommons.org/licenses/by/4.0/",
    modificationNotice: "Selected GRIB messages were processed locally.",
  };
  summary.completeness.modelsRequested = ["ecmwf"];
  summary.completeness.modelsAvailable = ["ecmwf"];
  summary.models[0].model = "ecmwf";
  for (const container of [...Object.values(summary.consensus.stations), summary.consensus]) {
    for (const day of container.days) {
      day.fieldContributors = Object.fromEntries(
        Object.keys(day.fieldContributors).map((field) => [field, ["ecmwf"]]),
      );
      for (const support of Object.values(day.eventSupport)) {
        support.contributorModels = ["ecmwf"];
      }
    }
  }
  summary.models[0].sourceUrls = ["https://data.ecmwf.int/forecasts/example.grib2"];
  summary.sourceAttributions = { ecmwf: structuredClone(attribution) };
  summary.sourceNotes = [`ECMWF licence: ${attribution.licenseUrl}`];
  summary.proposedForecastBenchmarks[0].model = "ecmwf";
  summary.proposedForecastBenchmarks[0].sourceUrl = summary.models[0].sourceUrls[0];
  summary.proposedForecastBenchmarks[0].attribution = structuredClone(attribution);
  summary.proposedForecastBenchmarks[0].note = [
    attribution.provider,
    attribution.licenseUrl,
    attribution.modificationNotice,
  ].join(" ");
  assert.deepEqual(validateGuidance(summary), []);

  delete summary.proposedForecastBenchmarks[0].attribution.licenseUrl;
  const errors = validateGuidance(summary);
  assert.ok(errors.some((error) => error.includes("attribution.licenseUrl")));
});

test("rejects mixed model cycles and inconsistent lead times", () => {
  const summary = validSummary();
  const record = summary.models[0].stations.KDVK.hourly[1];
  record.cycleTime = "2026-10-23T06:00:00Z";
  record.leadTimeHours = 70;
  const errors = validateGuidance(summary);
  assert.ok(errors.some((error) => error.includes("mixes a different model cycle")));
  assert.ok(errors.some((error) => error.includes("leadTimeHours is inconsistent")));
});

test("requires explicit missing-field flags and objectively verifiable benchmark stations", () => {
  const summary = validSummary();
  delete summary.models[0].stations.KLEX.days[0].missingFields;
  summary.proposedForecastBenchmarks[0].station = "Danville";
  const errors = validateGuidance(summary);
  assert.ok(errors.some((error) => error.includes("missingFields")));
  assert.ok(errors.some((error) => error.includes("station must be KDVK or KLEX")));
});

test("rejects contradictory completeness metadata", () => {
  const summary = validSummary();
  summary.completeness.modelsMissing = ["hrrr"];
  const errors = validateGuidance(summary);
  assert.ok(errors.some((error) => error.includes("both available and missing")));
  assert.ok(errors.some((error) => error.includes("complete guidance cannot list missing models")));
});

test("preserves the approval-gated publication boundary", () => {
  const summary = validSummary();
  summary.publicationSafety.writesForecastJson = true;
  assert.ok(validateGuidance(summary).some((error) => error.includes("publicationSafety")));
});

test("does not allow complete status to conceal warnings, empty days or failed provenance", () => {
  const summary = validSummary();
  summary.warnings = ["A source gap was detected."];
  summary.completeness.warningCount = 1;
  summary.models[0].warnings = ["One decoded field was skipped."];
  summary.models[0].stations.KDVK.days[1].sampleCount = 0;
  summary.input.statistics = { failedFileCount: 1, gapCount: 1 };
  const errors = validateGuidance(summary);
  assert.ok(errors.some((error) => error.includes("complete guidance cannot contain warnings")));
  assert.ok(errors.some((error) => error.includes("cannot contain warnings when completeness.status is complete")));
  assert.ok(errors.some((error) => error.includes("cannot have zero samples")));
  assert.ok(errors.some((error) => error.includes("failed-file or gap provenance")));
});

test("requires the fixed 72-hour, three-day product contract", () => {
  const summary = validSummary();
  summary.coverage.hours = 84;
  summary.coverage.end = "2026-10-27T00:00:00Z";
  summary.coverage.forecastDates.push("2026-10-26");
  const errors = validateGuidance(summary);
  assert.ok(errors.some((error) => error.includes("exactly 72")));
  assert.ok(errors.some((error) => error.includes("exactly three")));
});

test("requires source-day eligibility metadata and suppresses incomplete QPF", () => {
  const summary = validSummary();
  const day = summary.models[0].stations.KDVK.days[0];
  delete day.highFEligible;
  delete day.fieldEligibility.lowF.lastQualifyingSample;
  delete day.fieldEligibility.minimumVisibilityMi;
  day.qpfComplete = false;
  day.qpfIn = 0.25;
  const errors = validateGuidance(summary);
  assert.ok(errors.some((error) => error.includes("highFEligible must be boolean")));
  assert.ok(errors.some((error) => error.includes("lowF.lastQualifyingSample")));
  assert.ok(errors.some((error) => error.includes("minimumVisibilityMi must be an object")));
  assert.ok(errors.some((error) => error.includes("qpfIn must be null")));
});

test("rejects daily consensus support from a partial-horizon surface field", () => {
  const summary = validSummary();
  const source = summary.models[0].stations.KDVK.days[2];
  source.fieldEligibility.maximumGustMph.eligible = false;
  source.fieldEligibility.maximumGustMph.gaps = [{
    start: "2026-10-25T18:00:00Z",
    end: "2026-10-26T12:00:00Z",
    hours: 18,
  }];
  const consensus = summary.consensus.stations.KDVK.days[2];
  const errors = validateGuidance(summary);
  assert.ok(errors.some((error) => error.includes("fieldContributorCounts.maximumGustMph exceeds")));
  assert.ok(errors.some((error) => error.includes("fieldContributors.maximumGustMph includes an ineligible")));
  assert.ok(errors.some((error) => error.includes("fieldConfidence.maximumGustMph overstates")));
  assert.equal(consensus.fieldContributors.maximumGustMph[0], "HRRR");
});

test("rejects fog-support denominators that include a source day without usable fog coverage", () => {
  const summary = validSummary();
  const source = summary.models[0].stations.KDVK.days[0];
  source.fieldEligibility.fogSignal.eligible = false;
  source.fieldEligibility.fogSignal.gaps = [{
    start: "2026-10-23T18:00:00Z",
    end: "2026-10-24T12:00:00Z",
    hours: 18,
  }];
  const errors = validateGuidance(summary);
  assert.ok(errors.some((error) => error.includes(
    "eventSupport.fog.contributorModels must exactly match eligible source days",
  )));
});

test("rejects source signals assigned to an end-exclusive day boundary", () => {
  const summary = validSummary();
  const signals = summary.models[0].stations.KDVK.days[0].signals;
  signals.fog = true;
  signals.fogTimes = [signals.periodEnd];
  const errors = validateGuidance(summary);
  assert.ok(errors.some((error) => error.includes(
    "signals.fogTimes[0] violates the documented end boundary",
  )));
});

test("requires the recorded daily cadence tolerance to match the adaptive cadence", () => {
  const summary = validSummary();
  const coverage = summary.models[0].stations.KDVK.days[0]
    .fieldEligibility.maximumGustMph;
  coverage.nominalCadenceHours = 3;
  const errors = validateGuidance(summary);
  assert.ok(errors.some((error) => error.includes(
    "maximumGapHours must equal nominalCadenceHours plus the numeric epsilon",
  )));
});

test("rejects a global signal window that collapses multiple samples onto one timestamp", () => {
  const summary = validSummary();
  summary.signals.fogWindows = [{
    model: "HRRR",
    station: "KDVK",
    start: "2026-10-23T18:00:00Z",
    end: "2026-10-23T18:00:00Z",
    samples: 2,
  }];
  const errors = validateGuidance(summary);
  assert.ok(errors.some((error) => error.includes(
    "cannot collapse multiple samples onto one timestamp",
  )));
});

test("rejects complete QPF with an uncovered temporal gap", () => {
  const summary = validSummary();
  const day = summary.models[0].stations.KDVK.days[0];
  day.qpfCoverage = {
    requiredStart: "2026-10-23T11:00:00Z",
    requiredEnd: "2026-10-24T12:00:00Z",
    coveredStart: "2026-10-23T11:00:00Z",
    coveredEnd: "2026-10-24T06:00:00Z",
    gaps: [{
      start: "2026-10-24T06:00:00Z",
      end: "2026-10-24T12:00:00Z",
      hours: 6,
    }],
  };
  const errors = validateGuidance(summary);
  assert.ok(errors.some((error) => error.includes("gaps must be empty")));
  assert.ok(errors.some((error) => error.includes("extend through requiredEnd")));
});

test("validates exact non-overlapping QPF selection metadata", () => {
  const summary = validSummary();
  const day = summary.models[0].stations.KDVK.days[0];
  day.qpfIn = 0;
  day.qpfCoverage = {
    requiredStart: "2026-10-23T12:00:00Z",
    requiredEnd: "2026-10-24T12:00:00Z",
    coveredStart: "2026-10-23T12:00:00Z",
    coveredEnd: "2026-10-24T12:00:00Z",
    gaps: [],
    selectionMethod: "exact-non-overlapping-interval-path",
    sourceItemCount: 9,
    candidateIntervalCount: 9,
    excludedPositiveBoundaryCrossingCount: 1,
    selectedIntervalCount: 2,
    selectedIntervals: [
      {
        start: "2026-10-23T12:00:00Z",
        end: "2026-10-24T00:00:00Z",
        amountIn: 0,
        method: "same-start-cumulative-difference",
        sourceStepRange: "0-12",
        boundaryClipped: false,
      },
      {
        start: "2026-10-24T00:00:00Z",
        end: "2026-10-24T12:00:00Z",
        amountIn: 0,
        method: "native-window",
        sourceStepRange: "12-24",
        boundaryClipped: false,
      },
    ],
    exactPathTotalsIn: [0],
  };
  assert.deepEqual(validateGuidance(summary), []);

  day.qpfCoverage.selectedIntervals[1].start = "2026-10-24T01:00:00Z";
  day.qpfCoverage.exactPathTotalsIn = [0, 0.2];
  const errors = validateGuidance(summary);
  assert.ok(errors.some((error) => error.includes("must exactly continue")));
  assert.ok(errors.some((error) => error.includes("conflicting complete-path totals")));
});

test("allows benchmark fields only from the matching eligible source day", () => {
  const summary = validSummary();
  const source = summary.models[0].stations.KDVK.days[1];
  source.highFEligible = false;
  source.fieldEligibility.highF.eligible = false;
  source.qpfComplete = false;
  source.qpfIn = null;
  summary.proposedForecastBenchmarks[0].precipitationIn = 0;
  const errors = validateGuidance(summary);
  assert.ok(errors.some((error) => error.includes("highF requires an eligible")));
  assert.ok(errors.some((error) => error.includes("precipitationIn requires an eligible")));
});

test("prevents consensus contributor counts and confidence from exceeding eligible source days", () => {
  const summary = validSummary();
  const consensus = summary.consensus.stations.KDVK.days[0];
  consensus.fieldContributorCounts.highF = 2;
  consensus.highF.count = 2;
  consensus.fieldConfidence.highF = "high";
  consensus.confidence = "high";
  const errors = validateGuidance(summary);
  assert.ok(errors.some((error) => error.includes("fieldContributorCounts.highF exceeds")));
  assert.ok(errors.some((error) => error.includes("highF.count exceeds")));
  assert.ok(errors.some((error) => error.includes("fieldConfidence.highF overstates")));
  assert.ok(errors.some((error) => error.includes("confidence overstates")));
});

test("requires the third forecast day to retain following-night samples within the 72-hour window", () => {
  const summary = validSummary();
  const finalDay = summary.models[0].stations.KDVK.days[2];
  finalDay.followingNightSampleCount = 0;
  finalDay.sampleCount = finalDay.calendarSampleCount;
  finalDay.lastValidTime = "2026-10-25T20:00:00Z";
  const errors = validateGuidance(summary);
  assert.ok(errors.some((error) => error.includes("count all following-night samples")));
  assert.ok(errors.some((error) => error.includes("extend through its latest following-night sample")));
  assert.ok(errors.some((error) => error.includes("sampleCount must incorporate")));
});

test("validates profile missingness, coverage and pressure-level statistics", () => {
  const summary = validSummary();
  const profile = emptySourceProfile();
  summary.models[0].stations.KDVK.days[0].atmosphericProfile = profile;
  assert.deepEqual(validateGuidance(summary), []);

  profile.missingFields = profile.missingFields.filter((field) => field !== "temperature850hPaC");
  profile.availableFields = ["temperature850hPaC"];
  profile.coverageCompleteFields = ["temperature850hPaC"];
  profile.fields.temperature850hPaC = {
    count: 1,
    min: 8,
    max: 8,
    mean: 8,
    median: 8,
    spread: 0,
    firstValue: 8,
    lastValue: 8,
    firstValidTime: "2026-10-23T12:00:00Z",
    lastValidTime: "2026-10-23T12:00:00Z",
    nominalCadenceHours: 6,
    maximumGapHours: 6.01,
    coverageComplete: true,
    coverageGaps: [{
      kind: "trailing",
      start: "2026-10-23T12:00:00Z",
      end: "2026-10-24T12:00:00Z",
      hours: 24,
    }],
  };
  const errors = validateGuidance(summary);
  assert.ok(errors.some((error) => error.includes("coverageGaps must be empty")));
});

test("rejects a profile consensus contributor after its source-day coverage becomes incomplete", () => {
  const summary = validSummary();
  const sourceProfile = emptySourceProfile();
  sourceProfile.availableFields = ["temperature850hPaC"];
  sourceProfile.coverageCompleteFields = ["temperature850hPaC"];
  sourceProfile.missingFields = sourceProfile.missingFields.filter(
    (field) => field !== "temperature850hPaC",
  );
  sourceProfile.fields.temperature850hPaC = {
    count: 4,
    min: 8,
    max: 8,
    mean: 8,
    median: 8,
    spread: 0,
    firstValue: 8,
    lastValue: 8,
    firstValidTime: "2026-10-23T12:00:00Z",
    lastValidTime: "2026-10-24T06:00:00Z",
    nominalCadenceHours: 6,
    maximumGapHours: 6.01,
    coverageComplete: true,
    coverageGaps: [],
  };
  summary.models[0].stations.KDVK.days[0].atmosphericProfile = sourceProfile;

  const consensusProfile = emptyConsensusProfile();
  consensusProfile.models = ["HRRR"];
  consensusProfile.modelCount = 1;
  consensusProfile.missingFields = consensusProfile.missingFields.filter(
    (field) => field !== "temperature850hPaC",
  );
  consensusProfile.fields.temperature850hPaC = statistic(8);
  consensusProfile.fieldContributorCounts.temperature850hPaC = 1;
  consensusProfile.fieldConfidence.temperature850hPaC = "low";
  consensusProfile.fieldContributors.temperature850hPaC = ["HRRR"];
  consensusProfile.fieldExcludedModels.temperature850hPaC = [];
  summary.consensus.stations.KDVK.days[0].atmosphericProfile = consensusProfile;
  assert.deepEqual(validateGuidance(summary), []);

  sourceProfile.fields.temperature850hPaC.coverageComplete = false;
  sourceProfile.fields.temperature850hPaC.lastValidTime = "2026-10-24T00:00:00Z";
  sourceProfile.fields.temperature850hPaC.coverageGaps = [{
    kind: "trailing",
    start: "2026-10-24T00:00:00Z",
    end: "2026-10-24T12:00:00Z",
    hours: 12,
  }];
  sourceProfile.coverageCompleteFields = [];
  sourceProfile.incompleteFields = ["temperature850hPaC"];
  const errors = validateGuidance(summary);
  assert.ok(errors.some((error) => error.includes(
    "atmosphericProfile.fieldContributors.temperature850hPaC includes an ineligible",
  )));
});

test("keeps partial horizons valid and does not require data for unavailable models", () => {
  const summary = validSummary();
  summary.completeness = {
    status: "partial",
    modelsRequested: ["hrrr", "rap"],
    modelsAvailable: ["hrrr"],
    modelsMissing: ["rap"],
    warningCount: 1,
  };
  summary.models.push({
    model: "rap",
    cycle: "2026-10-23T09:00:00Z",
    status: "unavailable",
    sourceUrls: [],
    horizonHours: 0,
    stations: {},
    neighborhood: {},
    warnings: ["RAP was unavailable."],
  });
  summary.warnings = ["RAP was unavailable."];
  assert.deepEqual(validateGuidance(summary), []);
});

test("wrapper dry-run contract is fixed to 72 hours and strict extraction", () => {
  const wrapper = readFileSync(new URL("../grib/run-danville-guidance.ps1", import.meta.url), "utf8");
  assert.match(wrapper, /\[ValidateSet\(72\)\]/);
  assert.match(wrapper, /'--strict'/);
  assert.match(wrapper, /if \(\$DryRun\)[\s\S]*return/);
  assert.match(wrapper, /\$fetchResult\.Value\.manifestPath/);
  assert.match(wrapper, /mutable latest\.json alias/);
  assert.doesNotMatch(wrapper, /\$manifestPath\s*=\s*Join-Path[^\r\n]*latest\.json/);
});

test("wrapper requires an explicit issuance offset and directs setup to the lock", () => {
  const wrapper = readFileSync(new URL("../grib/run-danville-guidance.ps1", import.meta.url), "utf8");
  assert.match(wrapper, /\$issuedAtValue = \$IssuedAt\.Trim\(\)/);
  assert.match(wrapper, /\$issuedAtValue -notmatch '\(\?:\[zZ\]\|\[\+-\]\\d\{2\}:\\d\{2\}\)\$'/);
  assert.ok(wrapper.includes("trailing Z or explicit +/-HH:MM UTC offset"));
  assert.match(wrapper, /install requirements-grib-lock\.txt/);
});
