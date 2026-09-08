"""Parses RSV Nuyina MK21 EDF-format XBT cast files into structured Cast objects.

Shared by both the per-voyage and historical-backfill NetCDF builders (NDO-645)
so the EDF-parsing logic exists in exactly one place.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class EDFParseError(Exception):
    """Raised when an EDF file's header or data table cannot be parsed."""


@dataclass
class Cast:
    """One parsed XBT cast.

    Attributes:
        source_file: path to the original .edf file, kept for provenance.
        probe_type_raw: the probe type exactly as recorded in the EDF header's
            "Probe Type" field (e.g. "Deep Blue", "T-5") -- not validated
            against known types. This field never records a system self-test
            cast; see is_test_probe_cast() for how those are identified.
        probe_type: normalized probe type. Currently identical to
            probe_type_raw; kept distinct so a future normalization step has
            somewhere to write to without changing the raw provenance field.
        launch_time: cast launch date/time.
        latitude: decimal degrees, positive north.
        longitude: decimal degrees, positive east.
        serial_number: probe serial number, as a string (may contain leading
            zeros or non-numeric characters).
        voyage_id: the voyage this cast belongs to. Not present in the EDF
            file itself -- supplied by the caller, which knows it from the
            file's directory location.
        depth_m: 1-D array, depth in metres for each data row.
        temperature_c: 1-D array, same length as depth_m. The EDF's `-99`
            fault/fill sentinel has already been converted to NaN -- found
            2026-09-08 appearing here too (not just in sound_velocity_ms
            below), during the same kind of intermittent connector fault
            documented on the recorder-earthing Known Issue -- see
            is_test_probe_cast() and TEMP_VALID_MIN/TEMP_VALID_MAX.
        sound_velocity_ms: 1-D array, same length as depth_m. The EDF's `-99`
            sentinel (meaning "not yet valid at this shallow depth") has
            already been converted to NaN.
        elapsed_s: 1-D array, seconds since launch for each data row.
        memo: raw text of the EDF's "// Memo" section (may be empty). Not
            part of the header key:value dict -- Memo lines have no ":" and
            are free text, so they're captured separately. Used by
            is_test_probe_cast() as a second signal beyond Serial Number.
    """

    source_file: str
    probe_type_raw: str
    probe_type: str
    launch_time: datetime
    latitude: float
    longitude: float
    serial_number: str
    voyage_id: str
    depth_m: np.ndarray
    temperature_c: np.ndarray
    sound_velocity_ms: np.ndarray
    elapsed_s: np.ndarray
    memo: str = ""


_DATA_MARKER = "// Data\n"
_MEMO_MARKER = "// Memo\n"
_REQUIRED_HEADER_FIELDS = (
    "Date of Launch",
    "Time of Launch",
    "Probe Type",
    "Latitude",
    "Longitude",
    "Serial Number",
)
# Not exclusive to Sound Velocity -- see Cast.temperature_c above.
_EDF_FILL_SENTINEL = -99.0


def parse_edf(filepath: str, voyage_id: str) -> Cast:
    """Parses one MK21 EDF XBT cast file.

    Args:
        filepath: path to the .edf file.
        voyage_id: the voyage this cast belongs to.

    Returns:
        The parsed Cast.

    Raises:
        EDFParseError: if the file can't be read, the header is missing a
            required field, or the data table can't be parsed.
    """
    try:
        with open(filepath, "r", encoding="cp1252") as file_handle:
            lines = file_handle.readlines()
    except OSError as exc:
        raise EDFParseError(f"could not read {filepath}: {exc}") from exc

    header: dict[str, str] = {}
    memo_lines: list[str] = []
    in_memo = False
    data_start_line = None
    for lineno, line in enumerate(lines):
        if line == _DATA_MARKER:
            data_start_line = lineno
            break
        if line == _MEMO_MARKER:
            in_memo = True
            continue
        if line.startswith("//"):
            in_memo = False
            continue
        if in_memo:
            memo_lines.append(line.strip())
            continue
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        header[key.strip()] = rest.strip()

    if data_start_line is None:
        raise EDFParseError(f"{filepath}: no '// Data' marker found")

    missing = [field for field in _REQUIRED_HEADER_FIELDS if field not in header]
    if missing:
        raise EDFParseError(f"{filepath}: missing header field(s): {', '.join(missing)}")

    try:
        launch_date = datetime.strptime(header["Date of Launch"], "%m/%d/%Y").date()
        launch_time_of_day = datetime.strptime(header["Time of Launch"], "%H:%M:%S").time()
        launch_time = datetime.combine(launch_date, launch_time_of_day)
    except ValueError as exc:
        raise EDFParseError(f"{filepath}: bad launch date/time: {exc}") from exc

    try:
        latitude = _parse_edf_coordinate(header["Latitude"], positive="N", negative="S")
        longitude = _parse_edf_coordinate(header["Longitude"], positive="E", negative="W")
    except ValueError as exc:
        raise EDFParseError(f"{filepath}: bad latitude/longitude: {exc}") from exc

    try:
        table = pd.read_csv(
            filepath,
            skiprows=data_start_line + 1,
            delimiter="\t",
            encoding="cp1252",
            names=["elapsed_s", "resistance_ohms", "depth_m", "temperature_c", "sound_velocity_ms"],
        )
    except Exception as exc:  # pandas can raise several distinct error types here
        raise EDFParseError(f"{filepath}: could not parse data table: {exc}") from exc

    if table.empty:
        raise EDFParseError(f"{filepath}: data table is empty")

    sound_velocity = table["sound_velocity_ms"].to_numpy(dtype=float)
    sound_velocity[sound_velocity == _EDF_FILL_SENTINEL] = np.nan

    temperature = table["temperature_c"].to_numpy(dtype=float)
    temperature[temperature == _EDF_FILL_SENTINEL] = np.nan

    probe_type_raw = header["Probe Type"]

    return Cast(
        source_file=filepath,
        probe_type_raw=probe_type_raw,
        probe_type=probe_type_raw,
        launch_time=launch_time,
        latitude=latitude,
        longitude=longitude,
        serial_number=header["Serial Number"],
        voyage_id=voyage_id,
        depth_m=table["depth_m"].to_numpy(dtype=float),
        temperature_c=temperature,
        sound_velocity_ms=sound_velocity,
        elapsed_s=table["elapsed_s"].to_numpy(dtype=float),
        memo=" ".join(memo_lines),
    )


