import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parse_xbt_edf import (
    Cast,
    FAULT_POSITION_ERROR,
    FAULT_PROBE_TYPE_ERROR,
    FAULT_TEST_PROBE,
    GTSPP_GOOD,
    GTSPP_MISSING,
    GTSPP_PROBABLY_BAD,
    TEMP_VALID_MAX,
    TEMP_VALID_MIN,
    apply_qc,
    is_test_probe_cast,
    warn_on_failed_test_probes,
)


def test_fault_bit_values_match_appendix_f():
    # Appendix F p.89 pairs 22 flag_values with 22 space-separated
    # flag_meanings, positionally: ... constant_temperature(65536)
    # time_error(131072) position_error(262144) duplicate_profile(524288)
    # test_probe(1048576) probe_type_error(2097152).
    # 131072 is time_error, NOT test_probe -- the value this constant used to
    # hold, which would have published a time-error fault on every self-test.
    assert FAULT_TEST_PROBE == 1048576
    assert FAULT_POSITION_ERROR == 262144
    assert FAULT_PROBE_TYPE_ERROR == 2097152


def _cast(**overrides):
    defaults = dict(
        source_file="fake.edf",
        probe_type_raw="DB",
        probe_type="DB",
        launch_time=datetime(2025, 3, 1, 12, 0, 0),
        latitude=-42.0,
        longitude=149.0,
        serial_number="123",
        voyage_id="202425030",
        # Beyond SURFACE_TRANSIENT_DEPTH_M (3.6 m) so the surface-transient check
        # (tested separately below) doesn't incidentally fire on every other
        # test that uses this default.
        depth_m=np.array([10.0, 11.0, 12.0]),
        temperature_c=np.array([10.0, 10.0, 10.0]),
        sound_velocity_ms=np.array([1500.0, 1500.0, 1500.0]),
        elapsed_s=np.array([0.0, 0.1, 0.2]),
        memo="",
    )
    defaults.update(overrides)
    return Cast(**defaults)


def test_normal_cast_gets_all_good_flags():
    [qc] = apply_qc([_cast()])
    assert qc.time_qc == GTSPP_GOOD
    assert qc.latitude_qc == GTSPP_GOOD
    assert qc.longitude_qc == GTSPP_GOOD
    assert qc.probe_type_qc == GTSPP_GOOD
    assert np.all(qc.temperature_qc == GTSPP_GOOD)


def _speed_check_pair():
    # 60 nautical miles apart, 10 minutes apart -> 360 knots, way over the 25kt threshold
    first = _cast(launch_time=datetime(2025, 3, 1, 12, 0, 0), latitude=-42.0, longitude=149.0)
    second = _cast(launch_time=datetime(2025, 3, 1, 12, 10, 0), latitude=-41.0, longitude=149.0)
    return apply_qc([first, second])


def test_implausible_speed_between_casts_flags_position_error():
    _, qc_second = _speed_check_pair()
    assert qc_second.time_qc == GTSPP_PROBABLY_BAD
    assert qc_second.latitude_qc == GTSPP_PROBABLY_BAD
    assert qc_second.longitude_qc == GTSPP_PROBABLY_BAD


def test_speed_check_emits_separate_pe_and_te_entries():
    # PE (section 4.2.4) and TE (section 4.2.5) are two distinct cookbook codes
    # with two distinct metadata targets. An automated speed check cannot tell
    # which one applies -- normally an operator judgment call (section 4.1
    # step 9) -- so both are emitted rather than conflating them into one.
    _, qc_second = _speed_check_pair()
    assert [entry.qc_flag for entry in qc_second.history] == ["PE", "TE"]

    position_entry, time_entry = qc_second.history
    assert position_entry.parameter == "LATITUDE,LONGITUDE"
    assert time_entry.parameter == "TIME"


def test_speed_check_descriptions_admit_the_ambiguity():
    _, qc_second = _speed_check_pair()
    for entry in qc_second.history:
        description = entry.qc_flag_description
        assert "cannot determine whether this is a position or time error" in description
        assert "knot speed" in description


def test_speed_check_downgrades_temperature_but_not_depth():
    # Table 2, PE Reject and TE Reject rows: Temperature 3 from the surface,
    # Depth 1 from the surface (i.e. depth unchanged). Section 3.1's rationale:
    # unresolvable time/position errors mean the temperature data "cannot be
    # properly referenced".
    _, qc_second = _speed_check_pair()
    assert np.all(qc_second.temperature_qc == GTSPP_PROBABLY_BAD)
    assert np.all(qc_second.depth_qc == GTSPP_GOOD)


def test_speed_check_temperature_downgrade_preserves_missing_flags():
    first = _cast(launch_time=datetime(2025, 3, 1, 12, 0, 0), latitude=-42.0, longitude=149.0)
    second = _cast(
        launch_time=datetime(2025, 3, 1, 12, 10, 0), latitude=-41.0, longitude=149.0,
        temperature_c=np.array([10.0, np.nan, 9.0]),
    )
    _, qc_second = apply_qc([first, second])
    # A missing reading stays missing -- the blanket downgrade must not
    # relabel it as merely "probably bad", which would claim a reading exists.
    assert list(qc_second.temperature_qc) == [
        GTSPP_PROBABLY_BAD, GTSPP_MISSING, GTSPP_PROBABLY_BAD,
    ]


def test_plausible_speed_between_casts_stays_good():
    # ~6 nautical miles apart, 1 hour apart -> 6 knots, well under the threshold
    first = _cast(launch_time=datetime(2025, 3, 1, 12, 0, 0), latitude=-42.0, longitude=149.0)
    second = _cast(launch_time=datetime(2025, 3, 1, 13, 0, 0), latitude=-42.1, longitude=149.0)
    _, qc_second = apply_qc([first, second])
    assert qc_second.latitude_qc == GTSPP_GOOD
    assert qc_second.history == []


def test_unrecognised_probe_type_flags_probe_type_error():
    [qc] = apply_qc([_cast(probe_type_raw="NotARealProbe", probe_type="NotARealProbe")])
    assert qc.probe_type_qc == GTSPP_PROBABLY_BAD
    assert len(qc.history) == 1
    assert qc.history[0].parameter == "PROBE_TYPE"
    assert qc.history[0].qc_flag == "PR"


def test_probe_type_error_downgrades_both_temperature_and_depth():
    # Table 2, PR Reject row (section 4.2.6): Temperature 3 from the surface,
    # Depth 3 from the surface. Depth too, because an incorrect probe type
    # invalidates the fall-rate equation the depth axis is derived from
    # (section 3.1, citing Cheng et al 2016) -- not just the temperatures.
    [qc] = apply_qc([_cast(probe_type_raw="NotARealProbe", probe_type="NotARealProbe")])
    assert np.all(qc.temperature_qc == GTSPP_PROBABLY_BAD)
    assert np.all(qc.depth_qc == GTSPP_PROBABLY_BAD)


