# Danville GRIB guidance

This local pipeline downloads small, timestamped model subsets for Danville,
KDVK and KLEX, extracts comparable weather fields, and builds a reviewable
72-hour guidance summary. It is decision support for an independent forecast;
it is not an official forecast and it never writes `forecast.json`.

## Safety and publication boundary

The fetch, extraction, summary and validation programs write only beneath the
ignored `grib-data/` directory. Both derived-output programs enforce that
resolved-path boundary even when invoked directly and reject protected forecast,
verification and website filenames. The summary contains
`proposedForecastBenchmarks`, but those values are proposals for human review.
They are not copied into a forecast automatically.

After a forecaster reviews the guidance, the normal approval-gated workflow
still applies. A candidate may be published only after the user supplies the
exact phrase `Approve publication of forecast <forecastId>` for that unchanged
candidate. Running this pipeline, using `-KeepRaw`, or accepting a proposed
benchmark does not grant publication approval.

## Local setup

Use 64-bit Python 3.12 and keep all GRIB dependencies inside the
repository-local environment:

```powershell
py -3.12 -m venv .venv-grib
.\.venv-grib\Scripts\python.exe -m pip install -r requirements-grib-lock.txt
.\.venv-grib\Scripts\python.exe -m eccodes selfcheck
```

`requirements-grib-lock.txt` records the complete dependency set validated on
Windows with Python 3.12.14. `requirements-grib.txt` remains the shorter list of
direct dependencies used when deliberately testing upgrades; do not refresh the
lock without rerunning both the offline suites and a live end-to-end extraction.

If the Windows `python` alias does not start a real interpreter, substitute the
full path to an installed Python executable on the first line. Do not install
the decoder globally and do not add it to the system `PATH`.

## Run the pipeline

Run the normal 72-hour process from the repository root:

```powershell
.\scripts\grib\run-danville-guidance.ps1
```

Select models or retain the bounded raw-file cache for investigation. The
product window is fixed at exactly 72 hours and always contains three forecast
days:

```powershell
.\scripts\grib\run-danville-guidance.ps1 -Models hrrr,rap,gfs,gefs,ecmwf -KeepRaw
```

Preview the requests without downloading data:

```powershell
.\scripts\grib\run-danville-guidance.ps1 -DryRun
```

Rebuild the extracted data and summary without any network access by supplying
the original issuance time and requiring an exact local cache match:

```powershell
.\scripts\grib\run-danville-guidance.ps1 -IssuedAt '2026-10-23T11:00:00Z' -CacheOnly
```

`-IssuedAt` must include a trailing `Z` or an explicit `+/-HH:MM` UTC offset.
Offset-free local timestamps are rejected so the same command cannot resolve to
different UTC issuance times on computers in different time zones.

`-CacheOnly` accepts only one complete per-model cycle whose plan fingerprint,
file set, byte sizes and SHA-256 digests match the current configuration,
issuance and 72-hour window. It never downloads, combines model cycles, prunes
the cache or silently substitutes a nonmatching cycle. Every requested model
must have an exact verified cache or the rebuild stops before extraction.

When an issuance time is supplied, it is also the default model-availability
cutoff. The fetcher rejects an `--as-of` value later than `--issued-at`, so a
historical rebuild cannot silently use a model cycle that did not exist when the
forecast was issued. An earlier explicit cutoff is allowed for reproducing a
known data-availability delay.

Every enabled adapter also sets `maximumCycleAgeHours` to 12. Candidates older
than that at issuance are rejected before index discovery, download or cache
reuse. The manifest records `cycleAgeHours`, `maximumCycleAgeHours` and a
`freshnessStatus` for every accepted model, so an outage cannot silently replace
current guidance with an old but technically long enough run.

The wrapper runs these gates in order:

1. discover one complete cycle for each requested model and fetch only selected
   fields for the configured Kentucky box;
2. decode the staged files into normalized point and neighborhood records;
3. calculate daily guidance, signals, spread and benchmark proposals; and
4. validate timestamps, cycle consistency, completeness and benchmark fields.

Any failed command stops the process. A dry run intentionally stops after the
request plan because there are no downloaded files to extract.

## Outputs

