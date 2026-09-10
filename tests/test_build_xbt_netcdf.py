import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parse_xbt_edf import Cast, apply_qc
from build_xbt_netcdf import build_xbt_netcdf


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
        # Beyond SURFACE_SPIKE_DEPTH_M (3.7 m) so the surface-spike QC check
        # doesn't incidentally strip TEMP in tests that aren't about it.
        depth_m=np.array([10.0, 11.0, 12.0]),
        temperature_c=np.array([10.0, 9.5, 9.0]),
        sound_velocity_ms=np.array([1500.0, 1500.1, 1500.2]),
        elapsed_s=np.array([0.0, 0.1, 0.2]),
    )
    defaults.update(overrides)
    return Cast(**defaults)


def test_one_profile_per_cast():
    casts_qc = apply_qc([_cast(), _cast(launch_time=datetime(2025, 3, 1, 13, 0, 0))])
    ds = build_xbt_netcdf(casts_qc)
    assert ds.dims["PROFILE"] == 2


def test_depth_dimension_padded_to_longest_cast():
    short = _cast(depth_m=np.array([10.0, 11.0]), temperature_c=np.array([10.0, 9.5]),
                  sound_velocity_ms=np.array([1500.0, 1500.1]))
    long = _cast(launch_time=datetime(2025, 3, 1, 13, 0, 0),
                 depth_m=np.array([10.0, 11.0, 12.0, 13.0]),
                 temperature_c=np.array([10.0, 9.5, 9.0, 8.5]),
                 sound_velocity_ms=np.array([1500.0, 1500.1, 1500.2, 1500.3]))
    casts_qc = apply_qc([short, long])
    ds = build_xbt_netcdf(casts_qc)
    assert ds.dims["DEPTH"] == 4
    assert np.isnan(ds["TEMP"].values[0, 2])  # short cast padded beyond its own depth
    assert np.isnan(ds["TEMP"].values[0, 3])
    assert ds["TEMP"].values[1, 3] == pytest.approx(8.5)


def test_scalar_profile_variables():
    casts_qc = apply_qc([_cast(latitude=-42.5, longitude=149.5)])
    ds = build_xbt_netcdf(casts_qc)
    assert ds["LATITUDE"].values[0] == pytest.approx(-42.5)
    assert ds["LONGITUDE"].values[0] == pytest.approx(149.5)
    assert ds["PROBE_TYPE"].values[0] == "DB"
    assert ds["PROBE_TYPE_RAW"].values[0] == "DB"


def test_quality_control_companion_variables_present_with_correct_defaults():
    casts_qc = apply_qc([_cast()])
    ds = build_xbt_netcdf(casts_qc)
    assert ds["TIME_quality_control"].values[0] == 1
    assert ds["LATITUDE_quality_control"].values[0] == 1
    assert np.all(ds["TEMP_quality_control"].values[0] == 1)
    assert list(ds["TEMP_quality_control"].attrs["flag_values"]) == list(range(10))


def test_position_error_flag_is_reflected_in_the_dataset():
    first = _cast(launch_time=datetime(2025, 3, 1, 12, 0, 0), latitude=-42.0, longitude=149.0)
    second = _cast(launch_time=datetime(2025, 3, 1, 12, 10, 0), latitude=-41.0, longitude=149.0)
    casts_qc = apply_qc([first, second])
    ds = build_xbt_netcdf(casts_qc)
    assert ds["LATITUDE_quality_control"].values[1] == 3


def test_position_and_time_error_fault_bits_both_set_together():
    # An automated speed-check hit can't distinguish PE from TE, so both fault
    # bits are set together, matching the PE+TE history-entry split.
    first = _cast(launch_time=datetime(2025, 3, 1, 12, 0, 0), latitude=-42.0, longitude=149.0)
    second = _cast(launch_time=datetime(2025, 3, 1, 12, 10, 0), latitude=-41.0, longitude=149.0)
    ds = build_xbt_netcdf(apply_qc([first, second]))
    from parse_xbt_edf import FAULT_POSITION_ERROR, FAULT_TIME_ERROR
    flagged = ds["XBT_fault_and_feature_flag_type"].values[1]
    assert np.all(flagged & FAULT_POSITION_ERROR)
    assert np.all(flagged & FAULT_TIME_ERROR)