def test_probe_type_downgrade_preserves_missing_flags():
    cast = _cast(
        probe_type_raw="NotARealProbe", probe_type="NotARealProbe",
        # Depths kept beyond SURFACE_TRANSIENT_DEPTH_M so this test's own missing
        # flag isn't confused with the separate surface-transient check.
        depth_m=np.array([10.0, np.nan, 12.0]),
        temperature_c=np.array([10.0, 10.0, np.nan]),
    )
    [qc] = apply_qc([cast])
    assert list(qc.depth_qc) == [GTSPP_PROBABLY_BAD, GTSPP_MISSING, GTSPP_PROBABLY_BAD]
    assert list(qc.temperature_qc) == [GTSPP_PROBABLY_BAD, GTSPP_PROBABLY_BAD, GTSPP_MISSING]


def test_depth_qc_defaults_to_good_for_a_clean_cast():
    [qc] = apply_qc([_cast()])
    assert np.all(qc.depth_qc == GTSPP_GOOD)
    assert qc.depth_qc.shape == qc.cast.depth_m.shape


def test_a_cast_can_trigger_at_most_five_history_entries():
    # The speed check emits 2 entries (PE + TE), the probe-type check 1, the
    # TEMP range check 1 (RC), and -- since NDO-708 -- the neighbour-average
    # spike check 1 more (SP): 999.0 has real (non-NaN) neighbours on both
    # sides, which the isolated-only check ignores but the neighbour-average
    # check exists specifically to catch (a real value that wildly disagrees
    # with its real neighbours). This is the expected, validated new
    # behaviour, not a regression -- confirms the two checks are genuinely
    # complementary, not just independently correct in isolation.
    # Latitudes kept north of every TEMP_SHALLOW_LATITUDE_BANDS_C band (not what this test is
    # about) while preserving the same 1-degree/10-minute separation the speed check needs.
    first = _cast(launch_time=datetime(2025, 3, 1, 12, 0, 0), latitude=-32.0, longitude=149.0)
    second = _cast(
        launch_time=datetime(2025, 3, 1, 12, 10, 0), latitude=-31.0, longitude=149.0,
        probe_type_raw="NotARealProbe", probe_type="NotARealProbe",
        temperature_c=np.array([10.0, 999.0, 10.0]),
    )
    _, qc_second = apply_qc([first, second])
    assert [entry.qc_flag for entry in qc_second.history] == ["PE", "TE", "PR", "RC", "SP"]

    from build_xbt_netcdf import _N_HISTORY
    assert len(qc_second.history) <= _N_HISTORY


def test_temperature_outside_valid_range_flags_only_the_bad_points():
    cast = _cast(temperature_c=np.array([10.0, -99.0, 10.5]))
    [qc] = apply_qc([cast])
    assert list(qc.temperature_qc) == [GTSPP_GOOD, GTSPP_PROBABLY_BAD, GTSPP_GOOD]
    [entry] = [e for e in qc.history if e.qc_flag == "RC"]
    assert "1 TEMP sample" in entry.qc_flag_description
    assert str(TEMP_VALID_MIN) in entry.qc_flag_description
    assert str(TEMP_VALID_MAX) in entry.qc_flag_description


def test_temperature_within_valid_range_never_gets_an_rc_entry():
    # A perfectly linear ramp between the two boundary values, spread over
    # enough points that every step is small -- a single big point-to-point
    # jump between -2.5 and 40.0 would also trip the (separately tested)
    # neighbour-average spike check and the terminal-deflection check,
    # neither of which is what this test is about. Linear spacing keeps
    # every interior point exactly at its neighbours' average (zero spike
    # deviation) and every step (~4.7 degC) under the terminal-deflection
    # threshold.
    n = 10
    temps = np.linspace(TEMP_VALID_MIN, TEMP_VALID_MAX, n)
    cast = _cast(
        depth_m=np.linspace(10.0, 19.0, n),
        temperature_c=temps,
        sound_velocity_ms=np.full(n, 1500.0),
        elapsed_s=np.linspace(0.0, 0.9, n),
        latitude=0.0,  # north of every TEMP_SHALLOW_LATITUDE_BANDS_C band -- not what this test is about
    )
    [qc] = apply_qc([cast])
    assert np.all(qc.temperature_qc == GTSPP_GOOD)
    assert "RC" not in [entry.qc_flag for entry in qc.history]
    assert "SP" not in [entry.qc_flag for entry in qc.history]
    assert "WB" not in [entry.qc_flag for entry in qc.history]


def test_depth_outside_valid_range_flags_only_the_bad_points():
    from parse_xbt_edf import DEPTH_VALID_MAX
    cast = _cast(depth_m=np.array([0.0, DEPTH_VALID_MAX + 1.0, 2.0]))
    [qc] = apply_qc([cast])
    assert list(qc.depth_qc) == [GTSPP_GOOD, GTSPP_PROBABLY_BAD, GTSPP_GOOD]
    assert "DEPTH" in [e.parameter for e in qc.history if e.qc_flag == "RC"]


def test_sound_velocity_outside_valid_range_flags_only_the_bad_points():
    from parse_xbt_edf import SOUND_VELOCITY_VALID_MAX
    cast = _cast(sound_velocity_ms=np.array([1500.0, SOUND_VELOCITY_VALID_MAX + 100.0, 1500.0]))
    [qc] = apply_qc([cast])
    assert list(qc.sound_velocity_qc) == [GTSPP_GOOD, GTSPP_PROBABLY_BAD, GTSPP_GOOD]
    assert "SOUND_VELOCITY" in [e.parameter for e in qc.history if e.qc_flag == "RC"]


def test_latitude_outside_valid_range_downgrades_the_scalar_flag():
    cast = _cast(latitude=95.0)
    [qc] = apply_qc([cast])
    assert qc.latitude_qc == GTSPP_PROBABLY_BAD
    [entry] = [e for e in qc.history if e.qc_flag == "RC" and e.parameter == "LATITUDE"]
    assert "95.0" in entry.qc_flag_description


def test_longitude_outside_valid_range_downgrades_the_scalar_flag():
    cast = _cast(longitude=185.0)
    [qc] = apply_qc([cast])
    assert qc.longitude_qc == GTSPP_PROBABLY_BAD
    [entry] = [e for e in qc.history if e.qc_flag == "RC" and e.parameter == "LONGITUDE"]
    assert "185.0" in entry.qc_flag_description


def test_latitude_and_longitude_within_range_never_get_an_rc_entry():
    cast = _cast(latitude=-42.0, longitude=149.0)
    [qc] = apply_qc([cast])
    assert qc.latitude_qc == GTSPP_GOOD
    assert qc.longitude_qc == GTSPP_GOOD
    assert "RC" not in [e.qc_flag for e in qc.history]