def _parse_edf_coordinate(raw: str, positive: str, negative: str) -> float:
    """Parses an EDF-style "DD MM.MMMMMH" coordinate into signed decimal degrees.

    Args:
        raw: e.g. "45 19.56440S" or "144 46.65540E".
        positive: hemisphere letter meaning a positive value (N or E).
        negative: hemisphere letter meaning a negative value (S or W).

    Returns:
        Signed decimal degrees.

    Raises:
        ValueError: if raw doesn't match the expected shape.
    """
    raw = raw.strip()
    if len(raw) < 2:
        raise ValueError(f"coordinate too short: {raw!r}")
    hemisphere = raw[-1].upper()
    if hemisphere not in (positive, negative):
        raise ValueError(f"unrecognised hemisphere in {raw!r}")
    degrees_str, _, minutes_str = raw[:-1].strip().partition(" ")
    if not minutes_str:
        raise ValueError(f"expected 'DD MM.MMMM<hemisphere>', got {raw!r}")
    decimal_degrees = float(degrees_str) + float(minutes_str) / 60.0
    return -decimal_degrees if hemisphere == negative else decimal_degrees


GTSPP_GOOD = 1
GTSPP_PROBABLY_BAD = 3
GTSPP_MISSING = 9

# Known real probe types the ship stocks (NuyinAPI's aad_nuyina_interfaces XBT entry lists
# T5/T7/Deep Blue as primary stock; T-6 appears there too from an older record). The real EDF
# header's "Probe Type" field always spells these out in full ("Deep Blue", "T-5", "T-7" --
# confirmed against the ship's whole historical archive, 2026-09-07); the plain abbreviated
# forms (DB, T5, T7...) never actually appear there and are kept only in case an older or
# differently-configured MK21 unit ever writes them.
KNOWN_PROBE_TYPES = {
    "Deep Blue", "T-4", "T-5", "T-6", "T-7", "T-10", "T-11",
    "DB", "T4", "T5", "T6", "T7", "T10", "T11", "XSV01", "XSV02",
}

MAX_PLAUSIBLE_SPEED_KNOTS = 25.0
TEST_PROBE_MAX_TEMPERATURE_VARIATION_C = 0.005
_EARTH_RADIUS_NM = 3440.065

# Canonical source for every published variable's valid range -- build_xbt_netcdf.py
# imports these for its valid_min/valid_max NetCDF attributes rather than
# redeclaring the numbers, so metadata and the actual QC check can't drift
# apart the way they did before this fix. Added 2026-09-08 after a
# resistance/connector glitch mid-cast wrote a literal -99 into TEMP (the
# EDF's own fault sentinel, previously only handled for Sound Velocity) and
# it published as GTSPP flag 1 ("good") because TEMP had a correct valid_min/
# valid_max attribute but nothing ever checked real data against it. Once
# that gap was found for TEMP, the same audit was done for every other
# published variable -- LATITUDE/LONGITUDE already declared -90/90 and
# -180/180 but never enforced it either (a garbled coordinate with a valid
# hemisphere letter but an out-of-range degree magnitude would have passed
# silently), and DEPTH_VALUES/SOUND_VELOCITY had no declared range at all.
#
# TEMP's -2.5/40.0 bounds are the cookbook/GTSPP convention (see the module's
# QC-check comments for the citation). LATITUDE/LONGITUDE are the physical
# limits of a coordinate, not a cookbook citation. DEPTH_VALID_MAX is now a
# real cited figure, not a guess: the Lockheed Martin/Sippican MK21 ISA
# manual's probe capability table (confirmed via nuyina-docs-search
# 2026-09-08) gives T-5's 6000 ft (1830 m) as the deepest-rated probe this
# ship stocks (Deep Blue/T-7 are 760 m, per the same table) -- 2500 m is that
# real number plus margin, not an invented one. SOUND_VELOCITY_VALID_MIN/MAX
# remain a pragmatic engineering bound (standard oceanographic sound-velocity
# range), not tied to a specific citation -- chosen deliberately without a
# fabricated one, after finding a wrong cookbook-page citation elsewhere in
# this module once already (test_fault_bit_values_match_appendix_f), and
# since validated against real ship data: 335 real samples exceeding 1560 m/s
# all traced to casts independently confirmed corrupted (31-37 degC at
# 200-1800 m in 45-68 S water), not real warm-water data the bound wrongly
# excludes.
TEMP_VALID_MIN = -2.5
TEMP_VALID_MAX = 40.0
LATITUDE_VALID_MIN = -90.0
LATITUDE_VALID_MAX = 90.0
LONGITUDE_VALID_MIN = -180.0
LONGITUDE_VALID_MAX = 180.0
DEPTH_VALID_MIN = -1.0  # small negative slop for near-surface sensor/calibration noise
DEPTH_VALID_MAX = 2500.0  # deepest ship-stocked probe (T-5, 1830 m rated) plus margin
SOUND_VELOCITY_VALID_MIN = 1400.0
SOUND_VELOCITY_VALID_MAX = 1560.0

