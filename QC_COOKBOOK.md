# QC Cookbook — what this pipeline actually checks, and why

This document exists because the two source cookbooks it's built from
aren't included here (they're separate copyrighted documents — see
[References](#references)), and because *why a check exists in this exact
shape* lives in commit messages and code comments that are easy to miss.
It was written jointly: a human (Peter Shanks, AADC) read both cookbook
editions and made the calls on what to automate and how conservative to be;
an AI assistant (Claude, Anthropic) did the implementation, ran the
validation described below, and drafted this write-up from that session's
findings. Treat it as a design record, not a spec handed down from the
cookbooks themselves — where this pipeline's behaviour differs from a
literal reading of either cookbook, that's called out explicitly below.

**If you're an agent extending or reimplementing this pipeline**, the
[Checks at a glance](#checks-at-a-glance) and [Constants
reference](#constants-reference) tables below are the fast path — each row
links to the prose section with the full cookbook citation and reasoning.
Read [What we tried and rejected](#what-we-tried-and-rejected) before
adding or loosening any threshold: every rejection there was found by
running against real data, not by inspection.

## Contents

- [The two source documents](#the-two-source-documents)
- [Checks at a glance](#checks-at-a-glance)
- [Checks implemented](#checks-implemented)
  - [Test Probe detection](#test-probe-detection-not-a-per-point-check-but-gates-everything-else)
  - [Surface Transients — CS](#surface-transients--cs-v21-section-431-supersedes-v11-section-21s-csa)
  - [Isolated readings with no real neighbours — SP](#isolated-readings-with-no-real-neighbours--sp-v11-sections-32-wire-break-and-33-spikes)
  - [Wire Break cascade — WB](#wire-break-cascade--wb-v11-section-32)
  - [Neighbour-average Spikes retest — SP](#neighbour-average-spikes-retest--sp-v11-section-33-gtspp-real-time-qc-manual)
  - [Speed check — PE / TE](#speed-check--pe--te-v11-section-424425)
  - [Probe-type check — PR](#probe-type-check--pr-v11-section-426)
  - [Physical-plausibility range check — RC](#physical-plausibility-range-check--rc)
  - [Position on Land — PL](#position-on-land--pl-gtspp-real-time-qc-manual-test-14)
- [Constants reference](#constants-reference)
- [What we tried and rejected](#what-we-tried-and-rejected)
- [Open questions](#open-questions)
- [References](#references)

## The two source documents

| | Edition | Authors | What it's cited for here |
|---|---|---|---|
| v1.1 | *Quality Control Cookbook for XBT Data*, CSIRO Marine Laboratories Report 221 (1994) | Bailey, Gronell, Phillips, Tanner & Meyers | Section-by-section descriptions, accept/reject codes, and every numeric threshold used below |
| v2.1 | *Australian XBT Quality Control Cookbook* (2022) | Cowley & Krummel, CSIRO | `XBT_fault_and_feature_flag_type`'s Appendix F bitmask values |

Both describe the same underlying phenomena; v1.1's prose is what's quoted
throughout, since it's the edition this pipeline had full text access to
while these checks were built. If you have the 2022 edition and its
thresholds differ from what's below, that's worth resolving — see
[Open questions](#open-questions).

## Checks at a glance

Every check below only **flags** — none of them correct, interpolate, or
delete a value. `HISTORY_PREVIOUS_VALUE` is always the fill for exactly this
reason: these checks never have a genuine "previous value" to report.
"Applies to" is which casts reach the check at all; "Appendix F bit" is
whether it's visible in the `XBT_fault_and_feature_flag_type` bitmask on its
own, independent of `HISTORY_QC_FLAG`.

| Code | Check | Applies to | Effect | Appendix F bit | Status |
|---|---|---|---|---|---|
| — | [Test Probe detection](#test-probe-detection-not-a-per-point-check-but-gates-everything-else) | All casts | Excludes self-test casts from output | `FAULT_TEST_PROBE` | ✅ Implemented (gate) |
| TP | ↳ failed self-test | Self-test casts only | Warning logged, not a QC flag | `FAULT_TEST_PROBE` | ✅ Implemented |
| CS | [Surface Transients](#surface-transients--cs-v21-section-431-supersedes-v11-section-21s-csa) | Real casts | TEMP → probably bad above 3.6 m, value retained | none | ✅ Implemented (current v2.1 methodology since NDO-729 — was shipping deprecated v1.1 CSA, [details](#surface-transients--cs-v21-section-431-supersedes-v11-section-21s-csa)) |
| CS (Reject / CSR) | ↳ transient below 3.6 m | — | — | — | ❌ Not implemented — needs operator judgement |
| SP | [Isolated readings](#isolated-readings-with-no-real-neighbours--sp-v11-sections-32-wire-break-and-33-spikes) (zero real neighbours) | All casts, incl. self-test | TEMP → probably bad | none | ✅ Implemented (narrowed scope) |
| WB | [Wire Break cascade](#wire-break-cascade--wb-v11-section-32) (unrecovered NaN run at cast end) | All casts, incl. self-test | Audit trail only — no `_qc` change | none | ✅ Implemented |
| SP | ↳ [neighbour-average "Spikes" formula, at GTSPP's 2.0°C threshold](#neighbour-average-spikes-retest--sp-v11-section-33-gtspp-real-time-qc-manual) | All casts, incl. self-test | TEMP → probably bad | none | ✅ Implemented — simplified formula, deliberately not GTSPP's literal two-term one (NDO-727, [details](#neighbour-average-spikes-retest--sp-v11-section-33-gtspp-real-time-qc-manual)) |
| PE + TE | [Speed check](#speed-check--pe--te-v11-section-424425) | Real casts, vs. previous real cast | LATITUDE/LONGITUDE/TIME/TEMP → probably bad, both codes together | `FAULT_POSITION_ERROR` + `FAULT_TIME_ERROR` | ✅ Implemented (both emitted, can't disambiguate) |
| PR | [Probe-type check](#probe-type-check--pr-v11-section-426) | Real casts | PROBE_TYPE/TEMP/DEPTH → probably bad from surface | `FAULT_PROBE_TYPE_ERROR` | ✅ Implemented |
| RC | [Range check](#physical-plausibility-range-check--rc) | All casts, per variable | Out-of-range points/profiles → probably bad | none | ✅ Implemented (TEMP depth-banded below 1500 m — [details](#physical-plausibility-range-check--rc)) |
| PL | [Position on Land](#position-on-land--pl-gtspp-real-time-qc-manual-test-14) (GTSPP/SeaDataNet/SAMOS, not CSIRO) | All casts, incl. self-test | LATITUDE/LONGITUDE → probably bad | none | ✅ Implemented |
| — | Wire Stretch (§4.4/4.5) | — | — | — | ❌ Not automated — usually still caught indirectly via `SOUND_VELOCITY` RC ([details](#what-we-tried-and-rejected)) |
| SPR | Severe multi-point spiking (§3.3) | — | — | — | ❌ Not implemented — see [Open questions](#open-questions) |

## Checks implemented

### Test Probe detection (not a per-point check, but gates everything else)

**Status:** Implemented. Runs first; determines whether every check below
even applies to a given cast.

A self-test cast (the recorder's own built-in calibration check, not a real
deployed probe) is identified by *any* of: "test" (case-insensitive) in the
Serial Number, Memo, or source filename; or the cookbook's own documented
signature (v1.1 section 2.2): isothermal at ~1.5±0.15°C **from the very
first (surface) sample**, for at least half the cast. The surface
requirement matters — a genuine Southern Ocean cast can be isothermal near
1-2°C at depth without being a test probe, but a real deployed probe's
surface reading is the actual sea-surface temperature, never suspiciously
pinned at 1.5°C.

Self-test casts are excluded from the published NetCDF, but a *failed*
self-test (temperature variation ≥0.005°C, `TEST_PROBE_MAX_TEMPERATURE_VARIATION_C`)
still gets a warning logged before it's dropped — the cookbook frames this
as a recorder-health alarm ("repeated failures can indicate poor earthing
or other system errors"), not a per-profile data flag, so it has to surface
somewhere other than a QC flag nobody downstream will ever read.

### Surface Transients — CS (v2.1 section 4.3.1, supersedes v1.1 section 2.1's CSA)

**Status:** Implemented, at the current (v2.1) methodology — **corrected 2026-09-11 (NDO-729)
after this pipeline shipped the wrong one for its whole history.** If you're extending this
check, read this whole section: it's a worked example of the exact "validate against real data,
don't trust a citation you haven't re-checked" discipline this document asks of every other
threshold, this time applied to a whole check's *methodology*, not just a number.

**What this pipeline used to do, and why it was wrong.** v1.1 (1994) describes the CSA (Accept)
code: "Surface data is removed to 3.7 m depth and replaced with 99.99 to indicate no data." This
pipeline implemented exactly that — for its whole history, until NDO-729 — permanently deleting
every real TEMP reading above the surface-spike depth on every real cast. That methodology is
**deprecated**. Quoted directly from v2.1 section 4.3.1 (Cowley & Krummel, CSIRO, 2022):

> "The CS Accept code is no longer in use... Since 2020/2021, the Australian QC group elected to
> retain the temperature surface values and apply a GTSPP flag 3 (Reject) to the surface
> transients from the surface to 3.6m. For historical Australian XBT data, the temperature data
> will be retrieved and GTSPP flag 3 applied retrospectively as profiles prior to 2020 are
> re-processed."

**What this pipeline does now.** Applied unconditionally to every real (non-test-probe) cast:
TEMP above **3.6 m** (not 3.7 m — see below) is flagged `GTSPP_PROBABLY_BAD` (matching v2.1's
GTSPP flag 3). The real value is **kept**, not deleted — it reaches every downstream check and
the published NetCDF unchanged. A shallow sample that's genuinely missing for an unrelated
reason (e.g. a `-99` sentinel) stays `GTSPP_MISSING`, not promoted to `PROBABLY_BAD` just because
it's also shallow. `DEPTH_VALUES` is untouched in both editions' methodology.

**Why 3.6 m, not 3.7 m.** v1.1's own text is internally ambiguous — it uses both 3.7 m and 3.9 m
in different places, "perhaps due to a mix of probe types" (v2.1's own words, describing the
1994 document). v2.1 resolves this to a single standard: 3.6 m.

**Real-data validation before shipping the fix**, per this document's own discipline: since this
check used to mutate raw data *before* every other TEMP check ran, none of them had ever seen a
real shallow value in this pipeline's history. Checked against the real 369-cast archive before
assuming un-masking was safe: 2,214 real shallow samples were being destroyed (physically sane
distribution, -1.92°C to 37.06°C, zero range-check violations among them); simulating the
un-masking against the neighbour-average spike test produced 375 new flags, every one of them
*inside* the region this check itself already flags (the classic transient shape — an
erroneously hot first reading rapidly settling, e.g. 24°C → 14°C → 12°C) — zero spillover into
genuinely deeper, previously-good data. Confirmed again after deploying the real fix: exactly
2,214 samples recovered, and the neighbour-average spike test's own real-archive flag count
(112 profiles) is unchanged from before this fix — the "new" flags found during design were
already accounted for, not a regression introduced by shipping it.

**Bonus find while deploying: this fix also recovers one entire real cast that this pipeline had
been wrongly excluding for its whole history**, via a second, independent interaction with
`is_test_probe_cast()`'s data-driven isothermal-near-1.5°C signal. That signal looks at the
*first non-`NaN`* sample, not literally index 0 — under the old destroy-the-shallow-data
methodology, a real cast's true surface reading could be masked away, leaving the "first
surviving" sample to be whatever the check happens to see below 3.7 m. For
`202425VT1_TDB_20241009000411.edf`: the true surface is a real -0.03°C reading (with a brief,
genuine transient spike to 11.78°C at 0.68 m before settling), followed by a real, cold,
near-isothermal ~1.49°C water column from 1.37 m onward — a real polar layer, not a test probe.
Under the old methodology, masking deleted the -0.03°C surface value, leaving the isothermal
~1.49°C layer as the "first surviving" sample — which coincidentally matched the test-probe
signature closely enough to wrongly exclude this genuine cast from the published archive.
Confirmed this is the *only* cast in the archive affected (checked systematically, not assumed)
before trusting the archive's profile count changing from 368 to 369 after this fix.

#### Not implemented: CSR (Reject variant)

The Reject variant (CSR — the transient detected *below* 3.6 m and judged to actually affect the
data) isn't implemented in either methodology. Distinguishing that from real near-surface thermal
structure needs the same kind of operator judgement call the cookbook itself requires to
disambiguate PE from TE (next section) — this pipeline doesn't have that input.

### Isolated readings with no real neighbours — SP (v1.1 sections 3.2 "Wire Break" and 3.3 "Spikes")

**Status:** Implemented — narrowed scope (zero-real-neighbours case only).

A real TEMP value is flagged if **both** immediate neighbours (one
shallower, one deeper) are missing. This is the shape of a wire-break or
end-of-cast fault: "a short circuit causes the temperature readings to go
off scale" (section 3.2), leaving a stray reading that survived alone past
the point the rest of the cast had already failed. Applied to every cast,
real or test-probe, same as the [range check](#physical-plausibility-range-check--rc)
below.

### Wire Break cascade — WB (v1.1 section 3.2)

**Status:** Implemented (NDO-728), audit-trail only.

The cookbook's own Wire Break text — quoted above in the "Isolated readings" section as
background — actually specifies its own check: "the bottom of the XBT profile exhibits a
sudden deflection to the high or low temperature end of the scale... Downgrade data to Class 4
from depth of initial point of damage." This pipeline cited that text for years without ever
automating it as its own check; NDO-728 does.

When a cast's TEMP record ends in a run of `NaN` samples with no real value recovering before
the cast's own end, one `WB` history entry is recorded, naming the run's depth range. **This
changes no `_qc` flag values at all** — every `NaN` sample already becomes `GTSPP_MISSING` via
`CastQC`'s own initialisation, before any check runs, so a terminal fault tail was already
correctly excluded from "good" data before this check existed. What WB adds is the audit
trail: a reader of `HISTORY_QC_FLAG` can now tell *why* a cast's tail is missing (a Wire
Break-shaped fault, cited to the cookbook) instead of an unexplained gap.

**Suggested by Alison Herbert (Senior Acoustics Officer, Polar Technology)**, after reviewing
the NDO-654/645 XBT self-test findings, in wording broader than what shipped: "as soon as
there is a `-99`, just discard the rest of the values in that profile." Checked against all
369 real casts' raw EDF files before automating anything, per this document's own real-data
discipline (see [What we tried and rejected](#what-we-tried-and-rejected)):

- 133 of 369 real casts have at least one `NaN` run in TEMP.
- 130 of those have a run that reaches the cast's own end with no recovery — the clean Wire
  Break shape this check targets.
- 44 of those 130 *also* have an earlier, separate `NaN` run that recovered before the cast
  ended (lengths 1-632 samples, with 8-685 real-looking samples still following before the
  cast's actual end).

Applying Alison's suggestion literally — discard from the *first* `-99` onward, recovery or
not — would have discarded most of some real casts over a single mid-cast dropout the
instrument evidently recovered from (one real case would lose 878 of its 1226 samples). Run
length alone doesn't separate a genuine terminal cascade from a recovered mid-cast blip either
— the shortest terminal cascade in the archive (4 samples) is the same length as a run that
went on to recover. The one signal the real data actually supports cleanly is the narrower
rule this check implements: does the cast's *last* `NaN` run reach the array's end? Mid-cast
recoveries are left alone; telling a genuinely recovered real reading apart from a suspicious
residual artifact near a fault (the harder question in Alison's original suggestion) isn't
attempted here.

**This check's justification is different in kind from every other check in this document —
say so plainly, don't let a future reader assume it's purely statistical.** The clearest real
example behind it, a self-test cast's continuous 43-second `-99` tail with zero recovery, was
traced to a **ship procedure**, not a data pattern discoverable from first principles: WinMK21's
self-test protocol has the operator at the junction box physically disconnect the alligator
clips *before* the operator at the PC presses "End Probe Drop" in the software, so the circuit
sits open — and the DAQ keeps recording that open-circuit state as `-99` — for however long
that gap takes. Every other check in this document is justified by the data and the physics
alone; this one's real-world justification also depends on knowing how the crew actually run
the test, which nothing about the raw EDF file itself can tell you.

### Neighbour-average Spikes retest — SP (v1.1 section 3.3 / GTSPP Real-Time QC Manual)

**Status:** Implemented, at GTSPP's 2.0°C threshold rather than the
cookbook's own 0.2°C — and deliberately using a simplified version of
GTSPP's formula, not the literal one. Read this whole section before
"fixing" the formula to match GTSPP exactly; NDO-727 investigated doing
exactly that and found it would be a real regression, not a correction.

The full formula section 3.3 actually describes: average a point's two real
immediate neighbours, flag it if it deviates from that average by more than
a threshold. **This was tried once already at the cookbook's literal 0.2°C
figure and dropped** — it fired on genuine real oceanographic fine-scale
structure, not faults (see [What we tried and
rejected](#what-we-tried-and-rejected)). It was never retried at a looser
threshold until NDO-708: the GTSPP Real-Time QC Manual (IOC Manuals &
Guides No. 22) publishes the same reference-average spike-test formula with
a **2.0°C** threshold instead. Re-run against the same real historical
archive at 2.0°C, it flagged **190 samples across 112 of 369 profiles**,
with **zero apparent false positives** on inspection — every flagged point
sits at the edge of a sharp, fault-shaped ramp (tens of degrees over one or
two samples), not the sub-1°C step structure that sank the 0.2°C attempt.

**What's actually shipped is `|V2 - avg(V1,V3)| > threshold`** —
GTSPP's own manual (IOC M&G No. 22, §2.7) specifies a more sophisticated
two-term formula, `|V2 - avg(V1,V3)| - |V1-V3|/2 > threshold`, where the
second term discounts the neighbours' *own* spread so a point sitting on a
real, steep gradient isn't penalised just for differing from its
neighbours' average. NDO-727 was filed to bring the shipped formula in
line with that literal text. **Investigated before implementing (per this
document's own real-data discipline) — and the literal fix was rejected**:
run against the real historical archive, the correct two-term formula
flags only **5** of the current 190 samples. The other 185 aren't false
positives being fixed; they're mostly points on a **smooth, near-linear
ramp** — the exact "Wire Stretch" fault shape below — which the two-term
formula is specifically designed *not* to flag (that's precisely what its
correction term does: tolerate a real, steep, but locally-linear
gradient). Checking further: of the fault-onset values behind those 185
flags, **65 would never be flagged by any check at all** under the
corrected formula — not in the window that currently (accidentally)
catches them, and not later either, since GTSPP's own formula is not
designed to catch a ramp shape by construction, only a true point spike.

**So the shipped formula's simplification is load-bearing, not a bug.**
It's doing two jobs GTSPP's own manual splits into two separate,
unautomated checks here: the genuine point-spike test (§3.3, what GTSPP's
formula is actually for) and Wire Stretch ramp detection (§4.4/4.5, listed
below as a real, found-in-practice, never-automated fault shape). The
simplified formula catches both as one side effect; the literal GTSPP
formula would only do the first, silently dropping the second. NDO-727 is
closed as won't-fix on that basis — implementing GTSPP's formula exactly
would trade a spec-compliance win for a real loss of fault detection. If
Wire Stretch ever gets its own dedicated, real-data-validated check (see
[Open questions](#open-questions)), *then* revisit whether the spike test
should switch to GTSPP's literal formula, since the ramp-catching side
effect would no longer be the only thing catching those faults.

Applied to every cast, real or test-probe, same as every other
physical-plausibility check; additive to (not a replacement for) the
zero-real-neighbours case above — that case still catches genuinely
isolated readings this formula can't evaluate (it needs two *real*
neighbours).

### Speed check — PE / TE (v1.1 section 4.2.4/4.2.5)

**Status:** Implemented — both codes emitted together, deliberately not
disambiguated.

Implausible ship speed (>25 knots, `MAX_PLAUSIBLE_SPEED_KNOTS`) implied
between two consecutive real casts' positions and launch times. The
cookbook treats a failed check as evidence of *either* a position error
(PE) or a time error (TE) — two distinct codes, two distinct metadata
targets — and is explicit that disambiguating them is an operator
judgement call made against log sheets and a track plot. An automated
check can't make that call, so **both** codes are emitted together, each
downgrading only its own metadata field (LATITUDE/LONGITUDE for PE,
TIME for TE), and both `HISTORY_QC_FLAG_DESCRIPTION`s say plainly the check
couldn't tell them apart. Both are Reject-variant consequences: TEMP
downgraded from the surface, DEPTH left alone (Table 2).

### Probe-type check — PR (v1.1 section 4.2.6)

**Status:** Implemented.

The EDF header's Probe Type field checked against the ship's actual stocked
probes. An unrecognised value downgrades TEMP *and* DEPTH from the surface
— DEPTH too, because depth is derived from the probe-specific fall-rate
equation, so a wrong probe type invalidates the whole depth axis, not just
the indexed temperatures (cites Cheng et al. 2016).

### Physical-plausibility range check — RC

**Status:** Implemented — the one check with no direct cookbook section of
its own; it enforces attributes both cookbooks assume readers already
validate.

Every published variable's own declared `valid_min`/`valid_max` enforced
against its *real* data, per-point for the depth-indexed variables (TEMP,
DEPTH, SOUND_VELOCITY) and per-profile for the scalars (LATITUDE,
LONGITUDE). This exists because it *didn't*, once: a resistance/connector
glitch wrote a literal `-99` fault sentinel into TEMP, and it published as
GTSPP flag 1 ("good") because TEMP had a correct `valid_min`/`valid_max`
attribute but nothing ever checked real data against it. The same audit
found LATITUDE/LONGITUDE had the same gap (declared but unenforced), and
DEPTH/SOUND_VELOCITY had no declared range at all. All four `_VALID_MIN`/
`_VALID_MAX` constants are the *single* source of truth for both the
NetCDF attribute and this check, specifically so metadata and enforcement
can't drift apart again — see [Constants reference](#constants-reference)
for the exact values and where each comes from.

#### TEMP is depth-banded, not one flat range (v1.1 section 2.4 "Profile Envelope")

**Status:** Implemented (NDO-705). TEMP's range check tightens below
**1500 m**: the flat -2.5..40°C bound still applies above that depth, but
below it the upper bound drops to **20.0°C**. This replaced a single flat
range check across the whole profile.

The cookbook's own "Profile Envelope" concept — a depth-varying plausible
range, tighter than the whole-profile bound — was only automated where the
real historical archive actually supports it. Checked at every depth band
before picking 1500 m specifically: the deep water this ship actually
operates in (Southern Ocean, not the tropics the flat 40°C bound is
sized for) shows a clean statistical gap only from 1500 m down — real data
tops out at **16.92°C** at that depth and below, while the nearest known
fault cluster starts at **32.04°C**, a wide enough margin that 20.0°C
sits safely in between with no risk of catching real structure. Shallower
than 1500 m, real near-surface/thermocline water genuinely reaches high
enough temperatures that a tighter bound isn't safe to draw from this
archive — the flat bound stays there. Don't reuse the 1500 m/20.0°C figures
for a different ship or region without re-running this same real-data check
against that ship's own archive — see [What we tried and
rejected](#what-we-tried-and-rejected).

### Position on Land — PL (GTSPP Real-Time QC Manual test 1.4)

**Status:** Implemented (NDO-704). **This check's provenance is different from every other
check in this document** — it does not trace back to the CSIRO XBT QC Cookbook at all. It's
cited from GTSPP's Real-Time QC Manual (IOC Manuals & Guides No. 22) test 1.4, and two
independent standards (SeaDataNet QC Procedures v2.0 §4, SAMOS netCDF manual Flag L) specify
the same check. Read the source text before assuming any check in this pipeline traces to the
CSIRO cookbook by default.

GTSPP's test 1.4 is, in its original form, a 1990s interactive check — it displays a track
chart and neighbouring stations for a human operator to confirm before acting. Its terminal
automated consequence, though, is narrow: when a position is confirmed on land, only the
latitude/longitude quality flags are set to "doubtful" — the sounding/temperature measurements
themselves are untouched, since a position error doesn't invalidate what was physically
measured, only where it claims to have been measured. This pipeline implements exactly that
consequence, automatically: a cast's launch position on land sets both `LATITUDE_qc` and
`LONGITUDE_qc` to `GTSPP_PROBABLY_BAD` and appends one `PL` history entry — `TEMP_qc`,
`DEPTH_qc`, and `SOUND_VELOCITY_qc` are never touched. Test 1.4 also cross-checks a reported
sounding against known bathymetry (its own rules 1.4.3/1.4.4) — not implemented here, since it
doesn't map onto XBT's temperature-profile data (not a bathymetric survey). The check only
evaluates a position the existing LATITUDE/LONGITUDE range check (RC, above) left at
`GTSPP_GOOD` — matching GTSPP's own prerequisite chain, where test 1.4 only runs once test 1.3
("Impossible Location") hasn't already fired.

Land/sea determination uses the `global-land-mask` PyPI package: pure numpy, no other
dependencies, a bundled 1km-resolution global land/sea grid (2.5MB compressed, no runtime
download). **Validated against all 368 real historical XBT launch positions before shipping**,
per this document's own real-data discipline — zero false positives, including casts close to
the Tasmanian coast at voyage start/end. Re-validated against the live production archive after
deploy: 0 of the same real casts get a `PL` entry, matching the pre-deploy validation exactly.

## Constants reference

Every threshold used above, with its exact value and where it comes from.
Defined once in `parse_xbt_edf.py` and imported everywhere else — this
table exists so a change to a threshold and a change to this document can
be checked against each other without reading the source.

| Constant | Value | Used by | Source |
|---|---|---|---|
| `TEST_PROBE_ISOTHERMAL_CENTER_C` | 1.5°C | Test Probe detection | Cookbook signature, v1.1 §2.2 |
| `TEST_PROBE_ISOTHERMAL_TOLERANCE_C` | ±0.15°C | Test Probe detection | Cookbook signature, v1.1 §2.2 |
| `TEST_PROBE_ISOTHERMAL_MIN_FRACTION` | 0.5 (half the cast) | Test Probe detection | Cookbook signature, v1.1 §2.2 |
| `TEST_PROBE_MAX_TEMPERATURE_VARIATION_C` | 0.005°C | Failed self-test warning (TP) | Cookbook, v1.1 §2.2 |
| `SURFACE_TRANSIENT_DEPTH_M` | 3.6 m | Surface Transients (CS) | Cookbook v2.1 §4.3.1 — resolves v1.1's own internal 3.7/3.9 m ambiguity |
| `MAX_PLAUSIBLE_SPEED_KNOTS` | 25.0 kn | Speed check (PE/TE) | Cookbook, v1.1 §4.2.4/4.2.5 |
| `TEMP_VALID_MIN` / `TEMP_VALID_MAX` | −2.5°C / 40.0°C | Range check (RC), depths < 1500 m | Cookbook/GTSPP convention |
| `TEMP_DEEP_BAND_DEPTH_M` | 1500.0 m | Range check (RC) — depth-band boundary | Real-data statistical gap, this ship's own archive (see [above](#temp-is-depth-banded-not-one-flat-range-v11-section-24-profile-envelope)) |
| `TEMP_DEEP_VALID_MAX` | 20.0°C | Range check (RC), depths ≥ 1500 m | Real-data statistical gap, this ship's own archive — same source as `TEMP_DEEP_BAND_DEPTH_M` |
| `SPIKE_NEIGHBOUR_AVERAGE_MAX_DELTA_C` | 2.0°C | Neighbour-average Spikes retest (SP) | GTSPP Real-Time QC Manual (IOC M&G No. 22), not the cookbook's own 0.2°C (rejected — see [below](#what-we-tried-and-rejected)) |
| `LATITUDE_VALID_MIN` / `_MAX` | −90° / 90° | Range check (RC) | Physical bound |
| `LONGITUDE_VALID_MIN` / `_MAX` | −180° / 180° | Range check (RC) | Physical bound |
| `DEPTH_VALID_MIN` | −1.0 m | Range check (RC) | Small negative slop for near-surface sensor/calibration noise |
| `DEPTH_VALID_MAX` | 2500.0 m | Range check (RC) | MK21 ISA manual's deepest-rated ship-stocked probe (T-5, 1830 m) plus margin — not a guess |
| `SOUND_VELOCITY_VALID_MIN` / `_MAX` | 1400 / 1560 m/s | Range check (RC) | Pragmatic engineering bound, validated against real historical data (see [below](#what-we-tried-and-rejected)) rather than invented |

## What we tried and rejected

**The single most important thing in this document, if you're extending
this pipeline for your own instrument or ship:** a threshold that looks
reasonable on paper can still be wrong, and the only way to find out is to
run it against your *own* real historical archive before shipping it.

The "Spikes" check originally compared every point against the average of
its two immediate neighbours and flagged a >0.2°C deviation — the standard
formula, straight from the cookbook's own numeric threshold. Run against
the full 368-profile real historical archive this pipeline was built
against, it fired **97 times**, and inspection showed most of those were
**genuine real oceanographic fine-scale structure** — small step-like
temperature wiggles at 400-600 m depth, exactly what v1.1 section 5
("Structure / Signal Leakage Flags") describes as a real feature caused by
small-scale mixing, *not* a fault:

```
idx=666 depth=421.45 temp=1.92
idx=667 depth=422.07 temp=1.83
idx=668 depth=422.69 temp=1.74
idx=669 depth=423.31 temp=1.54   <- flagged, real data
idx=670 depth=423.93 temp=1.76
idx=671 depth=424.55 temp=1.93   <- flagged, real data
idx=672 depth=425.16 temp=1.68
```

Section 5's own text is explicit that telling this apart from a
malfunction needs "verification with neighbouring (or repeat) profiles and
previous knowledge of the region" — an input this pipeline doesn't have,
same as the CSR/PE-TE cases above. The check was narrowed to only the
zero-real-neighbours case (no threshold, no false positives on real
structure — an isolated point with nothing on either side to compare
against at all is unambiguous, regardless of its value), re-validated
against the same 368 profiles: **23 genuine faults, all 30-37°C at depths
where that's physically impossible for real Southern Ocean water, zero
apparent false positives.**

**Retried later (NDO-708) at a looser threshold, and this time it shipped.**
The 0.2°C figure above is the *cookbook's* number; the GTSPP Real-Time QC
Manual (IOC M&G No. 22) publishes the same reference-average formula with
its own, much looser **2.0°C** threshold. That had never been tried — the
check was dropped once at 0.2°C and never revisited at a different value.
Run against the full 369-profile archive (one profile more than the 368
above; the archive had grown by then) at 2.0°C: **190 flagged samples
across 112 profiles, zero apparent false positives** on inspection of every
flag's margin above threshold — even the closest-to-threshold flags
(e.g. a jump from 9.46°C to 16.09°C to 18.71°C at 904 m, deviation
2.005°C) are sharp fault-onset ramps, not the sub-1°C fine-scale structure
that sank the 0.2°C attempt. See [Neighbour-average Spikes
retest](#neighbour-average-spikes-retest--sp-v11-section-33-gtspp-real-time-qc-manual)
above for the shipped implementation.

**The TEMP range check's depth-banded envelope (NDO-705) went through the
same real-data-first process.** The cookbook's "Profile Envelope" concept
(section 2.4) suggests a depth-varying plausible range is possible in
principle, but doesn't hand over exact numbers to use. Checked band-by-band
against the same archive before picking anything: only the deepest band
(≥1500 m) shows a clean gap between real data (max 16.92°C at those
depths) and the nearest known fault cluster (starting at 32.04°C) — wide
enough to draw a bound through safely. No other depth band showed a
comparably clean gap in this ship's own data, so no other band got
tightened. **The lesson generalises past both of these specific checks:**
a plausible-looking bound (a rounder number, a value from a different
ship's cookbook, a threshold from a different ocean) is not evidence it's
safe for *this* ship's data — only running it against the real archive is.

**"Wire Stretch" (v1.1 sections 4.4/4.5)** — a sustained, real-looking
warming trend with depth over a wide range — was found in the same real
data (a ~7°C ramp over 33 m, suspiciously close to perfectly linear) but
was **not automated at all**. The cookbook's own text is explicit that
telling a genuine wire stretch apart from a real temperature inversion
needs neighbour/repeat-drop confirmation: "the approach... is to be
conservative... only those features that have been confirmed... are
flagged as real." A cast with this fault shape isn't left completely
unflagged in practice, though — `SOUND_VELOCITY` is computed from the same
corrupted temperature, so it usually still trips the existing
`SOUND_VELOCITY` range check (confirmed: it did, in the found case). TEMP
itself also usually catches it, but only by accident: NDO-727 confirmed
the shipped neighbour-average spike check's simplified formula is what
does that (see [above](#neighbour-average-spikes-retest--sp-v11-section-33-gtspp-real-time-qc-manual))
— the literal GTSPP formula it was checked against leaves 65 of the real
archive's fault-onset points uncaught by any TEMP check at all, since it's
specifically designed not to flag a smooth real-looking gradient, which is
exactly what a wire-stretch ramp looks like locally.

**Validate against real data, always, before trusting a threshold you
haven't run against anything but the cookbook's own worked examples.**

**A climatology-aware bounds check (NDO-686), modelled on ioos_qc's
`climatology_test`, was investigated for TEMP using NOAA's World Ocean
Atlas (WOA23) and found not viable for this ship's operating region —
closed as won't-fix, not implemented.** The idea: replace the flat,
unvalidated shallow-band TEMP bound (-2.5..40°C) with a per-location,
per-season expected range derived from WOA's `t_an` (climatological mean)
and `t_sd` (standard deviation), the same statistical-bound approach
`TEMP_DEEP_VALID_MAX` and the spike-test threshold were validated with.
Two real, sequential findings killed it:

1. **WOA's monthly climatology is too sparsely sampled here to trust its
   own `t_sd`.** Checked against this ship's real operating envelope
   (369 casts, -70° to -41° latitude): the median WOA grid cell has only
   **3** historical observations behind it, and 43% of real (cast, depth)
   lookups land in a cell built from 2 or fewer. A standard deviation
   computed from 2 profiles is not a real measure of variability — it
   produced flagged "deviations" of thousands of standard deviations from
   completely ordinary temperature readings. WOA's *annual* climatology
   (pooling all months) fixed this specifically — median observation
   count rose to 48 — but that's a sample-size fix, not the real problem.
2. **Even with adequate sample counts, a fixed climatological mean is a
   poor "expected value" in this region.** Southern Ocean fronts (the
   Antarctic Circumpolar Current's Subantarctic and Polar Fronts
   specifically) meander by degrees of latitude from their long-term
   average position on any given voyage. A real, correct cast taken on
   the warm side of where WOA's multi-decade average places a front will
   legitimately read many WOA standard deviations away from the
   climatological mean, with nothing wrong with the data. Confirmed
   directly: one real cast (54.5°S, 119.5°E) had ~600 consecutive points
   of smooth, physically sane 9-13°C water column flagged purely because
   WOA's climatological mean at that exact cell is 2-3°C — a real ~7-10°C
   offset from a shifted front, not a fault. Net result across the real
   archive: **42.5% of real casts (136 of 320) had at least one point
   flagged** at a threshold (6 standard deviations) already far looser
   than every other check in this document — several orders of magnitude
   worse than any other check's false-positive rate.

The one genuine fault this investigation did surface in that same example
cast (a 32.6°C jump at 637m, an obvious sensor fault) was already caught
independently by the existing neighbour-average spike check — WOA added
nothing there but the 600 false positives above it. **The lesson
generalises past this specific attempt:** an external reference dataset
being authoritative and well-documented (WOA is both) is not the same as
it being *the right kind of reference* for a specific check in a specific
region — a static climatological mean is fundamentally the wrong tool
where the dominant source of real variability is large, mobile, mesoscale
structure rather than smooth seasonal change. This finding likely
generalises to the underway-merger pipeline's own gross-range bounds too
(same ship, same region, same frontal dynamics) — not separately tested,
but worth checking before attempting the same approach there.

## Open questions

- **Resolved 2026-09-11 (NDO-729):** whether the 2022 edition (v2.1) revises the surface-spike
  depth, and whether it retired or consolidated any 1994-edition codes — yes, on both counts, and
  materially. v2.1 both revises the depth (3.7/3.9 m's own internal ambiguity resolved to 3.6 m)
  *and* retires the entire CSA methodology this pipeline used to implement (destroying the
  surface data) in favour of flagging it instead — see the [Surface
  Transients](#surface-transients--cs-v21-section-431-supersedes-v11-section-21s-csa) section
  above for the fix. v2.1's own "Historical QC codes no longer used" section (4.8) lists the
  broader set of 1994-era codes it retired — not yet cross-checked against this pipeline's other
  citations one by one; worth doing if another check's methodology is ever in question the way
  this one was. (The 0.2°C neighbour-average threshold question this entry used to also cover was
  already resolved separately — this pipeline ships GTSPP's own 2.0°C figure for that check
  instead of the cookbook's, so a 2022-edition revision to 0.2°C specifically wouldn't change what
  ships either way.)
- "Wire Stretch" and the multi-point "severe spiking... over a wide range
  of depths" case (v1.1 section 3.3, Reject code SPR) are both real,
  found-in-practice fault shapes with no *dedicated* automated check yet —
  see [What we tried and rejected](#what-we-tried-and-rejected). The
  neighbour-average spike retest (above) catches wire-stretch as a side
  effect at any point along a steep-enough ramp, but that's incidental, not
  a purpose-built detector for the shape as a whole — and NDO-727 confirmed
  it's load-bearing, not just incidental: closed as won't-fix rather than
  "corrected" to GTSPP's literal formula, specifically because that literal
  formula stops catching this shape. If Wire Stretch ever gets its own
  dedicated check, revisit whether the spike test should switch to GTSPP's
  literal formula at that point.

## References

- Bailey, R., Gronell, A., Phillips, H., Tanner, E. & Meyers, G. (1994).
  *Quality Control Cookbook for XBT Data*, CSIRO Marine Laboratories
  Report 221, v1.1.
- Cowley, R. & Krummel, S. (2022). *Australian XBT Quality Control
  Cookbook*, v2.1, CSIRO.
- IMOS NetCDF Conventions, v1.4 (July 2015) — the ancillary
  `<PARAM>_quality_control` variable convention this pipeline's output
  follows.