All runtime data are ignored by Git. The fetcher enforces that its runtime root
is `grib-data/` or one of its descendants; it rejects repository paths outside
that tree before discovery or download begins:

- `grib-data/manifests/fetch-<issuance>-<content-sha256>.json` (or its cache-only
  counterpart) records the exact cycle, valid time, source URL, local path,
  byte count and SHA-256 digest for each fetched file. `latest.json` is only a
  mutable convenience pointer and is never used in an extracted artifact's
  provenance chain.
- `grib-data/extracted/danville-hourly.json` contains normalized hourly fields
  for Danville, KDVK, KLEX and the configured neighborhood. It also carries the
  fetch `issuedAt` and each decoded model's accepted cycle age, configured
  maximum age and freshness status.
- `grib-data/summary/danville-guidance.json` is the final review artifact.

The extracted artifact records SHA-256 links to its exact fetch manifest and
configuration. The summary records the extracted file's SHA-256 and carries
those upstream links forward, using repository-relative paths so the complete
provenance chain is portable without exposing a local user or worktree path.
Validation recomputes all three referenced files on disk and rejects a mutable
manifest alias or any hash mismatch.

Provenance paths stored in these JSON artifacts are repository-relative POSIX
paths. They remain portable across worktrees and do not disclose the local
Windows account or checkout location.

The final summary reports model availability and missing fields explicitly. It
contains model and consensus daily highs/lows, wind and gusts, precipitation,
cloud/visibility and fog signals, thermodynamic fields where available,
neighborhood spread, cycle metadata, warnings and station-tagged proposed
benchmarks. Each available model includes a self-contained `freshness` block,
so reviewers can compare its cycle age and maximum age without reopening the
fetch manifest. Validation requires that age to equal `issuedAt - cycleTime`,
requires the freshness cycle to match the summarized cycle, and rejects any
model claiming `fresh` beyond its configured maximum. An unavailable field
remains missing; it is never silently replaced with a value from a different
cycle.

The broad `site.boundingBox` is only the network-download crop. Spatial
neighborhood statistics use the separate, smaller
`site.neighborhoodBounds` around Danville and KDVK, so a regional Kentucky
domain cannot masquerade as local spread. KLEX remains a separately named
comparison point rather than part of that local neighborhood envelope.

GRIB CAPE/CIN messages are also kept by their actual vertical definition.
Surface, 180-0-mb and 255-0-mb-above-ground values have distinct keys and
cannot overwrite one another. For forecast review, `forecastCapeJkg` selects
the maximum available CAPE (a conservative instability diagnostic), and
`forecastCinJkg` comes from that same encoded layer. The selected source field,
candidate fields and selection method remain in the extracted metadata; the
original alternatives are preserved for inspection.

Each point and neighborhood day also contains an `atmosphericProfile` review
block. It summarizes extracted 850-mb temperature, 700-mb relative humidity,
500-mb height and height tendency, precipitable water, 850/700/500-mb wind
speed, and 850/700/500-mb vertical velocity. Pressure-level wind speed is
calculated only when both extracted vector components are present at the same
valid time. Negative vertical velocity denotes ascent and positive values
denote descent. No missing level is interpolated or substituted.

Profile periods use the same non-overlapping issuance-to-8-AM / 8-AM-to-8-AM
day-night windows as daily QPF. Every field records its sample count, range,
mean, median, first/last valid time, temporal gaps and whether coverage is
complete. The model consensus accepts only coverage-complete fields, first
reduces each model to one documented daily representative, and then weights
models equally. It uses the daily mean for 850-mb temperature, 700-mb humidity
and 500-mb height; end-minus-start for the 500-mb height tendency; the daily
maximum for precipitable water and pressure-level winds; and the daily minimum
for vertical velocity (the strongest modeled ascent). Contributor names,
counts, confidence and exclusions remain visible for every consensus field.
Coverage tolerance is field- and day-specific: it uses the slowest repeatedly
observed cadence for that field, falls back to the model cadence when a short
sample cannot establish one, and is capped at six hours with a small numerical
epsilon. This retains legitimate six-hour GEFS guidance while exposing missing
three-hour HRRR samples instead of masking them with one fixed tolerance.

