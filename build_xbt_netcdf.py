"""Builds a CF-1.6/IMOS-1.4 profile NetCDF from a list of QC'd XBT casts (NDO-645).

One profile per cast. Depth is padded (NaN/fill) to the longest cast in the
batch, per IMOS's own recommended "orthogonal" representation for profile
collections where casts don't share a common vertical grid.
"""
from __future__ import annotations

import datetime

import numpy as np
import xarray as xr

from parse_xbt_edf import (
    DEPTH_VALID_MAX,
    DEPTH_VALID_MIN,
    LATITUDE_VALID_MAX,
    LATITUDE_VALID_MIN,
    LONGITUDE_VALID_MAX,
    LONGITUDE_VALID_MIN,
    SOUND_VELOCITY_VALID_MAX,
    SOUND_VELOCITY_VALID_MIN,
    TEMP_VALID_MAX,
    TEMP_VALID_MIN,
)

_QC_FLAG_VALUES = np.array(range(10), dtype=np.uint8)
_QC_FLAG_MEANINGS = (
    "No_QC_performed Good_data Probably_good_data "
    "Bad_data_that_are_potentially_correctable Bad_data Value_changed "
    "Not_used Not_used Not_used Missing_value"
)
_QC_FILL_VALUE = 99

# A cast can trigger at most 10 QC history entries: the surface-spike check
# emits 1 (CS), the speed check emits 2 (PE and TE -- an automated check
# cannot tell a position error from a time error), the probe-type check 1,
# the isolated-spike check 1 (SP), and the physical-plausibility range check
# up to 5 more (RC on TEMP, DEPTH, SOUND_VELOCITY, LATITUDE, LONGITUDE --
# each is independent, so worst case all 5 fire on the same cast). A test
# probe cast reaches none of the first two nor the surface-spike check, and
# emits at most 7 (TP + SP + up to 5 RC). See parse_xbt_edf.apply_qc.
_N_HISTORY = 12

# Australian XBT Quality Control Cookbook v2.1 (Cowley & Krummel, CSIRO 2022),
# Appendix F pp.86-87.
_GEOGRAPHIC_REFERENCE_DATUM = "geographical coordinates, WGS84 projection"
_PROBE_TYPE_REFERENCE_DATUM = "WMO code table 1770"
_TEMP_FILL_VALUE = 99.0

# The cookbook publishes fall-rate coefficients and a full probe name for
# exactly one probe type -- Sippican Deep Blue (Appendix F pp.86-87). It gives
# none for the T-5 and T-7 probes the ship also stocks, and none appear
# anywhere else in its 89 pages. Rather than fabricate them from another
# source, these attributes are written only when every profile in the file
# shares a probe type this table actually covers; a file containing a T-5 or
# T-7 cast simply omits them. Add a row here only from an authoritative source.
_COOKBOOK_PROBE_METADATA = {
    "Deep Blue": {
        "fall_rate_coefficients": "a:6.691,b:-0.00225",
        "probe_type_name": "Sippican Deep Blue",
    },
    "DB": {
        "fall_rate_coefficients": "a:6.691,b:-0.00225",
        "probe_type_name": "Sippican Deep Blue",
    },
}


