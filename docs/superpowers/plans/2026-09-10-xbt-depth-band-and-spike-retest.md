# XBT Depth-Band Envelope and Spike-Test Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a depth-banded TEMP range check (NDO-705) and a neighbour-average spike test at
GTSPP's 2.0°C threshold (NDO-708) to `parse_xbt_edf.py`, both already validated against the real
369-profile historical archive during design.

**Architecture:** Two new functions in `parse_xbt_edf.py`, following the exact pattern
`_flag_array_out_of_range`/`_flag_spikes` already use (mutate `qc.temperature_qc` in place, append
a `HistoryEntry`). The depth-banded check replaces TEMP's existing flat-range call in `apply_qc()`;
the spike test is additive alongside the existing isolated-only `_flag_spikes`. `QC_COOKBOOK.md`
updated to document both. Deployed the same way every prior fix to this pipeline was: pushed to
GitHub, `pip install --upgrade` on Skippy's `uwy_venv` (a separate step from `git pull` on
`cron_jobs_on_skippy`, since this logic now lives in the installed package), historical backfill
re-run and re-uploaded to AADC's S3 bucket.

**Tech Stack:** Python 3.8+, numpy, pytest. No new dependencies.

## Global Constraints

- GTSPP flag convention: `GTSPP_GOOD=1`, `GTSPP_PROBABLY_BAD=3`, `GTSPP_MISSING=9` (already defined).
- Every check only **flags**, never corrects/interpolates a value (matches this file's existing
  convention throughout).
- `TEMP_VALID_MIN`/`TEMP_VALID_MAX` keep their current names and values — they now implicitly mean
  "the shallow band (depth < `TEMP_DEEP_BAND_DEPTH_M`)", not "the whole cast".
