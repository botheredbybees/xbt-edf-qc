# Position on Land Check — Design

Status: Approved

## Goal

Add a position-on-land check (NDO-704) to `parse_xbt_edf.py`'s QC pipeline: flag a cast whose
launch LATITUDE/LONGITUDE falls on land, per GTSPP Real-Time QC Manual (IOC M&G No. 22) test
1.4 "Position on Land". Currently, `parse_xbt_edf.py`'s range check (RC) only validates that
LATITUDE/LONGITUDE are physically possible values (-90/90, -180/180) — a logging or GPS fault
that places a real cast's reported position onshore (a coastal transit, a fix offset, corrupted
GPS data near land) would pass QC undetected today.

## Background

Filed after a QA/QC standards review found three independent standards specify this check
(SeaDataNet QC Procedures v2.0 §4, GTSPP Real-Time QC Manual tests 1.3/1.4, SAMOS netCDF manual
Flag L), and neither of this workspace's two QC pipelines has one. A companion ticket
(NDO-703) covers the underway-merger's continuous track — a bigger lift, handled separately.
This spec covers only the XBT pass (NDO-704): one position per cast, not a continuous track.

**What GTSPP's actual test 1.4 does** (read directly from the source PDF, not assumed): it's a
1990s interactive, human-reviewed check — displays a track chart and neighbouring stations for
an operator to confirm. Its terminal automated consequence, though, is narrow: when a position
is confirmed on land, "the quality flags on the latitude and longitude are set to be '3'"
(doubtful/probably-bad in GTSPP's 1/3/9 scale) — the sounding/temperature measurements
themselves are untouched, since a position error doesn't invalidate what was physically
measured, only where it claims to have been measured. Test 1.4 also cross-checks a reported
sounding against known bathymetry (rules 1.4.3/1.4.4) — that sub-check needs a bathymetry
dataset and doesn't map onto anything XBT measures (a temperature profile, not a depth sounding
survey); explicitly out of scope here. Test 1.4's own prerequisite is that test 1.3
("Impossible Location", i.e. lat/lon physically out of range) hasn't already fired — this
pipeline's existing LATITUDE/LONGITUDE range check already covers 1.3, so the new check should
only evaluate a position that already passed that check.

## Data source: `global-land-mask` (validated)

Rather than build or vendor a coastline/bathymetry dataset from scratch, use the PyPI package
`global-land-mask` (https://github.com/toddkarin/global-land-mask): a pure-numpy, no-other-
dependencies package bundling a pre-computed 1km-resolution global land/sea grid (2.5MB
compressed, shipped inside the package itself — no runtime download, no extra native
dependencies like GDAL/shapely). `globe.is_land(lat, lon)` returns a boolean.

**Validated against the real archive before designing further**, per this repo's own
discipline: ran `globe.is_land()` against all 368 real historical XBT launch positions
(`xbt_historical_profiles.nc`'s source casts) — **zero false positives**, including several
casts close to the Tasmanian coast at voyage start/end. Also spot-checked: correctly classifies
Tasmania as land, deep Southern Ocean and near-Antarctic-coast/ice-covered water (e.g. McMurdo
Sound) as sea (a ship physically over sea ice is still over water, not land — correct behaviour
for this check's purpose).

## Detection rule and consequence

For each cast (real and self-test — matches how every other physical-plausibility check in
`apply_qc()` is scoped), after the existing LATITUDE/LONGITUDE range check:

- If LATITUDE or LONGITUDE is already `GTSPP_PROBABLY_BAD` from the range check (physically
  impossible value), skip — matches GTSPP's own prerequisite chain (1.4 only runs once 1.3
  hasn't already fired).
- Otherwise, call `global_land_mask.globe.is_land(latitude, longitude)`. If `True`:
  - `LATITUDE_qc` and `LONGITUDE_qc` → `GTSPP_PROBABLY_BAD`.
  - `TEMP_qc`/`DEPTH_qc`/`SOUND_VELOCITY_qc` are **not** touched — matches GTSPP's own
    consequence (position doubtful, measurement itself not invalidated).
  - One `HistoryEntry`: `qc_flag="PL"` (no CSIRO-cookbook code exists for this — it's cited
    from GTSPP, not the CSIRO cookbook this pipeline is otherwise built from), citing GTSPP
    test 1.4 by name, naming the cast's own latitude/longitude in the description.

## New dependency

`global-land-mask` added to `pyproject.toml`'s dependencies. No other new dependencies (it has
none beyond numpy, already required).

## Testing

- Unit tests on synthetic casts: a real land position (e.g. Hobart, -42.88, 147.33) gets a `PL`
  entry and both lat/lon downgraded, `TEMP`/`DEPTH` unaffected; a real ocean position doesn't;
  a position that's already out-of-range (e.g. latitude=200) is skipped by this check (RC
  already handles it, no double-flagging); self-test casts are also checked (scoping parity).
- Real-data regression: re-run against the same 368-cast historical archive and confirm 0 casts
  get a `PL` entry — matching the validation already done above. A nonzero count means either a
  real, previously-undetected position fault (investigate before assuming the check is wrong)
  or a bug in the implementation — the same "investigate the discrepancy, don't just adjust the
  expected number" discipline as every other real-data regression in this repo.

## Docs

`QC_COOKBOOK.md` needs a new section (checks-at-a-glance row + a "Position on Land" subsection)
citing GTSPP test 1.4 directly, noting the `global-land-mask` package and its validation, and
explicitly noting (like the Wire Break section already does) that this check's provenance is
GTSPP/SeaDataNet/SAMOS, not the CSIRO cookbook every other check in this document cites — a
reader shouldn't assume `PL` traces to the same source as `CS`/`SP`/`PE`/`TE`/`PR`/`RC`/`WB`.
