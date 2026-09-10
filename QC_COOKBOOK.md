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
  - [Surface Spikes — CS](#surface-spikes--cs-v11-section-21)
  - [Isolated readings with no real neighbours — SP](#isolated-readings-with-no-real-neighbours--sp-v11-sections-32-wire-break-and-33-spikes)
  - [Neighbour-average Spikes retest — SP](#neighbour-average-spikes-retest--sp-v11-section-33-gtspp-real-time-qc-manual)
  - [Speed check — PE / TE](#speed-check--pe--te-v11-section-424425)
  - [Probe-type check — PR](#probe-type-check--pr-v11-section-426)
  - [Physical-plausibility range check — RC](#physical-plausibility-range-check--rc)
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
| CS (Accept / CSA) | [Surface Spikes](#surface-spikes--cs-v11-section-21) | Real casts | TEMP → missing above 3.7 m | none | ✅ Implemented |
| CS (Reject / CSR) | ↳ transient below 3.7 m | — | — | — | ❌ Not implemented — needs operator judgement |
| SP | [Isolated readings](#isolated-readings-with-no-real-neighbours--sp-v11-sections-32-wire-break-and-33-spikes) (zero real neighbours) | All casts, incl. self-test | TEMP → probably bad | none | ✅ Implemented (narrowed scope) |
| SP | ↳ [full neighbour-average "Spikes" formula, at GTSPP's 2.0°C](#neighbour-average-spikes-retest--sp-v11-section-33-gtspp-real-time-qc-manual) | All casts, incl. self-test | TEMP → probably bad | none | ✅ Implemented (retried at GTSPP's threshold, not the cookbook's 0.2°C — [details](#what-we-tried-and-rejected)) |
| PE + TE | [Speed check](#speed-check--pe--te-v11-section-424425) | Real casts, vs. previous real cast | LATITUDE/LONGITUDE/TIME/TEMP → probably bad, both codes together | `FAULT_POSITION_ERROR` + `FAULT_TIME_ERROR` | ✅ Implemented (both emitted, can't disambiguate) |
| PR | [Probe-type check](#probe-type-check--pr-v11-section-426) | Real casts | PROBE_TYPE/TEMP/DEPTH → probably bad from surface | `FAULT_PROBE_TYPE_ERROR` | ✅ Implemented |
| RC | [Range check](#physical-plausibility-range-check--rc) | All casts, per variable | Out-of-range points/profiles → probably bad | none | ✅ Implemented (TEMP depth-banded below 1500 m — [details](#physical-plausibility-range-check--rc)) |
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

### Surface Spikes — CS (v1.1 section 2.1)

**Status:** Implemented — Accept (CSA) case only.

> "Surface spikes are caused by a minor start-up transient problem that
> leads to inaccurate temperature measurements in the top few metres... The
> CSA flag is applied to all XBT profiles in which the surface spike is
> undetectable below 3.7 m depth, as the start-up transient problem is
> ubiquitous... Surface data is removed to 3.7 m depth and replaced with
> 99.99 to indicate no data."

Applied unconditionally to every real (non-test-probe) cast: TEMP above
**3.7 m** is set to `NaN` (→ `GTSPP_MISSING` once written). Not a per-cast
judgement call — the cookbook's own framing is that this is universal
housekeeping, not a defect being flagged.

#### Not implemented: CSR (Reject variant)

The Reject variant (CSR — the transient detected *below* 3.7 m and judged
to actually affect the data) isn't implemented. Distinguishing that from
real near-surface thermal structure needs the same kind of operator
judgement call the cookbook itself requires to disambiguate PE from TE
(next section) — this pipeline doesn't have that input.

### Isolated readings with no real neighbours — SP (v1.1 sections 3.2 "Wire Break" and 3.3 "Spikes")

**Status:** Implemented — narrowed scope (zero-real-neighbours case only).

A real TEMP value is flagged if **both** immediate neighbours (one
shallower, one deeper) are missing. This is the shape of a wire-break or
end-of-cast fault: "a short circuit causes the temperature readings to go
off scale" (section 3.2), leaving a stray reading that survived alone past
the point the rest of the cast had already failed. Applied to every cast,
real or test-probe, same as the [range check](#physical-plausibility-range-check--rc)
below.

### Neighbour-average Spikes retest — SP (v1.1 section 3.3 / GTSPP Real-Time QC Manual)

**Status:** Implemented, at GTSPP's 2.0°C threshold rather than the
cookbook's own 0.2°C.

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
As a side effect, this formula also catches the "Wire Stretch" fault shape
(below) at any point along a steep-enough ramp, since each such point
deviates from its neighbours' average too — not by design, but confirmed
useful in practice. Applied to every cast, real or test-probe, same as
every other physical-plausibility check; additive to (not a replacement
for) the zero-real-neighbours case above — that case still catches
genuinely isolated readings this formula can't evaluate (it needs two
*real* neighbours).

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
| `SURFACE_SPIKE_DEPTH_M` | 3.7 m | Surface Spikes (CS) | Cookbook, v1.1 §2.1 |
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
`SOUND_VELOCITY` range check (confirmed: it did, in the found case).

**Validate against real data, always, before trusting a threshold you
haven't run against anything but the cookbook's own worked examples.**

## Open questions

- Whether the 2022 edition (v2.1) retired or consolidated any of the 1994
  edition's ~30 flag categories (Appendix A), and whether it revises the
  3.7 m surface-spike depth used above — unresolved as of this writing; a
  question is out to one of the 2022 edition's co-authors. (The 0.2°C
  neighbour-average threshold this question used to also cover is no
  longer open in the same sense — this pipeline now ships GTSPP's own 2.0°C
  figure for that check instead of the cookbook's, so a 2022-edition
  revision to 0.2°C specifically wouldn't change what's shipped either way.)
- "Wire Stretch" and the multi-point "severe spiking... over a wide range
  of depths" case (v1.1 section 3.3, Reject code SPR) are both real,
  found-in-practice fault shapes with no *dedicated* automated check yet —
  see [What we tried and rejected](#what-we-tried-and-rejected). The
  neighbour-average spike retest (above) catches wire-stretch as a side
  effect at any point along a steep-enough ramp, but that's incidental,
  not a purpose-built detector for the shape as a whole.

## References

- Bailey, R., Gronell, A., Phillips, H., Tanner, E. & Meyers, G. (1994).
  *Quality Control Cookbook for XBT Data*, CSIRO Marine Laboratories
  Report 221, v1.1.
- Cowley, R. & Krummel, S. (2022). *Australian XBT Quality Control
  Cookbook*, v2.1, CSIRO.
- IMOS NetCDF Conventions, v1.4 (July 2015) — the ancillary
  `<PARAM>_quality_control` variable convention this pipeline's output
  follows.
