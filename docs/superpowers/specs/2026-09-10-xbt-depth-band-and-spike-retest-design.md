# XBT QC — Depth-Banded TEMP Envelope and Spike-Test Retry at GTSPP's 2.0°C

**Status:** Approved by Peter, 2026-09-10.

## Background

A comparison of this pipeline's QC against external standards documents (SeaDataNet, GTSPP,
SAMOS, OceanSITES) found two real gaps in `parse_xbt_edf.py`, filed as
[NDO-705](https://ausantarctic.atlassian.net/browse/NDO-705) and
[NDO-708](https://ausantarctic.atlassian.net/browse/NDO-708) under the NDO-645 Epic:

- **TEMP's range check (RC) is one flat bound for the whole cast** (`TEMP_VALID_MIN = -2.5`,
  `TEMP_VALID_MAX = 40.0`), when GTSPP §2.4 specifies a depth-banded envelope that tightens with
  depth.
- **The rejected neighbour-average spike test was only ever validated at the CSIRO cookbook's
  0.2°C threshold** (97 false positives on real fine-scale structure, documented in
  `QC_COOKBOOK.md`'s "What we tried and rejected"), never at GTSPP's own, ~10x looser, 2.0°C
  figure for the same formula.

Both were investigated against the real 369-profile historical archive
(`~/Downloads/xbt_historical_profiles.nc`) before any design was proposed, per this repo's own
"validate against real data, always" rule.

## Real-data investigation — what actually held up

**NDO-705 turned out narrower than originally scoped, and that's a real finding, not a
compromise.** Checked TEMP-vs-depth across every currently-good sample, banded: at depths ≥1500m
there's a clean, wide gap in real values (nothing above 16.92°C, then nothing again until 32.04°C
— clearly the same already-known fault class described elsewhere in this file). At shallower
depths (0–1500m) there is **no clean gap** — traced one borderline case by hand (profile 182,
728.8m: a real, smooth 12.12°C→16.1°C rise over 7m, a genuine front/eddy, not a fault) and
confirmed genuine regional warm-water variability sits on a continuum with the fault values all
the way up to 37°C at every shallower band. A static range check cannot separate them there
without either missing real faults or clipping real oceanography. Scope narrowed accordingly (see
Design §1).

**NDO-708 validated cleanly, and turned up a bonus.** Ran the standard 3-point neighbour-average
formula (`|center - (left+right)/2| > threshold`) against the full archive at 2.0°C (after
simulating the CS surface-spike fix on the stale local archive copy, which predated it — confirmed
by checking depth_idx 0 was non-NaN in all 369 profiles when it should already be NaN below 3.7m
in production). Result: 190 point-flags across 112 of 369 profiles (30%). Inspected the shape
around every flagged point (a window of the ±4 surrounding samples): every one is either a genuine
isolated spike near a cast's end, or a **smooth-but-physically-impossible ramp** (e.g. −1.4°C to
35°C over 5m of depth) — the exact signature of the "wire-stretch" fault class this file's own
`QC_COOKBOOK.md` already documents as found-but-never-automated. Zero apparent false positives on
real structure, unlike the 0.2°C threshold's 97. The formula flags a monotonic ramp just as
readily as a true spike (each point along a steep ramp still deviates from its neighbours'
average), so this check gets wire-stretch detection as a side effect without any extra code.

## Scope

**In scope:** `parse_xbt_edf.py` (new `_flag_temperature_out_of_depth_band_range` function
replacing the flat TEMP call inside `_flag_array_out_of_range`'s use for TEMP specifically; new
`_flag_neighbour_average_spikes` function, additive to the existing `_flag_spikes` isolated-only
check, not a replacement for it). `QC_COOKBOOK.md` updated to document both checks (per Peter's
explicit ask) — this is the living design record for every check this pipeline implements, and
both of these are exactly what it exists to document.

**Explicitly out of scope:**
- Tightening TEMP's shallow/mid-depth (0–1500m) bounds — the real-data investigation found no
  clean, defensible way to do this with a static range check; see Background.
- The "severe multi-point spiking" (SPR) case and any further wire-stretch refinement beyond what
  the 2.0°C spike test catches as a side effect — still explicitly not automated, per the same
  reasoning `QC_COOKBOOK.md` already gives (needs neighbour/repeat-cast confirmation this pipeline
  doesn't have).
- Re-deriving `DEPTH_VALID_MIN`/`MAX`, `SOUND_VELOCITY_VALID_MIN`/`MAX`, or any other existing
  bound — this spec only touches TEMP's range check and adds the new spike check.

## Design

### 1. Depth-banded TEMP envelope (NDO-705)

Two bands, not the flat single range TEMP currently uses via `_flag_array_out_of_range`:

```python
TEMP_VALID_MIN = -2.5   # unchanged, applies to the whole cast (0-1500m band)
TEMP_VALID_MAX = 40.0   # unchanged, applies to the whole cast (0-1500m band)
TEMP_DEEP_BAND_DEPTH_M = 1500.0
TEMP_DEEP_VALID_MAX = 20.0  # applies only at depth >= TEMP_DEEP_BAND_DEPTH_M
```

`TEMP_VALID_MIN`/`_MAX` keep their current names and values deliberately (least churn to an
already-cited, already-tested pair of constants) and now implicitly mean "the 0–1500m band, and
the floor for the deep band too" (the deep band only tightens the *upper* bound — nothing in the
real-data investigation suggested the *lower* bound needs to differ by depth). A new
`_flag_temperature_out_of_depth_band_range(qc, temp, depth_m, now)` function replaces the plain
`_flag_array_out_of_range(qc, qc.temperature_qc, cast.temperature_c, TEMP_VALID_MIN,
TEMP_VALID_MAX, "TEMP", "degC", now)` call in `apply_qc()` — same RC flag/history-entry mechanics
(`qc_flag="RC"`), just a per-point depth-dependent `valid_max` instead of one constant, and two
separate `HistoryEntry` appends when both bands produce a finding (so `HISTORY_QC_FLAG_DESCRIPTION`
correctly cites which band's bound a given depth range violated, rather than a single ambiguous
entry). `DEPTH_VALUES`/`SOUND_VELOCITY`'s own `_flag_array_out_of_range` calls are unchanged — only
TEMP gets banding.

