# Surface Transient (CS) Methodology Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Exception for this plan:** per the established pattern this week (NDO-704/728), this plan is
> executed directly in the live interactive session, not via subagent-driven-development's
> fresh-implementer/fresh-reviewer loop -- a single well-scoped fix to one existing check doesn't
> warrant that machinery.

**Goal:** Fix `_remove_surface_spike` so it flags real shallow TEMP data as `GTSPP_PROBABLY_BAD`
instead of destroying it, per the approved design spec
(`docs/superpowers/specs/2026-09-11-surface-transient-methodology-fix-design.md`).

**Architecture:** `_remove_surface_spike(cast)` becomes `_flag_surface_transient(qc, now)`,
matching every other check's `_flag_*` signature, called after `CastQC` construction instead of
mutating raw `cast.temperature_c` beforehand. `SURFACE_SPIKE_DEPTH_M` (3.7) becomes
`SURFACE_TRANSIENT_DEPTH_M` (3.6).

**Tech Stack:** Python, numpy, pytest (matches the rest of the repo). No new dependencies.

## Global Constraints

- Depth threshold: 3.6m (not 3.7m) -- v2.1's resolution of the 1994 document's own 3.7m/3.9m
  ambiguity.
- Real casts only (matches current scoping -- self-test casts don't get this check).
- `TEMP_qc` -> `GTSPP_PROBABLY_BAD` for shallow points with a real value; genuinely-`NaN` shallow
  points stay `GTSPP_MISSING`. `DEPTH_qc` untouched.
- `cast.temperature_c` is never mutated by this check -- real values must reach every downstream
  check and the published NetCDF unchanged.
- Real-data regression must find zero new spike-test flags outside the shallow region (matching
  the design investigation's own 375-flags-all-inside-shallow-region result) -- any flag outside
  that region means a bug, not an expected finding.

---

### Task 1: Implement `_flag_surface_transient` and update the existing test suite

**Files:**
- Modify: `parse_xbt_edf.py` (rename+rewrite the function, rename the constant, move the call
  site, update the `apply_qc()` docstring if the history-entry description text changes)
- Test: `tests/test_parse_xbt_edf_qc.py`

**Interfaces:**
- Produces: `_flag_surface_transient(qc: CastQC, now: datetime) -> None`, called from
  `apply_qc()` in place of the current `_remove_surface_spike(cast)` call (currently at line 902,
  before `qc = CastQC(cast=cast)` at line 904) -- the new call goes immediately *after* line 904
  instead, replacing the separate `if np.any(cast.depth_m < SURFACE_SPIKE_DEPTH_M): ...
  qc.history.append(...)` block currently at lines 906-918 (the new function does both the
  flagging and the history-entry append itself, in one place, matching every other `_flag_*`
  function's shape).

**Current state, exactly as it reads today (read the file yourself before editing -- it may have
moved since this plan was written):**

```python
# parse_xbt_edf.py:333
SURFACE_SPIKE_DEPTH_M = 3.7
```

```python
# parse_xbt_edf.py:622-637
def _remove_surface_spike(cast: Cast) -> None:
    """Blanks TEMP to NaN above SURFACE_SPIKE_DEPTH_M.
    ...
    """
    shallow = cast.depth_m < SURFACE_SPIKE_DEPTH_M
    cast.temperature_c[shallow] = np.nan
```

```python
# parse_xbt_edf.py:899-918 (inside apply_qc()'s main loop)
    for cast in ordered:
        is_test_probe = is_test_probe_cast(cast)
        if not is_test_probe:
            _remove_surface_spike(cast)

        qc = CastQC(cast=cast)

        if not is_test_probe:
            if np.any(cast.depth_m < SURFACE_SPIKE_DEPTH_M):
                qc.history.append(HistoryEntry(
                    institution=_INSTITUTION, step=_QC_STEP, software=_SOFTWARE,
                    software_release=_SOFTWARE_RELEASE, date=now, parameter="TEMP",
                    start_depth=0.0, stop_depth=SURFACE_SPIKE_DEPTH_M,
                    qc_flag="CS",
                    qc_flag_description=(
                        f"Surface spike: start-up transient TEMP data above "
                        f"{SURFACE_SPIKE_DEPTH_M} m removed (CSIRO XBT QC Cookbook "
                        "v1.1 section 2.1, Accept code CSA)"
                    ),
                ))

            if previous_real_cast is not None:
                speed_knots = _speed_between_casts_knots(previous_real_cast, cast)
                ...
```

- [ ] **Step 1: Write the failing tests**

Read `tests/test_parse_xbt_edf_qc.py` yourself first -- these five tests currently assert the
OLD (destroy-and-NaN) behavior and need rewriting, not deleting. Replace them with:

```python
def test_surface_transient_flags_shallow_temperature_without_destroying_it():
    # Real shape found 2026-09-08: a spurious high reading right at the
    # surface, settling to a physically realistic profile within a couple of
    # metres -- the classic start-up transient. Current (v2.1, since
    # 2020/2021) methodology KEEPS this value and flags it, rather than
    # destroying it the way the deprecated v1.1 CSA code did.
    cast = _cast(
        depth_m=np.array([0.0, 1.0, 3.6, 3.7, 5.0]),
        temperature_c=np.array([37.0, 6.0, -0.3, -0.5, -0.5]),
    )
    [qc] = apply_qc([cast])
    assert list(qc.temperature_qc) == [
        GTSPP_PROBABLY_BAD, GTSPP_PROBABLY_BAD, GTSPP_GOOD, GTSPP_GOOD, GTSPP_GOOD,
    ]
    # The value itself must survive -- this check flags, it does not correct
    # or delete, matching every other check in this pipeline.
    assert cast.temperature_c[0] == pytest.approx(37.0)
    assert cast.temperature_c[1] == pytest.approx(6.0)
    assert cast.temperature_c[2] == pytest.approx(-0.3)


def test_surface_transient_leaves_a_genuinely_missing_shallow_value_as_missing():
    # A shallow sample that's NaN for an unrelated reason (e.g. a -99
    # sentinel already converted to NaN) must stay GTSPP_MISSING, not get
    # promoted to GTSPP_PROBABLY_BAD just because it's also shallow.
    cast = _cast(
        depth_m=np.array([0.0, 1.0, 5.0]),
        temperature_c=np.array([37.0, np.nan, -0.5]),
    )
    [qc] = apply_qc([cast])
    assert list(qc.temperature_qc) == [GTSPP_PROBABLY_BAD, GTSPP_MISSING, GTSPP_GOOD]


def test_surface_transient_check_only_touches_temperature():
    # DEPTH_VALUES comes from elapsed time and the fall-rate model, not the
    # thermistor -- the same start-up transient doesn't affect it. TEMP
    # itself must also be unchanged now (this check no longer mutates data).
    cast = _cast(
        depth_m=np.array([0.0, 1.0, 5.0]),
        temperature_c=np.array([37.0, 6.0, -0.5]),
        sound_velocity_ms=np.array([1500.0, 1500.0, 1500.0]),
    )
    [qc] = apply_qc([cast])
    assert np.all(qc.depth_qc == GTSPP_GOOD)
    assert np.all(qc.sound_velocity_qc == GTSPP_GOOD)
    assert list(cast.depth_m) == [0.0, 1.0, 5.0]
    assert list(cast.temperature_c) == [37.0, 6.0, -0.5]


def test_surface_transient_entry_cites_the_current_cookbook_edition():
    cast = _cast(depth_m=np.array([0.0, 1.0, 5.0]))
    [qc] = apply_qc([cast])
    [entry] = [e for e in qc.history if e.qc_flag == "CS"]
    assert entry.parameter == "TEMP"
    assert "CSIRO XBT QC Cookbook v2.1 section 4.3.1" in entry.qc_flag_description


def test_surface_transient_not_applied_at_or_beyond_the_threshold_depth():
    # No sample shallower than SURFACE_TRANSIENT_DEPTH_M (3.6) at all -- no
    # CS entry, and no flag change.
    cast = _cast(depth_m=np.array([3.6, 5.0, 10.0]), temperature_c=np.array([-0.5, -0.5, -0.5]))
    [qc] = apply_qc([cast])
    assert np.all(qc.temperature_qc == GTSPP_GOOD)
    assert list(cast.temperature_c) == [-0.5, -0.5, -0.5]
    assert "CS" not in [e.qc_flag for e in qc.history]


def test_surface_transient_not_applied_to_test_probe_casts():
    # A self-test cast doesn't involve a real probe entering the water, so
    # the physical start-up transient this check exists for doesn't apply --
    # and self-test casts are filtered out of the published NetCDF anyway.
    cast = _cast(
        serial_number="TestProbe",
        depth_m=np.array([0.0, 1.0, 2.0]),
        temperature_c=np.array([1.51, 1.5, 1.49]),
    )
    [qc] = apply_qc([cast])
    assert "CS" not in [e.qc_flag for e in qc.history]
    assert list(cast.temperature_c) == [1.51, 1.5, 1.49]
```

Also update the two stale prose comments referencing the old name/depth: search for
`SURFACE_SPIKE_DEPTH_M (3.7 m)` in `tests/test_parse_xbt_edf_qc.py` (two occurrences, near the
top of the file in `_cast()`'s own docstring-style comments) and change them to reference
`SURFACE_TRANSIENT_DEPTH_M (3.6 m)`.

- [ ] **Step 2: Run the new/changed tests to verify they fail**

Run: `python3 -m pytest tests/test_parse_xbt_edf_qc.py -k surface_transient -v`
Expected: FAIL (the old test names don't exist yet under these new names, and/or the behavior
doesn't match -- `_flag_surface_transient` and `SURFACE_TRANSIENT_DEPTH_M` don't exist yet).

- [ ] **Step 3: Rename the constant and rewrite the function**

```python
# Replace SURFACE_SPIKE_DEPTH_M = 3.7 with:
SURFACE_TRANSIENT_DEPTH_M = 3.6
```

Update that constant's own surrounding comment block (read it first -- it currently explains the
CSA/removal rationale and cites v1.1 section 2.1; rewrite it to explain the CURRENT v2.1
methodology instead: flag, don't destroy, citing section 4.3.1, and note that 3.6m resolves the
1994 document's own internal 3.7m/3.9m ambiguity).

Replace `_remove_surface_spike` with:

```python
def _flag_surface_transient(qc: CastQC, now: datetime) -> None:
    """Flags real TEMP data above SURFACE_TRANSIENT_DEPTH_M as probably bad,
    without destroying it. CSIRO XBT QC Cookbook v2.1 section 4.3.1: "Since
    2020/2021, the Australian QC group elected to retain the temperature
    surface values and apply a GTSPP flag 3 (Reject) to the surface
    transients from the surface to 3.6m" -- superseding the 1994 (v1.1)
    CSA code's "removed ... replaced with ... no data" approach, which this
    pipeline used to implement (see this check's own design spec for the
    real-data investigation behind the change). DEPTH_VALUES comes from
    elapsed time and the fall-rate model, not the thermistor, so it isn't
    affected by the same start-up transient -- only TEMP is touched."""
    shallow = qc.cast.depth_m < SURFACE_TRANSIENT_DEPTH_M
    if not np.any(shallow):
        return
    qc.temperature_qc[shallow] = GTSPP_PROBABLY_BAD
    qc.temperature_qc[np.isnan(qc.cast.temperature_c)] = GTSPP_MISSING
    qc.history.append(HistoryEntry(
        institution=_INSTITUTION, step=_QC_STEP, software=_SOFTWARE,
        software_release=_SOFTWARE_RELEASE, date=now, parameter="TEMP",
        start_depth=0.0, stop_depth=SURFACE_TRANSIENT_DEPTH_M,
        qc_flag="CS",
        qc_flag_description=(
            f"Surface transient: TEMP data above {SURFACE_TRANSIENT_DEPTH_M} m "
            "flagged probably bad, value retained (CSIRO XBT QC Cookbook "
            "v2.1 section 4.3.1)"
        ),
    ))
```

Update the call site in `apply_qc()`:

```python
    for cast in ordered:
        is_test_probe = is_test_probe_cast(cast)

        qc = CastQC(cast=cast)

        if not is_test_probe:
            _flag_surface_transient(qc, now)

            if previous_real_cast is not None:
                speed_knots = _speed_between_casts_knots(previous_real_cast, cast)
                ...
```

(Remove the old `_remove_surface_spike(cast)` call and the old separate history-entry block
entirely -- `_flag_surface_transient` now does both.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_parse_xbt_edf_qc.py -k surface_transient -v`
Expected: PASS (all 6 tests).

- [ ] **Step 5: Run the full test suite and fix any fallout**

Run: `python3 -m pytest tests/ -v 2>&1 | tail -60`

Check `test_n_history_capacity_covers_the_worst_case_cast`
(`tests/test_build_xbt_netcdf.py`) specifically -- read it before assuming it's fine. Its
fixture's `second` cast has `depth_m=np.array([0.0, 5000.0, 2.0])`, both 0.0 and 2.0 being
shallower than 3.6m, so CS's trigger condition (`np.any(shallow)`) still fires the same way it
did before -- but confirm the `HISTORY_QC_FLAG` sequence is unaffected by actually running it,
not by assuming the trigger condition alone is sufficient (the *consequence* changed, and this
fixture's own comment references CS's role in producing the WB entry via `_flag_wire_break_cascade`
reading `qc.cast.temperature_c` at the end of the array -- confirm that chain isn't disturbed by
CS no longer NaN-ing the shallow values, since WB only cares about the array's last run, not
these specific indices).

Fix any other failures the same way -- read the failing test, understand whether the fixture
needs updating for the new behavior or whether it's revealed a real bug, don't guess.

- [ ] **Step 6: Commit**

```bash
git add parse_xbt_edf.py tests/test_parse_xbt_edf_qc.py tests/test_build_xbt_netcdf.py
git commit -m "$(cat <<'EOF'
fix: Surface Transient (CS) check flags shallow TEMP instead of destroying it (NDO-729)

_remove_surface_spike previously implemented the deprecated 1994 (v1.1)
CSA methodology -- destroying real near-surface TEMP data by setting it
to NaN. The current, authoritative v2.1 methodology (since 2020/2021,
CSIRO XBT QC Cookbook section 4.3.1) keeps the real value and flags it
GTSPP_PROBABLY_BAD instead. Renamed _flag_surface_transient to match
every other check's naming, moved the call to after CastQC construction
since it no longer needs to mutate raw cast data first, and updated the
depth threshold 3.7m -> 3.6m (v2.1 resolves the 1994 document's own
internal 3.7m/3.9m ambiguity). Real-data investigation (this check's own
design spec) confirmed no downstream check is destabilised by shallow
TEMP no longer being masked to NaN.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BJquRw5tQxSjGtAAVFh1ib
EOF
)"
git push origin master
```

---

### Task 2: Real-data regression -- confirm zero spillover and a correct NetCDF round-trip

**Files:**
- Create (scratch, not committed to the repo): a script run on Skippy against
  `/mnt/data/VoyageData`, matching the pattern used for every prior regression this week.

**Interfaces:**
- Consumes: `_flag_surface_transient` (Task 1), via the real `apply_qc()` + `build_xbt_netcdf()`
  under the production venv.

- [ ] **Step 1: Deploy the updated package to Skippy**

```bash
ssh aadc@172.16.29.7 "source /home/aadc/uwy_venv/bin/activate && pip install --upgrade --force-reinstall --no-deps 'git+https://github.com/botheredbybees/xbt-edf-qc.git' && python3 -c 'import parse_xbt_edf; print(parse_xbt_edf.SURFACE_TRANSIENT_DEPTH_M)'"
```

Use `--no-deps` here -- this change adds no new dependency, so (per the gotcha found during
NDO-704's deploy) the plain `--force-reinstall --no-deps` form is correct and won't touch
`numpy`/`pandas`/`xarray`/`netCDF4`. Expected: prints `3.6`.

- [ ] **Step 2: Write and run the spillover-check regression script on Skippy**

```python
import glob, os, sys
sys.path.insert(0, os.path.expanduser("~/cron-jobs"))
from parse_xbt_edf import EDFParseError, GTSPP_GOOD, apply_qc, is_test_probe_cast, parse_edf
import numpy as np

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

recovered_samples = 0
spike_flags_outside_shallow = 0
for qc in casts_qc:
    depth, temp = qc.cast.depth_m, qc.cast.temperature_c
    shallow = depth < 3.6
    recovered_samples += int(np.sum(shallow & ~np.isnan(temp)))
    for h in qc.history:
        if h.qc_flag == "SP" and "deviating from the average" in h.qc_flag_description:
            if h.stop_depth >= 3.6:
                # a neighbour-average spike entry whose range reaches
                # outside the shallow region at all is worth a manual look
                spike_flags_outside_shallow += 1

print(f"Real casts: {len(real_casts)}")
print(f"Real shallow (<3.6m) TEMP samples now kept (not destroyed): {recovered_samples}")
print(f"Neighbour-average spike entries reaching outside the shallow region: {spike_flags_outside_shallow}")
```

Expected: `recovered_samples` close to the design investigation's own figure of 2,214 (may differ
slightly if the live archive has changed since design time -- if it differs by more than a
handful, investigate rather than assume it's fine, same discipline as every prior regression this
week). `spike_flags_outside_shallow` should be a small number consistent with pre-existing (not
newly introduced) spike detections deeper in real profiles -- cross-check any nonzero count
against the pre-fix spike-test entry count (which you can get by re-running the same script
against the previously-deployed commit, or by comparing to the 190-flag figure documented in
`QC_COOKBOOK.md`'s "Neighbour-average Spikes retest" section) rather than assuming a nonzero
count here is automatically a regression.

- [ ] **Step 3: Spot-check a real recovered profile through the full pipeline**

```python
from build_xbt_netcdf import build_xbt_netcdf
[qc] = [q for q in casts_qc if np.any(q.cast.depth_m < 3.6) and not np.all(np.isnan(q.cast.temperature_c[q.cast.depth_m < 3.6]))][:1]
ds = build_xbt_netcdf([qc])
shallow_idx = np.where(ds["DEPTH_VALUES"].values[0] < 3.6)[0]
print("TEMP at shallow indices:", ds["TEMP"].values[0][shallow_idx])
print("TEMP_quality_control at shallow indices:", ds["TEMP_quality_control"].values[0][shallow_idx])
```

Expected: real (non-NaN) TEMP values with `TEMP_quality_control` equal to `3` at those indices —
confirming the fix actually reaches the published NetCDF, not just the in-memory `CastQC` object.

- [ ] **Step 4: If anything doesn't match expectations, investigate before proceeding**

Same discipline as every other real-data regression this week: a discrepancy gets root-caused,
not papered over by adjusting the expected number.

---

### Task 3: Update `QC_COOKBOOK.md`, deploy, and close out

**Files:**
- Modify: `QC_COOKBOOK.md`

- [ ] **Step 1: Rewrite the "Surface Spikes — CS" section**

Read the current file first (`Read QC_COOKBOOK.md` -- it has moved multiple times this week).
The existing section under `### Surface Spikes — CS (v1.1 section 2.1)` describes the deprecated
methodology as current fact and needs a substantial rewrite, not a small edit:
- Rename the heading to reflect the current terminology, e.g.
  `### Surface Transients — CS (v2.1 section 4.3.1, supersedes v1.1 section 2.1's CSA)`.
- State plainly that this pipeline used to implement the *deprecated* 1994 methodology
  (destroy the data) and now implements the current v2.1 one (flag it, GTSPP flag 3, keep the
  value) -- name this as a real fix (NDO-729), not just a rewording.
- State the depth is 3.6m, not 3.7m, and why (v2.1 resolves the 1994 document's own internal
  ambiguity between 3.7m and 3.9m).
- Include the real-data validation from this check's own design spec: 2,214 real samples
  recovered across the historical archive, zero spillover of new spike-test flags into
  genuinely deeper data.
- Remove or rewrite the old `#### Not implemented: CSR (Reject variant)` subsection if it no
  longer accurately describes the current state -- read it fresh and decide, don't leave it
  unexamined next to a section it may now contradict.

- [ ] **Step 2: Update the checks-at-a-glance table**

The `CS` row currently says something like "TEMP → missing above 3.7 m" -- update it to reflect
the new consequence ("TEMP → probably bad above 3.6 m, value retained").

- [ ] **Step 3: Answer the "Open questions" entry**

Find the entry asking "Whether the 2022 edition (v2.1) retired or consolidated any of the 1994
edition's ~30 flag categories... and whether it revises the 3.7 m surface-spike depth used
above -- unresolved as of this writing." Replace "unresolved" with the real answer: yes, v2.1
both retired the CSA code and revised the depth to 3.6m, exactly the fix this ticket made --
link to the new CS section rather than duplicating the explanation.

- [ ] **Step 4: Run the test suite one more time and commit**

```bash
python3 -m pytest tests/ -q
git add QC_COOKBOOK.md
git commit -m "$(cat <<'EOF'
docs: document the Surface Transient (CS) methodology fix (NDO-729)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BJquRw5tQxSjGtAAVFh1ib
EOF
)"
git push origin master
```

- [ ] **Step 5: Re-run the historical backfill and re-publish to AADC's S3 bucket**

```bash
ssh aadc@172.16.29.7 "cd ~/cron-jobs && source /home/aadc/uwy_venv/bin/activate && python3 backfill_xbt_netcdf_historical.py"
```

Then re-upload via `rclone` (remote `aadc-kingston`) to `aadc-nuyina/xbt_historical_backfill/`,
and verify with `rclone check` that 0 differences remain -- same commands as every prior deploy
this week.

- [ ] **Step 6: Spot-check the real published output**

Confirm the rebuilt archive's shallow (<3.6m) TEMP values are no longer uniformly NaN -- pull a
handful of real profiles and check that `TEMP` has real values with `TEMP_quality_control == 3`
in that depth range, matching Task 2 Step 3's spot-check but against the actual published file
this time.

- [ ] **Step 7: Add a dated handover-notes entry**

Pull `newinapedia` first (Tess may have edited the same page), then append a new
`### YYYY MM DD` section to the current voyage's page describing what was deployed: the
methodology fix, the real-data recovery (2,214 samples), and that this is the first time this
pipeline has published real near-surface XBT temperature data. Commit and push, pathspec-scoped
to just that file.

- [ ] **Step 8: Update Jira**

Transition NDO-729 to Done (`mcp__atlassian__getTransitionsForJiraIssue` first to confirm the
transition ID), with a comment citing the real validation numbers and the deployed commit
hashes.
