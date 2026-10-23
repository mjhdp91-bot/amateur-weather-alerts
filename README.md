# amateur-weather-alerts

Live NWS weather alerts and independent 72-hour forecasts for Central Kentucky
and Upstate South Carolina.

## Forecast publishing

Publish a new issue by replacing `forecast.json` and committing that one file.
The **Archive issued forecast** GitHub Actions workflow automatically:

1. reads the prior version of `forecast.json` from Git history;
2. stores a complete, locked copy in `forecast-history.json`;
3. preserves any verification results already recorded for that issue; and
4. commits the updated archive back to `main`.

The workflow will not duplicate an issue. If an older compact history entry exists,
it adds the full original forecast while preserving the existing entry and its
verification status. A conflicting locked copy causes the workflow to fail instead
of overwriting the archive.

GitHub Pages then serves both the current forecast and the updated archive. The
only routine manual publishing step is updating `forecast.json`. Verification is
maintained independently so an issued forecast is never silently rewritten.

## Automatic accuracy feedback

The **Verify forecast accuracy** workflow runs daily and can also be started
manually. It:

1. finds every due 24-, 48- and 72-hour checkpoint for both the active forecast
   and archived forecasts;
2. retrieves official NWS observations for KDVK, KLEX, KCEU and KLQK;
3. scores high and low temperature ranges, precipitation probability, wind,
   fog/visibility, cloud cover, thunder, observable hazards, event timing,
   weather-regime narrative, issuance-time alert documentation and confidence
   calibration while continuing to exclude Upstate Tempest data;
4. stores the full checkpoint result in `forecast-verifications.json` and mirrors
   archived results into `forecast-history.json`; and
5. rebuilds `forecast-accuracy.json`, the cumulative machine-readable accuracy
   summary used when preparing the next forecast.

The summary reports temperature bias, mean absolute error, RMSE, range coverage
and average range width by region, station, lead time and weather regime. It also
reports precipitation Brier score and reliability bins. Model guidance is scored
only from benchmarks captured at issue time; model values are never reconstructed
after the event. Model results are summarized by model, official station and
forecast lead time. Station-tagged benchmarks use that station's observation;
legacy untagged benchmarks retain the regional official-station mean.

`allCategoryAccuracy.overallAccuracyPercent` is the comprehensive headline score.
Each available category receives equal weight so numerous temperature observations
cannot drown out hazards or timing. `categoryCoverage` always accompanies the
percentage; categories without objective evidence are omitted rather than counted
as successes or failures. The summary also reports the category-balanced score by
region and by 24-, 48- and 72-hour lead time.

Scoring rules are versioned. When the scoring version changes, the daily workflow
re-fetches official observations for previously verified checkpoints once, which
prevents historical results from mixing incompatible definitions. NWS wind units
are read from each observation's unit code before conversion.

Bias corrections require at least four matching samples and an absolute mean bias
of at least 1.5°F. Qualified corrections are shrunk by 25% and limited to ±4°F.
This prevents a single unusual observation from becoming an automatic correction.

The generated `forecastDecisionSupport` section turns verification into concrete
preparation rules. It recommends high/low range widths at each lead time from the
historical 70th-percentile absolute error (bounded to 4–10°F), and model weights
by weather regime when a model has at least three matching samples. The validator
requires each qualified station correction to be applied exactly or explicitly
overridden with a reason. It also requires issued range widths and model blends to
be recorded for every region and lead time.

Fog always receives its own probability. Every observable hazard receives a
probability and measurable threshold, while meaningful events receive absolute
start/end windows with a 0–6 hour tolerance. Alert checks must use the configured
regional NWS point and occur within three hours before issuance; active alerts
must preserve their NWS alert IDs.

Every issue also stores an `intermediateUpdatePolicy`. Its default triggers are a
4°F temperature departure, a 25-point precipitation or hazard-probability change,
a three-hour timing shift, or any new official NWS alert. This is a review gate,
not a publishing bypass: a resulting candidate still uses approval-gated
publication.