- No new pip dependency.
- TDD throughout, matching this repo's established convention (failing test first).
- `git remote -v` confirms `github.com/botheredbybees/xbt-edf-qc` — normal AI-attribution commit
  trailers apply in this repo (public GitHub under Peter's personal account, not AAD Bitbucket).

## File Structure

- **Modify** `parse_xbt_edf.py` — add `TEMP_DEEP_BAND_DEPTH_M`, `TEMP_DEEP_VALID_MAX`,
  `_flag_temperature_out_of_depth_band_range` (replaces TEMP's `_flag_array_out_of_range` call in
  `apply_qc()`); add `SPIKE_NEIGHBOUR_AVERAGE_MAX_DELTA_C`, `_flag_neighbour_average_spikes`
  (additive call in `apply_qc()`, right after the existing `_flag_spikes` call); bump
  `apply_qc()`'s own docstring history-count math.
- **Modify** `build_xbt_netcdf.py` — bump `_N_HISTORY` from 10 to 12.
- **Modify** `tests/test_parse_xbt_edf_qc.py` — new tests for both functions, following this
  file's existing `_cast()`/`apply_qc([cast])` pattern.
- **Modify** `QC_COOKBOOK.md` — document both checks, correct the now-superseded "tried, rejected"
  claim about the neighbour-average formula.
- **Create** (scratch, not committed) a real-data regression script run once against the local
  historical archive to confirm the design-time 190/112 figures.

---

### Task 1: Depth-banded TEMP envelope (NDO-705)

**Files:**
- Modify: `parse_xbt_edf.py`
- Modify: `tests/test_parse_xbt_edf_qc.py`

**Interfaces:**
- Produces: `TEMP_DEEP_BAND_DEPTH_M: float`, `TEMP_DEEP_VALID_MAX: float`,
  `_flag_temperature_out_of_depth_band_range(qc: CastQC, now: datetime) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_parse_xbt_edf_qc.py`:

```python
def test_temperature_shallow_band_uses_the_existing_flat_bound():
    from parse_xbt_edf import TEMP_DEEP_BAND_DEPTH_M
    cast = _cast(
        depth_m=np.array([10.0, 11.0, 12.0]),
        temperature_c=np.array([TEMP_VALID_MIN, 25.0, TEMP_VALID_MAX]),
    )
    assert cast.depth_m[-1] < TEMP_DEEP_BAND_DEPTH_M
    [qc] = apply_qc([cast])
    assert np.all(qc.temperature_qc == GTSPP_GOOD)


def test_temperature_shallow_band_still_flags_outside_the_flat_bound():
    cast = _cast(
        depth_m=np.array([10.0, 11.0, 12.0]),
        temperature_c=np.array([10.0, TEMP_VALID_MAX + 1.0, 10.0]),
    )
    [qc] = apply_qc([cast])
    assert list(qc.temperature_qc) == [GTSPP_GOOD, GTSPP_PROBABLY_BAD, GTSPP_GOOD]
    [entry] = [e for e in qc.history if e.qc_flag == "RC" and e.parameter == "TEMP"]
    assert "1 TEMP sample" in entry.qc_flag_description
    assert "depth < 1500.0" in entry.qc_flag_description


def test_temperature_deep_band_flags_a_value_the_shallow_bound_would_have_passed():
    from parse_xbt_edf import TEMP_DEEP_BAND_DEPTH_M, TEMP_DEEP_VALID_MAX
    cast = _cast(
        depth_m=np.array([10.0, TEMP_DEEP_BAND_DEPTH_M + 100.0, 20.0]),
        temperature_c=np.array([5.0, TEMP_DEEP_VALID_MAX + 5.0, 5.0]),
    )
    assert TEMP_DEEP_VALID_MAX + 5.0 <= TEMP_VALID_MAX  # would have passed the old flat bound
    [qc] = apply_qc([cast])
    assert list(qc.temperature_qc) == [GTSPP_GOOD, GTSPP_PROBABLY_BAD, GTSPP_GOOD]
    [entry] = [e for e in qc.history if e.qc_flag == "RC" and e.parameter == "TEMP"]
    assert "depth >= 1500.0" in entry.qc_flag_description


def test_temperature_deep_band_at_exactly_the_boundary_depth_uses_the_tighter_bound():
    from parse_xbt_edf import TEMP_DEEP_BAND_DEPTH_M, TEMP_DEEP_VALID_MAX
    cast = _cast(
        depth_m=np.array([10.0, TEMP_DEEP_BAND_DEPTH_M, 20.0]),
        temperature_c=np.array([5.0, TEMP_DEEP_VALID_MAX + 1.0, 5.0]),
    )
    [qc] = apply_qc([cast])
    assert qc.temperature_qc[1] == GTSPP_PROBABLY_BAD


def test_temperature_deep_band_within_tightened_bound_stays_good():
    from parse_xbt_edf import TEMP_DEEP_BAND_DEPTH_M, TEMP_DEEP_VALID_MAX
    cast = _cast(
        depth_m=np.array([10.0, TEMP_DEEP_BAND_DEPTH_M + 100.0, 20.0]),
        temperature_c=np.array([5.0, TEMP_DEEP_VALID_MAX, 5.0]),
    )
    [qc] = apply_qc([cast])
    assert np.all(qc.temperature_qc == GTSPP_GOOD)


def test_temperature_both_bands_bad_produces_two_separate_rc_entries():
    from parse_xbt_edf import TEMP_DEEP_BAND_DEPTH_M, TEMP_DEEP_VALID_MAX
    cast = _cast(
        depth_m=np.array([10.0, TEMP_DEEP_BAND_DEPTH_M + 100.0]),
        temperature_c=np.array([TEMP_VALID_MAX + 1.0, TEMP_DEEP_VALID_MAX + 1.0]),
    )
    [qc] = apply_qc([cast])
    rc_entries = [e for e in qc.history if e.qc_flag == "RC" and e.parameter == "TEMP"]
    assert len(rc_entries) == 2
    assert list(qc.temperature_qc) == [GTSPP_PROBABLY_BAD, GTSPP_PROBABLY_BAD]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_parse_xbt_edf_qc.py -v -k temperature_shallow_band_uses or temperature_shallow_band_still or temperature_deep_band`
Expected: FAIL — the shallow-band tests fail because `TEMP_DEEP_BAND_DEPTH_M` doesn't exist yet
(`ImportError`); the deep-band tests fail the same way.

- [ ] **Step 3: Add the constants**

In `parse_xbt_edf.py`, directly below the existing `SOUND_VELOCITY_VALID_MIN`/`_MAX` constants
(after line 276):

```python
# GTSPP Real-Time QC Manual (IOC M&G No. 22), Section 2.4 "Profile
# Envelope": temperature's valid range should tighten with depth, not stay
# one flat bound for the whole cast. Validated against the real 369-profile
# historical archive before trusting a specific depth/bound pair
# (2026-09-10, docs/superpowers/specs/2026-09-10-xbt-depth-band-and-spike-
# retest-design.md): at depths >=1500 m there is a clean, wide gap in real
# TEMP values -- nothing above 16.92 degC, then nothing again until 32.04
# degC (the same already-known fault signature this file's SOUND_VELOCITY
# bound and isolated-spike check both independently confirm). Shallower
# depths show NO clean gap -- real regional fronts/eddies (confirmed by
# hand: a genuine, smooth 12.12->16.1 degC rise over 7 m at ~730 m depth)
# sit on a continuum with the same fault values all the way up to 37 degC,
# so TEMP_VALID_MIN/TEMP_VALID_MAX (unchanged) remain the only defensible
# bound there. Only the upper bound tightens at depth -- nothing in the
# real-data investigation suggested the lower bound needs to differ by
# depth.
TEMP_DEEP_BAND_DEPTH_M = 1500.0
TEMP_DEEP_VALID_MAX = 20.0
```

- [ ] **Step 4: Implement `_flag_temperature_out_of_depth_band_range`**

Add to `parse_xbt_edf.py`, directly above `_flag_spikes` (which currently sits below
`_flag_array_out_of_range`/`_flag_scalar_out_of_range`):

```python
def _flag_temperature_out_of_depth_band_range(qc: CastQC, now: datetime) -> None:
    """Depth-banded version of TEMP's physical-plausibility range check --
    see TEMP_DEEP_VALID_MAX's own module-level comment for the real-data
    validation behind the specific depth/bound chosen. Replaces the plain
    _flag_array_out_of_range(..., TEMP_VALID_MIN, TEMP_VALID_MAX, "TEMP",
    ...) call in apply_qc() -- this IS TEMP's range check now, not an
    addition alongside it. DEPTH_VALUES/SOUND_VELOCITY are unaffected,
    still handled by the shared, non-banded _flag_array_out_of_range."""
    temp = qc.cast.temperature_c
    depth = qc.cast.depth_m
    shallow = depth < TEMP_DEEP_BAND_DEPTH_M
    deep = ~shallow

    shallow_bad = shallow & ((temp < TEMP_VALID_MIN) | (temp > TEMP_VALID_MAX))
    deep_bad = deep & ((temp < TEMP_VALID_MIN) | (temp > TEMP_DEEP_VALID_MAX))

    if np.any(shallow_bad):
        qc.temperature_qc[shallow_bad] = GTSPP_PROBABLY_BAD
        bad_depths = depth[shallow_bad]
        valid_bad_depths = bad_depths[~np.isnan(bad_depths)]
        qc.history.append(HistoryEntry(
            institution=_INSTITUTION, step=_QC_STEP, software=_SOFTWARE,
            software_release=_SOFTWARE_RELEASE, date=now, parameter="TEMP",
            start_depth=float(np.min(valid_bad_depths)) if valid_bad_depths.size else 0.0,
            stop_depth=float(np.max(valid_bad_depths)) if valid_bad_depths.size else 0.0,
            qc_flag="RC",
            qc_flag_description=(
                f"{int(np.sum(shallow_bad))} TEMP sample(s) outside valid range "
                f"[{TEMP_VALID_MIN}, {TEMP_VALID_MAX}] degC (depth < {TEMP_DEEP_BAND_DEPTH_M} m)"
            ),
        ))

    if np.any(deep_bad):
        qc.temperature_qc[deep_bad] = GTSPP_PROBABLY_BAD
        bad_depths = depth[deep_bad]
        valid_bad_depths = bad_depths[~np.isnan(bad_depths)]
        qc.history.append(HistoryEntry(
            institution=_INSTITUTION, step=_QC_STEP, software=_SOFTWARE,
            software_release=_SOFTWARE_RELEASE, date=now, parameter="TEMP",
            start_depth=float(np.min(valid_bad_depths)) if valid_bad_depths.size else 0.0,
            stop_depth=float(np.max(valid_bad_depths)) if valid_bad_depths.size else 0.0,
            qc_flag="RC",
            qc_flag_description=(
                f"{int(np.sum(deep_bad))} TEMP sample(s) outside valid range "
                f"[{TEMP_VALID_MIN}, {TEMP_DEEP_VALID_MAX}] degC (depth >= {TEMP_DEEP_BAND_DEPTH_M} m)"
            ),
        ))
```

- [ ] **Step 5: Wire it into `apply_qc()`, replacing TEMP's flat-range call**

In `parse_xbt_edf.py`'s `apply_qc()`, change:

```python
        _flag_array_out_of_range(qc, qc.temperature_qc, cast.temperature_c,
                                  TEMP_VALID_MIN, TEMP_VALID_MAX, "TEMP", "degC", now)
```

to:

```python
        _flag_temperature_out_of_depth_band_range(qc, now)
```

(This line sits immediately before the existing `_flag_spikes(qc, now)` call — leave that call,
and the `DEPTH_VALUES`/`SOUND_VELOCITY`/`LATITUDE`/`LONGITUDE` `_flag_array_out_of_range` calls
right after it, untouched.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_parse_xbt_edf_qc.py -v -k "temperature_shallow_band or temperature_deep_band or temperature_both_bands"`
Expected: 6 passed

- [ ] **Step 7: Run the full existing suite to confirm nothing regressed**

Run: `pytest tests/ -v`
Expected: all previously-passing tests still pass (the two pre-existing flat-range tests,
`test_temperature_outside_valid_range_flags_only_the_bad_points` and
`test_temperature_within_valid_range_never_gets_an_rc_entry`, both use depths well under 1500m in
`_cast()`'s default, so they exercise the shallow band and must be unaffected).

- [ ] **Step 8: Commit**

```bash
git add parse_xbt_edf.py tests/test_parse_xbt_edf_qc.py
git commit -m "feat: depth-band TEMP range check at 1500m (NDO-705)

Validated against the full 369-profile historical archive during design
-- see docs/superpowers/specs/2026-09-10-xbt-depth-band-and-spike-retest-design.md."
```

---

### Task 2: Neighbour-average spike test at GTSPP's 2.0°C (NDO-708)

**Files:**
- Modify: `parse_xbt_edf.py`
- Modify: `build_xbt_netcdf.py`
- Modify: `tests/test_parse_xbt_edf_qc.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `SPIKE_NEIGHBOUR_AVERAGE_MAX_DELTA_C: float`,
  `_flag_neighbour_average_spikes(qc: CastQC, now: datetime) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_parse_xbt_edf_qc.py`:

```python
def test_neighbour_average_spike_agreeing_neighbours_stays_good():
    cast = _cast(
        depth_m=np.array([10.0, 11.0, 12.0]),
        temperature_c=np.array([5.0, 5.5, 5.0]),
    )
    [qc] = apply_qc([cast])
    assert np.all(qc.temperature_qc == GTSPP_GOOD)
    assert "SP" not in [e.qc_flag for e in qc.history]


def test_neighbour_average_spike_real_spike_is_flagged():
    from parse_xbt_edf import SPIKE_NEIGHBOUR_AVERAGE_MAX_DELTA_C
    cast = _cast(
        depth_m=np.array([10.0, 11.0, 12.0]),
        temperature_c=np.array([5.0, 5.0 + SPIKE_NEIGHBOUR_AVERAGE_MAX_DELTA_C + 1.0, 5.0]),
    )
    [qc] = apply_qc([cast])
    assert list(qc.temperature_qc) == [GTSPP_GOOD, GTSPP_PROBABLY_BAD, GTSPP_GOOD]
    [entry] = [e for e in qc.history if e.qc_flag == "SP" and "neighbours" in e.qc_flag_description]
    assert "1 TEMP reading" in entry.qc_flag_description
    assert "2.0" in entry.qc_flag_description


def test_neighbour_average_spike_exactly_at_threshold_stays_good():
    from parse_xbt_edf import SPIKE_NEIGHBOUR_AVERAGE_MAX_DELTA_C
    cast = _cast(
        depth_m=np.array([10.0, 11.0, 12.0]),
        temperature_c=np.array([5.0, 5.0 + SPIKE_NEIGHBOUR_AVERAGE_MAX_DELTA_C, 5.0]),
    )
    [qc] = apply_qc([cast])
    assert np.all(qc.temperature_qc == GTSPP_GOOD)


def test_neighbour_average_spike_one_missing_neighbour_is_not_evaluated():
    # Complementary to _flag_spikes -- that check handles the fully-isolated
    # (both sides missing) case; this one needs BOTH real neighbours to
    # compute an average at all.
    cast = _cast(
        depth_m=np.array([10.0, 11.0, 12.0]),
        temperature_c=np.array([5.0, np.nan, 20.0]),
    )
    [qc] = apply_qc([cast])
    assert list(qc.temperature_qc) == [GTSPP_GOOD, GTSPP_MISSING, GTSPP_GOOD]
    assert "SP" not in [e.qc_flag for e in qc.history]


def test_neighbour_average_spike_catches_a_synthetic_wire_stretch_ramp():
    # The real-data validation found this formula also catches the
    # "wire-stretch" fault class (a smooth, physically-impossible ramp) as
    # a side effect, not just isolated single-point spikes -- confirm that
    # shape is genuinely detected, not just a coincidence of the real data.
    cast = _cast(
        depth_m=np.array([10.0, 11.0, 12.0, 13.0, 14.0, 15.0]),
        temperature_c=np.array([-1.4, -1.4, -1.0, 9.5, 21.7, 29.4]),
    )
    [qc] = apply_qc([cast])
    assert np.sum(qc.temperature_qc == GTSPP_PROBABLY_BAD) >= 2


def test_neighbour_average_spike_check_also_applies_to_test_probe_casts():
    cast = _cast(
        serial_number="TestProbe",
        depth_m=np.array([0.0, 1.0, 2.0]),
        temperature_c=np.array([1.51, 1.5 + 5.0, 1.49]),
    )
    [qc] = apply_qc([cast])
    assert qc.temperature_qc[1] == GTSPP_PROBABLY_BAD
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_parse_xbt_edf_qc.py -v -k neighbour_average_spike`
Expected: FAIL with `ImportError: cannot import name 'SPIKE_NEIGHBOUR_AVERAGE_MAX_DELTA_C'`

- [ ] **Step 3: Add the constant**

In `parse_xbt_edf.py`, directly below `TEMP_DEEP_VALID_MAX` (added in Task 1):

```python
# GTSPP Real-Time QC Manual (IOC M&G No. 22)'s own spike-test threshold --
# 10x looser than the CSIRO cookbook's 0.2 degC, which this pipeline
# already tried and rejected for 97 false positives on real fine-scale
# ocean structure (see QC_COOKBOOK.md's "What we tried and rejected").
# Validated against the full 369-profile historical archive before
# trusting it (2026-09-10, docs/superpowers/specs/2026-09-10-xbt-depth-
# band-and-spike-retest-design.md): 190 point-flags across 112 profiles,
# zero apparent false positives on real structure -- every flagged point
# is either a genuine isolated spike or a physically-impossible smooth
# ramp (the "wire-stretch" fault class this file already documents as
# found-but-not-automated, caught here as a side effect of the same
# formula).
SPIKE_NEIGHBOUR_AVERAGE_MAX_DELTA_C = 2.0
```

- [ ] **Step 4: Implement `_flag_neighbour_average_spikes`**

Add to `parse_xbt_edf.py`, directly below `_flag_spikes`:

```python
def _flag_neighbour_average_spikes(qc: CastQC, now: datetime) -> None:
    """Flags a real TEMP value that deviates from the average of its two
    real immediate neighbours by more than SPIKE_NEIGHBOUR_AVERAGE_MAX_DELTA_C
    (the standard 3-point spike-test formula, GTSPP Real-Time QC Manual,
    IOC M&G No. 22). Additive to _flag_spikes (which only catches a reading
    with NO real data on either side) -- this catches a reading with real,
    but very different, neighbours instead. See
    SPIKE_NEIGHBOUR_AVERAGE_MAX_DELTA_C's own comment for the real-data
    validation behind this specific threshold. Applied to every cast, real
    or test-probe, same as every other physical-plausibility check."""
    temp = qc.cast.temperature_c
    n = temp.size
    if n < 3:
        return
    flagged = np.zeros(n, dtype=bool)
    for i in range(1, n - 1):
        v1, v2, v3 = temp[i - 1], temp[i], temp[i + 1]
        if np.isnan(v1) or np.isnan(v2) or np.isnan(v3):
            continue
        if abs(v2 - (v1 + v3) / 2.0) > SPIKE_NEIGHBOUR_AVERAGE_MAX_DELTA_C:
            flagged[i] = True
    if not np.any(flagged):
        return

    qc.temperature_qc[flagged] = GTSPP_PROBABLY_BAD
    bad_depths = qc.cast.depth_m[flagged]
    valid_bad_depths = bad_depths[~np.isnan(bad_depths)]
    qc.history.append(HistoryEntry(
        institution=_INSTITUTION, step=_QC_STEP, software=_SOFTWARE,
        software_release=_SOFTWARE_RELEASE, date=now, parameter="TEMP",
        start_depth=float(np.min(valid_bad_depths)) if valid_bad_depths.size else 0.0,
        stop_depth=float(np.max(valid_bad_depths)) if valid_bad_depths.size else 0.0,
        qc_flag="SP",
        qc_flag_description=(
            f"{int(np.sum(flagged))} TEMP reading(s) deviating from the average of "
            f"their real neighbours by more than {SPIKE_NEIGHBOUR_AVERAGE_MAX_DELTA_C} degC "
            "(CSIRO XBT QC Cookbook v1.1 section 3.3 / GTSPP Real-Time QC Manual spike test)"
        ),
    ))
```

- [ ] **Step 5: Wire it into `apply_qc()`, additive after the existing isolated-spike check**

In `parse_xbt_edf.py`'s `apply_qc()`, change:

```python
        _flag_temperature_out_of_depth_band_range(qc, now)
        _flag_spikes(qc, now)
```

to:

```python
        _flag_temperature_out_of_depth_band_range(qc, now)
        _flag_spikes(qc, now)
        _flag_neighbour_average_spikes(qc, now)
```

- [ ] **Step 6: Update `apply_qc()`'s own docstring history-count math**

The docstring currently reads (after Task 1's change, the TEMP RC count is already effectively 2,
not 1, but the docstring prose hasn't been updated yet — do that now together with this task's
addition):

```
    A cast can accumulate at most 10 history entries: the surface-spike
    check emits 1 (CS), the speed check 2 (PE and TE, which cannot be told
    apart automatically), the probe-type check 1, the isolated-spike check 1
    (SP), and the physical-plausibility range check up to 5 more (RC on
    TEMP, DEPTH, SOUND_VELOCITY, LATITUDE, LONGITUDE -- each independent, so
    worst case all 5 fire on the same cast). A test probe cast never reaches
    the surface-spike check (see _remove_surface_spike's docstring) or the
    first two, and emits at most 7 (TP + SP + up to 5 RC). Keep
    build_xbt_netcdf._N_HISTORY at or above that ceiling.
```

Replace with:

```
    A cast can accumulate at most 12 history entries: the surface-spike
    check emits 1 (CS), the speed check 2 (PE and TE, which cannot be told
    apart automatically), the probe-type check 1, the isolated-spike check 1
    (SP), the neighbour-average spike check 1 more (SP -- a second, distinct
    entry, since a cast can trigger both the isolated and the
    neighbour-average check independently), and the physical-plausibility
    range check up to 6 more (RC -- TEMP alone can now produce 2 entries,
    one per depth band, plus DEPTH, SOUND_VELOCITY, LATITUDE, LONGITUDE each
    independently, so worst case 6 RC entries on the same cast). A test
    probe cast never reaches the surface-spike check (see
    _remove_surface_spike's docstring) or the first two, and emits at most
    9 (TP + both SP checks + up to 6 RC). Keep build_xbt_netcdf._N_HISTORY
    at or above that ceiling.
```

- [ ] **Step 7: Bump `_N_HISTORY` in `build_xbt_netcdf.py`**

Change:

```python
_N_HISTORY = 10
```

to:

```python
_N_HISTORY = 12
```

- [ ] **Step 8: Fix a pre-existing test this change genuinely breaks**

`test_a_cast_can_trigger_at_most_four_history_entries` (already in `tests/test_parse_xbt_edf_qc.py`,
added before this plan) uses `temperature_c=np.array([10.0, 999.0, 10.0])` — real (non-NaN)
neighbours on both sides of `999.0`. Its own comment explicitly says "the isolated-spike check does
not fire here since 999.0 has real (not missing) neighbours on both sides" — true for the OLD
isolated-only check, but the new neighbour-average check added in this task exists specifically to
catch exactly this shape (a real value with real, very different neighbours). This is a genuine,
expected behaviour change, not a bug — update the test to match reality rather than leaving a now-
false comment and a now-failing assertion in the suite.

Change:

```python
def test_a_cast_can_trigger_at_most_four_history_entries():
    # The speed check emits 2 entries (PE + TE), the probe-type check 1, and
    # the TEMP range check 1 (RC) -- the isolated-spike check does not fire
    # here since 999.0 has real (not missing) neighbours on both sides.
    first = _cast(launch_time=datetime(2025, 3, 1, 12, 0, 0), latitude=-42.0, longitude=147.0)
    second = _cast(
        launch_time=datetime(2025, 3, 1, 12, 10, 0), latitude=-41.0, longitude=147.0,
        probe_type_raw="NotARealProbe", probe_type="NotARealProbe",
        temperature_c=np.array([10.0, 999.0, 10.0]),
    )
    _, qc_second = apply_qc([first, second])
    assert [entry.qc_flag for entry in qc_second.history] == ["PE", "TE", "PR", "RC"]

    from build_xbt_netcdf import _N_HISTORY
    assert len(qc_second.history) <= _N_HISTORY
```

to:

```python
def test_a_cast_can_trigger_at_most_five_history_entries():
    # The speed check emits 2 entries (PE + TE), the probe-type check 1, the
    # TEMP range check 1 (RC), and -- since NDO-708 -- the neighbour-average
    # spike check 1 more (SP): 999.0 has real (non-NaN) neighbours on both
    # sides, which the isolated-only check ignores but the neighbour-average
    # check exists specifically to catch (a real value that wildly disagrees
    # with its real neighbours). This is the expected, validated new
    # behaviour, not a regression -- confirms the two checks are genuinely
    # complementary, not just independently correct in isolation.
    first = _cast(launch_time=datetime(2025, 3, 1, 12, 0, 0), latitude=-42.0, longitude=147.0)
    second = _cast(
        launch_time=datetime(2025, 3, 1, 12, 10, 0), latitude=-41.0, longitude=147.0,
        probe_type_raw="NotARealProbe", probe_type="NotARealProbe",
        temperature_c=np.array([10.0, 999.0, 10.0]),
    )
    _, qc_second = apply_qc([first, second])
    assert [entry.qc_flag for entry in qc_second.history] == ["PE", "TE", "PR", "RC", "SP"]

    from build_xbt_netcdf import _N_HISTORY
    assert len(qc_second.history) <= _N_HISTORY
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `pytest tests/test_parse_xbt_edf_qc.py -v -k "neighbour_average_spike or history_entries"`
Expected: 7 passed (6 new + the updated pre-existing test)

- [ ] **Step 10: Run the full existing suite**

Run: `pytest tests/ -v`
Expected: all tests pass. Specifically confirm no OTHER pre-existing test's `temperature_c` values
happen to have real neighbours that now also trigger the new spike check unexpectedly — grep the
test file for other `temperature_c=` arrays containing an outlier value with real numbers on both
sides if any other test starts failing, and apply the same fix (update the expected `qc_flag` list
and comment) rather than weakening the new check to make an unrelated test pass.

- [ ] **Step 11: Commit**

```bash
git add parse_xbt_edf.py build_xbt_netcdf.py tests/test_parse_xbt_edf_qc.py
git commit -m "feat: neighbour-average spike test at GTSPP's 2.0degC (NDO-708)

Validated against the full 369-profile historical archive during design:
190 flags across 112 profiles, zero apparent false positives, also
catches the previously-unautomated wire-stretch fault class as a side
effect. _N_HISTORY bumped 10 -> 12 (this check + Task 1's depth-banded
TEMP check both add to the per-cast history-entry ceiling)."
```

---

### Task 3: Real-data regression run against the full historical archive

**Files:** none modified — verification only, using a scratch script (not committed).

**Interfaces:** none (verification task).

- [ ] **Step 1: Write a scratch verification script**

Save to `/tmp/verify_xbt_qc_changes.py` (not committed — this is a one-off check, not a permanent
tool; the repo's own pytest suite is the permanent regression coverage):

```python
import sys
sys.path.insert(0, "/home/peter_sha/sourcecode/Nuyina/xbt-edf-qc")
import numpy as np
import xarray as xr
from parse_xbt_edf import (
    TEMP_DEEP_BAND_DEPTH_M, TEMP_DEEP_VALID_MAX,
    SPIKE_NEIGHBOUR_AVERAGE_MAX_DELTA_C,
)

ds = xr.open_dataset("/home/peter_sha/Downloads/xbt_historical_profiles.nc", decode_times=False)
temp = ds["TEMP"].values.astype(float).copy()
depth = ds["DEPTH_VALUES"].values.astype(float)
qc = ds["TEMP_quality_control"].values.copy()
temp[depth < 3.7] = np.nan  # simulate the CS fix on this stale local archive copy

# Depth-band check
shallow = depth < TEMP_DEEP_BAND_DEPTH_M
deep = ~shallow
shallow_bad = shallow & (qc == 1) & ((temp < -2.5) | (temp > 40.0))
deep_bad = deep & (qc == 1) & ((temp < -2.5) | (temp > TEMP_DEEP_VALID_MAX))
print(f"Depth-band newly-flagged: shallow={shallow_bad.sum()} deep={deep_bad.sum()}")

# Spike test
n_profiles, n_depth = temp.shape
flagged = []
for p in range(n_profiles):
    t = temp[p]
    for i in range(1, n_depth - 1):
        v1, v2, v3 = t[i-1], t[i], t[i+1]
        if np.isnan(v1) or np.isnan(v2) or np.isnan(v3):
            continue
        if abs(v2 - (v1 + v3) / 2.0) > SPIKE_NEIGHBOUR_AVERAGE_MAX_DELTA_C and qc[p, i] == 1:
            flagged.append((p, i))
profiles = {p for p, i in flagged}
print(f"Spike test: {len(flagged)} flags across {len(profiles)} profiles")
```

- [ ] **Step 2: Run it and compare against the design-time figures**

Run: `python3 /tmp/verify_xbt_qc_changes.py`
Expected: `Spike test: 190 flags across 112 profiles` (matches the design investigation exactly,
since the constant/formula are unchanged from what was validated). The depth-band print is new —
inspect it, but no specific number was committed to during design (the deep band's own validation
was about the clean *gap*, not a specific flagged count) — confirm the numbers look sane (a small
fraction of the ~11000 real deep-band samples, consistent with the earlier investigation's finding
that the fault cluster there was a handful of profiles, not a large fraction).

- [ ] **Step 3: If either figure differs meaningfully from expectations, stop and investigate**

Do not proceed to Task 4 with an unexplained discrepancy — the whole point of this task is
confirming the shipped constants/formula reproduce what was validated during design. A difference
would mean either a transcription error in Task 1/2's constants, or a real behavioural difference
between the scratch validation logic and the shipped function — both need resolving before this is
considered done.

---

### Task 4: Update `QC_COOKBOOK.md`

**Files:**
- Modify: `QC_COOKBOOK.md`

**Interfaces:** none (documentation task).

- [ ] **Step 1: Update the "Checks at a glance" table**

Find the table row for the isolated-spike check:

```
| SP | [Isolated readings](#isolated-readings-with-no-real-neighbours--sp-v11-sections-32-wire-break-and-33-spikes) (zero real neighbours) | All casts, incl. self-test | TEMP → probably bad | none | ✅ Implemented (narrowed scope) |
| — | ↳ full neighbour-average "Spikes" formula | — | — | — | 🔬 Tried, rejected (97 false positives on real data — [details](#what-we-tried-and-rejected)) |
```

Replace the second row (the rejected-formula one) with:

```
| SP | ↳ full neighbour-average "Spikes" formula, at GTSPP's 2.0°C | All casts, incl. self-test | TEMP → probably bad | none | ✅ Implemented (2026-09-10, at GTSPP's threshold — [details](#what-we-tried-and-rejected)) |
```

Also find and update the range-check row:

```
| RC | [Range check](#physical-plausibility-range-check--rc) | All casts, per variable | Out-of-range points/profiles → probably bad | none | ✅ Implemented |
```

to:

```
| RC | [Range check](#physical-plausibility-range-check--rc) (depth-banded for TEMP since 2026-09-10) | All casts, per variable | Out-of-range points/profiles → probably bad | none | ✅ Implemented |
```

- [ ] **Step 2: Update the "Isolated readings" section's "Not implemented" sub-heading**

Find:

```
#### Not implemented: full neighbour-average "Spikes" formula

**This is narrower than a literal reading of section 3.3.** The cookbook
describes "Spikes" as any isolated deviation from *real* neighbours, with
a >0.2°C threshold and a standard reference-average formula (average the
two neighbours, flag the point between them if it deviates by more than
the threshold — the same formula used in, e.g., the IOOS/QARTOD real-time
QC manuals). **That version was built, then dropped after validation** —
see [What we tried and rejected](#what-we-tried-and-rejected) below. Only
the zero-real-neighbours case ships.
```

Replace with:

```
#### Also implemented, at a different threshold: full neighbour-average "Spikes" formula

**This is broader than a literal reading of section 3.3's own 0.2°C.** The
cookbook describes "Spikes" as any isolated deviation from *real*
neighbours, with a >0.2°C threshold and a standard reference-average
formula (average the two neighbours, flag the point between them if it
deviates by more than the threshold — the same formula used in, e.g., the
IOOS/QARTOD real-time QC manuals, and specified independently by the GTSPP
Real-Time QC Manual, IOC M&G No. 22). **Built and rejected at the
cookbook's own 0.2°C threshold** (97 false positives on real fine-scale
ocean structure) **— then retried and shipped at GTSPP's own, ~10x looser,
2.0°C figure for the same formula** (2026-09-10), after validating that
threshold separately against the full historical archive. See
[What we tried and rejected](#what-we-tried-and-rejected) below for both
results side by side.
```

- [ ] **Step 3: Update the "What we tried and rejected" section**

Find the paragraph beginning "The check was narrowed to only the zero-real-neighbours case..." and
add a new paragraph directly after it:

```
**Revisited 2026-09-10 at GTSPP's own 2.0°C threshold for the same formula** (the Real-Time QC
Manual, IOC M&G No. 22, specifies this exact 3-point formula independently of the CSIRO cookbook,
at a threshold ~10x looser than 0.2°C). Re-run against the full 369-profile historical archive:
190 point-flags across 112 profiles (30%), and every single one inspected by hand (a window of the
surrounding samples) showed either a genuine isolated spike near a cast's end, or a
smooth-but-physically-impossible ramp — exactly the "Wire Stretch" fault class described below,
which this formula turns out to also catch as a side effect (each point along a steep ramp still
deviates from its neighbours' average, even though no single point is an isolated "spike" in the
colloquial sense). Zero apparent false positives on real fine-scale structure at this threshold,
unlike 0.2°C's 97. Shipped as `_flag_neighbour_average_spikes`/`SPIKE_NEIGHBOUR_AVERAGE_MAX_DELTA_C`.
```

- [ ] **Step 4: Update the "Checks implemented" section's own RC write-up**

Find the "Physical-plausibility range check — RC" section's prose and add a short note after its
existing content (before the "## Constants reference" heading) documenting the depth band:

```
**Depth-banded for TEMP since 2026-09-10** (GTSPP Real-Time QC Manual, IOC M&G No. 22, Section
2.4 "Profile Envelope"): the upper bound tightens from `TEMP_VALID_MAX` (40°C) to
`TEMP_DEEP_VALID_MAX` (20°C) at depths >= `TEMP_DEEP_BAND_DEPTH_M` (1500 m). Validated against the
real historical archive before choosing this specific depth/bound: shallower depths show no clean
statistical separation between real regional warm-water variability and the known fault class, so
only the deep band — where the data shows a clean gap — was tightened. `DEPTH_VALUES`/
`SOUND_VELOCITY` are unaffected, still one flat bound each.
```

- [ ] **Step 5: Update the "Constants reference" table**

Add two new rows after the existing `SOUND_VELOCITY_VALID_MIN`/`_MAX` row:

```
| `TEMP_DEEP_BAND_DEPTH_M` | 1500 m | Range check (RC) | GTSPP §2.4, depth validated against real historical archive (clean gap in real TEMP values below/above this depth) |
| `TEMP_DEEP_VALID_MAX` | 20.0°C | Range check (RC) | GTSPP §2.4, bound validated against real historical archive (real deep-water max 16.92°C, fault cluster starts at 32.04°C) |
| `SPIKE_NEIGHBOUR_AVERAGE_MAX_DELTA_C` | 2.0°C | Neighbour-average spike test (SP) | GTSPP Real-Time QC Manual (IOC M&G No. 22), validated against real historical archive (190 flags/112 profiles, zero apparent false positives) |
```

- [ ] **Step 6: Commit**

```bash
git add QC_COOKBOOK.md
git commit -m "docs: update QC_COOKBOOK.md for depth-band TEMP check and spike-test retry (NDO-705/708)"
```

---

### Task 5: Push, deploy to Skippy, verify, handover note

**Files:** none modified — deployment and verification only.

**Interfaces:** none (final task).

- [ ] **Step 1: Run the full local test suite one more time**

Run: `pytest tests/ -v`
Expected: 100% pass.

- [ ] **Step 2: Push to GitHub**

```bash
git push origin master
```

- [ ] **Step 3: Deploy the updated package to Skippy**

Per `cron_jobs_on_skippy/CLAUDE.md`'s own note: this logic is consumed as an installed package, not
vendored — a `git pull` on `cron_jobs_on_skippy` alone does NOT pick this up.

```bash
ssh aadc@172.16.29.7 "source /home/aadc/uwy_venv/bin/activate && pip install --upgrade 'git+https://github.com/botheredbybees/xbt-edf-qc.git' && python3 -c 'import parse_xbt_edf; print(parse_xbt_edf.TEMP_DEEP_BAND_DEPTH_M, parse_xbt_edf.SPIKE_NEIGHBOUR_AVERAGE_MAX_DELTA_C)'"
```

Expected: the import check prints `1500.0 2.0`, confirming the new constants are live under the
real production interpreter.

- [ ] **Step 4: Re-run the historical backfill and re-upload to AADC's S3 bucket**

Per this repo's established pattern (see the current voyage's handover notes for the exact prior
precedent — the 2026-09-08 `-99`/self-test-cast fix followed the identical re-run/re-upload/verify
sequence):

```bash
ssh aadc@172.16.29.7 "cd ~/cron-jobs && source /home/aadc/uwy_venv/bin/activate && python3 backfill_xbt_netcdf_historical.py"
```

Then re-upload via `rclone` (remote `aadc-kingston`) to `aadc-nuyina/xbt_historical_backfill/`, and
verify with `rclone check` that 0 differences remain against the freshly-uploaded copy — same
commands as documented in the handover notes' prior XBT re-run entries.

- [ ] **Step 5: Spot-check the real published output**

Pull the freshly-rebuilt historical NetCDF (or inspect it directly on Skippy) and confirm:
- The profile count is unchanged (369, or 368/367 if any additional casts get excluded by the new
  checks — investigate and document if the count changes, don't assume it's fine).
- At least the deep-band and spike-test flags fire somewhere in the real, freshly-built output
  (a nonzero count of newly-`GTSPP_PROBABLY_BAD` TEMP samples attributable to the new checks) —
  confirms the checks aren't silently inert in the real pipeline, not just the scratch script from
  Task 3.

- [ ] **Step 6: Add a dated handover-notes entry**

Per root `CLAUDE.md`'s ship-deployment rule — pull `newinapedia` first (Tess may have edited the
same page), then append a new `### YYYY MM DD` section to the current voyage's page describing what
was deployed, matching the format every other entry in that file uses. Commit and push.

- [ ] **Step 7: Update Jira**

Transition NDO-705 and NDO-708 to Done (check
`mcp__atlassian__getTransitionsForJiraIssue` for the real available transition names/IDs first),
with a comment citing the real validation numbers (190/112, the deep-band gap) and the deployed
commit hash.