def test_surface_transient_flags_shallow_temperature_without_destroying_it():
    # A modest, smoothly-settling start-up transient -- close enough
    # together that it doesn't also incidentally trip the (separately
    # tested) neighbour-average spike check, which isn't what this test is
    # about. Real shallow transients settle just like this in the archive
    # (see this check's own design spec's real-data investigation); the
    # point is that the current (v2.1, since 2020/2021) methodology KEEPS
    # these values and flags them, rather than destroying them the way the
    # deprecated v1.1 CSA code did.
    cast = _cast(
        depth_m=np.array([0.0, 1.0, 3.6, 3.7, 5.0]),
        temperature_c=np.array([10.0, 5.0, 2.0, 1.8, 1.8]),
    )
    [qc] = apply_qc([cast])
    assert list(qc.temperature_qc) == [
        GTSPP_PROBABLY_BAD, GTSPP_PROBABLY_BAD, GTSPP_GOOD, GTSPP_GOOD, GTSPP_GOOD,
    ]
    # The value itself must survive -- this check flags, it does not correct
    # or delete, matching every other check in this pipeline.
    assert cast.temperature_c[0] == pytest.approx(10.0)
    assert cast.temperature_c[1] == pytest.approx(5.0)
    assert cast.temperature_c[2] == pytest.approx(2.0)


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


def test_spike_flags_a_reading_isolated_by_missing_data_on_both_sides():
    # The real shape found 2026-09-08: a single 37 degC reading at 953 m on
    # an otherwise ordinary Southern Ocean cast, with every sample
    # immediately around it already missing (a wire-break/end-of-cast
    # shape) -- confirmed against the full 368-profile historical archive
    # (23 more real cases, all similarly implausible for their depth).
    cast = _cast(
        depth_m=np.array([10.0, 11.0, 12.0, 13.0, 14.0]),
        temperature_c=np.array([2.0, np.nan, 37.0, np.nan, 1.6]),
    )
    [qc] = apply_qc([cast])
    assert list(qc.temperature_qc) == [
        GTSPP_GOOD, GTSPP_MISSING, GTSPP_PROBABLY_BAD, GTSPP_MISSING, GTSPP_GOOD,
    ]


def test_spike_does_not_flag_a_reading_with_one_real_neighbour():
    # Only one side missing -- not enough to call this "isolated"; a real
    # cast commonly has occasional single missing samples without the whole
    # rest of the trace having failed.
    cast = _cast(
        depth_m=np.array([10.0, 11.0, 12.0]),
        temperature_c=np.array([2.0, np.nan, 1.8]),
    )
    [qc] = apply_qc([cast])
    assert list(qc.temperature_qc) == [GTSPP_GOOD, GTSPP_MISSING, GTSPP_GOOD]
    assert "SP" not in [e.qc_flag for e in qc.history]


def test_spike_does_not_flag_real_fine_scale_structure():
    # Small, genuine step-like wiggles between real neighbours (the exact
    # shape found in real Southern Ocean casts around 400-600 m depth
    # during validation) -- this is documented as a real oceanographic
    # feature (section 5 "Structure / Signal Leakage Flags"), not a fault,
    # and must not be flagged just because it deviates from its neighbours.
    cast = _cast(
        depth_m=np.array([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0]),
        temperature_c=np.array([1.92, 1.83, 1.74, 1.54, 1.76, 1.93, 1.68]),
    )
    [qc] = apply_qc([cast])
    assert np.all(qc.temperature_qc == GTSPP_GOOD)
    assert "SP" not in [e.qc_flag for e in qc.history]


def test_spike_entry_cites_the_cookbook():
    cast = _cast(
        depth_m=np.array([10.0, 11.0, 12.0]),
        temperature_c=np.array([np.nan, 37.0, np.nan]),
    )
    [qc] = apply_qc([cast])
    [entry] = [e for e in qc.history if e.qc_flag == "SP"]
    assert entry.parameter == "TEMP"
    assert "CSIRO XBT QC Cookbook v1.1 sections 3.2/3.3" in entry.qc_flag_description


def test_spike_check_also_applies_to_test_probe_casts():
    cast = _cast(
        serial_number="TestProbe",
        depth_m=np.array([10.0, 11.0, 12.0]),
        temperature_c=np.array([1.51, 1.5, 1.49]),
    )
    [qc] = apply_qc([cast])
    assert "SP" not in [e.qc_flag for e in qc.history]  # nothing isolated here


def test_range_check_also_applies_to_test_probe_casts():
    # A self-test cast is excluded from the published NetCDF entirely, but
    # warn_on_failed_test_probes runs on the pre-filter QC results, so a
    # physically-impossible reading on a test probe should still be flagged,
    # not silently pass because it's a test-probe branch.
    cast = _cast(serial_number="TestProbe", temperature_c=np.array([20.0, -99.0, 20.0]))
    [qc] = apply_qc([cast])
    assert list(qc.temperature_qc) == [GTSPP_PROBABLY_BAD, GTSPP_PROBABLY_BAD, GTSPP_PROBABLY_BAD]
    # TP fires too (0.0 -> ... variation check compares max/min ignoring the
    # already-downgraded points isn't special-cased -- both checks run
    # independently on the same raw temperature_c array).
    assert "RC" in [entry.qc_flag for entry in qc.history]


def test_history_previous_value_is_never_repurposed_as_a_diagnostic():
    # Appendix F p.88 defines HISTORY_PREVIOUS_VALUE as "Parameter previous
    # value before action" -- a previous *data* value. These checks only flag,
    # never correct, so there is no previous value and the field stays NaN;
    # the diagnostic lives in qc_flag_description instead.
    first = _cast(launch_time=datetime(2025, 3, 1, 12, 0, 0), latitude=-42.0, longitude=149.0)
    second = _cast(
        launch_time=datetime(2025, 3, 1, 12, 10, 0), latitude=-41.0, longitude=149.0,
        probe_type_raw="NotARealProbe", probe_type="NotARealProbe",
    )
    unstable_test_probe = _cast(
        serial_number="TestProbe", temperature_c=np.array([20.0, 20.5, 20.0]),
    )
    for qc in apply_qc([first, second, unstable_test_probe]):
        for entry in qc.history:
            assert np.isnan(entry.previous_value), entry.qc_flag


def test_known_probe_type_stays_good():
    [qc] = apply_qc([_cast(probe_type_raw="T5", probe_type="T5")])
    assert qc.probe_type_qc == GTSPP_GOOD