def build_xbt_netcdf(casts_qc: list, voyage: str = None,
                      global_attrs_overrides: dict = None) -> "xr.Dataset":
    """Builds the profile NetCDF dataset from a list of QC'd casts.

    Args:
        casts_qc: CastQC objects, in any order (already QC'd -- see
            parse_xbt_edf.apply_qc). Should already exclude TestProbe casts;
            this function does not filter them.
        voyage: if all casts belong to a single voyage, its ID -- written as
            a `voyage` global attribute. Leave as None for a multi-voyage
            (historical backfill) build, which instead gets a per-profile
            `VOYAGE_ID` variable.
        global_attrs_overrides: merged over DEFAULT_GLOBAL_ATTRS (which
            describe AADC's own RSV Nuyina deployment) -- pass this for any
            other ship/institution. See DEFAULT_GLOBAL_ATTRS's own comment.

    Returns:
        An xarray Dataset ready to write with `.to_netcdf()`.
    """
    n_profiles = len(casts_qc)
    max_depth = max(len(qc.cast.depth_m) for qc in casts_qc)

    time = np.empty(n_profiles, dtype="datetime64[ns]")
    latitude = np.empty(n_profiles, dtype="float64")
    longitude = np.empty(n_profiles, dtype="float64")
    probe_type = np.empty(n_profiles, dtype=object)
    probe_type_raw = np.empty(n_profiles, dtype=object)
    voyage_id = np.empty(n_profiles, dtype=object)

    time_qc = np.empty(n_profiles, dtype="uint8")
    latitude_qc = np.empty(n_profiles, dtype="uint8")
    longitude_qc = np.empty(n_profiles, dtype="uint8")
    probe_type_qc = np.empty(n_profiles, dtype="uint8")

    depth = np.full((n_profiles, max_depth), np.nan, dtype="float64")
    temp = np.full((n_profiles, max_depth), np.nan, dtype="float64")
    sound_velocity = np.full((n_profiles, max_depth), np.nan, dtype="float64")
    temp_qc = np.full((n_profiles, max_depth), _QC_FILL_VALUE, dtype="uint8")
    sound_velocity_qc = np.full((n_profiles, max_depth), _QC_FILL_VALUE, dtype="uint8")
    depth_qc = np.full((n_profiles, max_depth), _QC_FILL_VALUE, dtype="uint8")
    fault_flags = np.zeros((n_profiles, max_depth), dtype="uint32")

    history_institution = np.full((n_profiles, _N_HISTORY), "", dtype=object)
    history_step = np.full((n_profiles, _N_HISTORY), "", dtype=object)
    history_software = np.full((n_profiles, _N_HISTORY), "", dtype=object)
    history_software_release = np.full((n_profiles, _N_HISTORY), "", dtype=object)
    history_date = np.full((n_profiles, _N_HISTORY), np.datetime64("NaT"), dtype="datetime64[ns]")
    history_parameter = np.full((n_profiles, _N_HISTORY), "", dtype=object)
    history_start_depth = np.full((n_profiles, _N_HISTORY), np.nan, dtype="float64")
    history_stop_depth = np.full((n_profiles, _N_HISTORY), np.nan, dtype="float64")
    history_previous_value = np.full((n_profiles, _N_HISTORY), np.nan, dtype="float64")
    history_qc_flag = np.full((n_profiles, _N_HISTORY), "", dtype=object)
    history_qc_flag_description = np.full((n_profiles, _N_HISTORY), "", dtype=object)

    for profile_index, qc in enumerate(casts_qc):
        cast = qc.cast
        n = len(cast.depth_m)

        time[profile_index] = np.datetime64(cast.launch_time.replace(microsecond=0), "ns")
        latitude[profile_index] = cast.latitude
        longitude[profile_index] = cast.longitude
        probe_type[profile_index] = cast.probe_type
        probe_type_raw[profile_index] = cast.probe_type_raw
        voyage_id[profile_index] = cast.voyage_id

        time_qc[profile_index] = qc.time_qc
        latitude_qc[profile_index] = qc.latitude_qc
        longitude_qc[profile_index] = qc.longitude_qc
        probe_type_qc[profile_index] = qc.probe_type_qc

        depth[profile_index, :n] = cast.depth_m
        temp[profile_index, :n] = cast.temperature_c
        sound_velocity[profile_index, :n] = cast.sound_velocity_ms
        temp_qc[profile_index, :n] = qc.temperature_qc
        sound_velocity_qc[profile_index, :n] = qc.sound_velocity_qc
        depth_qc[profile_index, :n] = qc.depth_qc
        fault_flags[profile_index, :n] = qc.fault_flags

        for history_index, entry in enumerate(qc.history[:_N_HISTORY]):
            history_institution[profile_index, history_index] = entry.institution
            history_step[profile_index, history_index] = entry.step
            history_software[profile_index, history_index] = entry.software
            history_software_release[profile_index, history_index] = entry.software_release
            history_date[profile_index, history_index] = np.datetime64(entry.date.replace(microsecond=0), "ns")
            history_parameter[profile_index, history_index] = entry.parameter
            history_start_depth[profile_index, history_index] = entry.start_depth
            history_stop_depth[profile_index, history_index] = entry.stop_depth
            history_previous_value[profile_index, history_index] = entry.previous_value
            history_qc_flag[profile_index, history_index] = entry.qc_flag
            history_qc_flag_description[profile_index, history_index] = entry.qc_flag_description

    data_vars = {
        "TIME": (("PROFILE",), time, {
            "standard_name": "time", "long_name": "time", "axis": "T",
            "ancillary_variables": "TIME_quality_control",
        }),
        "TIME_quality_control": (("PROFILE",), time_qc, _qc_attrs("time")),
        "LATITUDE": (("PROFILE",), latitude, {
            "standard_name": "latitude", "long_name": "latitude", "units": "degrees_north",
            "axis": "Y", "valid_min": LATITUDE_VALID_MIN, "valid_max": LATITUDE_VALID_MAX,
            "reference_datum": _GEOGRAPHIC_REFERENCE_DATUM,
            "ancillary_variables": "LATITUDE_quality_control",
        }),
        "LATITUDE_quality_control": (("PROFILE",), latitude_qc, _qc_attrs("latitude")),
        "LONGITUDE": (("PROFILE",), longitude, {
            "standard_name": "longitude", "long_name": "longitude", "units": "degrees_east",
            "axis": "X", "valid_min": LONGITUDE_VALID_MIN, "valid_max": LONGITUDE_VALID_MAX,
            "reference_datum": _GEOGRAPHIC_REFERENCE_DATUM,
            "ancillary_variables": "LONGITUDE_quality_control",
        }),
        "LONGITUDE_quality_control": (("PROFILE",), longitude_qc, _qc_attrs("longitude")),
        "PROBE_TYPE": (("PROFILE",), probe_type, _probe_type_attrs(
            probe_type,
            long_name="XBT_probe_type",
            ancillary_variables="PROBE_TYPE_quality_control",
        )),
        "PROBE_TYPE_RAW": (("PROFILE",), probe_type_raw, _probe_type_attrs(
            probe_type_raw,
            # Appendix F (p.87) names the "as recorded" field's long_name
            # "PROBE_TYPE_original", not a free-text description of it.
            long_name="PROBE_TYPE_original",
        )),
        "PROBE_TYPE_quality_control": (("PROFILE",), probe_type_qc, _qc_attrs("PROBE_TYPE")),
        "DEPTH_VALUES": (("PROFILE", "DEPTH"), depth, {
            "standard_name": "depth", "long_name": "depth", "units": "m",
            "positive": "down", "axis": "Z",
            "valid_min": DEPTH_VALID_MIN, "valid_max": DEPTH_VALID_MAX,
            "ancillary_variables": "DEPTH_VALUES_quality_control",
        }),
        "DEPTH_VALUES_quality_control": (("PROFILE", "DEPTH"), depth_qc, _qc_attrs("depth")),
        "TEMP": (("PROFILE", "DEPTH"), temp, {
            "standard_name": "sea_water_temperature", "long_name": "sea_water_temperature",
            "units": "Celsius", "coordinates": "TIME LATITUDE LONGITUDE DEPTH_VALUES",
            "valid_min": TEMP_VALID_MIN, "valid_max": TEMP_VALID_MAX,
            "ancillary_variables": "TEMP_quality_control",
        }),
        "TEMP_quality_control": (("PROFILE", "DEPTH"), temp_qc, _qc_attrs("sea_water_temperature")),
        "SOUND_VELOCITY": (("PROFILE", "DEPTH"), sound_velocity, {
            "long_name": "sound_velocity", "units": "m s-1",
            "coordinates": "TIME LATITUDE LONGITUDE DEPTH_VALUES",
            "valid_min": SOUND_VELOCITY_VALID_MIN, "valid_max": SOUND_VELOCITY_VALID_MAX,
            "ancillary_variables": "SOUND_VELOCITY_quality_control",
        }),
        "SOUND_VELOCITY_quality_control": (("PROFILE", "DEPTH"), sound_velocity_qc, _qc_attrs("sound_velocity")),
        "XBT_fault_and_feature_flag_type": (("PROFILE", "DEPTH"), fault_flags, {
            "long_name": "XBT_fault_and_feature_flag",
            "_FillValue": np.uint32(0),
            "valid_min": np.uint32(0),
            # Appendix F states valid_max = 65536, but that is internally
            # inconsistent with its own flag_values list, which runs up to
            # 2097152 -- a defect in the cookbook, not in this data. Published
            # here as the true maximum of the flag_values actually written
            # below, so the range genuinely covers the values in the variable.
            "valid_max": np.uint32(2097152),
            # Appendix F documents this variable with flag_values/flag_meanings
            # even though it is a bitmask (CF-1.6 section 3.5 would prefer
            # flag_masks for non-exclusive bit flags). The cookbook's own
            # convention is followed here, since it is the compliance target.
            "flag_values": np.array([131072, 262144, 1048576, 2097152], dtype=np.uint32),
            "flag_meanings": "time_error position_error test_probe probe_type_error",
            "comment": (
                "Bitmask. Only time_error (131072), position_error (262144), "
                "test_probe (1048576), and probe_type_error (2097152) are ever set "
                "by this pipeline -- a documented subset of the full 22-value enum "
                "in the Australian XBT Quality Control Cookbook v2.1 (Cowley & "
                "Krummel, CSIRO 2022), Appendix F. time_error and position_error "
                "are always set together: an automated speed check cannot "
                "distinguish the two (see HISTORY_QC_FLAG 'PE'/'TE' for the same "
                "cast)."
            ),
        }),
        # The four Conventions attributes below name the GTSPP code table each
        # field is documented against in Appendix F (p.88). They are pointers to
        # the relevant table for a reader, NOT a claim that the values written
        # here are members of it: HISTORY_STEP in particular stays
        # "AADC_XBT_QC", this pipeline's own honest identifier, rather than
        # being changed to a real PRC_CODE like the cookbook example's "CSCB",
        # which would falsely assert that CSIRO's formal QC process had run.
        "HISTORY_INSTITUTION": (("PROFILE", "N_HISTORY"), history_institution, {
            "long_name": "Institution which performed action",
            "Conventions": "GTSPP IDENT_CODE table",
        }),
        "HISTORY_STEP": (("PROFILE", "N_HISTORY"), history_step, {
            "long_name": "Step in data processing",
            "Conventions": "GTSPP PRC_CODE table",
        }),
        "HISTORY_SOFTWARE": (("PROFILE", "N_HISTORY"), history_software, {
            "long_name": "Name of software which performed action",
        }),
        "HISTORY_SOFTWARE_RELEASE": (("PROFILE", "N_HISTORY"), history_software_release, {
            "long_name": "Version/Release of software which performed action",
        }),
        "HISTORY_DATE": (("PROFILE", "N_HISTORY"), history_date, {
            "long_name": "Date the history record was created",
        }),
        "HISTORY_PARAMETER": (("PROFILE", "N_HISTORY"), history_parameter, {
            "long_name": "Parameter that action is performed on",
            "Conventions": "GTSPP PC_PROF table",
        }),
        "HISTORY_START_DEPTH": (("PROFILE", "N_HISTORY"), history_start_depth, {
            "long_name": "Start depth action applied to", "units": "m", "positive": "down",
        }),
        "HISTORY_STOP_DEPTH": (("PROFILE", "N_HISTORY"), history_stop_depth, {
            "long_name": "End depth action applied to", "units": "m", "positive": "down",
        }),
        # Always the fill (NaN): none of this pipeline's checks correct a value,
        # so there is never a genuine previous value to report. Diagnostics
        # (implied speed, temperature variation) live in
        # HISTORY_QC_FLAG_DESCRIPTION -- putting one here would publish a number
        # under an attribute that says it is something else.
        "HISTORY_PREVIOUS_VALUE": (("PROFILE", "N_HISTORY"), history_previous_value, {
            "long_name": "Parameter previous value before action",
        }),
        "HISTORY_QC_FLAG": (("PROFILE", "N_HISTORY"), history_qc_flag, {
            "long_name": "QC flag applied",
            "Conventions": "GTSPP ACT_CODE table and CSIRO XBT Cookbook",
        }),
        "HISTORY_QC_FLAG_DESCRIPTION": (("PROFILE", "N_HISTORY"), history_qc_flag_description, {
            "long_name": "Description of HISTORY_QC_FLAG",
            "Conventions": "GTSPP ACT_CODE table and CSIRO XBT Cookbook",
        }),
    }

    if voyage is not None:
        global_attrs = _global_attributes(voyage, overrides=global_attrs_overrides)
    else:
        data_vars["VOYAGE_ID"] = (("PROFILE",), voyage_id, {
            "long_name": "voyage this cast was collected on",
        })
        global_attrs = _global_attributes(voyage=None, overrides=global_attrs_overrides)

    dataset = xr.Dataset(data_vars=data_vars, attrs=global_attrs)

    # Fix a stable, explicit epoch for both datetime64 variables rather than
    # letting xarray infer one from the actual data on each build -- otherwise
    # the on-disk `units` string (and hence byte-for-byte output) varies run
    # to run depending on which dates happen to be present. Matches
    # rebuild_underway_merger_netcdf.py's own convention.
    dataset["TIME"].encoding.update({
        "units": "seconds since 1970-01-01T00:00:00Z",
        "calendar": "gregorian",
        "dtype": "int64",
    })
    # HISTORY_DATE deliberately does NOT force dtype=int64: unfired history
    # slots are NaT, and forcing an integer dtype here without also supplying
    # an explicit integer _FillValue silently corrupts every NaT slot into
    # the epoch itself (1970-01-01T00:00:00Z) instead of a genuine missing
    # marker -- confirmed via a real to_netcdf()/reopen round-trip while
    # writing this fix. Leaving dtype unset lets xarray keep its existing,
    # already-correct behaviour of picking float64 with _FillValue=NaN
    # whenever NaT is present, just under the fixed units/calendar below.
    dataset["HISTORY_DATE"].encoding.update({
        "units": "seconds since 1970-01-01T00:00:00Z",
        "calendar": "gregorian",
    })

    # TEMP's fill goes in .encoding, not .attrs: the in-memory array must stay
    # NaN-padded (the whole padding/masking scheme downstream of build_xbt_netcdf
    # depends on that), while the on-disk file gets the cookbook's own fill
    # convention of 99 (Appendix F p.87). Setting it as an attribute instead
    # would leave xarray writing NaN and merely *claiming* the fill was 99.
    dataset["TEMP"].encoding.update({"_FillValue": _TEMP_FILL_VALUE})

    return dataset