# CSIRO Marine Laboratories Report 221, "Quality Control Cookbook for XBT
# Data" v1.1 (Bailey, Gronell, Phillips, Tanner & Meyers, 1994), section 2.1
# "Surface Spikes (CS)": "Surface spikes are caused by a minor start-up
# transient problem that leads to inaccurate temperature measurements in the
# top few metres of a temperature profile." Accept code CSA: "applied to all
# XBT profiles in which the surface spike is undetectable below 3.7 m depth,
# as the start-up transient problem is ubiquitous ... Surface data is removed
# to 3.7 m depth and replaced with 99.99 to indicate no data. No change to
# the class of data." Applied unconditionally to every real (non-test-probe)
# cast -- found 2026-09-08 when a sample voyage built for an external
# reviewer had a 37 degC reading at 0 m depth in Southern Ocean water,
# published as GTSPP flag 1 ("good") because it sat inside TEMP's 40 degC
# range-check ceiling with nothing else checking for it.
#
# The Reject variant (CSR, when the transient is detectable below 3.7 m and
# judged to actually affect data quality) is deliberately NOT implemented:
# telling a genuine deeper transient apart from real near-surface thermal
# structure needs the same kind of operator judgement call the cookbook
# itself requires to disambiguate PE from TE (section 4.1 step 9), which an
# automated check cannot make -- see the PE/TE handling in apply_qc() for the
# same reasoning applied elsewhere in this module.
SURFACE_SPIKE_DEPTH_M = 3.7

# CSIRO Marine Laboratories Report 221, "Quality Control Cookbook for XBT
# Data" v1.1 (Bailey, Gronell, Phillips, Tanner & Meyers, 1994), section 3.3
# "Spikes (SP)" ("Isolated or intermittent spikes...") and section 3.2 "Wire
# Break (WB)" ("a short circuit causes the temperature readings to go off
# scale"). Found 2026-09-08: a single 37 degC reading at 953 m depth on an
# otherwise unremarkable Southern Ocean cast, with every sample immediately
# around it already missing (-99/NaN) -- i.e. a stray reading that survived
# alone past the point the rest of the cast had already failed, the
# signature these two sections describe. Detected here as exactly that:
# TEMP is flagged when it has a real value but BOTH immediate neighbours
# (one shallower, one deeper) are missing, so there is no real data on
# either side to corroborate it at all.
#
# An earlier version of this check also flagged any point that deviated
# from the average of two REAL neighbours by some threshold (the standard
# spike-test formula used by, e.g., the IOOS/QARTOD real-time QC manuals).
# Checked against the full 368-profile historical archive before trusting
# it (the same discipline SOUND_VELOCITY_VALID_MAX was held to above) --
# and dropped: at any threshold small enough to catch the 953 m case
# (which never had real neighbours anyway, so no threshold there would have
# mattered), it also fired dozens of times on genuine real fine-scale
# ocean structure at depth (small, real step-like temperature wiggles --
# section 5 "Structure / Signal Leakage Flags" describes exactly this as a
# real oceanographic feature, not a fault). That section is explicit that
# telling a real feature from a malfunction there needs "verification with
# neighbouring (or repeat) profiles and previous knowledge of the region"
# -- the same kind of operator judgement call this module already declines
# to automate for CSR and for disambiguating PE from TE. So only the
# isolated-point case (zero real neighbours, not "differs from real
# neighbours") is implemented.
#
# Like every other check here, this only flags, it never corrects a value
# (see HistoryEntry.previous_value's docstring).