def test_real_full_name_probe_types_are_recognised():
    # The real EDF header's Probe Type field always spells these out in full
    # (confirmed against the ship's whole historical archive, 2026-09-07) --
    # the plain abbreviated forms above are a defensive fallback, not what
    # real files actually contain.
    for probe_type in ("Deep Blue", "T-5", "T-7"):
        [qc] = apply_qc([_cast(probe_type_raw=probe_type, probe_type=probe_type)])
        assert qc.probe_type_qc == GTSPP_GOOD, probe_type


def test_unstable_test_probe_flags_temperature():
    # A self-test cast is identified via Serial Number, not Probe Type -- the
    # real EDF header's Probe Type field for a self-test still records an
    # ordinary real probe type (confirmed against the ship's real archive).
    unstable = _cast(
        serial_number="TestProbe",
        temperature_c=np.array([20.0, 20.5, 20.0]),  # 0.5 degC variation, way over 0.005
        latitude=0.0,  # north of every TEMP_SHALLOW_LATITUDE_BANDS_C band -- not what this test is about
    )
    [qc] = apply_qc([unstable])
    assert np.all(qc.temperature_qc == GTSPP_PROBABLY_BAD)
    assert len(qc.history) == 1
    assert qc.history[0].qc_flag == "TP"


def test_stable_test_probe_stays_good():
    stable = _cast(
        serial_number="TestProbe",
        temperature_c=np.array([20.001, 20.002, 20.0015]),
        latitude=0.0,  # north of every TEMP_SHALLOW_LATITUDE_BANDS_C band -- not what this test is about
    )
    [qc] = apply_qc([stable])
    assert np.all(qc.temperature_qc == GTSPP_GOOD)


def test_test_probe_identified_by_a_different_serial_number_spelling():
    # Real spellings seen in the ship's archive besides "TestProbe" itself:
    # "Test Probe", "Test", and "BT_Test_Device" -- a case-insensitive
    # substring match on "test" catches all of them without an exact allow-list.
    [qc] = apply_qc([_cast(serial_number="BT_Test_Device")])
    assert qc.probe_type_qc == GTSPP_GOOD  # never reaches the probe-type check at all


def test_test_probe_is_never_flagged_as_unrecognised_probe_type():
    [qc] = apply_qc([_cast(serial_number="TestProbe")])
    assert qc.probe_type_qc == GTSPP_GOOD


def test_test_probe_identified_via_memo_when_serial_number_is_plain():
    # Found 2026-09-08: voyage 202324VT1A's real
    # VT1A_testprobe20230807033912.edf has Serial Number "0" (no "test"
    # anywhere in it) and only its Memo field ("Test probe") gives it away.
    # This cast was published in the "370 real profiles" historical NetCDF
    # before this fix, as if it were real T-5 data.
    cast = _cast(serial_number="0", memo="Test probe", source_file="fake.edf")
    assert is_test_probe_cast(cast)


def test_test_probe_identified_via_filename_when_others_dont_say_test():
    cast = _cast(serial_number="0", memo="", source_file="VT1A_testprobe20230807033912.edf")
    assert is_test_probe_cast(cast)


def test_real_cast_with_no_test_indication_is_not_a_test_probe():
    cast = _cast(serial_number="1396178", memo="DeepBlue XBT on 202425030", source_file="202425030_DB_20250301203416.edf")
    assert not is_test_probe_cast(cast)


def test_test_probe_identified_via_isothermal_1_5c_signature_alone():
    # CSIRO Cookbook v1.1 section 2.2: a test probe reads isothermal near
    # 1.5 degC. No "test" anywhere in serial_number/memo/filename here --
    # this must be caught by the data signature alone.
    cast = _cast(
        serial_number="1396178", memo="", source_file="202425030_DB_20250301203416.edf",
        depth_m=np.array([0.5, 1.0, 1.5, 2.0, 2.5, 3.0]),  # all within SURFACE_TRANSIENT_DEPTH_M
        temperature_c=np.array([1.51, 1.51, 1.51, 1.52, 1.5, 1.49]),
    )
    assert is_test_probe_cast(cast)


def test_cold_real_cast_with_no_test_indication_is_not_a_test_probe():
    # A genuine Southern Ocean cast can be isothermal near 1-2 degC at depth,
    # but a real deployed probe's very first (surface) sample is the actual
    # sea-surface temperature -- never suspiciously pinned at 1.5 degC too.
    # This must NOT be misclassified just because the deep layers alone
    # resemble a test-probe signature.
    cast = _cast(
        serial_number="1396178", memo="", source_file="202425030_DB_20250301203416.edf",
        # The isothermal-near-1.5 run starts at index 3, depth 4.0m -- beyond
        # SURFACE_TRANSIENT_DEPTH_M, so it must not count as "from the surface".
        depth_m=np.array([0.5, 1.0, 1.5, 4.0, 5.0, 6.0, 7.0]),
        temperature_c=np.array([-1.2, -0.5, 0.8, 1.51, 1.51, 1.52, 1.49]),
    )
    assert not is_test_probe_cast(cast)


def test_brief_surface_coincidence_near_1_5c_is_not_a_test_probe():
    # Surface happens to read near 1.5 degC, but the rest of the profile is
    # nothing like isothermal -- a real cast, not a test probe.
    cast = _cast(
        serial_number="1396178", memo="", source_file="202425030_DB_20250301203416.edf",
        depth_m=np.array([0.5, 1.0, 1.5, 2.0, 2.5, 3.0]),
        temperature_c=np.array([1.5, 4.0, 8.0, 12.0, 15.0, 18.0]),
    )
    assert not is_test_probe_cast(cast)


def test_isothermal_signature_requires_contiguity_ndo_791():
    # Regression for NDO-791: a real surface layer AND a separate real deep
    # layer both near 1.5 degC, with real Winter Water between them, must
    # not be misclassified just because their combined (non-contiguous)
    # fraction crosses the majority threshold. Modelled on a real archive
    # case (a cast whose surface and its 477m+ CDW layer both read ~1.5degC).
    cast = _cast(
        serial_number="1396178", memo="", source_file="202425030_DB_20250301203416.edf",
        depth_m=np.array([0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5]),
        temperature_c=np.array([1.5, 1.51, 1.49, -0.7, -0.7, -0.7, -0.7]),
    )
    assert not is_test_probe_cast(cast)


def test_isothermal_signature_requires_a_surface_start_ndo_791():
    # Regression for NDO-791: a long, genuinely isothermal-near-1.5 real CDW
    # layer starting well below the surface (not from index 0) must not be
    # misclassified just because it's a majority of the cast. Modelled on
    # real archive cases (CDW layers starting 160-250m down).
    cast = _cast(
        serial_number="1396178", memo="", source_file="202425030_DB_20250301203416.edf",
        depth_m=np.array([0.5, 1.0, 1.5, 5.0, 5.5, 6.0, 6.5]),
        temperature_c=np.array([-1.2, -0.8, -0.3, 1.51, 1.5, 1.49, 1.51]),
    )
    assert not is_test_probe_cast(cast)