def _probe_type_attrs(probe_types, long_name: str, ancillary_variables: str = None) -> dict:
    """Appendix F attrs for PROBE_TYPE / PROBE_TYPE_RAW.

    Args:
        probe_types: the probe type recorded for every profile in the file.
        long_name: the variable's long_name.
        ancillary_variables: the QC companion variable, if this variable has one.

    Returns:
        The attribute dict. fall_rate_coefficients/probe_type_name are included
        only when every profile shares one probe type the cookbook actually
        publishes those values for -- they describe the variable as a whole, so
        writing one probe's coefficients onto a file that also contains a
        different probe would misdescribe every other profile in it.
    """
    # Appendix F also shows _FillValue = 'Unknown' on both probe-type
    # variables, and it is deliberately NOT written here: netCDF4 cannot set a
    # fill value on a variable-length string at all (Unidata/netcdf4-python#730
    # -- to_netcdf raises NotImplementedError), and the only way around it is
    # to store the variable as fixed-width NC_CHAR, which would change the
    # published variable's on-disk type and still could not hold the
    # multi-character string "Unknown" as a fill. Nothing is lost in practice:
    # "Probe Type" is a required EDF header field (parse_edf rejects a file
    # without one), so every profile in this product has a real probe type and
    # the fill is never used.
    attrs = {
        "long_name": long_name,
        "reference_datum": _PROBE_TYPE_REFERENCE_DATUM,
    }
    if ancillary_variables is not None:
        attrs["ancillary_variables"] = ancillary_variables

    distinct = set(probe_types)
    if len(distinct) == 1:
        known = _COOKBOOK_PROBE_METADATA.get(distinct.pop())
        if known is not None:
            attrs.update(known)
    return attrs