# CSIRO Marine Laboratories Report 221, "Quality Control Cookbook for XBT
# Data" v1.1 (Bailey, Gronell, Phillips, Tanner & Meyers, 1994), section 2.2
# "Test Probe (TP)": "A test probe is recognized by a characteristic
# isothermal temperature profile, usually 1.5 degC +/- 0.15 degC." This is a
# DATA-driven signal, unlike Serial Number/Memo/filename -- confirmed
# 2026-09-08 against both real leaked/flagged self-test casts found this
# session, whose stable baseline was independently measured at 1.51 degC in
# each. TEST_PROBE_ISOTHERMAL_MIN_FRACTION is this module's own choice, not
# the cookbook's (it gives a temperature band, not a required fraction of a
# profile inside it): a bare majority, because a resistance/connector glitch
# can corrupt a large-but-still-minority fraction of an otherwise-genuine
# test-probe cast (see the recorder-earthing Known Issue) without the cast
# ceasing to BE a test probe.
TEST_PROBE_ISOTHERMAL_CENTER_C = 1.5
TEST_PROBE_ISOTHERMAL_TOLERANCE_C = 0.15
TEST_PROBE_ISOTHERMAL_MIN_FRACTION = 0.5

# XBT_fault_and_feature_flag_type bit values (Australian XBT Quality Control Cookbook v2.1,
# Cowley & Krummel, CSIRO 2022, Appendix F, p.89). Only the 3 bits this pipeline's checks use
# are named here; the cookbook's full 22-value enum is a superset. The values below are read
# positionally off Appendix F's own paired flag_values/flag_meanings lists (22 values, 22
# space-separated meanings): ... constant_temperature(65536) time_error(131072)
# position_error(262144) duplicate_profile(524288) test_probe(1048576)
# probe_type_error(2097152).
FAULT_TEST_PROBE = 1 << 20        # 1048576 -- 1 << 17 (131072) is time_error, not test_probe
FAULT_TIME_ERROR = 1 << 17        # 131072
FAULT_POSITION_ERROR = 1 << 18    # 262144
FAULT_PROBE_TYPE_ERROR = 1 << 21  # 2097152

_INSTITUTION = "Australian Antarctic Division"
_SOFTWARE = "cron_jobs_on_skippy/parse_xbt_edf.py"
_SOFTWARE_RELEASE = "1.0"
_QC_STEP = "AADC_XBT_QC"


@dataclass
class HistoryEntry:
    """One entry in a cast's QC audit trail (maps to the IMOS HISTORY_* variables).

    Attributes:
        institution: who/what performed the QC action.
        step: processing step name.
        software: name of the software that performed the action.
        software_release: version of that software.
        date: when the action was recorded.
        parameter: which variable(s) the action applies to, comma-separated.
        start_depth: start of the depth range the action applies to, metres.
        stop_depth: end of the depth range the action applies to, metres.
        qc_flag: a short GTSPP-style action code.
        qc_flag_description: human-readable description of the action.
        previous_value: the parameter's previous *data* value before the action,
            per the cookbook's own definition of HISTORY_PREVIOUS_VALUE
            ("Parameter previous value before action", Appendix F p.88). None of
            this pipeline's checks correct a value -- they only flag -- so there
            is never a genuine previous value to report and this stays NaN (the
            fill). Any diagnostic (implied speed, temperature variation) belongs
            in qc_flag_description, not here: writing one into this field would
            mean publishing a number under an attribute that says it is
            something else entirely.
    """

    institution: str
    step: str
    software: str
    software_release: str
    date: datetime
    parameter: str
    start_depth: float
    stop_depth: float
    qc_flag: str
    qc_flag_description: str
    previous_value: float = float("nan")


@dataclass
class CastQC:
    """A Cast plus its QC results.

    Attributes:
        cast: the underlying parsed Cast.
        time_qc, latitude_qc, longitude_qc, probe_type_qc: per-profile scalar
            GTSPP flags.
        temperature_qc, sound_velocity_qc, depth_qc: per-depth-point GTSPP flag
            arrays, same shape as cast.temperature_c / cast.sound_velocity_ms /
            cast.depth_m. Depth gets its own flags because the cookbook's flag
            model covers depth at every point alongside temperature ("GTSPP
            flags are used to indicate the quality of each and every temperature
            and depth data point" -- section 4, p.15), and the PR Reject rule
            (section 4.2.6/Table 2) downgrades depth specifically.
        fault_flags: per-depth-point bitmask (XBT_fault_and_feature_flag_type),
            same shape as cast.depth_m.
        history: ordered list of HistoryEntry, one per check that fired.
    """

    cast: Cast
    time_qc: int = GTSPP_GOOD
    latitude_qc: int = GTSPP_GOOD
    longitude_qc: int = GTSPP_GOOD
    probe_type_qc: int = GTSPP_GOOD
    temperature_qc: np.ndarray = None
    sound_velocity_qc: np.ndarray = None
    depth_qc: np.ndarray = None
    fault_flags: np.ndarray = None
    history: list = None

    def __post_init__(self):
        if self.temperature_qc is None:
            self.temperature_qc = np.full(self.cast.temperature_c.shape, GTSPP_GOOD, dtype=np.uint8)
        if self.sound_velocity_qc is None:
            self.sound_velocity_qc = np.full(self.cast.sound_velocity_ms.shape, GTSPP_GOOD, dtype=np.uint8)
        if self.depth_qc is None:
            self.depth_qc = np.full(self.cast.depth_m.shape, GTSPP_GOOD, dtype=np.uint8)
        if self.fault_flags is None:
            self.fault_flags = np.zeros(self.cast.depth_m.shape, dtype=np.uint32)
        if self.history is None:
            self.history = []

        # A missing reading (NaN -- e.g. the EDF's -99 sound-velocity sentinel,
        # already converted to NaN by the parser) must never be published as
        # "good data". Applied unconditionally, regardless of whether the *_qc
        # array above was just defaulted or passed in explicitly by a caller.
        self.temperature_qc[np.isnan(self.cast.temperature_c)] = GTSPP_MISSING
        self.sound_velocity_qc[np.isnan(self.cast.sound_velocity_ms)] = GTSPP_MISSING
        # Depth has no EDF fill sentinel of its own (unlike sound velocity's
        # -99), so a NaN here only arises from a blank or non-numeric depth cell
        # that pandas coerced -- rare, but reachable from a truncated/corrupt
        # data row, and the same "never publish a missing reading as good"
        # rule applies.
        self.depth_qc[np.isnan(self.cast.depth_m)] = GTSPP_MISSING