def test_isothermal_signature_with_a_short_leading_transient_is_a_test_probe_ndo_791():
    # Regression for NDO-791: the real, confirmed case -- a genuine numbered
    # probe launched with the self-test resistor still clipped in. Two
    # samples of power-on transient before the pin settles, both still
    # within SURFACE_TRANSIENT_DEPTH_M, must not defeat the surface-start
    # requirement. No "test" anywhere in serial_number/memo/filename --
    # this must be caught by the data signature alone.
    cast = _cast(
        serial_number="1383580", memo="Deep Blue XBT on 202425VT1", source_file="202425VT1_TDB_20241009000411.edf",
        depth_m=np.array([0.0, 0.7, 1.4, 2.0, 2.7, 3.4]),
        temperature_c=np.array([-0.03, 11.78, 1.5, 1.49, 1.49, 1.49]),
    )
    assert is_test_probe_cast(cast)


def test_isothermal_only_test_probe_logs_a_distinct_warning_ndo_791(caplog):
    import logging

    cast = _cast(
        serial_number="1383580", memo="Deep Blue XBT on 202425VT1", source_file="202425VT1_TDB_20241009000411.edf",
        depth_m=np.array([0.0, 0.7, 1.4, 2.0, 2.7, 3.4]),
        temperature_c=np.array([-0.03, 11.78, 1.5, 1.49, 1.49, 1.49]),
    )
    with caplog.at_level(logging.WARNING):
        apply_qc([cast])
    [message] = [r.getMessage() for r in caplog.records]
    assert "1383580" in message
    assert "self-test resistor still connected" in message


def test_test_probe_never_participates_in_the_speed_check():
    # A self-test cast sitting between two widely-separated real casts must not
    # itself get flagged, and must not become the "previous real cast" reference.
    real_1 = _cast(launch_time=datetime(2025, 3, 1, 12, 0, 0), latitude=-42.0, longitude=149.0)
    test_probe = _cast(
        serial_number="TestProbe",
        launch_time=datetime(2025, 3, 1, 12, 5, 0), latitude=-42.0, longitude=149.0,
    )
    real_2 = _cast(launch_time=datetime(2025, 3, 1, 12, 10, 0), latitude=-42.05, longitude=149.0)
    _, qc_test, qc_real_2 = apply_qc([real_1, test_probe, real_2])
    assert qc_test.latitude_qc == GTSPP_GOOD
    assert qc_real_2.latitude_qc == GTSPP_GOOD  # real_1 -> real_2 is a plausible speed


def test_nan_readings_flagged_as_gtspp_missing():
    cast = _cast(
        temperature_c=np.array([10.0, np.nan, 9.0]),
        sound_velocity_ms=np.array([1500.0, 1500.1, np.nan]),
    )
    [qc] = apply_qc([cast])
    assert list(qc.temperature_qc) == [GTSPP_GOOD, GTSPP_MISSING, GTSPP_GOOD]
    assert list(qc.sound_velocity_qc) == [GTSPP_GOOD, GTSPP_GOOD, GTSPP_MISSING]


def test_warn_on_failed_test_probes_reports_only_actual_failures(caplog):
    import logging

    unstable = _cast(
        source_file="/data/202425030/xbt/raw/unstable.edf",
        serial_number="TestProbe",
        launch_time=datetime(2025, 3, 1, 18, 0, 0),
        temperature_c=np.array([20.0, 20.5, 20.0]),  # 0.5 degC, way over 0.005
    )
    stable = _cast(
        serial_number="TestProbe",
        launch_time=datetime(2025, 3, 1, 19, 0, 0),
        temperature_c=np.array([20.001, 20.002, 20.0015]),
    )
    real = _cast(launch_time=datetime(2025, 3, 1, 20, 0, 0))

    casts_qc = apply_qc([unstable, stable, real])
    with caplog.at_level(logging.WARNING):
        count = warn_on_failed_test_probes(casts_qc, "202425030")

    assert count == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "202425030" in message
    assert "2025-03-01T18:00:00" in message
    assert "unstable.edf" in message
    assert "0.5000 degC variation" in message


def test_warn_on_failed_test_probes_falls_back_to_each_casts_own_voyage(caplog):
    import logging

    unstable = _cast(
        serial_number="TestProbe", voyage_id="202526010",
        temperature_c=np.array([20.0, 20.5, 20.0]),
    )
    casts_qc = apply_qc([unstable])
    with caplog.at_level(logging.WARNING):
        warn_on_failed_test_probes(casts_qc)  # no voyage_id -- backfill run

    assert "202526010" in caplog.records[0].getMessage()


def test_casts_are_qcd_in_launch_time_order_regardless_of_input_order():
    early = _cast(launch_time=datetime(2025, 3, 1, 12, 0, 0), latitude=-42.0, longitude=149.0)
    late = _cast(launch_time=datetime(2025, 3, 1, 13, 0, 0), latitude=-42.1, longitude=149.0)
    results = apply_qc([late, early])  # deliberately out of order
    assert [qc.cast.launch_time for qc in results] == [early.launch_time, late.launch_time]


def test_temperature_shallow_band_uses_the_existing_flat_bound():
    # A moderate, gently-varying profile -- comfortably within the flat
    # -2.5..40 bound and without any point-to-point jump big enough to also
    # trip the (separately tested) neighbour-average spike check.
    from parse_xbt_edf import TEMP_DEEP_BAND_DEPTH_M
    cast = _cast(
        depth_m=np.array([10.0, 11.0, 12.0]),
        temperature_c=np.array([10.0, 12.0, 14.0]),
    )
    assert cast.depth_m[-1] < TEMP_DEEP_BAND_DEPTH_M
    [qc] = apply_qc([cast])
    assert np.all(qc.temperature_qc == GTSPP_GOOD)


def test_temperature_shallow_band_still_flags_outside_the_flat_bound():
    cast = _cast(
        depth_m=np.array([10.0, 11.0, 12.0]),
        temperature_c=np.array([10.0, TEMP_VALID_MAX + 1.0, 10.0]),
        latitude=0.0,  # north of every TEMP_SHALLOW_LATITUDE_BANDS_C band -- not what this test is about
    )
    [qc] = apply_qc([cast])
    assert list(qc.temperature_qc) == [GTSPP_GOOD, GTSPP_PROBABLY_BAD, GTSPP_GOOD]
    [entry] = [e for e in qc.history if e.qc_flag == "RC" and e.parameter == "TEMP"]
    assert "1 TEMP sample" in entry.qc_flag_description
    assert "depth < 200.0" in entry.qc_flag_description


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
    assert "depth >= 200.0" in entry.qc_flag_description