def test_history_variables_populated_for_a_flagged_cast():
    first = _cast(launch_time=datetime(2025, 3, 1, 12, 0, 0), latitude=-42.0, longitude=149.0)
    second = _cast(launch_time=datetime(2025, 3, 1, 12, 10, 0), latitude=-41.0, longitude=149.0)
    casts_qc = apply_qc([first, second])
    ds = build_xbt_netcdf(casts_qc)
    assert ds.dims["N_HISTORY"] == 13
    assert ds["HISTORY_QC_FLAG"].values[1, 0] == "PE"
    assert ds["HISTORY_QC_FLAG"].values[1, 1] == "TE"
    assert ds["HISTORY_PARAMETER"].values[1, 0] == "LATITUDE,LONGITUDE"
    assert ds["HISTORY_PARAMETER"].values[1, 1] == "TIME"
    assert ds["HISTORY_QC_FLAG"].values[0, 0] == ""  # unflagged cast has no history entries


def test_history_previous_value_is_always_the_fill():
    # Appendix F p.88: "Parameter previous value before action". These checks
    # only flag, never correct, so there is no previous value to report.
    first = _cast(launch_time=datetime(2025, 3, 1, 12, 0, 0), latitude=-42.0, longitude=149.0)
    second = _cast(launch_time=datetime(2025, 3, 1, 12, 10, 0), latitude=-41.0, longitude=149.0)
    ds = build_xbt_netcdf(apply_qc([first, second]))
    assert np.all(np.isnan(ds["HISTORY_PREVIOUS_VALUE"].values))
    assert ds["HISTORY_PREVIOUS_VALUE"].attrs["long_name"] == (
        "Parameter previous value before action"
    )


def test_n_history_capacity_covers_the_worst_case_cast():
    # This specific fixture produces exactly 11 real entries: CS from
    # the surface-spike check (fires here because depth_m includes points
    # below 3.7 m), PE + TE from the speed check, PR from the probe-type
    # check, RC once each for TEMP, DEPTH, SOUND_VELOCITY, LATITUDE and
    # LONGITUDE all independently out of range, SP from the isolated-spike
    # check -- CS's masking leaves the 999.0 reading stranded between two
    # NaNs, which the isolated-spike check flags as uncorroborated on its
    # own (and which the neighbour-average spike check, added since
    # NDO-708, does NOT also flag -- it needs two real neighbours to
    # compute an average from, and both of this reading's neighbours are
    # NaN after CS) -- and WB from the Wire Break cascade check (added
    # since NDO-728): CS's masking also leaves the cast's own last sample
    # (depth 2.0 m, below the 3.7 m surface-spike depth) as an unrecovered
    # trailing NaN, which is exactly the shape WB looks for. _N_HISTORY
    # itself is 13 (not 11), for headroom covering the true theoretical
    # worst case across all checks (e.g. a cast whose TEMP goes out of
    # range in both depth bands AND triggers both spike checks AND ends
    # in an unrecovered NaN run) -- this fixture doesn't hit that ceiling,
    # so the two trailing slots are the fixed-width array's own
    # empty-string padding, not a missing finding.
    first = _cast(launch_time=datetime(2025, 3, 1, 12, 0, 0), latitude=-42.0, longitude=149.0)
    second = _cast(
        launch_time=datetime(2025, 3, 1, 12, 10, 0), latitude=95.0, longitude=185.0,
        probe_type_raw="NotARealProbe", probe_type="NotARealProbe",
        temperature_c=np.array([10.0, 999.0, 10.0]),
        depth_m=np.array([0.0, 5000.0, 2.0]),
        sound_velocity_ms=np.array([1500.0, 100.0, 1500.0]),
    )
    ds = build_xbt_netcdf(apply_qc([first, second]))
    assert list(ds["HISTORY_QC_FLAG"].values[1]) == [
        "CS", "PE", "TE", "PR", "RC", "SP", "WB", "RC", "RC", "RC", "RC", "", "",
    ]


def test_depth_quality_control_companion_variable():
    casts_qc = apply_qc([_cast()])
    ds = build_xbt_netcdf(casts_qc)
    assert np.all(ds["DEPTH_VALUES_quality_control"].values[0] == 1)
    assert ds["DEPTH_VALUES"].attrs["ancillary_variables"] == "DEPTH_VALUES_quality_control"
    assert ds["DEPTH_VALUES_quality_control"].attrs["standard_name"] == "depth status_flag"


