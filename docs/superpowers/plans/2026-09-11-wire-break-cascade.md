# Wire Break Cascade Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Exception for this plan:** per explicit direction from Peter (NDO-728, 2026-09-11), this plan is being executed directly in the live interactive session, not via subagent-driven-development's fresh-implementer/fresh-reviewer loop -- a single well-scoped check doesn't warrant that machinery. Noted here so a future reader isn't confused about why no ledger/ worktree exists for it.

**Goal:** Add a `WB` (Wire Break) history entry when a cast's TEMP record ends in an unrecovered run of NaN samples, per the approved design spec (`docs/superpowers/specs/2026-09-11-wire-break-cascade-design.md`).

**Architecture:** One new `_flag_wire_break_cascade(qc, now)` function in `parse_xbt_edf.py`, matching the shape of the existing `_flag_spikes`/`_flag_neighbour_average_spikes` functions, wired into `apply_qc()` alongside them. Audit-trail only -- it appends a `HistoryEntry`, it does not change any `_qc` array (every NaN sample already becomes `GTSPP_MISSING` via `CastQC.__post_init__`, before any check runs).

**Tech Stack:** Python, numpy, pytest (matches the rest of the repo).

## Global Constraints

- Applies to every cast, real and self-test (matches how every other physical-plausibility check in `apply_qc()` is scoped).
- No minimum run length -- any terminal NaN run (length >= 1) triggers the entry.
- Does not touch `temperature_qc` or any other `_qc` array.
- New `qc_flag` code: `"WB"`.
- Cite CSIRO XBT QC Cookbook v1.1 section 3.2 in the description.
- Real-data regression must find exactly 130 of the 369 real casts in the historical archive getting a `WB` entry (the number found during design investigation) -- a different number means the implementation doesn't match the designed rule.

---

### Task 1: Implement `_flag_wire_break_cascade` and wire it into `apply_qc`

