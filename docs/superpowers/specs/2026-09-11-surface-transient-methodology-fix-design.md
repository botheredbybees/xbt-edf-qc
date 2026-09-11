# Surface Transient (CS) Methodology Fix — Design

Status: Approved

## Goal

Fix `parse_xbt_edf.py`'s Surface Transients check (`CS`), which currently implements the
**deprecated** 1994 (v1.1) methodology — destroying real near-surface TEMP data — instead of
the current, authoritative 2022 (v2.1) methodology, which keeps the real value and only flags
it. This recovers 2,214 real temperature samples across the 369-cast historical archive that
this pipeline has been permanently discarding.

## Background

Found while cross-referencing `QC_COOKBOOK.md` against the actual CSIRO cookbook PDFs directly
(both now available: `Quality-Control-Cookbook-for-XBT-data-Bai1994a.pdf`, v1.1, and
`EP2022-1825.pdf`, "Australian XBT Quality Control Cookbook Version 2.1", Cowley & Krummel,
CSIRO 2022). `_remove_surface_spike()` currently mutates `cast.temperature_c` to `NaN` for
`depth < SURFACE_SPIKE_DEPTH_M` (3.7m), which `CastQC.__post_init__` then turns into
`GTSPP_MISSING` — permanently destroying the value. The code's own comment cites this as "the
cookbook['s]" approach and quotes v1.1's own wording almost verbatim ("removed ... and replaced
with 99.99 to indicate no data").

Reading v2.1 section 4.3.1 directly, quoted from the source:

> "The CS Accept code is no longer in use... Since 2020/2021, the Australian QC group elected to
> retain the temperature surface values and apply a GTSPP flag 3 (Reject) to the surface
> transients from the surface to 3.6m. For historical Australian XBT data, the temperature data
> will be retrieved and GTSPP flag 3 applied retrospectively as profiles prior to 2020 are
> re-processed."

Two corrections follow directly from this text:

1. **Behavior**: keep the real value, flag `TEMP_qc` as `GTSPP_PROBABLY_BAD` (GTSPP flag 3)
   instead of destroying it. `DEPTH_qc` is untouched in both editions' methodology — unchanged.
2. **Depth**: v2.1 resolves the 1994 document's own internal ambiguity (it used both 3.7m and
   3.9m in different places, "perhaps due to a mix of probe types") to a single standard of
   3.6m.

This also directly answers `QC_COOKBOOK.md`'s own "Open questions" entry about whether the 2022
edition revises anything from 1994 — it does, materially, for this exact check.

## Real-data risk investigated before designing further

`_remove_surface_spike` currently runs *before* `CastQC` construction, mutating raw
`cast.temperature_c` in place — every other TEMP check in `apply_qc()` (the depth-banded range
check, both spike checks, the Wire Break cascade) has therefore never seen a real near-surface
(<3.7m) value in this pipeline's whole history, since it was always already `NaN` by the time
they ran. If CS stops destroying the data, those checks see real shallow data for the first
time. Checked against the real 369-cast archive (raw, pre-masking values) before assuming this
is safe:

- 2,214 real shallow (<3.6m) TEMP samples currently destroyed. Distribution is physically sane:
  min -1.92°C, max 37.06°C, mean 7.48°C, median 4.63°C — no RC (range-check) violations among
  them at all.
- Simulated un-masking against the neighbour-average spike test: 375 new flags appear, but
  **every single one is inside the region CS itself already flags** (depths 0.6-1.4m, showing
  the classic start-up-transient shape — an erroneously hot first reading rapidly settling,
  e.g. 24°C → 14°C → 12°C). Zero new flags appear on data *outside* the shallow region — no
  spillover into genuinely deeper, previously-good data.
- The Wire Break cascade check only examines the array's *last* run of NaN samples — shallow
  data sits at the start of the array, so un-masking it cannot affect WB's behavior at all
  (reasoned, not just tested, since the mechanism makes this structurally impossible).
- The depth-banded range check only applies at ≥1500m — no interaction with the shallow region
  possible.

Conclusion: un-masking is safe. The only "new" flags are redundant corroboration on points CS
already flags as bad, not new false positives on good data.

## Implementation shape

- `_remove_surface_spike(cast)` → `_flag_surface_transient(qc: CastQC, now: datetime) -> None`,
  matching every other check's signature and the `_flag_*` naming convention. Called from the
  same position in `apply_qc()`'s sequence (immediately after `CastQC` construction, before the
  speed check), for real casts only — matches current scoping; self-test casts aren't real
  oceanographic profiles and this check doesn't apply to them.