def test_probe_type_error_downgrades_depth_qc_in_the_dataset():
    # Table 2, PR Reject (section 4.2.6): Depth 3 from the surface.
    casts_qc = apply_qc([_cast(probe_type_raw="NotARealProbe", probe_type="NotARealProbe")])
    ds = build_xbt_netcdf(casts_qc)
    assert np.all(ds["DEPTH_VALUES_quality_control"].values[0] == 3)
    assert np.all(ds["TEMP_quality_control"].values[0] == 3)


def test_depth_qc_padded_with_the_fill_beyond_a_short_cast():
    short = _cast(depth_m=np.array([10.0, 11.0]), temperature_c=np.array([10.0, 9.5]),
                  sound_velocity_ms=np.array([1500.0, 1500.1]))
    long = _cast(launch_time=datetime(2025, 3, 1, 13, 0, 0),
                 depth_m=np.array([10.0, 11.0, 12.0]),
                 temperature_c=np.array([10.0, 9.5, 9.0]),
                 sound_velocity_ms=np.array([1500.0, 1500.1, 1500.2]))
    ds = build_xbt_netcdf(apply_qc([short, long]))
    assert ds["DEPTH_VALUES_quality_control"].values[0, 2] == 99  # padding, not real data
    assert ds["DEPTH_VALUES_quality_control"].values[1, 2] == 1


def test_fault_flag_bitmask_set_for_test_probe():
    casts_qc = apply_qc([_cast(serial_number="TestProbe")])
    ds = build_xbt_netcdf(casts_qc)
    from parse_xbt_edf import FAULT_TEST_PROBE
    assert FAULT_TEST_PROBE == 1048576  # Appendix F p.89; 131072 is time_error
    assert np.all(ds["XBT_fault_and_feature_flag_type"].values[0] & FAULT_TEST_PROBE)


def test_fault_flag_variable_has_cf_flag_attributes():
    # Appendix F p.89 documents this variable with flag_values/flag_meanings
    # (rather than CF's flag_masks), so that convention is matched here.
    ds = build_xbt_netcdf(apply_qc([_cast()]))
    attrs = ds["XBT_fault_and_feature_flag_type"].attrs
    assert list(attrs["flag_values"]) == [131072, 262144, 1048576, 2097152]
    assert attrs["flag_meanings"] == "time_error position_error test_probe probe_type_error"
    assert attrs["_FillValue"] == 0
    assert attrs["valid_min"] == 0
    # Appendix F's own valid_max of 65536 does not cover its own flag_values,
    # which reach 2097152 -- a defect in the cookbook. Published as the real
    # maximum of the values this variable can actually hold.
    assert attrs["valid_max"] == 2097152


def test_fault_flag_attrs_are_uint32_matching_the_variable_dtype():
    ds = build_xbt_netcdf(apply_qc([_cast()]))
    attrs = ds["XBT_fault_and_feature_flag_type"].attrs
    assert attrs["flag_values"].dtype == np.uint32
    assert isinstance(attrs["valid_min"], np.uint32)
    assert isinstance(attrs["valid_max"], np.uint32)
    assert ds["XBT_fault_and_feature_flag_type"].dtype == np.uint32


def test_appendix_f_attributes_on_position_and_probe_type_variables():
    ds = build_xbt_netcdf(apply_qc([_cast()]))
    datum = "geographical coordinates, WGS84 projection"
    assert ds["LATITUDE"].attrs["reference_datum"] == datum
    assert ds["LONGITUDE"].attrs["reference_datum"] == datum
    assert ds["PROBE_TYPE"].attrs["reference_datum"] == "WMO code table 1770"
    assert ds["PROBE_TYPE_RAW"].attrs["reference_datum"] == "WMO code table 1770"
    # Appendix F p.87 names the "as recorded" field PROBE_TYPE_original.
    assert ds["PROBE_TYPE_RAW"].attrs["long_name"] == "PROBE_TYPE_original"
    assert ds["PROBE_TYPE"].attrs["long_name"] == "XBT_probe_type"


def test_temp_valid_range_matches_appendix_f():
    ds = build_xbt_netcdf(apply_qc([_cast()]))
    assert ds["TEMP"].attrs["valid_min"] == -2.5
    assert ds["TEMP"].attrs["valid_max"] == 40.0


def test_history_variables_cite_their_gtspp_code_tables():
    ds = build_xbt_netcdf(apply_qc([_cast()]))
    assert ds["HISTORY_INSTITUTION"].attrs["Conventions"] == "GTSPP IDENT_CODE table"
    assert ds["HISTORY_STEP"].attrs["Conventions"] == "GTSPP PRC_CODE table"
    assert ds["HISTORY_PARAMETER"].attrs["Conventions"] == "GTSPP PC_PROF table"
    expected = "GTSPP ACT_CODE table and CSIRO XBT Cookbook"
    assert ds["HISTORY_QC_FLAG"].attrs["Conventions"] == expected
    assert ds["HISTORY_QC_FLAG_DESCRIPTION"].attrs["Conventions"] == expected