### 2. Neighbour-average spike test at 2.0°C (NDO-708)

**`_flag_neighbour_average_spikes(qc, now, threshold=SPIKE_NEIGHBOUR_AVERAGE_MAX_DELTA_C) -> None`**
— for every real (non-NaN) TEMP sample with two real (non-NaN) immediate neighbours, flags it
`GTSPP_PROBABLY_BAD` if it deviates from the average of those two neighbours by more than the
threshold. Standard 3-point spike-test formula (GTSPP Real-Time QC Manual, IOC M&G No. 22 —
already cited in this repo's earlier work comparing against that manual).

```python
# GTSPP Real-Time QC Manual (IOC M&G No. 22)'s own spike-test threshold --
# 10x looser than the CSIRO cookbook's 0.2 degC, which this pipeline already
# tried and rejected for 97 false positives on real fine-scale ocean
# structure (see QC_COOKBOOK.md's "What we tried and rejected"). Validated
# against the full 369-profile historical archive before trusting it
# (2026-09-10): 190 point-flags across 112 profiles, zero apparent false
# positives on real structure -- every flagged point is either a genuine
# isolated spike or a physically-impossible smooth ramp (the "wire-stretch"
# fault class this file already documents as found-but-not-automated,
# caught here as a side effect of the same formula).
SPIKE_NEIGHBOUR_AVERAGE_MAX_DELTA_C = 2.0
```

Applied to every cast (real or test-probe), same as the existing isolated-spike check
(`_flag_spikes`) and the range check — additive, not a replacement: a sample can be caught by
either check independently (a fully-isolated sample with NaN on both sides is never evaluated by
this new check at all, since it requires two real neighbours; `_flag_spikes` is exactly the
complementary case). Composes into `qc.temperature_qc` the same way every other TEMP check does —
`qc_array[flagged] = GTSPP_PROBABLY_BAD` is idempotent against an already-bad flag, no `np.maximum`
needed (this file doesn't use that composition pattern the way `underway_merger_qc.py` does; a
flat assignment is this file's existing convention, confirmed by reading `_flag_array_out_of_range`
and `_flag_spikes` above).

One new `HistoryEntry` per cast when this check fires (`qc_flag="SP"`, reusing the same code the
isolated-spike check already uses — GTSPP's `XBT_fault_and_feature_flag_type` Appendix F has no
separate bit for this narrower formula, and both checks describe the same underlying "Spikes"
cookbook section, so sharing the flag code is consistent with the existing SP entry's own citation
rather than inventing a new one). `apply_qc()`'s own docstring, which enumerates the maximum
possible `HISTORY_*` entry count per cast, needs its count updated (+1) to account for this new
entry alongside the existing checks — `build_xbt_netcdf.py`'s `_N_HISTORY` ceiling constant needs
the same +1 (mirrors how the surface-spike (CS) check's addition bumped it 8→9, and the
neighbour-confirmed isolated-spike (SP) check's own addition bumped it 9→10, per this repo's own
prior history).

### 3. `QC_COOKBOOK.md` update

Per Peter's explicit ask: update the "Checks at a glance" table, add a "Physical-plausibility range
check — RC" sub-section note about the new depth band, and update the "Isolated readings with no
real neighbours — SP" section (or add a new sibling section) documenting the neighbour-average
spike test's real validation result — including correcting the existing "🔬 Tried, rejected (97
false positives...)" table row, since it's no longer simply rejected, just rejected *at 0.2°C
specifically*. The "What we tried and rejected" section's own prose needs the same correction: it
currently reads as a closed question ("only the zero-real-neighbours case ships"); this spec
reopens it at the GTSPP threshold with a positive result, which the document should say plainly
rather than leaving the older, now-superseded conclusion standing uncorrected.

## Testing

- Unit tests for `_flag_temperature_out_of_depth_band_range`: within-band-good, within-band-bad
  (shallow), deep-band-good, deep-band-bad (a value that would pass the shallow bound but fails
  the deep one), a cast spanning both bands producing two separate `HistoryEntry` appends.
- Unit tests for `_flag_neighbour_average_spikes`: agreeing neighbours (not flagged), a real spike
  exceeding threshold (flagged), a value just under threshold (not flagged), one-neighbour-missing
  (not evaluated, matches the fully-isolated case being `_flag_spikes`'s job instead), a synthetic
  ramp (multiple consecutive points each individually exceeding threshold, confirming the
  side-effect wire-stretch detection works as validated).
- A real-data regression run against the full historical archive after implementation, confirming
  the same 190/112 figures (or documenting why they changed, if the CS-simulation workaround used
  during design differs from the real committed pipeline's actual current output).

## Deferred to v2+

- Tightening shallow/mid-depth TEMP bounds, if a future technique (e.g. neighbour/repeat-cast
  cross-referencing) can separate real fronts from faults there.
- The SPR "severe multi-point spiking" case and any further wire-stretch-specific refinement.