def is_test_probe_cast(cast: Cast) -> bool:
    """True if this cast is a system self-test rather than a real, deployed cast.

    A self-test is identified via the Serial Number field, not Probe Type --
    confirmed against the ship's whole historical archive (2026-09-07), which
    never once records "TestProbe" as a Probe Type value; the header's Probe
    Type field for a self-test still shows an ordinary real probe type (e.g.
    "Deep Blue"). Serial Number spellings actually seen for a self-test
    include "TestProbe", "Test Probe", "Test", and "BT_Test_Device"; a real
    probe's serial number is numeric and cannot plausibly contain "test", so
    a case-insensitive substring match is used rather than an exact allow-list
    (which would silently miss a spelling not yet seen).

    Serial Number alone isn't sufficient, though: found 2026-09-08 in
    voyage 202324VT1A's historical archive (VT1A_testprobe20230807033912.edf)
    -- a genuine self-test cast with Serial Number literally "0" and the
    "test" indication only in its Memo field ("Test probe") and filename.
    That cast passed the Serial-Number-only check and was published in the
    "370 real profiles" historical NetCDF as if it were real T-5 data. So this
    now also checks Memo and the source filename -- each is independently
    unreliable (Memo is free text with no fixed convention; filenames aren't
    guaranteed either, per the original note above), but a real probe's cast
    is vanishingly unlikely to have "test" show up in all three by accident,
    and any single match is enough to exclude a cast that shouldn't be
    published as real data.

    A fourth, data-driven signal was added the same day: the CSIRO Cookbook's
    own documented test-probe signature (see TEST_PROBE_ISOTHERMAL_CENTER_C)
    -- isothermal near 1.5 degC for a majority of the cast AND at the very
    first (surface) sample. The surface requirement matters: a real deployed
    probe's surface reading is the actual sea-surface temperature, which is
    never suspiciously pinned near 1.5 degC at the first sample together with
    a majority of every other depth also sitting there -- a genuine Southern
    Ocean cast can have long isothermal stretches near 1-2 degC at depth, but
    not from the surface down, so requiring both together is what keeps this
    from excluding real cold-water data (not validated against a real false
    positive during development -- if one ever turns up, this is the first
    place to look).
    """
    haystacks = (
        cast.serial_number,
        cast.memo,
        os.path.basename(cast.source_file),
    )
    return (
        any("test" in haystack.lower() for haystack in haystacks)
        or _is_isothermal_near_1_5c(cast.temperature_c)
    )


def _is_isothermal_near_1_5c(temperature_c: np.ndarray) -> bool:
    """True if `temperature_c` is isothermal near 1.5 degC from the surface.

    See is_test_probe_cast()'s docstring for why both the surface condition
    and the majority-fraction condition are required together.
    """
    valid = temperature_c[~np.isnan(temperature_c)]
    if valid.size == 0:
        return False
    lower = TEST_PROBE_ISOTHERMAL_CENTER_C - TEST_PROBE_ISOTHERMAL_TOLERANCE_C
    upper = TEST_PROBE_ISOTHERMAL_CENTER_C + TEST_PROBE_ISOTHERMAL_TOLERANCE_C
    surface_near = lower <= valid[0] <= upper
    if not surface_near:
        return False
    near = (valid >= lower) & (valid <= upper)
    return (np.sum(near) / valid.size) >= TEST_PROBE_ISOTHERMAL_MIN_FRACTION


def _flag_array_out_of_range(qc: CastQC, qc_array: np.ndarray, values: np.ndarray,
                              valid_min: float, valid_max: float, param_name: str,
                              units: str, now: datetime) -> None:
    """Downgrades any per-point sample of `values` outside [valid_min, valid_max].

    Shared by every per-depth-point variable's physical-plausibility check
    (TEMP, DEPTH_VALUES, SOUND_VELOCITY) -- see TEMP_VALID_MIN's module-level
    comment for why this exists and why fault_flags is deliberately left
    unset here.
    """
    out_of_range = (values < valid_min) | (values > valid_max)
    if not np.any(out_of_range):
        return
    qc_array[out_of_range] = GTSPP_PROBABLY_BAD
    bad_depths = qc.cast.depth_m[out_of_range]
    valid_bad_depths = bad_depths[~np.isnan(bad_depths)]
    qc.history.append(HistoryEntry(
        institution=_INSTITUTION, step=_QC_STEP, software=_SOFTWARE,
        software_release=_SOFTWARE_RELEASE, date=now, parameter=param_name,
        start_depth=float(np.min(valid_bad_depths)) if valid_bad_depths.size else 0.0,
        stop_depth=float(np.max(valid_bad_depths)) if valid_bad_depths.size else 0.0,
        qc_flag="RC",
        qc_flag_description=(
            f"{int(np.sum(out_of_range))} {param_name} sample(s) outside valid range "
            f"[{valid_min}, {valid_max}] {units}".rstrip()
        ),
    ))


