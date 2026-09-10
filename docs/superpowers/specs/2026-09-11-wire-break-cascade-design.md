# Wire Break Cascade Detection — Design

Status: Approved

## Goal

Automate cookbook v1.1 section 3.2 "Wire Break" (WBR) as its own check: when a real or
self-test cast's temperature record ends in an unrecovered run of `-99`/NaN samples, record a
dedicated audit-trail entry for it (`WB` history flag) rather than leaving that tail as an
unexplained generic `GTSPP_MISSING` run. Filed as NDO-728, suggested by Alison Herbert
(Senior Acoustics Officer) after reviewing the XBT self-test findings from NDO-654/645's
wider QC-gap work.

## Background: what the real data actually shows

Alison's original suggestion was broader: "as soon as there is a -99, discard the rest of the
values in that profile." Checked against all 369 real casts' raw EDF files (Skippy,
`/mnt/data/VoyageData`) before designing anything, per this repo's real-data-validation
discipline (see `QC_COOKBOOK.md`'s "What we tried and rejected"):

- 133 of 369 real casts have at least one NaN run in TEMP.
- 130 of those have a run that reaches the very end of the cast with no recovery — the clean
  Wire Break shape (fault, then nothing, cast just stops). Lengths range 4-1936 samples.
- 44 of those 130 *also* have an earlier, separate NaN run that recovered before the cast
  ended. Recovered gaps range 1-632 samples, with 8-685 real-looking samples still following
  before the cast's actual end.
- Only 3 casts have NaN runs that are entirely recovery (no terminal cascade at all).

Applying Alison's rule literally (discard everything from the *first* `-99` onward) would
discard most of some real casts over a single mid-cast dropout the instrument evidently
recovered from — one real case would lose 878 of 1226 samples. Run length alone doesn't
separate "terminal cascade" from "mid-cast blip that recovered" either: the shortest terminal
cascade (4 samples) is the same length as a run that recovered.

The one signal the data actually supports cleanly: **does the cast's last NaN run reach the
end of the array with no recovery after it?** That's the shape both Wire Break's own cookbook
text describes ("downgrade from depth of initial point of damage") and Alison's self-test
evidence showed directly (a raw-sample analysis of `202526010_TP_20250927003232.edf` found a
continuous 43-second, zero-recovery `-99`/pinned-resistance tail through to the end of that
test, versus two short mid-test hiccups elsewhere in the same file that both recovered
cleanly). This design implements that narrower, real-data-supported rule only. Mid-cast
recoveries (the 44-case overlap) are left alone — distinguishing a "real recovered value" from
a "suspicious residual artifact" among those needs a different kind of check (e.g. comparing
the recovered values' own plausibility) and isn't attempted here.

## What actually changes

Nothing about published quality flags. `CastQC.__post_init__` already sets
`temperature_qc`/`sound_velocity_qc` to `GTSPP_MISSING` for every NaN sample unconditionally,
before any check runs (`parse_xbt_edf.py:489-490`) — a terminal NaN run is already correctly
excluded from "good" data today. This check is audit-trail only: it adds a `HistoryEntry`
documenting *why* the tail is missing (a Wire Break-shaped fault, not generic no-data),
citing the cookbook section directly. No interaction with the existing RC range check to
design around, since RC never fires on NaN input (`< min` / `> max` comparisons are both
`False` for NaN) and this check doesn't touch any `_qc` array.

## Detection rule

For each cast (real and self-test — matches how every other physical-plausibility check in
`apply_qc()` is scoped, "applied to every cast, real or test-probe"), on `temperature_c`:

- Find the last contiguous run of NaN samples, if the array's last sample is NaN.
- If that run exists (any length — the real archive's shortest terminal cascade is 4 samples,
  so there's no real data to calibrate a minimum against, and since this is audit-trail-only
  there's no data-quality cost to flagging honestly rather than inventing an uncalibrated
  cutoff), record one `HistoryEntry`:
  - `qc_flag`: `"WB"`
  - `qc_flag_description`: states the run length and depth range, cites v1.1 §3.2 by name
  - `start_depth`/`stop_depth`: the run's real depth range (same pattern as every other check)
- If the cast has no NaN samples, or its NaN samples don't reach the array's end, do nothing.

This mirrors the existing `_flag_spikes`/`_flag_neighbour_average_spikes` shape (a private
`_flag_*` function taking `(qc, now)`, appending a `HistoryEntry` when it fires) and slots into
`apply_qc()` alongside them.

## `_N_HISTORY` impact

A cast can now produce at most one additional history entry. Current ceiling is 12 (per
`build_xbt_netcdf.py`'s `_N_HISTORY`, from NDO-705/708's depth-band + spike additions).
Whether this raises the realistic worst case needs checking against the existing "worst case
cast" test fixture, not assumed — see plan.

## Testing

- Unit tests on synthetic casts: terminal-only NaN run (fires), NaN run that recovers before
  the end (does not fire), no NaN at all (does not fire), NaN run of length 1 at the very end
  (fires — no minimum), a cast with both a recovering run and a terminal run (fires once, for
  the terminal run only).
- Self-test casts: confirm the check still runs and appends history for them (scoping
  decision above), independent of `is_test_probe_cast()` filtering (which happens later, at
  output time, not inside `apply_qc()`).
- Real-data regression: re-run against the same 369-cast archive used for the investigation
  above and confirm exactly 130 real casts get a `WB` entry (the number found during design),
  not more or fewer — a mismatch means the implementation doesn't match the rule as designed.

## Docs

`QC_COOKBOOK.md` needs a new section (alongside the existing "Isolated readings" SP section,
which currently cites §3.2 as background text only) documenting this check, explicitly
including — per an explicit ask, not optional — that unlike every other check in that
document, Wire Break's justification in the self-test case rests on *ship procedure*
(alligator clips physically removed before "End Probe Drop" is pressed in WinMK21), not
purely on the data. A future reader shouldn't mistake this for a purely statistical rule the
way the rest of the document's checks are.
