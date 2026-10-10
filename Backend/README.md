# SARGuardian

**Could NASA's NISAR satellite have warned before the Langtang Lirung collapse?**
We measured the answer instead of assuming it. NISAR's offset-tracking product
was roughly **30 to 100 times too insensitive** to see the precursor that was
there, and at the most likely detachment its interferometric phase recorded
nothing at all.

### ▶ [Run the analysis yourself](https://moshiour0.github.io/SARGuardian/)

Eight stations in the browser, computing on the real measurements. No
install, no credentials.

![The precursor that existed, against what NISAR L2 could resolve](docs/figures/ladder.png)

On 26 August 2026 a rock and ice face on the north side of Langtang Lirung,
Nepal, detached, fell about 1,200 m, dammed the Lhende Khola and burst. More
than 1,300 people died. NISAR had imaged the slope every 12 days on two
tracks. We had sixteen of its Level-2 offset products over that ground.

## The result

A precursor existed: Sentinel-1 interferometry measured the slope creeping at
about **0.33 mm/day** from January to August 2026, accelerating at the end
(Shirzaei, Virginia Tech, as reported). Against it, line of sight against line
of sight, at the Sentinel-2 candidate scar that matches the published
detachment elevation:

| | mm/day | vs the precursor |
|---|---|---|
| Precursor, Sentinel-1 | **0.33** | - |
| NISAR offset floor, 1 km window median | 9.2 | **28x** |
| NISAR offset floor, one pixel | 32.0 | **96x** |
| NISAR phase floor at that candidate | no valid pixel in any pair | - |
| NISAR phase floor at the two other candidates with phase, winter | 4.1-13.9 | 12x-42x |

The bracket is the estimator: a 1 km average is the optimistic end, one pixel
the pessimistic end. The three other detachment-like candidates give the same
answer where they are observed at all (`python src/bound.py`).

**What it means.** Offset tracking at a 12-day repeat cannot see creep of this
kind. Interferometric phase is the right measurement, and Sentinel-1 made it -
but NISAR's own interferograms have no valid pixel on the steep north face that
most likely failed. The requirement this names is phase that survives on that
terrain, stacked over many passes, plus shorter revisit. It is a specification
for the next system, not a warning system.

## What is measured

- **Where it failed.** Sentinel-2 snow-loss mapping leaves four detachment-like
  candidates, all facing within 13 degrees of north. Only one sits at the
  published 5,200-5,400 m. The coordinate the reports gave is observed and is
  not a scar.
- **What the radar could see.** Detection floors per pair, per track, at every
  candidate, per pixel and for a 1 km window median, with bootstrap intervals.
  The ascending track is 5x quieter than the descending one over this terrain.
- **Did anything move.** An inverse-velocity (Fukuzono) detector gated on each
  pair's own floor, with every interval touching the event excluded. No alarm
  in any block. On a common datum the fastest pre-event interval at the
  published-elevation candidate is 5.2 mm/day, under both floors.
- **The collapse itself.** Both tracks record their largest decorrelation of
  the archive in the pair spanning 26 August: 35x and 14x the event-free
  baseline, against a random-loss null and a second processing chain.
- **What revisit a warning needs.** A simulation calibrated on Blatten (2025):
  detection collapses once revisit exceeds about precursor/2.5, and short
  revisit buys date accuracy at the cost of lead time.

Every number, method and correction is in
**[docs/TECHNICAL_NOTES.md](docs/TECHNICAL_NOTES.md)**.

## Read this before quoting anything

- **Two product maturities.** Every winter pair is NISAR's pre-calibration
  **BETA** release; every summer pair is calibrated **PROVISIONAL**. A
  winter-against-monsoon comparison is also a BETA-against-PROVISIONAL one.
- **A release gap.** No NISAR product was public for acquisitions from 20
  January to 17 June 2026 - the months the creep was building. That is the
  archive, not the physics. Re-run when the validated reprocessing lands.
- **The precursor's geometry is not reported.** It is treated as line of sight.
  If it is downslope, compare it with the downslope bound (56-67 mm/day at the
  observed candidates) and the gap roughly doubles.