def _flag_scalar_out_of_range(qc: CastQC, current_flag: int, value: float,
                               valid_min: float, valid_max: float, param_name: str,
                               units: str, now: datetime) -> int:
    """Returns a downgraded flag if `value` (a per-profile scalar) is out of range.

    Used for LATITUDE/LONGITUDE -- unlike the per-depth-point variables, these
    are single values per cast, so there's one flag to return, not an array to
    mutate in place.
    """
    if valid_min <= value <= valid_max:
        return current_flag
    qc.history.append(HistoryEntry(
        institution=_INSTITUTION, step=_QC_STEP, software=_SOFTWARE,
        software_release=_SOFTWARE_RELEASE, date=now, parameter=param_name,
        start_depth=float(qc.cast.depth_m[0]) if qc.cast.depth_m.size else 0.0,
        stop_depth=float(qc.cast.depth_m[-1]) if qc.cast.depth_m.size else 0.0,
        qc_flag="RC",
        qc_flag_description=(
            f"{param_name} value {value} outside valid range "
            f"[{valid_min}, {valid_max}] {units}".rstrip()
        ),
    ))
    return GTSPP_PROBABLY_BAD


def _remove_surface_spike(cast: Cast) -> None:
    """Blanks TEMP to NaN above SURFACE_SPIKE_DEPTH_M.

    See SURFACE_SPIKE_DEPTH_M's own comment for the cookbook citation and why
    only the Accept (CSA) case is implemented. Mutates cast.temperature_c in
    place, matching the cookbook's own action ("removed ... and replaced with
    99.99 to indicate no data") -- this reuses the same NaN-then-flag-missing
    convention CastQC.__post_init__ already applies to the EDF's -99 fault
    sentinel, rather than duplicating it: setting the value to NaN here is
    enough for that existing logic to flag it TEMP_quality_control=GTSPP_MISSING
    on its own once CastQC wraps this cast. Only TEMP is touched: DEPTH_VALUES
    comes from elapsed time and the probe's fall-rate model, not the
    thermistor, so it isn't affected by the same start-up transient.
    """
    shallow = cast.depth_m < SURFACE_SPIKE_DEPTH_M
    cast.temperature_c[shallow] = np.nan


def _flag_spikes(qc: CastQC, now: datetime) -> None:
    """Flags a real TEMP value with no real data on either immediate side.

    See the comment above this function's constants for the cookbook
    citation and why this only catches a completely isolated reading, not
    one that merely differs from real neighbours (that version was tried
    and dropped -- see the same comment). Applied to every cast, real or
    test-probe, matching where the physical-plausibility range check
    (_flag_array_out_of_range) already runs unconditionally.
    """
    temp = qc.cast.temperature_c
    n = temp.size
    if n < 3:
        return
    isolated = np.zeros(n, dtype=bool)
    for i in range(1, n - 1):
        if np.isnan(temp[i]):
            continue
        if np.isnan(temp[i - 1]) and np.isnan(temp[i + 1]):
            isolated[i] = True
    if not np.any(isolated):
        return

    qc.temperature_qc[isolated] = GTSPP_PROBABLY_BAD
    bad_depths = qc.cast.depth_m[isolated]
    valid_bad_depths = bad_depths[~np.isnan(bad_depths)]
    qc.history.append(HistoryEntry(
        institution=_INSTITUTION, step=_QC_STEP, software=_SOFTWARE,
        software_release=_SOFTWARE_RELEASE, date=now, parameter="TEMP",
        start_depth=float(np.min(valid_bad_depths)) if valid_bad_depths.size else 0.0,
        stop_depth=float(np.max(valid_bad_depths)) if valid_bad_depths.size else 0.0,
        qc_flag="SP",
        qc_flag_description=(
            f"{int(np.sum(isolated))} isolated TEMP reading(s) with no real data on either "
            "immediate side (CSIRO XBT QC Cookbook v1.1 sections 3.2/3.3)"
        ),
    ))