def test_history_step_value_is_our_own_identifier_not_a_gtspp_prc_code():
    # The Conventions attribute above points at the code table for
    # documentation; it must not tempt anyone into writing a real PRC_CODE
    # like the cookbook example's "CSCB", which would falsely claim CSIRO's
    # own formal QC process had run over this data.
    first = _cast(launch_time=datetime(2025, 3, 1, 12, 0, 0), latitude=-42.0, longitude=149.0)
    second = _cast(launch_time=datetime(2025, 3, 1, 12, 10, 0), latitude=-41.0, longitude=149.0)
    ds = build_xbt_netcdf(apply_qc([first, second]))
    assert ds["HISTORY_STEP"].values[1, 0] == "AADC_XBT_QC"
    assert ds["HISTORY_INSTITUTION"].values[1, 0] == "Australian Antarctic Division"


def test_fall_rate_coefficients_written_only_for_a_cookbook_documented_probe():
    # The cookbook publishes fall-rate coefficients for exactly one probe type
    # (Sippican Deep Blue, Appendix F pp.86-87) and none for T-5/T-7, so those
    # attributes are omitted rather than fabricated -- and omitted entirely
    # when the file mixes probe types, since they describe the whole variable.
    deep_blue = build_xbt_netcdf(apply_qc([_cast(probe_type="DB", probe_type_raw="DB")]))
    assert deep_blue["PROBE_TYPE"].attrs["fall_rate_coefficients"] == "a:6.691,b:-0.00225"
    assert deep_blue["PROBE_TYPE"].attrs["probe_type_name"] == "Sippican Deep Blue"

    t5_only = build_xbt_netcdf(apply_qc([_cast(probe_type="T-5", probe_type_raw="T-5")]))
    assert "fall_rate_coefficients" not in t5_only["PROBE_TYPE"].attrs

    mixed = build_xbt_netcdf(apply_qc([
        _cast(probe_type="DB", probe_type_raw="DB"),
        _cast(launch_time=datetime(2025, 3, 1, 13, 0, 0),
              probe_type="T-5", probe_type_raw="T-5"),
    ]))
    assert "fall_rate_coefficients" not in mixed["PROBE_TYPE"].attrs


def test_appendix_f_attributes_round_trip_to_disk(tmp_path):
    import xarray as xr

    ds = build_xbt_netcdf(apply_qc([_cast(probe_type="DB", probe_type_raw="DB")]))
    nc_path = tmp_path / "attrs_check.nc"
    ds.to_netcdf(nc_path)

    # mask_and_scale=False for the same reason as the QC-dtype round trip
    # above: it reads the physical on-disk attributes rather than xarray's
    # CF-decoded view of them.
    reopened = xr.open_dataset(nc_path, mask_and_scale=False)
    try:
        fault = reopened["XBT_fault_and_feature_flag_type"]
        assert fault.dtype == np.uint32
        assert list(fault.attrs["flag_values"]) == [131072, 262144, 1048576, 2097152]
        assert fault.attrs["flag_values"].dtype == np.uint32
        assert fault.attrs["flag_meanings"] == "time_error position_error test_probe probe_type_error"
        assert fault.attrs["valid_max"] == 2097152

        datum = "geographical coordinates, WGS84 projection"
        assert reopened["LATITUDE"].attrs["reference_datum"] == datum
        assert reopened["LONGITUDE"].attrs["reference_datum"] == datum
        assert reopened["PROBE_TYPE"].attrs["reference_datum"] == "WMO code table 1770"
        assert reopened["PROBE_TYPE"].attrs["fall_rate_coefficients"] == "a:6.691,b:-0.00225"
        assert reopened["PROBE_TYPE_RAW"].attrs["long_name"] == "PROBE_TYPE_original"

        assert reopened["TEMP"].attrs["valid_min"] == -2.5
        assert reopened["TEMP"].attrs["valid_max"] == 40.0
        # Appendix F p.87's fill convention for TEMP, set via .encoding so the
        # in-memory array keeps its NaN padding.
        assert reopened["TEMP"].attrs["_FillValue"] == 99.0

        assert reopened["HISTORY_STEP"].attrs["Conventions"] == "GTSPP PRC_CODE table"
        assert reopened["DEPTH_VALUES"].attrs["ancillary_variables"] == (
            "DEPTH_VALUES_quality_control"
        )
        assert reopened["DEPTH_VALUES_quality_control"].dtype == np.uint8
    finally:
        reopened.close()