- **The scar is inferred.** The reported collapse volume is larger than any
  mapped candidate can hold, and half the area is never cloud-free afterwards.
- **No ground truth** for the deformation field: no GNSS, no field survey.

## Try it

```bash
git clone https://github.com/moshiour0/SARGuardian.git && cd SARGuardian
pip install -r requirements.txt
python demo.py                 # the argument in 30 s, computed from outputs/
python src/bound.py            # the headline table, per candidate
python -m pytest tests/ -q     # the test suite
python tests/mutate.py         # every historical bug put back; each must be caught
```

No credentials and no downloads for any of these: every derived measurement
is committed under `outputs/`. The 51 GB of NISAR products are not; see
[REPRODUCE_RESULTS.md](REPRODUCE_RESULTS.md) for the granule list and the
commands that rebuild every number from them.

## The pipeline

| Step | Tool | Needs |
|---|---|---|
| Find and download NISAR L2 | `src/nisar_acquisition.py`, `src/organise.py` | Earthdata login |
| Read phase and offsets | `src/gunw_reader.py`, `src/goff_reader.py` | products |
| Floors at a point, per pixel and window median | `src/local_floor.py` | exports |
| One datum for a stack | `src/common_ref.py` | exports |
| Time series and forecast | `src/timeseries.py`, `src/inverse_velocity.py` | stats CSVs |
| Which tracks can see a slope | `src/geometry_merge.py` | DEM (online) |
| Scar candidates from Sentinel-2 | `src/scar_map.py` | nothing (public STAC) |
| The headline, like for like | `src/bound.py` | committed CSVs |
| Revisit requirement | `src/detectability.py` | nothing |
| Landslide-dam susceptibility | `src/impoundment.py` | DEM (online) |

Setup, credentials, every command-line option and the six traps that bit this
project are in the [technical notes](docs/TECHNICAL_NOTES.md#setup).

## Known limitations

The full list, nineteen items, is in the
[technical notes](docs/TECHNICAL_NOTES.md#known-limitations). The ones that
change how the result reads: the final seven days before failure are
unobserved; every NISAR L2 time series is a chain with zero redundancy; the
stratified troposphere is measured but not removed from phase; and there is no
independent ground validation.

## Provenance

This repository is independent, open-source prior work (MIT), built in August
and September 2026. It is not a NASA product. Data: NASA/JPL NISAR L2 via the
Alaska Satellite Facility, Copernicus Sentinel-2 via Element84 Earth Search,
SRTM via OpenTopoData. The precursor rate is another team's measurement and
is cited as such.

## References

Voight (1989), *Science* 243:200 · Fukuzono (1985), *J. Japan Landslide Soc.*
22(2):8 · Itoh (1982), *Appl. Opt.* 21:2470 · Berardino et al. (2002), *IEEE
TGRS* 40:2375 · NISAR GUNW and GOFF product specifications (JPL D-102272,
D-105010) · ESA/MODULATE, Blatten L-band analysis (2025) · Shirzaei, Sentinel-1
analysis of Langtang Lirung (reported September 2026). Full list with links in
the [technical notes](docs/TECHNICAL_NOTES.md#references).

Cite this work with [CITATION.cff](CITATION.cff).

## Optional public-web metadata preflight API

The separate `api_service/` directory adds a lightweight FastAPI endpoint for a
web UI to query the ASF catalogue **without downloading granules**. It accepts
one GUNW or GOFF product family, a bounded AOI and a date window of up to 90 days,
then returns the available metadata and a full-product-size estimate. See
[`api_service/README.md`](api_service/README.md).

This API intentionally does **not** call the research scripts, download HDF5,
or calculate movement. The research/reproduction workflow above remains a local
scientific analysis pipeline. Production live processing needs a separate
asynchronous Harmony extraction + bounded raster processing/job-queue stage;
see [`docs/architecture/FREE_ON_DEMAND_PIPELINE_BN.md`](docs/architecture/FREE_ON_DEMAND_PIPELINE_BN.md).