def apply_qc(casts: list) -> list:
    """Applies the 5 automated QC checks to a voyage's casts.

    Args:
        casts: every cast for one voyage (TestProbe casts included -- the
            stability check needs them), in any order.

    Returns:
        One CastQC per input cast, sorted into launch_time order.

    A cast can accumulate at most 10 history entries: the surface-spike
    check emits 1 (CS), the speed check 2 (PE and TE, which cannot be told
    apart automatically), the probe-type check 1, the isolated-spike check 1
    (SP), and the physical-plausibility range check up to 5 more (RC on
    TEMP, DEPTH, SOUND_VELOCITY, LATITUDE, LONGITUDE -- each independent, so
    worst case all 5 fire on the same cast). A test probe cast never reaches
    the surface-spike check (see _remove_surface_spike's docstring) or the
    first two, and emits at most 7 (TP + SP + up to 5 RC). Keep
    build_xbt_netcdf._N_HISTORY at or above that ceiling.
    """
    ordered = sorted(casts, key=lambda cast: cast.launch_time)
    now = datetime.utcnow()
    results = []
    previous_real_cast = None

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
                if speed_knots is not None and speed_knots > MAX_PLAUSIBLE_SPEED_KNOTS:
                    # The cookbook treats a failed speed check as evidence of
                    # EITHER a position error (PE, section 4.2.4) OR a time
                    # error (TE, section 4.2.5) -- two distinct codes with two
                    # distinct metadata targets (lat/lon vs date/time).
                    # Disambiguating them is an operator judgment call made
                    # against log sheets and a track plot (section 4.1 step 9);
                    # an automated check cannot make it. So both codes are
                    # emitted, each downgrading only its own metadata field,
                    # and the description says plainly that the check could not
                    # tell them apart.
                    #
                    # Both are Reject variants (no correction is applied), and
                    # Table 2 gives both PE Reject and TE Reject the same
                    # temperature/depth consequence: Temperature 3 from the
                    # surface, Depth 1 from the surface (i.e. depth unchanged,
                    # which is why depth_qc is deliberately untouched here).
                    description = (
                        f"Implausible {speed_knots:.1f} knot speed since previous cast -- "
                        "automated check cannot determine whether this is a position or "
                        "time error"
                    )
                    start_depth = float(cast.depth_m[0]) if cast.depth_m.size else 0.0
                    stop_depth = float(cast.depth_m[-1]) if cast.depth_m.size else 0.0

                    # Temperature 3 from the surface is required by both codes
                    # (Table 2, PE Reject and TE Reject rows) -- applied once,
                    # not once per entry, since the two entries describe the
                    # same triggering event on the same profile.
                    qc.temperature_qc[:] = GTSPP_PROBABLY_BAD
                    qc.temperature_qc[np.isnan(cast.temperature_c)] = GTSPP_MISSING

                    # Both fault bits are set, matching the "flag both, since
                    # the automated check can't tell them apart" reasoning
                    # applied above to the metadata flags and HISTORY_* entries
                    # -- a consumer reading the bitmask alone (without
                    # cross-referencing HISTORY_QC_FLAG) should see the same
                    # ambiguity, not just half of it.
                    qc.latitude_qc = GTSPP_PROBABLY_BAD
                    qc.longitude_qc = GTSPP_PROBABLY_BAD
                    qc.fault_flags[:] |= FAULT_POSITION_ERROR | FAULT_TIME_ERROR
                    qc.history.append(HistoryEntry(
                        institution=_INSTITUTION, step=_QC_STEP, software=_SOFTWARE,
                        software_release=_SOFTWARE_RELEASE, date=now,
                        parameter="LATITUDE,LONGITUDE",
                        start_depth=start_depth, stop_depth=stop_depth,
                        qc_flag="PE", qc_flag_description=f"Position error: {description}",
                    ))

                    qc.time_qc = GTSPP_PROBABLY_BAD
                    qc.history.append(HistoryEntry(
                        institution=_INSTITUTION, step=_QC_STEP, software=_SOFTWARE,
                        software_release=_SOFTWARE_RELEASE, date=now,
                        parameter="TIME",
                        start_depth=start_depth, stop_depth=stop_depth,
                        qc_flag="TE", qc_flag_description=f"Time error: {description}",
                    ))
            previous_real_cast = cast

            if cast.probe_type_raw not in KNOWN_PROBE_TYPES:
                # PR Reject (section 4.2.6/Table 2): metadata 3 to the original
                # probe type, Temperature 3 from the surface, Depth 3 from the
                # surface. Depth is downgraded too because an incorrect probe
                # type invalidates the fall-rate equation the depth axis is
                # derived from, not just the temperatures indexed against it
                # (section 3.1, citing Cheng et al 2016).
                qc.probe_type_qc = GTSPP_PROBABLY_BAD
                qc.temperature_qc[:] = GTSPP_PROBABLY_BAD
                qc.depth_qc[:] = GTSPP_PROBABLY_BAD
                qc.temperature_qc[np.isnan(cast.temperature_c)] = GTSPP_MISSING
                qc.depth_qc[np.isnan(cast.depth_m)] = GTSPP_MISSING
                qc.fault_flags[:] |= FAULT_PROBE_TYPE_ERROR
                qc.history.append(HistoryEntry(
                    institution=_INSTITUTION, step=_QC_STEP, software=_SOFTWARE,
                    software_release=_SOFTWARE_RELEASE, date=now, parameter="PROBE_TYPE",
                    start_depth=float(cast.depth_m[0]) if cast.depth_m.size else 0.0,
                    stop_depth=float(cast.depth_m[-1]) if cast.depth_m.size else 0.0,
                    qc_flag="PR",
                    qc_flag_description=f"Probe type not recognised: {cast.probe_type_raw!r}",
                ))
        else:
            qc.fault_flags[:] |= FAULT_TEST_PROBE
            if cast.temperature_c.size > 1:
                variation = float(np.nanmax(cast.temperature_c) - np.nanmin(cast.temperature_c))
                if variation >= TEST_PROBE_MAX_TEMPERATURE_VARIATION_C:
                    qc.temperature_qc[:] = GTSPP_PROBABLY_BAD
                    qc.history.append(HistoryEntry(
                        institution=_INSTITUTION, step=_QC_STEP, software=_SOFTWARE,
                        software_release=_SOFTWARE_RELEASE, date=now, parameter="TEMP",
                        start_depth=float(cast.depth_m[0]), stop_depth=float(cast.depth_m[-1]),
                        qc_flag="TP",
                        qc_flag_description=f"Test probe unstable: {variation:.4f} degC variation",
                    ))

        # Physical-plausibility checks, applied to every cast (real or
        # self-test) after the checks above: a value outside a variable's own
        # declared valid range can never be "good data", whatever else is
        # true about the cast. Per-point for the depth-indexed variables --
        # unlike the speed/probe-type checks, a resistance glitch mid-cast
        # (see module docstring on temperature_c) only corrupts the specific
        # samples it hit, not the whole profile. Not tied to a specific
        # XBT_fault_and_feature_flag_type bit: none of the Appendix F bits
        # named in this module map cleanly onto "value outside instrument
        # range" and guessing one risks repeating the exact mistake
        # FAULT_TEST_PROBE's wrong value was (see test_fault_bit_values_match_
        # appendix_f) -- fault_flags is deliberately left unset by all of these.
        _flag_array_out_of_range(qc, qc.temperature_qc, cast.temperature_c,
                                  TEMP_VALID_MIN, TEMP_VALID_MAX, "TEMP", "degC", now)
        _flag_spikes(qc, now)
        _flag_array_out_of_range(qc, qc.depth_qc, cast.depth_m,
                                  DEPTH_VALID_MIN, DEPTH_VALID_MAX, "DEPTH", "m", now)
        _flag_array_out_of_range(qc, qc.sound_velocity_qc, cast.sound_velocity_ms,
                                  SOUND_VELOCITY_VALID_MIN, SOUND_VELOCITY_VALID_MAX,
                                  "SOUND_VELOCITY", "m/s", now)
        qc.latitude_qc = _flag_scalar_out_of_range(
            qc, qc.latitude_qc, cast.latitude,
            LATITUDE_VALID_MIN, LATITUDE_VALID_MAX, "LATITUDE", "degrees_north", now)
        qc.longitude_qc = _flag_scalar_out_of_range(
            qc, qc.longitude_qc, cast.longitude,
            LONGITUDE_VALID_MIN, LONGITUDE_VALID_MAX, "LONGITUDE", "degrees_east", now)

        results.append(qc)

    return results