def test_temperature_deep_band_at_exactly_the_boundary_depth_uses_the_tighter_bound():
    from parse_xbt_edf import TEMP_DEEP_BAND_DEPTH_M, TEMP_DEEP_VALID_MAX
    cast = _cast(
        depth_m=np.array([10.0, TEMP_DEEP_BAND_DEPTH_M, 20.0]),
        temperature_c=np.array([5.0, TEMP_DEEP_VALID_MAX + 1.0, 5.0]),
    )
    [qc] = apply_qc([cast])
    assert qc.temperature_qc[1] == GTSPP_PROBABLY_BAD


def test_temperature_deep_band_within_tightened_bound_stays_good():
    # All three points sit in the deep band, close together and right at the
    # tightened bound -- exercises only the depth-band range check, without a
    # point-to-point jump big enough to also trip the (separately tested)
    # neighbour-average spike check.
    from parse_xbt_edf import TEMP_DEEP_BAND_DEPTH_M, TEMP_DEEP_VALID_MAX
    cast = _cast(
        depth_m=np.array(
            [TEMP_DEEP_BAND_DEPTH_M + 50.0, TEMP_DEEP_BAND_DEPTH_M + 100.0, TEMP_DEEP_BAND_DEPTH_M + 150.0]
        ),
        temperature_c=np.array([TEMP_DEEP_VALID_MAX - 1.0, TEMP_DEEP_VALID_MAX, TEMP_DEEP_VALID_MAX - 1.0]),
    )
    [qc] = apply_qc([cast])
    assert np.all(qc.temperature_qc == GTSPP_GOOD)


def test_temperature_both_bands_bad_produces_two_separate_rc_entries():
    from parse_xbt_edf import TEMP_DEEP_BAND_DEPTH_M, TEMP_DEEP_VALID_MAX
    cast = _cast(
        depth_m=np.array([10.0, TEMP_DEEP_BAND_DEPTH_M + 100.0]),
        temperature_c=np.array([TEMP_VALID_MAX + 1.0, TEMP_DEEP_VALID_MAX + 1.0]),
        latitude=0.0,  # north of every TEMP_SHALLOW_LATITUDE_BANDS_C band -- not what this test is about
    )
    [qc] = apply_qc([cast])
    rc_entries = [e for e in qc.history if e.qc_flag == "RC" and e.parameter == "TEMP"]
    assert len(rc_entries) == 2
    assert list(qc.temperature_qc) == [GTSPP_PROBABLY_BAD, GTSPP_PROBABLY_BAD]


def test_temperature_mid_depth_fault_plateau_now_flagged_ndo_763():
    # Regression for NDO-763: a 25-37 degC fault ramp at 200-1400 m used to
    # publish as GTSPP "good" because the tightened band only started at
    # 1500 m. Shape modelled on the real archive (e.g. cast 38/19): a smooth
    # climb from real water into recorder saturation at ~300-400 m.
    cast = _cast(
        depth_m=np.array([298.0, 300.0, 302.0, 304.0]),
        temperature_c=np.array([15.0, 19.0, 25.0, 30.0]),
    )
    [qc] = apply_qc([cast])
    assert list(qc.temperature_qc) == [
        GTSPP_GOOD, GTSPP_GOOD, GTSPP_PROBABLY_BAD, GTSPP_PROBABLY_BAD,
    ]
    [entry] = [e for e in qc.history if e.qc_flag == "RC" and e.parameter == "TEMP"]
    assert "depth >= 200.0" in entry.qc_flag_description


def test_temperature_real_water_at_200m_below_tightened_ceiling_stays_good():
    # The real archive's own highest confirmed-clean value at depth >=200 m
    # sits close under TEMP_DEEP_VALID_MAX -- this stays good, not a false
    # positive of the NDO-763 tightening.
    cast = _cast(
        depth_m=np.array([200.0, 205.0, 210.0]),
        temperature_c=np.array([18.0, 19.0, 18.5]),
    )
    [qc] = apply_qc([cast])
    assert np.all(qc.temperature_qc == GTSPP_GOOD)


def test_shallow_latitude_band_flags_a_fault_south_of_60_degrees_ndo_790():
    # Regression for NDO-790: real casts at high southern latitude (e.g. cast
    # 96/102 in the historical archive) had a fault ramp cross the 200 m
    # boundary from below, publishing as good between ~150-200 m. South of
    # -60 degrees the real ceiling is 6 degC -- comfortably under any real
    # water this ship has ever recorded there.
    cast = _cast(
        depth_m=np.array([150.0, 160.0, 170.0]),
        temperature_c=np.array([2.0, 8.0, 15.0]),
        latitude=-63.0,
    )
    [qc] = apply_qc([cast])
    assert list(qc.temperature_qc) == [GTSPP_GOOD, GTSPP_PROBABLY_BAD, GTSPP_PROBABLY_BAD]
    [entry] = [e for e in qc.history if e.qc_flag == "RC" and "latitude-banded" in e.qc_flag_description]
    assert "6.0 degC" in entry.qc_flag_description
    assert "-63.00" in entry.qc_flag_description


def test_shallow_latitude_band_real_water_stays_good_at_each_band():
    # One point comfortably under the ceiling in each defined band, plus one
    # north of every band -- none of these should ever be flagged.
    for lat, temp in [(-65.0, 1.0), (-55.0, 10.0), (-47.0, 15.0), (-42.0, 18.0), (-10.0, 35.0)]:
        cast = _cast(depth_m=np.array([50.0, 60.0, 70.0]), temperature_c=np.array([temp] * 3), latitude=lat)
        [qc] = apply_qc([cast])
        assert np.all(qc.temperature_qc == GTSPP_GOOD), f"lat={lat} temp={temp}"


def test_shallow_latitude_band_does_not_apply_below_surface_transient_depth():
    # A hot near-surface reading inside the CS zone (<3.6 m) is CS's job, not
    # this check's -- it must not also pick up an RC flag from the latitude band.
    cast = _cast(
        depth_m=np.array([0.0, 1.0, 2.0]),
        temperature_c=np.array([30.0, 30.0, 30.0]),
        latitude=-65.0,
    )
    [qc] = apply_qc([cast])
    assert "RC" not in [e.qc_flag for e in qc.history if e.parameter == "TEMP"]