def _qc_attrs(standard_name_or_long_name: str) -> dict:
    """Standard IMOS attrs for a `<VAR>_quality_control` companion variable."""
    return {
        "standard_name": f"{standard_name_or_long_name} status_flag",
        "long_name": f"quality flag for {standard_name_or_long_name}",
        "quality_control_conventions": "IMOS standard flags",
        "valid_min": np.uint8(0),
        "valid_max": np.uint8(9),
        "flag_values": _QC_FLAG_VALUES,
        "flag_meanings": _QC_FLAG_MEANINGS,
        "_FillValue": _QC_FILL_VALUE,
    }


# The default global attributes below describe AADC's own RSV Nuyina
# deployment -- this module started life inside AADC's Nuyina pipeline
# (cron_jobs_on_skippy) and was extracted here so other institutions running
# an MK21 EDF system can reuse the parsing/QC/NetCDF-building logic without
# reimplementing it. A caller for a different ship/institution should pass
# `global_attrs_overrides` to build_xbt_netcdf() rather than editing these
# defaults in place, so a `git pull` of this repo never silently reverts a
# downstream institution's own identity back to AADC's.
DEFAULT_GLOBAL_ATTRS = {
    "institution": "Australian Antarctic Division (AAD)",
    "ship": "RSV Nuyina",
    "project": "Australian Antarctic Program (AAP)",
    "title": "XBT profiles from the RSV Nuyina",
    "abstract": (
        "Expendable bathythermograph (XBT) temperature/depth profiles collected aboard "
        "the RSV Nuyina."
    ),
    "naming_authority": "AADC",
    "data_centre": "Australian Antarctic Data Centre (AADC)",
    "data_centre_email": "Nuyina.Data.Officer@aad.gov.au",
    "author": "Nuyina Data Officer",
    "disclaimer": (
        "Data, products and services from the AADC are provided \"as is\" without any "
        "warranty as to fitness for a particular purpose."
    ),
    "license": "http://creativecommons.org/licenses/by/4.0/",
}