def warn_on_failed_test_probes(casts_qc: list, voyage_id: str = None) -> int:
    """Logs one warning per test-probe cast whose stability check failed.

    Test-probe casts are filtered out before the NetCDF is built, so without
    this their verdict would be computed and then silently discarded. The
    cookbook frames it as a recorder-health alarm, not a per-profile data
    flag: "Multiple failures in test probes can indicate poor earthing or
    other system errors that can manifest in large or subtle errors in
    profiles" (section 3.1, p.13). That is only actionable if somebody sees it.

    Args:
        casts_qc: the CastQC list from apply_qc, BEFORE test probes are
            filtered out.
        voyage_id: voyage to name in the warning; falls back to each cast's own
            voyage_id when not given (a multi-voyage backfill run).

    Returns:
        How many failed test probes were warned about.
    """
    failed = 0
    for qc in casts_qc:
        for entry in qc.history:
            if entry.qc_flag != "TP":
                continue
            failed += 1
            logger.warning(
                "XBT recorder health: test probe stability check FAILED on voyage %s "
                "at %s (%s) -- %s. Repeated failures can indicate poor earthing or "
                "other recording-system errors affecting real profiles "
                "(XBT Cookbook v2.1 section 3.1).",
                voyage_id or qc.cast.voyage_id,
                qc.cast.launch_time.isoformat(),
                qc.cast.source_file,
                entry.qc_flag_description,
            )
    return failed


def _speed_between_casts_knots(earlier: Cast, later: Cast):
    """Great-circle speed implied by two casts' positions and launch times, in knots.

    Returns:
        The speed in knots, or None if the two casts share the same launch_time
        (can't compute a rate).
    """
    elapsed_hours = (later.launch_time - earlier.launch_time).total_seconds() / 3600.0
    if elapsed_hours <= 0:
        return None
    distance_nm = _haversine_nm(earlier.latitude, earlier.longitude, later.latitude, later.longitude)
    return distance_nm / elapsed_hours


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in nautical miles."""
    phi1, phi2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlambda = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlambda / 2) ** 2
    return _EARTH_RADIUS_NM * 2 * np.arcsin(np.sqrt(a))