def test_shallow_latitude_band_does_not_apply_at_or_below_200m():
    # depth >= TEMP_DEEP_BAND_DEPTH_M is TEMP_DEEP_VALID_MAX's job, not this one's.
    cast = _cast(
        depth_m=np.array([200.0, 210.0, 220.0]),
        temperature_c=np.array([5.9, 5.9, 5.9]),
        latitude=-65.0,
    )
    [qc] = apply_qc([cast])
    assert np.all(qc.temperature_qc == GTSPP_GOOD)


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
    # compute an average at all. The trailing 20.0 repeat keeps the jump
    # across the gap away from the cast's terminal sample, so the
    # (separately tested) terminal-deflection check doesn't also fire here.
    cast = _cast(
        depth_m=np.array([10.0, 11.0, 12.0, 13.0]),
        temperature_c=np.array([5.0, np.nan, 20.0, 20.0]),
        sound_velocity_ms=np.array([1500.0, 1500.0, 1500.0, 1500.0]),
        elapsed_s=np.array([0.0, 0.1, 0.2, 0.3]),
    )
    [qc] = apply_qc([cast])
    assert list(qc.temperature_qc) == [GTSPP_GOOD, GTSPP_MISSING, GTSPP_GOOD, GTSPP_GOOD]
    assert "SP" not in [e.qc_flag for e in qc.history]
    assert "WB" not in [e.qc_flag for e in qc.history]


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


def test_terminal_deflection_flags_a_sharp_jump_on_the_casts_last_sample():
    # Real shape found during NDO-763 (modelled closely on cast 171 in the
    # historical archive): a long, stable, physically sane reading (real
    # Antarctic shelf water) followed by one odd intermediate value the
    # neighbour-average spike check already catches, then a terminal jump
    # into double digits -- invisible to the depth-band range check when
    # the jump still lands under TEMP_DEEP_VALID_MAX, and invisible to the
    # spike check itself since it needs a real neighbour *after* the
    # terminal sample too.
    cast = _cast(
        depth_m=np.array([500.0, 500.6, 501.2, 501.8]),
        temperature_c=np.array([-1.58, -1.58, 1.61, 19.18]),
    )
    [qc] = apply_qc([cast])
    assert list(qc.temperature_qc) == [
        GTSPP_GOOD, GTSPP_GOOD, GTSPP_PROBABLY_BAD, GTSPP_PROBABLY_BAD,
    ]
    [entry] = [e for e in qc.history if e.qc_flag == "WB"]
    assert "20.76 degC" in entry.qc_flag_description  # delta from the last GOOD value (-1.58), not from 1.61
    assert "section 3.2" in entry.qc_flag_description


def test_terminal_deflection_within_threshold_stays_good():
    # Linear steps (zero spike-check deviation) with the final step exactly
    # AT the threshold -- the check is strictly "greater than", so this
    # must not flag.
    from parse_xbt_edf import TEMP_TERMINAL_DEFLECTION_MAX_DELTA_C as D
    cast = _cast(
        depth_m=np.array([500.0, 500.6, 501.2]),
        temperature_c=np.array([0.0, D, 2 * D]),
    )
    [qc] = apply_qc([cast])
    assert np.all(qc.temperature_qc == GTSPP_GOOD)
    assert "WB" not in [e.qc_flag for e in qc.history]


def test_terminal_deflection_skips_over_an_already_bad_immediate_predecessor():
    # The immediate predecessor is itself already flagged bad (here, by the
    # depth-band range check -- 40.0 exceeds TEMP_DEEP_VALID_MAX at this
    # depth) -- comparing the terminal value against it would be
    # meaningless, so this must fall back to the last CONFIRMED-GOOD value
    # (10.0) instead. Real analogue: cast 171 in the historical archive,
    # where the sample right before the terminal deflection was itself
    # already an artifact.
    cast = _cast(
        depth_m=np.array([500.0, 500.6, 501.2, 501.8]),
        temperature_c=np.array([10.0, np.nan, 40.0, 10.5]),
    )
    [qc] = apply_qc([cast])
    # index 2 (40.0) exceeds the depth-band ceiling -- flagged bad by the
    # range check, independent of this check.
    assert qc.temperature_qc[2] == GTSPP_PROBABLY_BAD
    # index 3 (10.5) is close to the last confirmed-good value (10.0, index 0)
    # -- must stay good, not be compared against the already-bad 40.0.
    assert qc.temperature_qc[3] == GTSPP_GOOD


def test_terminal_deflection_does_not_double_flag_an_already_bad_terminal_sample():
    cast = _cast(
        depth_m=np.array([10.0, 11.0, 12.0]),
        temperature_c=np.array([10.0, 10.0, TEMP_VALID_MAX + 1.0]),
    )
    [qc] = apply_qc([cast])
    wb_entries = [e for e in qc.history if e.qc_flag == "WB"]
    assert wb_entries == []  # RC already caught it; terminal-deflection must not add a second entry


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
    cast = _cast(latitude=200.0, longitude=147.0)
    [qc] = apply_qc([cast])
    assert "PL" not in [h.qc_flag for h in qc.history]


def test_position_on_land_check_also_applies_to_test_probe_casts():
    cast = _cast(serial_number="TestProbe", latitude=-42.8806, longitude=147.3250)
    [qc] = apply_qc([cast])
    assert "PL" in [h.qc_flag for h in qc.history]


def _repeat_cast_pair(lat=-65.0, minutes=5.0, lat_offset=0.01, depths=None, temp_a=None, temp_b=None):
    from datetime import timedelta
    depths = np.array([50.0, 60.0, 70.0, 80.0, 90.0, 100.0, 110.0, 120.0, 130.0, 140.0, 150.0]) \
        if depths is None else depths
    temp_a = np.full(depths.size, 2.0) if temp_a is None else temp_a
    temp_b = np.full(depths.size, 2.0) if temp_b is None else temp_b
    earlier = _cast(
        launch_time=datetime(2025, 3, 1, 12, 0, 0), latitude=lat, longitude=140.0,
        depth_m=depths, temperature_c=temp_a,
        sound_velocity_ms=np.full(depths.size, 1500.0), elapsed_s=np.arange(depths.size, dtype=float),
    )
    later = _cast(
        launch_time=datetime(2025, 3, 1, 12, 0, 0) + timedelta(minutes=minutes),
        latitude=lat + lat_offset, longitude=140.0,
        depth_m=depths, temperature_c=temp_b,
        sound_velocity_ms=np.full(depths.size, 1500.0), elapsed_s=np.arange(depths.size, dtype=float),
    )
    return earlier, later