**Files:**
- Modify: `parse_xbt_edf.py` (new function before `apply_qc`, one new call inside `apply_qc`, `apply_qc`'s own docstring, possibly `_N_HISTORY` in `build_xbt_netcdf.py`)
- Modify: `build_xbt_netcdf.py:43` (`_N_HISTORY`, only if Step 8 below finds the true worst case now exceeds 12)
- Test: `tests/test_parse_xbt_edf_qc.py`
- Test: `tests/test_build_xbt_netcdf.py` (worst-case fixture, only if it changes)

**Interfaces:**
- Produces: `_flag_wire_break_cascade(qc: CastQC, now: datetime) -> None`, called from `apply_qc()` right after `_flag_neighbour_average_spikes(qc, now)` (`parse_xbt_edf.py:928`, the existing call in the `apply_qc` body shown below), before the `DEPTH`/`SOUND_VELOCITY`/`LATITUDE`/`LONGITUDE` range checks. Reads `qc.cast.temperature_c` and `qc.cast.depth_m` (already CS-masked by the time it runs, since CS masking happens earlier via `_remove_surface_spike`, before `CastQC` is even constructed).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_parse_xbt_edf_qc.py` (uses the existing `_cast()` helper already in that file):

```python
def test_temperature_ending_in_unrecovered_nan_run_gets_a_wb_entry():
    cast = _cast(
        depth_m=np.array([10.0, 11.0, 12.0, 13.0]),
        temperature_c=np.array([10.0, 10.0, np.nan, np.nan]),
    )
    [qc] = apply_qc([cast])
    wb_entries = [h for h in qc.history if h.qc_flag == "WB"]
    assert len(wb_entries) == 1
    assert wb_entries[0].start_depth == 12.0
    assert wb_entries[0].stop_depth == 13.0
    assert "section 3.2" in wb_entries[0].qc_flag_description


def test_temperature_nan_run_that_recovers_before_the_end_gets_no_wb_entry():
    cast = _cast(
        depth_m=np.array([10.0, 11.0, 12.0, 13.0]),
        temperature_c=np.array([10.0, np.nan, np.nan, 10.0]),
    )
    [qc] = apply_qc([cast])
    assert "WB" not in [h.qc_flag for h in qc.history]


def test_temperature_with_no_nan_at_all_gets_no_wb_entry():
    cast = _cast(temperature_c=np.array([10.0, 10.0, 10.0]))
    [qc] = apply_qc([cast])
    assert "WB" not in [h.qc_flag for h in qc.history]


def test_temperature_ending_in_a_single_nan_sample_still_gets_a_wb_entry():
    cast = _cast(
        depth_m=np.array([10.0, 11.0, 12.0]),
        temperature_c=np.array([10.0, 10.0, np.nan]),
    )
    [qc] = apply_qc([cast])
    wb_entries = [h for h in qc.history if h.qc_flag == "WB"]
    assert len(wb_entries) == 1
    assert wb_entries[0].start_depth == 12.0
    assert wb_entries[0].stop_depth == 12.0


def test_temperature_with_a_recovering_run_and_a_terminal_run_gets_one_wb_entry():
    # The recovering run (indices 1-2) must NOT produce its own WB entry --
    # only the terminal run (index 5) should.
    cast = _cast(
        depth_m=np.array([10.0, 11.0, 12.0, 13.0, 14.0, 15.0]),
        temperature_c=np.array([10.0, np.nan, np.nan, 10.0, 10.0, np.nan]),
    )
    [qc] = apply_qc([cast])
    wb_entries = [h for h in qc.history if h.qc_flag == "WB"]
    assert len(wb_entries) == 1
    assert wb_entries[0].start_depth == 15.0
    assert wb_entries[0].stop_depth == 15.0


def test_wire_break_cascade_check_also_runs_on_test_probe_casts():
    cast = _cast(
        serial_number="TestProbe",
        depth_m=np.array([1.0, 2.0, 3.0]),
        temperature_c=np.array([1.5, 1.5, np.nan]),
    )
    [qc] = apply_qc([cast])
    assert "WB" in [h.qc_flag for h in qc.history]
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `python3 -m pytest tests/test_parse_xbt_edf_qc.py -k wb_entry -v`
Expected: FAIL with `AttributeError` or the assertions failing (no `WB` ever appended, since `_flag_wire_break_cascade` doesn't exist yet).

- [ ] **Step 3: Implement `_flag_wire_break_cascade`**

Add this function to `parse_xbt_edf.py`, directly after `_flag_neighbour_average_spikes` (before `apply_qc`):

```python
def _flag_wire_break_cascade(qc: CastQC, now: datetime) -> None:
    """Flags a cast whose TEMP record ends in an unrecovered run of NaN
    samples -- the automated form of the cookbook's own Wire Break check
    (v1.1 section 3.2): "a short circuit causes the temperature readings to
    go off scale... downgrade data to Class 4 from depth of initial point of
    damage." Audit-trail only: every NaN sample already becomes
    GTSPP_MISSING via CastQC.__post_init__, before any check runs, so this
    doesn't change temperature_qc -- it documents WHY the tail is missing.
    Deliberately narrower than a literal reading of Alison Herbert's original
    suggestion ("as soon as there's a -99, discard the rest of the cast") --
    see this check's own design spec for the real-data investigation that
    found her literal wording would discard most of some real casts over a
    single mid-cast dropout the instrument evidently recovered from. Applied
    to every cast, real or test-probe, matching every other
    physical-plausibility check in apply_qc()."""
    temp = qc.cast.temperature_c
    n = temp.size
    if n == 0 or not np.isnan(temp[-1]):
        return
    end = n
    start = n - 1
    while start > 0 and np.isnan(temp[start - 1]):
        start -= 1
    depth = qc.cast.depth_m
    run_depths = depth[start:end]
    valid_run_depths = run_depths[~np.isnan(run_depths)]
    qc.history.append(HistoryEntry(
        institution=_INSTITUTION, step=_QC_STEP, software=_SOFTWARE,
        software_release=_SOFTWARE_RELEASE, date=now, parameter="TEMP",
        start_depth=float(np.min(valid_run_depths)) if valid_run_depths.size else 0.0,
        stop_depth=float(np.max(valid_run_depths)) if valid_run_depths.size else 0.0,
        qc_flag="WB",
        qc_flag_description=(
            f"{end - start} TEMP sample(s) ending the cast with no recovery -- "
            "Wire Break signature (CSIRO XBT QC Cookbook v1.1 section 3.2)"
        ),
    ))
```

Wire it into `apply_qc()` immediately after the existing `_flag_neighbour_average_spikes(qc, now)` call:

```python
        _flag_temperature_out_of_depth_band_range(qc, now)
        _flag_spikes(qc, now)
        _flag_neighbour_average_spikes(qc, now)
        _flag_wire_break_cascade(qc, now)
        _flag_array_out_of_range(qc, qc.depth_qc, cast.depth_m,
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `python3 -m pytest tests/test_parse_xbt_edf_qc.py -k wb_entry -v`
Expected: PASS (all 6 tests).

- [ ] **Step 5: Run the full test suite and see what else breaks**

Run: `python3 -m pytest tests/ -v 2>&1 | tail -60`

Expect at least `test_n_history_capacity_covers_the_worst_case_cast` (in `tests/test_build_xbt_netcdf.py`) to change, because that fixture's `second` cast already ends in a CS-masked (depth < 3.7 m) NaN point at index 2 -- it will now also earn a `WB` entry. Check the full failure list before assuming this is the only one; fix each failure by understanding *why* the new check fired there (a test using a temperature array that happens to end in NaN), not by blindly editing assertions.

- [ ] **Step 6: Fix `test_n_history_capacity_covers_the_worst_case_cast`**

Read `tests/test_build_xbt_netcdf.py:119-147` first (`Read` the file, don't guess from memory -- this plan's snapshot of it may already be stale by the time you run this task). Update the expected `HISTORY_QC_FLAG` list to include `"WB"` in the correct position (it fires after `SP`, alongside the existing checks, so it lands right after the existing `"SP"` entry and before the `"RC"` entries -- match `apply_qc`'s actual call order) and update the trailing `""` padding count to match (one fewer `""`, since one more real entry now fires). Update the test's own explanatory comment too -- it currently says the fixture produces "exactly 10 real entries"; that number needs to become 11.

- [ ] **Step 7: Re-run the full suite**

Run: `python3 -m pytest tests/ -v 2>&1 | tail -30`
Expected: all pass.

- [ ] **Step 8: Determine the true worst-case entry count and update `_N_HISTORY`/the `apply_qc` docstring if needed**

`apply_qc`'s docstring currently claims a real cast's theoretical worst case is exactly 12 entries (CS 1 + PE/TE 2 + PR 1 + SP-isolated 1 + SP-neighbour 1 + RC up to 6 = 12), matching `_N_HISTORY = 12` with zero slack. Work out on paper (or with a constructed test cast) whether a cast can simultaneously hit every one of those 12 AND separately end in an unrecovered NaN run for the new WB check -- the RC entries can come from out-of-range *real* values earlier/elsewhere in the profile while the profile's own last points are independently NaN, so these are not mutually exclusive. If a worst-case cast can reach 13, bump `_N_HISTORY` to 13 in `build_xbt_netcdf.py:43` and update `apply_qc`'s docstring (both the real-cast max and the test-probe max, which also gains a possible WB entry, going from 9 to 10) to say so explicitly, citing WB the same way the other checks are cited. If you construct a genuine 13-entry worst-case test cast while verifying this, consider whether `test_n_history_capacity_covers_the_worst_case_cast` should be extended to actually hit the true ceiling rather than stopping at 11 -- use your judgement, note the reasoning in a comment either way.

- [ ] **Step 9: Re-run the full suite one more time**

Run: `python3 -m pytest tests/ -v 2>&1 | tail -10`
Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add parse_xbt_edf.py build_xbt_netcdf.py tests/test_parse_xbt_edf_qc.py tests/test_build_xbt_netcdf.py
git commit -m "$(cat <<'EOF'
feat: automate Wire Break cascade detection (NDO-728)

Adds a WB history entry when a cast's TEMP record ends in an unrecovered
run of NaN samples -- the automated form of CSIRO XBT QC Cookbook v1.1
section 3.2, suggested by Alison Herbert after reviewing the NDO-654/645
XBT self-test findings. Deliberately narrower than her literal wording
("discard everything after any -99") -- see this check's own design spec
for the real-data investigation behind that choice. Audit-trail only: no
_qc array changes, since every NaN sample already becomes GTSPP_MISSING
before any check runs.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BJquRw5tQxSjGtAAVFh1ib
EOF
)"
git push origin master
```

---

### Task 2: Real-data regression -- confirm exactly 130 of 369 real casts get a WB entry

**Files:**
- Create (scratch, not committed to the repo): a script run on Skippy against `/mnt/data/VoyageData`, matching the pattern used for the design investigation.

**Interfaces:**
- Consumes: `_flag_wire_break_cascade` (Task 1), via the real `apply_qc()` under the production venv.

- [ ] **Step 1: Deploy the updated package to Skippy**

```bash
ssh aadc@172.16.29.7 "source /home/aadc/uwy_venv/bin/activate && pip install --upgrade --force-reinstall --no-deps 'git+https://github.com/botheredbybees/xbt-edf-qc.git' && python3 -c 'import parse_xbt_edf; print(hasattr(parse_xbt_edf, \"_flag_wire_break_cascade\"))'"
```

Expected: prints `True`. Remember plain `--upgrade` alone silently no-ops for this package (its version string never bumps) -- `--force-reinstall --no-deps` is required, confirmed the hard way during NDO-705/708's deploy.

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
wb_count = sum(1 for qc in casts_qc if "WB" in [h.qc_flag for h in qc.history])
print(f"Real casts: {len(real_casts)}, WB entries: {wb_count}")
```

Run it under the same venv as Step 1. Expected: `Real casts: 369, WB entries: 130` (369 real casts total, per the design investigation's own count -- if the total differs, the archive has changed since design time, which is fine; what matters is the WB count matching the cascade-run count found during design for whatever the current real-cast total is).

- [ ] **Step 3: If the count doesn't match, investigate before proceeding**

Do not adjust the expected number to match the code's output. If they disagree, read through a handful of the newly-WB-flagged (or newly-unflagged) casts by hand and determine whether the implementation has a bug (most likely: an off-by-one in the run-boundary search, or the CS-masking interaction) or the design's own investigation script had one. Fix whichever is wrong. This step has no fixed number of sub-steps -- work the discrepancy to a real, understood conclusion, the same discipline this repo's own `QC_COOKBOOK.md` documents for every other threshold.

---

### Task 3: Update `QC_COOKBOOK.md`, deploy, and close out

**Files:**
- Modify: `QC_COOKBOOK.md`
- Modify (on the ship, via deploy steps below, not a repo file): none further -- Task 2 already deployed the code.

- [ ] **Step 1: Add a new QC_COOKBOOK.md section**

Read the current file first (`Read QC_COOKBOOK.md`) -- it has moved since this plan was written (NDO-705/708 restructured it 2026-09-10). Add a new subsection to the "Checks implemented" part of the document, sibling to "Isolated readings with no real neighbours -- SP", titled something like `### Wire Break cascade — WB (v1.1 section 3.2)`. It must cover, plainly, in this order:
1. What the check does (ends-in-unrecovered-NaN-run -> WB history entry, no `_qc` array change).
2. That it's audit-trail only, and why (every NaN already becomes `GTSPP_MISSING` regardless).
3. **The real-data investigation finding** (133/369 real casts have a NaN run, 130 reach the end with no recovery, 44 of those also have an earlier recovering run, one real case would lose 878 of 1226 samples under Alison's literal wording) and why the shipped rule is narrower than what was originally suggested.
4. **Explicitly, in its own clearly-labelled paragraph** -- per an explicit ask, not optional -- that unlike every other check in this document, this rule's justification in the self-test case rests on *ship procedure* (alligator clips physically removed before "End Probe Drop" is pressed in WinMK21, producing an open circuit that pins the reading at the `-99` sentinel with no recovery through to the end of the test), not purely on the data. A reader should not come away thinking this is a purely statistical rule the way the rest of the document's checks are.

Also update:
- The checks-at-a-glance table (near the top): add a `WB` row, Status `✅ Implemented (audit-trail only, no `_qc` change)`.
- The table of contents: add a link to the new section.
- The Constants reference table: this check has no tunable constant (no minimum run length), so add a note in the new section itself rather than a spurious table row -- don't invent a constant that doesn't exist just to fill a row.

- [ ] **Step 2: Commit and push**

```bash
git add QC_COOKBOOK.md
git commit -m "$(cat <<'EOF'
docs: document Wire Break cascade detection (NDO-728)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BJquRw5tQxSjGtAAVFh1ib
EOF
)"
git push origin master
```

- [ ] **Step 3: Re-run the historical backfill and re-upload to AADC's S3 bucket**

```bash
ssh aadc@172.16.29.7 "cd ~/cron-jobs && source /home/aadc/uwy_venv/bin/activate && python3 backfill_xbt_netcdf_historical.py"
```

Then re-upload via `rclone` (remote `aadc-kingston`) to `aadc-nuyina/xbt_historical_backfill/`, and verify with `rclone check` that 0 differences remain -- same commands as NDO-705/708's deploy (see the current voyage's handover notes for the exact prior invocation).

- [ ] **Step 4: Spot-check the real published output**

Confirm the rebuilt archive's `HISTORY_QC_FLAG` array contains `WB` entries, and that the count matches Task 2's regression figure (adjust for whatever the live archive's real-cast total is at deploy time, same caveat as Task 2 Step 2).

- [ ] **Step 5: Add a dated handover-notes entry**

Pull `newinapedia` first (Tess may have edited the same page), then append a new `### YYYY MM DD` section to the current voyage's page (find it the same way as prior entries -- check the most recent dated section for the current voyage ID) describing what was deployed: the WB check, the real-data investigation that narrowed the rule from Alison's original suggestion, and that this is audit-trail only (no reclassification of previously-published data). Commit and push, pathspec-scoped to just that file.

- [ ] **Step 6: Update Jira**

Transition NDO-728 to Done (`mcp__atlassian__getTransitionsForJiraIssue` first to confirm the transition ID, same as every other ticket this session), with a comment citing the real validation numbers (130/369, or whatever Task 2 actually found) and the deployed commit hashes.