Surface gust, cloud-cover and visibility consensus follows the same coverage
rule. Each source day records its required span, sampled span, maximum allowed
gap and any actual gaps in `fieldEligibility`. A partial-horizon value remains
visible for manual review but cannot contribute to the consensus statistic,
event-support denominator or confidence. `fieldContributors` preserves the
exact accepted model identities, and validation rejects a count, identity or
confidence level that exceeds the eligible source days.
Fog support has its own assessability record. A model enters that denominator
only when at least one usable visibility or dewpoint-based diagnostic covers
the full source-day period; models with no usable fog inputs are identified but
do not count as negative fog guidance. Event-support records retain both the
eligible contributor identities and the identities that actually support the
event.

Proposed benchmarks use the canonical forecast-facing model names used by
`modelStatus` (`HRRR`, `RAP`, `NAM`, `NAM 3-km Nest`, `GFS`, `GEFS` and
`ECMWF IFS`). A complete deterministic QPF amount may be retained as
`precipitationIn` for review, but it is not a precipitation probability and is
not used as a Brier-score input. Only an independently justified
`precipitationProbability` may enter the probability scoring system.

Daily QPF windows do not overlap. The first starts at `issuedAt`; each later
window starts at 8 AM local time after the preceding forecast day and ends at
the following 8 AM, capped by `validThrough`. A QPF total is retained only when
valid accumulation increments continuously cover that entire window. Native
accumulation windows are preserved, and successive cumulative fields with the
same encoded start also produce a nonnegative difference candidate. The
summarizer then finds an exact, non-overlapping interval path from the required
start to end. Redundant one-, two- and three-hour windows therefore cannot be
double-counted. A gap, cumulative decrease, conflicting interval amount or
disagreement between exact paths withholds the total. The summary records the
selection method, candidates, selected intervals, required span, covered span
and any gaps in `qpfCoverage`. Positive accumulations that cross a window
boundary are not guessed or time-prorated. They are excluded, and QPF remains
complete only when separate in-window candidates still provide an exact path;
otherwise it is withheld. A zero interval can be clipped without changing its
meaning.

HRRR and RAP request every available forecast hour because their later APCP
messages may still represent only a one-hour interval. Sampling those files at
three-hour steps would create irrecoverable QPF gaps. NAM and NAM CONUS Nest
retain three-hour base sampling after their fine window because their later
three-hour accumulation intervals are contiguous.

Explicit GRIB maximum and minimum temperatures also retain their encoded step
range. A maximum is eligible only when its complete interval lies inside both
the 72-hour guidance window and its target local calendar day. A minimum is
eligible only when its complete interval lies inside both the guidance window
and the following-night period from 6 PM through 8 AM local time. Boundary-
crossing extrema are excluded rather than assigned to whichever day contains
their ending timestamp. `fieldEligibility` reports candidate, accepted,
boundary-excluded and unknown-interval counts for review. An explicit extremum
with no usable interval is also excluded when day/window boundaries are being
enforced; instantaneous temperature samples remain available instead.

Daily fog, wind and precipitation signals use start-inclusive/end-exclusive
periods, with the final endpoint included only at the end of the 72-hour
window. The global windows also deduplicate identical model/station/valid-time
events before counting samples. A boundary event therefore belongs to exactly
one daily period and contributes once, not twice, to its global window.

To validate an existing summary again:

```powershell
node scripts/grib/validate-guidance.mjs grib-data/summary/danville-guidance.json
```

## Review checklist

Before using the values in a forecast candidate:

- confirm `completeness.status`, `modelsMissing`, warnings and each model's
  partial horizon;
- confirm every `cycle`, `validTime` and `leadTimeHours` covers the intended
  forecast day;
- confirm each available model's `freshnessStatus` is `fresh` and its
  `cycleAgeHours` does not exceed `maximumCycleAgeHours`;
- compare KDVK, KLEX, the Danville point and neighborhood spread rather than
  treating one grid point as truth;
- use `atmosphericProfile` only where the field's `coverageComplete` flag is
  true, and inspect its contributor/exclusion lists before describing model
  consensus;
- interpret 500-mb height tendency as period-end minus period-start and omega
  with the documented negative-ascent sign convention;
- treat visibility/fog output as supporting evidence, especially for Danville's
  small valleys;