def test_repeat_cast_flags_the_warmer_cast_over_a_sustained_disagreement_ndo_792():
    # Regression for NDO-792: modelled on the real cast 192/193 pair -- a
    # sustained (>=10m), large (>3 degC) disagreement between two close,
    # near-simultaneous real casts, with the warmer cast attributed as
    # faulty (validated against all 12 real disagreeing pairs in the
    # archive: every one was correctly the warmer member). 4.5 vs 1.0 degC
    # (not a bigger jump) deliberately stays under the -65 degree latitude's
    # own TEMP_SHALLOW_LATITUDE_BANDS_C ceiling (6.0 degC) and under the
    # neighbour-average spike check's own boundary deviation (1.75 < 2.0),
    # so only the check under test reacts.
    earlier, later = _repeat_cast_pair(
        temp_a=np.full(11, 1.0),
        temp_b=np.array([1.0, 4.5, 4.5, 4.5, 4.5, 4.5, 4.5, 4.5, 4.5, 4.5, 1.0]),
    )
    [qc_earlier, qc_later] = apply_qc([earlier, later])
    # The disagreeing run is depth 60-140m (9 grid points, 80m span) -- the
    # first and last points agree (1.0 vs 1.0) so stay good on both sides.
    assert list(qc_later.temperature_qc) == (
        [GTSPP_GOOD] + [GTSPP_PROBABLY_BAD] * 9 + [GTSPP_GOOD]
    )
    assert np.all(qc_earlier.temperature_qc == GTSPP_GOOD)  # the sibling's own TEMP is untouched

    [ws_later] = [h for h in qc_later.history if h.qc_flag == "WS"]
    assert "disagree with a repeat cast" in ws_later.qc_flag_description
    [ws_earlier] = [h for h in qc_earlier.history if h.qc_flag == "WS"]
    assert "traced to the other cast" in ws_earlier.qc_flag_description
    assert "no _qc change" in ws_earlier.qc_flag_description


def test_repeat_cast_disagreement_within_threshold_stays_good():
    from parse_xbt_edf import REPEAT_CAST_MAX_DELTA_C
    earlier, later = _repeat_cast_pair(
        temp_a=np.full(11, 2.0),
        temp_b=np.full(11, 2.0 + REPEAT_CAST_MAX_DELTA_C),
    )
    results = apply_qc([earlier, later])
    for qc in results:
        assert np.all(qc.temperature_qc == GTSPP_GOOD)
        assert "WS" not in [h.qc_flag for h in qc.history]


def test_repeat_cast_disagreement_over_too_short_a_run_stays_good():
    from parse_xbt_edf import REPEAT_CAST_MIN_RUN_M
    # Only two adjacent grid points disagree -- 10m span exactly at the
    # boundary is fine, but a shorter run below REPEAT_CAST_MIN_RUN_M must not flag.
    depths = np.array([50.0, 55.0, 60.0, 65.0, 70.0])  # a 5m gap in the middle
    earlier, later = _repeat_cast_pair(
        depths=depths, temp_a=np.full(5, 1.0),
        temp_b=np.array([1.0, 1.0, 4.5, 4.5, 1.0]),  # stays under the -65 deg shallow ceiling (6.0)
    )
    assert REPEAT_CAST_MIN_RUN_M > 5.0  # sanity: the disagreement run here (60-65m) is shorter
    results = apply_qc([earlier, later])
    for qc in results:
        assert np.all(qc.temperature_qc == GTSPP_GOOD)


def test_repeat_cast_check_does_not_apply_beyond_the_time_window():
    from parse_xbt_edf import REPEAT_CAST_MAX_MINUTES
    earlier, later = _repeat_cast_pair(
        minutes=REPEAT_CAST_MAX_MINUTES + 1.0,
        temp_a=np.full(11, 1.0),
        temp_b=np.full(11, 4.5),  # stays under the -65 deg shallow ceiling (6.0)
    )
    results = apply_qc([earlier, later])
    for qc in results:
        assert np.all(qc.temperature_qc == GTSPP_GOOD)
        assert "WS" not in [h.qc_flag for h in qc.history]


def test_repeat_cast_check_does_not_apply_beyond_the_distance_window():
    # ~11 km apart in 5 minutes is also an implausible speed, so the existing
    # PE/TE speed check legitimately downgrades TEMP for its own, unrelated
    # reason here -- this test only asserts the repeat-cast check itself
    # (WS) didn't also fire, not that every check stayed quiet.
    earlier, later = _repeat_cast_pair(
        lat_offset=0.1,  # ~11 km -- beyond REPEAT_CAST_MAX_DISTANCE_KM
        temp_a=np.full(11, 1.0),
        temp_b=np.full(11, 4.5),  # stays under the -65 deg shallow ceiling (6.0)
    )
    results = apply_qc([earlier, later])
    for qc in results:
        assert "WS" not in [h.qc_flag for h in qc.history]


def test_repeat_cast_check_does_not_apply_north_of_the_latitude_gate():
    from parse_xbt_edf import REPEAT_CAST_MAX_LATITUDE_DEG
    earlier, later = _repeat_cast_pair(
        lat=REPEAT_CAST_MAX_LATITUDE_DEG + 1.0,  # north of the gate
        temp_a=np.full(11, 1.0),
        temp_b=np.full(11, 4.5),  # stays under the -65 deg shallow ceiling (6.0)
    )
    results = apply_qc([earlier, later])
    for qc in results:
        assert np.all(qc.temperature_qc == GTSPP_GOOD)
        assert "WS" not in [h.qc_flag for h in qc.history]


def test_repeat_cast_check_skips_over_a_test_probe_between_two_real_casts():
    # The test probe sitting chronologically between earlier and later must not
    # become "previous_real_qc" itself -- but earlier and later (5 min apart in
    # total, within REPEAT_CAST_MAX_MINUTES) must still be compared directly,
    # skipping over it, exactly as the speed check already does.
    earlier, later = _repeat_cast_pair(temp_a=np.full(11, 1.0), temp_b=np.full(11, 4.5))
    test_probe = _cast(
        serial_number="TestProbe",
        launch_time=datetime(2025, 3, 1, 12, 2, 0), latitude=-65.0, longitude=140.0,
    )
    [_, qc_test, qc_later] = apply_qc([earlier, test_probe, later])
    assert "WS" not in [h.qc_flag for h in qc_test.history]
    [ws] = [h for h in qc_later.history if h.qc_flag == "WS"]
    assert "disagree with a repeat cast" in ws.qc_flag_description


def test_repeat_cast_check_only_compares_samples_still_good_in_both_casts():
    # A point already flagged bad by another check (here, an out-of-range value)
    # must be excluded from the comparison entirely, not treated as a real 2.0.
    from parse_xbt_edf import TEMP_VALID_MAX
    depths = np.array([50.0, 60.0, 70.0, 80.0, 90.0, 100.0, 110.0, 120.0, 130.0, 140.0, 150.0])
    earlier, later = _repeat_cast_pair(
        depths=depths,
        temp_a=np.array([2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, TEMP_VALID_MAX + 1.0]),
        temp_b=np.full(11, 2.0),
    )
    [qc_earlier, qc_later] = apply_qc([earlier, later])
    # earlier's last point is already RC-bad on its own; the repeat-cast check must not
    # also produce a WS entry for a point neither cast holds at GOOD.
    assert qc_earlier.temperature_qc[-1] == GTSPP_PROBABLY_BAD
    assert qc_later.temperature_qc[-1] == GTSPP_GOOD
    assert "WS" not in [h.qc_flag for h in qc_earlier.history]
    assert "WS" not in [h.qc_flag for h in qc_later.history]
