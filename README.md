# xbt-edf-qc

Parses Sippican/Lockheed Martin **MK21** XBT EDF cast files, applies
automated QC checks from the CSIRO **Quality Control Cookbook for XBT Data**
(both the 1994 v1.1 edition and the 2022 v2.1 edition), and builds a
**CF-1.6 / IMOS-1.4** compliant profile NetCDF.

Extracted from the Australian Antarctic Data Centre's RSV *Nuyina* XBT
pipeline, where it runs daily and against the ship's full historical
archive. Two modules, no framework, no ship-specific assumptions:

- **`parse_xbt_edf.py`** — parses one EDF file into a `Cast`, and
  `apply_qc()` runs the automated checks over a voyage's casts, returning a
  `CastQC` per cast (GTSPP-style per-point/per-profile flags, a
  `HISTORY_*`-style audit trail, and an `XBT_fault_and_feature_flag_type`
  bitmask).
- **`build_xbt_netcdf.py`** — turns a list of `CastQC` into an
  `xarray.Dataset` ready for `.to_netcdf()`, with all the IMOS ancillary
  quality-control variables, ready-to-cite `valid_min`/`valid_max`
  attributes, and a full `HISTORY_*` audit trail per cast.

See **[QC_COOKBOOK.md](QC_COOKBOOK.md)** for exactly which checks are
implemented, why, with what threshold, and — just as importantly — which
checks were tried, found to misfire on real data, and deliberately left out.
That document is itself a joint human+AI-assisted write-up from the session
that built these checks; read it before extending or reimplementing any of
this against different data.

## Install

```bash
pip install git+https://github.com/botheredbybees/xbt-edf-qc.git
```

or clone and `pip install -e .` for local development.

## Use

```python
import glob
from parse_xbt_edf import parse_edf, apply_qc, is_test_probe_cast, warn_on_failed_test_probes
from build_xbt_netcdf import build_xbt_netcdf

casts = [parse_edf(path, voyage_id="my_voyage") for path in glob.glob("*.edf")]
casts_qc = apply_qc(casts)
warn_on_failed_test_probes(casts_qc)  # logs a recorder-health warning for any failed self-test
real_casts_qc = [qc for qc in casts_qc if not is_test_probe_cast(qc.cast)]

dataset = build_xbt_netcdf(real_casts_qc, voyage="my_voyage", global_attrs_overrides={
    "institution": "Your Institution",
    "ship": "R/V Your Ship",
    "data_centre": "Your Data Centre",
    "data_centre_email": "data@your-institution.example",
    "author": "Your Data Officer",
})
dataset.to_netcdf("my_voyage_xbt_profiles.nc")
```

`global_attrs_overrides` is optional — omitted, the file describes AADC's
own RSV *Nuyina* deployment (see `DEFAULT_GLOBAL_ATTRS` in
`build_xbt_netcdf.py`). The `references` attribute always points back at
this repo by default, regardless of institution, since it names the QC
code that actually ran — pass your own value there too if you fork or wrap
this.

## What this deliberately doesn't do

This is the EDF-parsing/QC/NetCDF-building core only. It has no opinion on:

- where your raw EDF files live, or how you find "this voyage's casts"
- what triggers a rebuild (cron, a file-watcher, a button)
- metadata-record submission to your data centre's catalogue

Those are all genuinely institution-specific — AADC's own versions of them
(reading `/mnt/data/VoyageData`, resolving the current voyage from OpenVDM,
building an AADC GCMD DIF XML record) stay in AADC's own `cron_jobs_on_skippy`
repo, which imports this package rather than vendoring a copy of it.

## Tests

```bash
pip install -r requirements.txt
pytest tests/
```

Includes real EDF fixture files and, notably, tests that check a *rejected*
design (the ones documented as false-positive-prone in QC_COOKBOOK.md) — see
the git history if you want to see what didn't make the cut and why.

## License

MIT — see [LICENSE](LICENSE). The CSIRO cookbooks this code implements are
separate copyrighted documents, cited but not included here.
