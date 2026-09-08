"""Tests for parse_xbt_edf.py EDF parsing."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parse_xbt_edf import Cast, EDFParseError, parse_edf

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "xbt"


def test_parses_real_cast_header_fields():
    cast = parse_edf(str(FIXTURES / "real_cast.edf"), voyage_id="202425030")
    assert isinstance(cast, Cast)
    assert cast.probe_type_raw == "DB"
    assert cast.serial_number == "1396178"
    assert cast.voyage_id == "202425030"
    assert cast.launch_time.isoformat() == "2025-03-01T20:34:16"


def test_parses_nmea_style_latitude_and_longitude():
    cast = parse_edf(str(FIXTURES / "real_cast.edf"), voyage_id="202425030")
    # 45 19.56440 S -> -(45 + 19.56440/60)
    assert cast.latitude == pytest.approx(-(45 + 19.56440 / 60), abs=1e-6)
    # 144 46.65540 E -> +(144 + 46.65540/60)
    assert cast.longitude == pytest.approx(144 + 46.65540 / 60, abs=1e-6)


def test_parses_data_table():
    cast = parse_edf(str(FIXTURES / "real_cast.edf"), voyage_id="202425030")
    assert cast.depth_m.shape == (6,)
    assert cast.depth_m[0] == pytest.approx(0.0)
    assert cast.depth_m[-1] == pytest.approx(3.2355)
    assert cast.temperature_c[0] == pytest.approx(24.90)


def test_minus_99_sound_velocity_sentinel_becomes_nan():
    cast = parse_edf(str(FIXTURES / "real_cast.edf"), voyage_id="202425030")
    assert np.isnan(cast.sound_velocity_ms[0])
    assert np.isnan(cast.sound_velocity_ms[2])
    assert cast.sound_velocity_ms[3] == pytest.approx(1501.70)


def test_minus_99_temperature_sentinel_becomes_nan():
    # Found 2026-09-08: a resistance/connector glitch mid-cast writes a
    # literal -99 into Temperature too, not just Sound Velocity -- this
    # published as a real -99.0 degC "good data" point in the historical
    # backfill before this fix. Real rows adapted from voyage 202324VT1A's
    # actual archive.
    cast = parse_edf(str(FIXTURES / "real_cast_temp_glitch.edf"), voyage_id="202425030")
    assert cast.temperature_c[0] == pytest.approx(13.56)  # unaffected row, before the glitch
    assert np.isnan(cast.temperature_c[3])  # -99 row
    assert np.isnan(cast.temperature_c[4])  # -99 row
    assert cast.temperature_c[5] == pytest.approx(13.48)  # recovers after the glitch
    # Sound Velocity's own -99 handling must be untouched by this fix.
    assert np.isnan(cast.sound_velocity_ms[2])
    assert np.isnan(cast.sound_velocity_ms[3])


def test_parses_memo_section():
    cast = parse_edf(str(FIXTURES / "real_cast.edf"), voyage_id="202425030")
    assert cast.memo == "DeepBlue XBT on 202425030 DOM 240227"


def test_parses_single_line_memo():
    cast = parse_edf(str(FIXTURES / "test_probe_serial_zero.edf"), voyage_id="202324VT1A")
    assert cast.memo == "Test probe"


def test_missing_data_marker_raises_edf_parse_error():
    with pytest.raises(EDFParseError):
        parse_edf(str(FIXTURES / "corrupt_cast.edf"), voyage_id="202425030")


def test_unreadable_file_raises_edf_parse_error(tmp_path):
    missing = tmp_path / "does_not_exist.edf"
    with pytest.raises(EDFParseError):
        parse_edf(str(missing), voyage_id="202425030")


def test_parses_test_probe_cast_without_special_casing():
    cast = parse_edf(str(FIXTURES / "test_probe_cast.edf"), voyage_id="202425030")
    assert cast.probe_type_raw == "TestProbe"