If official observations are incomplete, the checkpoint is marked `deferred`
with a reason and is retried on the next workflow run rather than being treated as
verified.

## Requirements for every new forecast

The **Validate forecast feedback** workflow rejects new forecast issues unless
they include:

- `weatherRegime` on each of the three days in both regions;
- `verificationTargets` on each day, containing structured wind, fog, cloud,
  thunder, probabilistic hazard, timestamped timing, confidence and fresh
  official-alert targets;
- `modelBenchmarks` containing the point values actually consulted at issuance,
  or a documented `modelBenchmarkExclusions` entry when a listed model cannot be
  scored. A benchmark may include `station: "KDVK"` (or another official station
  configured for that region) so it is verified against that exact station;
  untagged legacy benchmarks continue to use the regional official-station mean;
  and
- `accuracyFeedback`, identifying the exact `forecast-accuracy.json` generation,
  all verified forecasts consulted, and the adjustment or explicit no-adjustment
  decision for each region; and
- `intermediateUpdatePolicy`, defining the observation and guidance changes that
  justify an interim candidate.

### Supplemental forecast research

Review the latest relevant [Kentucky Weather Center](https://kyweathercenter.com/)
post when preparing every Central Kentucky forecast. Use its Kentucky-focused
pattern, temperature, precipitation and timing discussion as supplemental
analysis, then compare it with current NWS observations, alerts and forecast
discussions plus the available model guidance.

When Kentucky Weather Center materially informs a forecast, record the specific
post URL and publication timestamp in `sources`, include the site in
`analysisLinks`, and summarize the agreement or disagreement in `sourceNotes`.

### Local Danville GRIB guidance

The optional local GRIB pipeline adds timestamped HRRR, RAP, GFS, GEFS, ECMWF,
NAM and NAM CONUS Nest guidance for Danville, KDVK, KLEX and the nearby
neighborhood. NOAA downloads use field and geographic subsets. ECMWF uses its
official indexes to retrieve selected global messages, then retains only the
local point/neighborhood values in the summary. The pipeline preserves
cycle/source metadata and produces `grib-data/summary/danville-guidance.json`
plus proposed station-tagged model benchmarks. The entire `grib-data/`
directory is local and ignored by Git.

The download crop and the local neighborhood are configured separately, so
regional subset bounds do not inflate Danville-area spatial spread. Surface
and layer CAPE/CIN variants are retained independently; the review diagnostic
is selected traceably without overwriting those source fields.

Daily QPF uses an exact non-overlapping path through native and safely derived
accumulation intervals. Conflicts, decreases, unresolved boundary ambiguity and
temporal gaps suppress the amount instead of guessing or double-counting it.
HRRR and RAP retain hourly files across their available horizons so later
one-hour APCP messages do not leave artificial three-hour sampling gaps.

Its complete Windows/Python 3.12 dependency environment is pinned in
`requirements-grib-lock.txt`; `requirements-grib.txt` lists only the direct
packages for deliberate upgrade testing.

Run it from PowerShell with:

```powershell
.\scripts\grib\run-danville-guidance.ps1
```

An already downloaded issuance can be rebuilt with no network access by using
its exact timestamp and `-CacheOnly`; the run stops unless every requested model
has an exact plan and SHA-256-verified cache match. Model cycles more than 12
hours old at issuance are rejected and accepted cycle ages are recorded in the
manifest.

The output is review-only: the pipeline never edits `forecast.json`, the site or
GitHub, and it does not bypass exact forecast-ID publication approval. NOAA/NWS
files remain credited as official source data used in locally processed
independent analysis. ECMWF Open Data is used under CC BY 4.0 with ECMWF
attribution and disclosure of local processing. NAM adapters are optional and
deprecated because NOAA has scheduled NAM products to end November 3, 2026.

See [docs/grib-guidance.md](docs/grib-guidance.md) for setup, source terms,
failure behavior, output validation, review requirements and retention controls.
It is an independent supplemental source: never present its statements as an
official NWS alert, observation or forecast, and never let it override a current
official alert or reliable timestamped observational evidence.

Minimal feedback structure:

```json
{
  "accuracyFeedback": {
    "source": "forecast-accuracy.json",
    "summaryGeneratedAt": "2026-09-29T17:40:25.736Z",
    "consultedForecastIds": ["20260925T1320Z"],
    "appliedAdjustments": [
      {
        "region": "kentucky",
        "decision": "no-adjustment",
        "reason": "No bias has reached the four-sample threshold."
      },
      {
        "region": "upstate",
        "decision": "monitor",
        "reason": "KLQK low bias is notable but has only one verified sample."
      }
    ]
  }
}
```

Minimal per-day verification target structure:

```json
{
  "verificationTargets": {
    "wind": {
      "sustainedRangeMph": [5, 10],
      "gustRangeMph": [15, 20]
    },
    "visibility": {
      "fogProbability": 0.1,
      "minimumVisibilityRangeMiles": null
    },
    "sky": {
      "categories": ["clear", "mostly-clear"]
    },
    "thunderProbability": 0.05,
    "hazards": [],
    "hazardProbabilities": {
      "fog": 0.1,
      "wind": 0.1,
      "thunder": 0.05,
      "heavy-rain": 0.05,
      "heat": 0.05,
      "freeze": 0.05
    },
    "hazardThresholds": {
      "fogVisibilityMiles": 1,
      "windSustainedMph": 25,
      "windGustMph": 35,
      "heavyRainHourlyInches": 0.3,
      "heatHighF": 95,
      "freezeLowF": 32
    },
    "eventTiming": {
      "rain": [],
      "thunder": [],
      "fog": [],
      "wind": ["afternoon"]
    },
    "eventWindows": [
      {
        "event": "wind",
        "start": "2026-10-13T12:00:00-04:00",
        "end": "2026-10-13T18:00:00-04:00",
        "probability": 0.6,
        "toleranceHours": 3
      }
    ],
    "confidence": 0.8,
    "officialAlerts": {
      "status": "none",
      "checkedAt": "2026-10-13T12:40:00Z",
      "sourceUrl": "https://api.weather.gov/alerts/active?point=37.6456,-84.7722"
    }
  }
}
```

Allowed sky categories are `clear`, `mostly-clear`, `partly-cloudy`,
`mostly-cloudy` and `overcast`. Observable hazard labels are `fog`, `wind`,
`thunder`, `heavy-rain`, `heat` and `freeze`. Event periods are `morning`,
`afternoon`, `evening` and `night` in `America/New_York`.

`accuracyFeedback` additionally records `stationBiasCorrections`, six
`temperatureRangeDecisions`, six regime-matched `modelWeighting` entries, and
regional `learningDecisions` for fog, wind, hazards and timing. A future issue cannot
silently ignore a qualified correction or a known miss.

Minimal intermediate-update policy:

```json
{
  "intermediateUpdatePolicy": {
    "evaluationCadenceHours": 6,
    "temperatureDeviationF": 4,
    "precipitationProbabilityChange": 0.25,
    "timingShiftHours": 3,
    "hazardProbabilityChange": 0.25,
    "newOfficialAlert": true
  }
}
```

This makes the feedback step auditable. A forecast cannot merely claim that prior
performance was reviewed; it must record which evidence was consulted and what
decision resulted.

## Local archive check

Run the same archive logic locally with:

```text
node scripts/archive-forecast.mjs previous-forecast.json forecast.json forecast-history.json
```

Run the accuracy tools locally with:

```text
node scripts/verify-forecasts.mjs forecast-history.json forecast.json forecast-verifications.json
node scripts/build-accuracy-summary.mjs forecast-history.json forecast.json forecast-verifications.json forecast-accuracy.json
node scripts/validate-forecast.mjs forecast.json forecast-accuracy.json --allow-legacy
node --test scripts/tests/*.test.mjs
```

`--allow-legacy` exists only for the forecast that was already live when this
system was introduced. New forecast issues must pass strict validation without it.