- check accumulated precipitation `stepRange` metadata and never difference
  accumulations from separate cycles;
- retain only KDVK- or KLEX-tagged benchmark proposals for later objective
  station verification; and
- document why guidance was used, adjusted or rejected in the candidate's
  `accuracyFeedback` and `sourceNotes`.

GRIB guidance supplements current observations, NWS discussions and alerts,
radar/satellite analysis, BUFKIT soundings, ensemble spread and Kentucky Weather
Center context. It does not replace any of those sources.

## Models and lifecycle

- HRRR and RAP emphasize short-range timing and are deliberately marked as
  partial-horizon guidance beyond their available runs.
- GFS and ECMWF provide deterministic coverage through the full period.
- GEFS mean and spread describe ensemble uncertainty without downloading every
  member.
- NAM parent and NAM CONUS Nest are optional, deprecated adapters. NOAA has
  scheduled all NAM products to end at 12 UTC November 3, 2026. The downstream
  JSON contract does not depend on NAM, so an RRFS/REFS adapter can replace it
  without changing forecast review or verification.

Do not hard-code a resolution for the NAM nest. NOAA product pages have used
different labels; the extractor retains the grid spacing reported by each GRIB
message.

## Sources, rate limits and attribution

NOAA model subsets come from [NCEP NOMADS](https://nomads.ncep.noaa.gov/).
Automated Grib Filter requests are serialized with at least the ten-second pause
specified in the [NOMADS Grib Filter guidance](https://nomads.ncep.noaa.gov/info.php?page=gribfilter).
The pipeline records the original NOAA URL and identifies the output as locally
processed independent analysis, not official NOAA output or endorsement. See the
[NWS disclaimer and data-use terms](https://www.weather.gov/disclaimer/).

ECMWF data come from [ECMWF Open Data](https://www.ecmwf.int/en/forecasts/datasets/open-data).
Those data are licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Every retained ECMWF
source URL and downstream note must credit ECMWF, link the licence, and state
that the values were locally subset, extracted or otherwise modified. ECMWF
attribution is retained as structured `sourceAttributions` metadata in the
fetch manifest, extracted data and final summary, and is repeated on each ECMWF
benchmark proposal.
ECMWF does not provide geographic server-side subsetting. The fetcher uses the
official indexes to retrieve only selected global GRIB messages, not an
area-cropped GRIB. Extraction then reads those messages locally and retains only
the Danville, KDVK, KLEX and neighborhood values in the compact JSON summary.
The selected raw global messages remain under the bounded ignored cache until
normal retention cleanup removes them; `-KeepRaw` disables that cleanup for the
run.

NAM retirement is documented by
[NWS Service Change Notice 26-47](https://forecast.weather.gov/product.php?format=CI&glossary=1&issuedby=WSH&product=PNS&site=CTP&version=8),
with its RRFS/REFS replacement described in
[Service Change Notice 26-48](https://forecast.weather.gov/product.php?format=CI&glossary=1&issuedby=WSH&product=PNS&site=EWX&version=2).

## Failure behavior

The fetcher rejects HTML/error pages masquerading as GRIB, incomplete cycles,
short HTTP responses when a `Content-Length` is declared, unexpected cycle or
valid times, invalid GRIB framing and checksum/size mismatches. Each NOAA model
also declares core ecCodes `shortName` fields in
`config/grib/danville.json`; every newly downloaded or reused cached file must
contain them. This catches a transport that ends cleanly on a complete GRIB
message boundary but omits later messages. The fetcher falls back to an older
complete cycle for one model rather than mixing cycles inside that model. A
missing optional model is disclosed as partial guidance; missing required
guidance or an invalid summary stops the normal pipeline before any forecast
preparation.

Cache-only rebuilds are stricter than normal downloads: a missing, changed or
checksum-invalid cache for any requested model stops the run. They report cache
usage but do not enforce retention or delete cached cycles.

Raw-file retention is bounded by `config/grib/danville.json`. `-KeepRaw` skips
normal post-run cleanup for investigation. Without it, successful runs prune
completed cycles beyond the configured retention count rather than deleting the
just-validated input. The switch does not make raw GRIB files eligible for Git,
and the ignored cache should not be copied into the website.
