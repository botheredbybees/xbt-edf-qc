# Position on Land Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Exception for this plan:** per Peter's explicit direction on NDO-728 (the prior check added
> this same week), and reused here for the same reason, this plan is being executed directly in
> the live interactive session, not via subagent-driven-development's fresh-implementer/
> fresh-reviewer loop -- a single well-scoped check doesn't warrant that machinery.

**Goal:** Add a `PL` (Position on Land) history entry and LATITUDE/LONGITUDE downgrade when a
cast's launch position falls on land, per the approved design spec
(`docs/superpowers/specs/2026-09-11-position-on-land-design.md`).

**Architecture:** One new `_flag_position_on_land(qc, now)` function in `parse_xbt_edf.py`,
using the `global-land-mask` package's `globe.is_land(lat, lon)`, wired into `apply_qc()`
immediately after the existing LATITUDE/LONGITUDE range checks. Only evaluates a position that
those range checks left at `GTSPP_GOOD` (matches GTSPP's own prerequisite chain: test 1.4 only
runs if test 1.3, "Impossible Location", hasn't already fired).

**Tech Stack:** Python, `global-land-mask` (new dependency, pure numpy under the hood, no
further transitive dependencies), pytest.

## Global Constraints

- Applies to every cast, real and self-test (matches every other physical-plausibility check).
- Only `LATITUDE_qc`/`LONGITUDE_qc` are touched — `TEMP_qc`/`DEPTH_qc`/`SOUND_VELOCITY_qc` are
  never touched by this check.
- Skip entirely if `LATITUDE_qc` or `LONGITUDE_qc` is already not `GTSPP_GOOD` (the range check
  already downgraded it) — do not double-flag.
- New `qc_flag` code: `"PL"`.
- Cite GTSPP Real-Time QC Manual (IOC M&G No. 22) test 1.4 in the description.
- Real-data regression must find exactly 0 of the 368 real casts in the historical archive
  getting a `PL` entry (matching the design-time validation) — a nonzero count means either a
  real, previously-undetected position fault (investigate before assuming the check is wrong)
  or a bug in the implementation.

---

### Task 1: Add the dependency, implement `_flag_position_on_land`, wire it into `apply_qc`

**Files:**
- Modify: `pyproject.toml` (add `global-land-mask` to `dependencies`)
- Modify: `parse_xbt_edf.py` (new function before `apply_qc`, one new call inside `apply_qc`
  after the existing lat/lon range checks, `apply_qc`'s own docstring, possibly `_N_HISTORY` in
  `build_xbt_netcdf.py`)
- Modify: `build_xbt_netcdf.py:43` (`_N_HISTORY`, only if Step 7 below finds the true worst case
  now exceeds 13)
- Test: `tests/test_parse_xbt_edf_qc.py`
- Test: `tests/test_build_xbt_netcdf.py` (worst-case fixture, only if it changes)

**Interfaces:**
- Produces: `_flag_position_on_land(qc: CastQC, now: datetime) -> None`, called from
  `apply_qc()` right after the existing:
  ```python
  qc.latitude_qc = _flag_scalar_out_of_range(
      qc, qc.latitude_qc, cast.latitude,
      LATITUDE_VALID_MIN, LATITUDE_VALID_MAX, "LATITUDE", "degrees_north", now)
  qc.longitude_qc = _flag_scalar_out_of_range(
      qc, qc.longitude_qc, cast.longitude,
      LONGITUDE_VALID_MIN, LONGITUDE_VALID_MAX, "LONGITUDE", "degrees_east", now)
  ```
  (the exact last two statements in `apply_qc()`'s current body, per
  `parse_xbt_edf.py:991-996`). Reads `qc.latitude_qc`, `qc.longitude_qc`, `qc.cast.latitude`,
  `qc.cast.longitude`.

- [ ] **Step 1: Add the dependency**

Edit `pyproject.toml`'s `dependencies` list:

```toml
dependencies = [
    "numpy>=1.24",
    "pandas>=2.0",
    "xarray>=2023.1",
    "netCDF4>=1.6",
    "global-land-mask>=1.0",
]
```

Install it locally for development: `pip install "global-land-mask>=1.0"`.

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_parse_xbt_edf_qc.py` (uses the existing `_cast()` helper already in that
file). Hobart is a real, unambiguous land position; a point in the open Southern Ocean is a
real, unambiguous sea position — both already used as reference points during design
validation:

```python
def test_launch_position_on_land_gets_a_pl_entry_and_downgraded_lat_lon():
    cast = _cast(latitude=-42.8806, longitude=147.3250)  # Hobart, Tasmania -- on land
    [qc] = apply_qc([cast])
    pl_entries = [h for h in qc.history if h.qc_flag == "PL"]
    assert len(pl_entries) == 1
    assert "test 1.4" in pl_entries[0].qc_flag_description
    assert qc.latitude_qc == GTSPP_PROBABLY_BAD
    assert qc.longitude_qc == GTSPP_PROBABLY_BAD


def test_launch_position_at_sea_gets_no_pl_entry():
    cast = _cast(latitude=-65.0, longitude=140.0)  # open Southern Ocean
    [qc] = apply_qc([cast])
    assert "PL" not in [h.qc_flag for h in qc.history]
    assert qc.latitude_qc == GTSPP_GOOD
    assert qc.longitude_qc == GTSPP_GOOD


def test_launch_position_on_land_does_not_touch_temp_or_depth():
    cast = _cast(latitude=-42.8806, longitude=147.3250)
    [qc] = apply_qc([cast])
    assert np.all(qc.temperature_qc == GTSPP_GOOD)
    assert np.all(qc.depth_qc == GTSPP_GOOD)


def test_already_out_of_range_latitude_is_not_also_flagged_pl():
    # latitude=200 fails the existing range check first -- this check must
    # not also fire and double-flag/add a second history entry for the same
    # underlying problem.
    cast = _cast(latitude=200.0, longitude=147.0)
    [qc] = apply_qc([cast])
    assert "PL" not in [h.qc_flag for h in qc.history]


def test_position_on_land_check_also_applies_to_test_probe_casts():
    cast = _cast(serial_number="TestProbe", latitude=-42.8806, longitude=147.3250)
    [qc] = apply_qc([cast])
    assert "PL" in [h.qc_flag for h in qc.history]
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `python3 -m pytest tests/test_parse_xbt_edf_qc.py -k position_on_land -v`
Expected: FAIL (import error or assertion failures — `_flag_position_on_land` doesn't exist yet
and `global-land-mask` isn't imported).

- [ ] **Step 4: Implement `_flag_position_on_land`**

Add this function to `parse_xbt_edf.py`, directly before `apply_qc`, and add
`from global_land_mask import globe` to the top-level imports alongside the existing `numpy`
import:

```python
def _flag_position_on_land(qc: CastQC, now: datetime) -> None:
    """Flags a cast whose launch position is on land -- GTSPP Real-Time QC
    Manual (IOC M&G No. 22) test 1.4 "Position on Land". Only evaluates a
    position the existing LATITUDE/LONGITUDE range check left at
    GTSPP_GOOD -- matches GTSPP's own prerequisite chain, where test 1.4
    only runs once test 1.3 ("Impossible Location") hasn't already fired.
    Uses the `global-land-mask` package (pure numpy, a bundled 1km-
    resolution land/sea grid, no other dependencies) -- validated against
    all 368 real historical XBT launch positions before shipping (zero
    false positives, see this check's own design spec). Matches GTSPP's
    own consequence: only LATITUDE/LONGITUDE are downgraded, since a
    position error doesn't invalidate what was physically measured, only
    where it claims to have been measured -- TEMP/DEPTH/SOUND_VELOCITY are
    untouched. Applied to every cast, real or test-probe, same as every
    other physical-plausibility check."""
    if qc.latitude_qc != GTSPP_GOOD or qc.longitude_qc != GTSPP_GOOD:
        return
    if not globe.is_land(qc.cast.latitude, qc.cast.longitude):
        return
    qc.latitude_qc = GTSPP_PROBABLY_BAD
    qc.longitude_qc = GTSPP_PROBABLY_BAD
    qc.history.append(HistoryEntry(
        institution=_INSTITUTION, step=_QC_STEP, software=_SOFTWARE,
        software_release=_SOFTWARE_RELEASE, date=now, parameter="LATITUDE,LONGITUDE",
        start_depth=float(qc.cast.depth_m[0]) if qc.cast.depth_m.size else 0.0,
        stop_depth=float(qc.cast.depth_m[-1]) if qc.cast.depth_m.size else 0.0,
        qc_flag="PL",
        qc_flag_description=(
            f"Launch position ({qc.cast.latitude}, {qc.cast.longitude}) is on land "
            "(GTSPP Real-Time QC Manual test 1.4)"
        ),
    ))
```

Wire it into `apply_qc()` immediately after the existing longitude range-check call:

```python
        qc.latitude_qc = _flag_scalar_out_of_range(
            qc, qc.latitude_qc, cast.latitude,
            LATITUDE_VALID_MIN, LATITUDE_VALID_MAX, "LATITUDE", "degrees_north", now)
        qc.longitude_qc = _flag_scalar_out_of_range(
            qc, qc.longitude_qc, cast.longitude,
            LONGITUDE_VALID_MIN, LONGITUDE_VALID_MAX, "LONGITUDE", "degrees_east", now)
        _flag_position_on_land(qc, now)
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `python3 -m pytest tests/test_parse_xbt_edf_qc.py -k position_on_land -v`
Expected: PASS (all 5 tests).

- [ ] **Step 6: Run the full test suite and see what else breaks**

Run: `python3 -m pytest tests/ -v 2>&1 | tail -60`

Check the full failure list before assuming nothing else is affected. `test_cast()`'s default
fixture uses `latitude=-42.0, longitude=147.0` (`tests/test_parse_xbt_edf_qc.py`'s `_cast()`
helper) — confirm whether that specific point is land or sea under `global-land-mask` before
assuming every existing test using the default `_cast()` is unaffected (it's just offshore of
Hobart, close enough to the coast that this needs checking directly, not assumed): run
`python3 -c "from global_land_mask import globe; print(globe.is_land(-42.0, 147.0))"` and note
the result. If it prints `True`, every existing test relying on the default `_cast()` fixture
inheriting `GTSPP_GOOD` lat/lon would break — if so, the fix is to move the default fixture's
lat/lon a little further offshore (e.g. `-42.0, 147.5` or similar), verified the same way, not
to weaken this check.

- [ ] **Step 7: Fix any fixture/expectation fallout from Step 6, one at a time**

Read whichever test file/line actually failed before editing it — this plan cannot predict
every fixture in the existing suite that might reference a borderline coastal coordinate.
Follow the same pattern as NDO-705/708/728's own fixture fixes: understand why the new check
fired where a test didn't expect it, then fix the fixture's input value (not this check's
logic) unless the failure reveals a genuine bug in `_flag_position_on_land` itself.

- [ ] **Step 8: Determine whether `_N_HISTORY` needs to change**

`apply_qc`'s docstring currently claims a real cast's theoretical worst case is exactly 13
entries (set during NDO-728). Work out on paper whether a cast can simultaneously hit all 13 of
the existing triggers AND separately have its launch position on land — these are independent
(a cast's TEMP/DEPTH/SOUND_VELOCITY faults have nothing to do with where it was launched), so
the true worst case likely becomes 14. If so, bump `_N_HISTORY` to 14 in `build_xbt_netcdf.py:43`
and update `apply_qc`'s docstring (both the real-cast max and the test-probe max, which also
gains a possible `PL` entry) the same way NDO-728's own implementation updated it for `WB`.

- [ ] **Step 9: Run the full suite again**

Run: `python3 -m pytest tests/ -v 2>&1 | tail -10`
Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add pyproject.toml parse_xbt_edf.py build_xbt_netcdf.py tests/test_parse_xbt_edf_qc.py tests/test_build_xbt_netcdf.py
git commit -m "$(cat <<'EOF'
feat: add position-on-land check (NDO-704)

Adds a PL history entry and LATITUDE/LONGITUDE downgrade when a cast's
launch position falls on land -- GTSPP Real-Time QC Manual test 1.4.
Uses the global-land-mask package (pure numpy, bundled 1km-resolution
land/sea grid), validated against all 368 real historical launch
positions before shipping (zero false positives -- see this check's own
design spec). TEMP/DEPTH/SOUND_VELOCITY are untouched, matching GTSPP's
own consequence: a position error doesn't invalidate what was physically
measured, only where it claims to have been measured.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BJquRw5tQxSjGtAAVFh1ib
EOF
)"
git push origin master
```

---

### Task 2: Real-data regression -- confirm 0 of the real historical casts get a PL entry

**Files:**
- Create (scratch, not committed to the repo): a script run on Skippy against
  `/mnt/data/VoyageData`, matching the pattern used for NDO-705/708/728's own regressions.

**Interfaces:**
- Consumes: `_flag_position_on_land` (Task 1), via the real `apply_qc()` under the production
  venv.

- [ ] **Step 1: Deploy the updated package to Skippy**

```bash
ssh aadc@172.16.29.7 "source /home/aadc/uwy_venv/bin/activate && pip install --upgrade --force-reinstall 'git+https://github.com/botheredbybees/xbt-edf-qc.git' && python3 -c 'import parse_xbt_edf; print(hasattr(parse_xbt_edf, \"_flag_position_on_land\"))'"
```

Note this is `--force-reinstall` WITHOUT `--no-deps` this time, unlike NDO-705/708/728's own
deploy commands -- this change adds a brand new dependency (`global-land-mask`), which
`--no-deps` would skip installing. Expected: pip installs `global-land-mask` as a new
dependency, and the import check prints `True`.

- [ ] **Step 2: Write and run the regression script on Skippy**

```python
import glob, os, sys
sys.path.insert(0, os.path.expanduser("~/cron-jobs"))
from parse_xbt_edf import EDFParseError, apply_qc, is_test_probe_cast, parse_edf

VOYAGE_DATA_ROOT = "/mnt/data/VoyageData"
voyage_dirs = sorted(
    name for name in os.listdir(VOYAGE_DATA_ROOT)
    if os.path.isdir(os.path.join(VOYAGE_DATA_ROOT, name, "xbt", "raw"))
)
all_casts = []
for voyage in voyage_dirs:
    raw_dir = os.path.join(VOYAGE_DATA_ROOT, voyage, "xbt", "raw")
    for edf_path in sorted(glob.glob(os.path.join(raw_dir, "**", "*.edf"), recursive=True)):
        try:
            all_casts.append(parse_edf(edf_path, voyage_id=voyage))
        except EDFParseError:
            continue

real_casts = [c for c in all_casts if not is_test_probe_cast(c)]
casts_qc = apply_qc(real_casts)
pl_count = sum(1 for qc in casts_qc if "PL" in [h.qc_flag for h in qc.history])
print(f"Real casts: {len(real_casts)}, PL entries: {pl_count}")
```

Run it under the same venv as Step 1. Expected: `PL entries: 0` — matching the design-time
validation. (The known North Pacific outlier from an earlier investigation this same week, one
cast with a genuinely bad -138°W/51.8°N latitude/longitude, is NOT expected to trigger `PL`: it
would already be flagged by the existing range check only if it's outside [-90,90]/[-180,180],
which it isn't — 51.8°N is a valid latitude, just wrong. Confirm directly whether this specific
cast is flagged `PL` or not, and if it IS land under `global-land-mask`, that's a genuine
finding worth reporting, not a bug to suppress — per this repo's own real-data-validation
discipline, an unexpected finding in the real archive gets investigated, not filtered out.)

- [ ] **Step 3: If the count doesn't match, investigate before proceeding**

Do not adjust the expected number to match the code's output. If nonzero, look up the specific
cast(s) by hand and determine whether it's a genuine, previously-undetected position fault (in
which case 0 was the wrong expectation, and the finding itself is real and worth documenting) or
a bug in the implementation (e.g. a lat/lon swap, an off-by-one). Fix whichever is wrong.

---

### Task 3: Update `QC_COOKBOOK.md`, deploy, and close out

**Files:**
- Modify: `QC_COOKBOOK.md`

- [ ] **Step 1: Add a new QC_COOKBOOK.md section**

Read the current file first (`Read QC_COOKBOOK.md` — it has moved since this plan was written,
most recently by the NDO-686 investigation writeup on 2026-09-11). Add a new subsection to the
"Checks implemented" part, e.g. `### Position on Land — PL (GTSPP Real-Time QC Manual test 1.4)`.
It must cover, plainly:
1. What the check does (launch position on land → `PL` entry, `LATITUDE`/`LONGITUDE` downgraded,
   `TEMP`/`DEPTH`/`SOUND_VELOCITY` untouched) and why (a position error doesn't invalidate the
   physical measurement, only its claimed location).
2. **This check's provenance is different from every other check in this document** — cite
   GTSPP test 1.4 (and note SeaDataNet/SAMOS independently specify the same check) explicitly,
   and say plainly that unlike `CS`/`SP`/`PE`/`TE`/`PR`/`RC`/`WB`, this one does NOT trace back to
   the CSIRO XBT QC Cookbook.
3. The `global-land-mask` package and the real-data validation behind trusting it (zero false
   positives across all 368 real historical launch positions).
4. Whatever Task 2 actually found for the real archive (0 flagged, or a genuine finding if not).

Also update the checks-at-a-glance table (a new `PL` row) and the table of contents.

- [ ] **Step 2: Commit and push**

```bash
git add QC_COOKBOOK.md
git commit -m "$(cat <<'EOF'
docs: document position-on-land check (NDO-704)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BJquRw5tQxSjGtAAVFh1ib
EOF
)"
git push origin master
```

- [ ] **Step 3: Re-run the historical backfill and re-upload to AADC's S3 bucket**

Check whether NDO-728's own historical-archive republish is still pending (Peter deliberately
deferred it on 2026-09-11 to batch with other same-day tickets, per the current voyage's
handover notes) before running this step. If it is, batch this check's republish with that one
rather than running the backfill/upload twice in the same session — one rebuild-and-republish
covering everything landed since the last publish, not one per ticket.

When ready to publish:

```bash
ssh aadc@172.16.29.7 "cd ~/cron-jobs && source /home/aadc/uwy_venv/bin/activate && python3 backfill_xbt_netcdf_historical.py"
```

Then re-upload via `rclone` (remote `aadc-kingston`) to `aadc-nuyina/xbt_historical_backfill/`,
and verify with `rclone check` that 0 differences remain — same commands as every prior deploy
this week.

- [ ] **Step 4: Add a dated handover-notes entry**

Pull `newinapedia` first (Tess may have edited the same page), then append a new
`### YYYY MM DD` section to the current voyage's page describing what was deployed. If batched
with NDO-728's own pending publish (Step 3 above), cover both in one entry rather than two.
Commit and push, pathspec-scoped to just that file.

- [ ] **Step 5: Update Jira**

Transition NDO-704 to Done (`mcp__atlassian__getTransitionsForJiraIssue` first to confirm the
transition ID), with a comment citing the real validation numbers and the deployed commit
hashes.