# Points back at the QC/NetCDF-building code that actually produced the
# file -- not AADC-specific, so it isn't in DEFAULT_GLOBAL_ATTRS above: this
# is the right provenance value regardless of which institution's data it
# is, since it names the algorithm that ran, not who ran it. A caller can
# still override it (e.g. to point at a downstream fork) via
# global_attrs_overrides.
QC_COOKBOOK_REPO_URL = "https://github.com/botheredbybees/xbt-edf-qc"


def _global_attributes(voyage: str = None, overrides: dict = None) -> dict:
    """Global attributes for the published NetCDF.

    Args:
        voyage: if given, written as the `voyage` attribute (single-voyage
            build); omitted for a multi-voyage (historical backfill) build.
        overrides: merged over DEFAULT_GLOBAL_ATTRS -- pass institution/
            ship/project/etc for a deployment other than AADC's Nuyina one.
    """
    attrs = {
        "featureType": "profile",
        **DEFAULT_GLOBAL_ATTRS,
        "Conventions": "CF-1.6,IMOS-1.4",
        "standard_name_vocabulary": (
            "NetCDF Climate and Forecast (CF) Metadata Convention Standard Name Table Version 29"
        ),
        "date_created": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "references": QC_COOKBOOK_REPO_URL,
        **(overrides or {}),
    }
    if voyage is not None:
        attrs["voyage"] = voyage
    return attrs