def test_temp_fill_value_does_not_disturb_in_memory_nan_padding(tmp_path):
    import xarray as xr

    short = _cast(depth_m=np.array([10.0, 11.0]), temperature_c=np.array([10.0, 9.5]),
                  sound_velocity_ms=np.array([1500.0, 1500.1]))
    long = _cast(launch_time=datetime(2025, 3, 1, 13, 0, 0),
                 depth_m=np.array([10.0, 11.0, 12.0]),
                 temperature_c=np.array([10.0, 9.5, 9.0]),
                 sound_velocity_ms=np.array([1500.0, 1500.1, 1500.2]))
    ds = build_xbt_netcdf(apply_qc([short, long]))
    assert np.isnan(ds["TEMP"].values[0, 2])  # in memory: still NaN, not 99

    nc_path = tmp_path / "fill_check.nc"
    ds.to_netcdf(nc_path)

    # Decoded on read, the padding comes back as NaN rather than a literal 99
    # masquerading as a real temperature.
    reopened = xr.open_dataset(nc_path)
    try:
        assert np.isnan(reopened["TEMP"].values[0, 2])
        assert reopened["TEMP"].values[1, 2] == pytest.approx(9.0)
    finally:
        reopened.close()


def test_global_attributes_match_repo_convention():
    casts_qc = apply_qc([_cast()])
    ds = build_xbt_netcdf(casts_qc, voyage="202425030")
    assert ds.attrs["Conventions"] == "CF-1.6,IMOS-1.4"
    assert ds.attrs["featureType"] == "profile"
    assert ds.attrs["voyage"] == "202425030"


def test_references_points_at_this_repo_by_default():
    ds = build_xbt_netcdf(apply_qc([_cast()]))
    assert ds.attrs["references"] == "https://github.com/botheredbybees/xbt-edf-qc"


def test_global_attrs_overrides_customise_institution_without_editing_defaults():
    ds = build_xbt_netcdf(apply_qc([_cast()]), global_attrs_overrides={
        "institution": "Some Other Institute",
        "ship": "R/V Someone Else",
    })
    assert ds.attrs["institution"] == "Some Other Institute"
    assert ds.attrs["ship"] == "R/V Someone Else"
    # References isn't AADC-specific -- an override that doesn't touch it
    # should leave it pointing at the QC code that actually ran.
    assert ds.attrs["references"] == "https://github.com/botheredbybees/xbt-edf-qc"
    # Everything not overridden keeps the AADC/Nuyina default.
    assert ds.attrs["data_centre"] == "Australian Antarctic Data Centre (AADC)"


def test_voyage_id_variable_present_when_no_single_voyage_given():
    casts_qc = apply_qc([_cast(voyage_id="202425030"), _cast(voyage_id="202526010")])
    ds = build_xbt_netcdf(casts_qc)  # voyage=None -> historical/multi-voyage build
    assert list(ds["VOYAGE_ID"].values) == ["202425030", "202526010"]
    assert "voyage" not in ds.attrs


def test_qc_flag_attrs_are_uint8_matching_the_variable_dtype():
    casts_qc = apply_qc([_cast()])
    ds = build_xbt_netcdf(casts_qc)
    attrs = ds["TEMP_quality_control"].attrs
    assert attrs["flag_values"].dtype == np.uint8
    assert isinstance(attrs["valid_min"], np.uint8)
    assert isinstance(attrs["valid_max"], np.uint8)
    assert ds["TEMP_quality_control"].dtype == np.uint8


def test_qc_flag_attrs_round_trip_as_ubyte_on_disk(tmp_path):
    casts_qc = apply_qc([_cast()])
    ds = build_xbt_netcdf(casts_qc)
    nc_path = tmp_path / "qc_dtype_check.nc"
    ds.to_netcdf(nc_path)

    # mask_and_scale=False: with the default True, xarray sees the
    # _FillValue attr and CF-decodes the variable to float (to represent a
    # masked fill as NaN), which would hide the actual on-disk storage type.
    # This reads the physical on-disk type instead -- confirmed against
    # `ncdump -h`, which independently reports `ubyte TEMP_quality_control`.
    import xarray as xr
    reopened = xr.open_dataset(nc_path, mask_and_scale=False)
    var = reopened["TEMP_quality_control"]
    assert var.dtype == np.uint8
    assert var.attrs["flag_values"].dtype == np.uint8
    assert isinstance(var.attrs["valid_min"], np.uint8)
    assert isinstance(var.attrs["valid_max"], np.uint8)