- `SURFACE_SPIKE_DEPTH_M` (3.7) → `SURFACE_TRANSIENT_DEPTH_M` (3.6).
- Detection: `shallow = cast.depth_m < SURFACE_TRANSIENT_DEPTH_M`. Set
  `qc.temperature_qc[shallow] = GTSPP_PROBABLY_BAD`, then re-correct genuinely-missing points
  back to `GTSPP_MISSING`: `qc.temperature_qc[np.isnan(cast.temperature_c)] = GTSPP_MISSING` —
  the same "blanket-set-then-fix-NaN" idiom already used by the PE/TE and PR checks, reused
  rather than inventing a new pattern.
- One `CS` `HistoryEntry` per applicable cast (any real cast whose depth range reaches below
  3.6m), same trigger condition as today, updated description text and citation (v2.1 section
  4.3.1, not v1.1 section 2.1's now-superseded CSA).
- `cast.temperature_c` is never mutated by this check anymore — real values pass through
  unchanged to every downstream check and to the published NetCDF.

## Historical reprocessing

Re-run the historical backfill and re-publish to S3, same process as every prior deploy this
week. Expect the published archive to show real TEMP values (not `NaN`) at depths <3.6m for the
first time, each with `TEMP_quality_control` GTSPP flag 3 where a real reading exists (9 where
genuinely missing). Spot-check a handful of recovered profiles against the raw EDF files
directly before trusting the rebuilt archive, per this repo's standard "validate the real
output, not just the code" discipline.

## Testing

- Unit tests on synthetic casts: a shallow real value is kept (not destroyed) and flagged
  `GTSPP_PROBABLY_BAD`; a shallow genuinely-`NaN` value (e.g. a `-99` sentinel within the
  shallow band) is re-corrected to `GTSPP_MISSING`, not left at `PROBABLY_BAD`; `DEPTH_qc` is
  untouched; a cast entirely below 3.6m triggers no `CS` entry at all; self-test casts are
  unaffected (matches current scoping — confirm this stays true, don't just assume).
- Real-data regression: re-run the same masked-vs-unmasked spike-test comparison from the
  design investigation above against the shipped code directly (not the scratch simulation),
  confirming the same "zero spillover outside the shallow region" result holds for the actual
  implementation, not just the simulation.
- Existing tests referencing `SURFACE_SPIKE_DEPTH_M`/`_remove_surface_spike` need updating for
  the rename; existing tests asserting the *old* destroy-and-MISSING behavior for shallow depths
  need updating to assert the new keep-and-flag behavior instead — read each one before editing,
  don't assume which existing tests are affected without checking.

## Docs

`QC_COOKBOOK.md`'s "Surface Spikes — CS" section currently describes the *deprecated*
methodology as current fact and needs substantial rewriting, not a small edit: the depth
(3.6m, not 3.7m), the action (flag, don't destroy), the citation (v2.1 section 4.3.1's current
methodology, contrasted explicitly with v1.1's now-superseded CSA), and the real-data validation
above. The checks-at-a-glance table's `CS` row also needs updating (currently says "TEMP →
missing above 3.7 m", which will no longer be true). The "Open questions" entry asking whether
v2.1 revised anything gets a real answer instead of "unresolved as of this writing."