def test_time_and_history_date_encode_with_fixed_epoch_regardless_of_data(tmp_path):
    import xarray as xr

    # Two datasets with genuinely different real dates -- if the epoch were
    # data-dependent, these would encode with two different `units` strings.
    early = apply_qc([_cast(launch_time=datetime(2020, 1, 1, 0, 0, 0))])
    late = apply_qc([_cast(launch_time=datetime(2025, 3, 1, 12, 0, 0))])

    early_path = tmp_path / "early.nc"
    late_path = tmp_path / "late.nc"
    build_xbt_netcdf(early).to_netcdf(early_path)
    build_xbt_netcdf(late).to_netcdf(late_path)

    # xarray/cftime normalise the "Z" we set in .encoding to "+00:00" when it
    # re-serialises the units string on write -- confirmed via a real
    # to_netcdf()/ncdump round-trip while writing this fix. What matters for
    # this fix is that the string is now IDENTICAL regardless of the actual
    # cast dates (a fixed epoch), not its exact spelling.
    expected_units = "seconds since 1970-01-01T00:00:00+00:00"
    for path in (early_path, late_path):
        reopened = xr.open_dataset(path, decode_times=False)
        assert reopened["TIME"].attrs["units"] == expected_units
        assert reopened["HISTORY_DATE"].attrs["units"] == expected_units
        reopened.close()


def test_history_date_nat_slots_round_trip_with_fixed_epoch(tmp_path):
    import xarray as xr

    # The failed speed check fires two entries on the second cast (PE and TE),
    # filling slots 0 and 1 and leaving only slot 2 NaT -- confirm the
    # fixed-epoch encoding doesn't break that fill.
    first = _cast(launch_time=datetime(2025, 3, 1, 12, 0, 0), latitude=-42.0, longitude=149.0)
    second = _cast(launch_time=datetime(2025, 3, 1, 12, 10, 0), latitude=-41.0, longitude=149.0)
    casts_qc = apply_qc([first, second])
    ds = build_xbt_netcdf(casts_qc)
    nc_path = tmp_path / "nat_check.nc"
    ds.to_netcdf(nc_path)

    reopened = xr.open_dataset(nc_path)
    history_date = reopened["HISTORY_DATE"].values
    assert np.isnat(history_date[0, 0])  # first cast: no history entries fired at all
    assert np.isnat(history_date[0, 1])
    assert not np.isnat(history_date[1, 0])  # second cast: PE entry, slot 0
    assert not np.isnat(history_date[1, 1])  # second cast: TE entry, slot 1
    assert np.isnat(history_date[1, 2])


def test_depth_values_variable_preserves_real_measurements():
    short = _cast(depth_m=np.array([10.0, 15.3, 22.7]), temperature_c=np.array([10.0, 9.2, 8.1]),
                  sound_velocity_ms=np.array([1500.0, 1500.5, 1501.2]))
    long = _cast(launch_time=datetime(2025, 3, 1, 13, 0, 0),
                 depth_m=np.array([10.0, 12.1, 18.8, 25.0]),
                 temperature_c=np.array([10.0, 9.7, 8.9, 8.0]),
                 sound_velocity_ms=np.array([1500.0, 1500.3, 1501.0, 1501.5]))
    casts_qc = apply_qc([short, long])
    ds = build_xbt_netcdf(casts_qc)
    assert "DEPTH_VALUES" in ds.data_vars
    assert ds["DEPTH_VALUES"].values[0, 1] == pytest.approx(15.3)
    assert ds["DEPTH_VALUES"].values[0, 2] == pytest.approx(22.7)
    assert np.isnan(ds["DEPTH_VALUES"].values[0, 3])  # short cast padded
    assert ds["DEPTH_VALUES"].values[1, 2] == pytest.approx(18.8)
    assert ds["DEPTH_VALUES"].values[1, 3] == pytest.approx(25.0)
    assert ds["TEMP"].attrs["coordinates"] == "TIME LATITUDE LONGITUDE DEPTH_VALUES"
    assert ds["SOUND_VELOCITY"].attrs["coordinates"] == "TIME LATITUDE LONGITUDE DEPTH_VALUES"
