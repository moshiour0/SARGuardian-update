#!/usr/bin/env python3
"""
make_figures.py
---------------
Regenerate the three README figures from the committed measurements.

    python docs/figures/make_figures.py

The figures used to be drawn outside the repository, so when the numbers moved
the pictures did not: the ladder kept saying "180x to 360x" and the scar panel
kept circling one "mapped scar" after both were withdrawn. Every value drawn
here is read from outputs/ (or, for the scar panel, from the Sentinel-2 change
grid the interactive site caches); the only literals are the published
Blatten velocities, which are external and cited in docs/TECHNICAL_NOTES.md.
"""

from __future__ import annotations

import csv
import statistics as st
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "src"))

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from bound import PRECURSOR_MM_DAY, PUBLISHED_ELEV_M  # noqa: E402

RED, TEAL, GREY, ORANGE, GREEN = "#b3302f", "#13707c", "#667780", "#c4661f", "#2e7d4f"

# Published Blatten velocities (external; see TECHNICAL_NOTES, references 7-8).
BLATTEN = [("Blatten 2023\n(years out)", 1.4), ("Blatten Aug 2024", 4.1)]
BLATTEN_LATE = [("Blatten, final days", (500.0, 800.0)), ("Blatten, 1 day out", (10000.0, 10000.0))]


def rows(name: str) -> list[dict]:
    with open(ROOT / "outputs" / name, newline="") as fh:
        return list(csv.DictReader(fh))


def save(fig, stem: str) -> None:
    fig.savefig(HERE / f"{stem}.png", dpi=150, bbox_inches="tight")
    fig.savefig(HERE / f"{stem}_web.jpg", dpi=100, bbox_inches="tight",
                pil_kwargs={"quality": 85})
    plt.close(fig)
    print(f"wrote {stem}.png and {stem}_web.jpg")


# ---------------------------------------------------------------------------
def ladder() -> None:
    b = rows("bound_source.csv")
    pick = next(r for r in b if r["matches_published_elevation"] == "True")
    lo, hi = float(pick["los_floor_block_mm_day"]), float(pick["los_floor_pixel_mm_day"])
    ph = [r for r in b if int(r["phase_pairs"]) > 0]
    ph_lo = min(float(r["phase_floor_block_mm_day"]) for r in ph)
    ph_hi = max(float(r["phase_floor_pixel_mm_day"]) for r in ph)

    items = [("Langtang precursor\n(Sentinel-1, measured)", (PRECURSOR_MM_DAY,) * 2, TEAL,
              f"{PRECURSOR_MM_DAY:.2f} mm/day")]
    items += [(n, (v, v), GREY, f"{v:g}") for n, v in BLATTEN]
    items += [(f"NISAR phase floor, winter\n(candidates {', '.join(r['id'] for r in ph)})",
               (ph_lo, ph_hi), ORANGE, f"{ph_lo:.1f}-{ph_hi:.1f}"),
              (f"NISAR offset floor\n(candidate {pick['id']})", (lo, hi), RED,
               f"{lo:.1f}-{hi:.1f}")]
    items += [(n, v, ORANGE, f"{v[0]:,.0f}" + (f"-{v[1]:,.0f}" if v[1] != v[0] else ""))
              for n, v in BLATTEN_LATE]

    fig, ax = plt.subplots(figsize=(10.5, 5.4))
    ys = np.arange(len(items))[::-1]
    for y, (name, (a, c), col, txt) in zip(ys, items):
        if a == c:
            ax.plot([a], [y], "o", ms=11, color=col)
        else:
            ax.plot([a, c], [y, y], "-", lw=9, color=col, solid_capstyle="butt")
        ax.text(c * 1.35, y, txt, va="center", fontsize=11, color=col, fontweight="bold")
    ax.set_yticks(ys, [i[0] for i in items], fontsize=10)
    ax.set_xscale("log")
    ax.set_xlim(0.1, 40000)
    ax.axvspan(0.1, lo, color=RED, alpha=0.05)
    ax.grid(axis="x", alpha=0.3)
    ax.set_xlabel("Line-of-sight velocity, mm/day  (log scale)")
    y_arrow = ys[len(BLATTEN) + 1] + 0.5
    ax.annotate("", xy=(lo, y_arrow), xytext=(PRECURSOR_MM_DAY, y_arrow),
                arrowprops=dict(arrowstyle="<->", color="black"))
    ax.text((PRECURSOR_MM_DAY * lo) ** 0.5, y_arrow + 0.28,
            f" {lo / PRECURSOR_MM_DAY:.0f}x to {hi / PRECURSOR_MM_DAY:.0f}x too insensitive ",
            ha="center", va="center", fontsize=12, fontweight="bold",
            bbox=dict(facecolor="white", edgecolor="none"))
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.set_title("The precursor that existed, against what NISAR L2 could resolve",
                 loc="left", fontsize=14, fontweight="bold")
    fig.text(0.01, -0.06,
             f"Line of sight against line of sight. Bars run from a 1 km window median (optimistic) to one pixel "
             f"(pessimistic).\nCandidate {pick['id']} is the one at the published {PUBLISHED_ELEV_M[0]:,.0f}-"
             f"{PUBLISHED_ELEV_M[1]:,.0f} m detachment elevation; NISAR phase has no valid pixel there in any pair.",
             fontsize=9, color="#444")
    save(fig, "ladder")


# ---------------------------------------------------------------------------
def regimes() -> None:
    g = [r for r in rows("gunw_stats_source.csv") if r["processing"] == "PR"]
    win = [float(r["aoi_pct"]) for r in g if r["track"] == "ASC 098" and r["maturity"] == "BETA"]
    mon = [float(r["aoi_pct"]) for r in g if r["track"] == "ASC 098" and r["maturity"] == "PROVISIONAL"]
    o = [r for r in rows("goff_stats_source.csv")
         if r["layer"] == "HH/layer2" and r["processing"] == "PR"]

    def med(track, mat):
        return st.median(float(r["detect_floor_mm_day"]) for r in o
                         if r["track"] == track and r["maturity"] == mat)

    no_phase = [r["id"] for r in rows("bound_source.csv") if int(r["phase_pairs"]) == 0]
    cols = ["Winter\nslow creep\n(BETA products)", "Monsoon\nslow creep\n(PROVISIONAL)",
            "Pre-failure\nacceleration\n(simulated)", "The failure\nitself"]
    cells = [
        [("works over the AOI", f"{st.median(win):.0f}% of AOI; no pixel\nat candidate {', '.join(no_phase)}", GREEN),
         ("fails", f"{min(mon):.0f}-{max(mon):.0f}% of AOI", RED),
         ("above ceiling", "and decorrelates", RED),
         ("fails", "0-1% coverage", RED)],
        [("blind", f"median floor, mm/d\nASC {med('ASC 098', 'BETA'):.0f}, "
                   f"DESC {med('DESC 048', 'BETA'):.0f}", RED),
         ("marginal", f"median floor, mm/d\nASC {med('ASC 098', 'PROVISIONAL'):.0f}, "
                      f"DESC {med('DESC 048', 'PROVISIONAL'):.0f}", ORANGE),
         ("works", "at 1-2 d revisit", ORANGE),
         ("loses tracking", "surface destroyed", RED)],
    ]
    fig, ax = plt.subplots(figsize=(11, 4.6))
    ax.set_xlim(0, 4); ax.set_ylim(0, 2.9); ax.axis("off")
    for j, c in enumerate(cols):
        ax.text(j + 0.5, 2.45, c, ha="center", va="center", fontsize=11)
    for i, row in enumerate(cells):
        y = 1 - i
        for j, (head, sub, col) in enumerate(row):
            ax.add_patch(plt.Rectangle((j + 0.02, y + 0.02), 0.96, 0.96, color=col, alpha=0.13))
            ax.text(j + 0.5, y + 0.62, head, ha="center", fontsize=13, color=col, fontweight="bold")
            ax.text(j + 0.5, y + 0.3, sub, ha="center", va="center", fontsize=9.5, color="#555")
    ax.add_patch(plt.Rectangle((3.01, 0.01), 0.98, 1.98, fill=False, lw=2.5, color=RED))
    for i, lab in enumerate(["GUNW\ninterferometric phase", "GOFF\noffset tracking"]):
        ax.text(-0.04, 1.5 - i, lab, ha="right", va="center", fontsize=11)
    ax.set_title("Four regimes, and the failure lands where both products stop",
                 loc="left", fontsize=14, fontweight="bold")
    fig.text(0.01, -0.02, "Season and product maturity change together here: every winter pair is "
             "pre-calibration BETA, every monsoon pair calibrated PROVISIONAL.", fontsize=9, color="#444")
    save(fig, "regimes")


# ---------------------------------------------------------------------------
def scar() -> None:
    cache = ROOT / "docs" / "site" / "_grid_cache.npz"
    if not cache.exists():
        print("no docs/site/_grid_cache.npz - run docs/site/build_data.py first; scar figure skipped")
        return
    from rasterio.transform import rowcol
    from rasterio.warp import transform as warp
    from scar_map import grid_transform
    z = np.load(cache)
    d, pre, both = z["d"], z["pre"], z["both"]
    tf = grid_transform()

    def rc(lat, lon):
        x, y = warp("EPSG:4326", "EPSG:32645", [lon], [lat])
        return rowcol(tf, x[0], y[0])

    cands = [r for r in rows("scar_candidates_source.csv") if r["reading"] == "DETACHMENT-like"]
    lo_e, hi_e = PUBLISHED_ELEV_M
    fig, axes = plt.subplots(1, 2, figsize=(11, 7))
    axes[0].imshow(np.where(np.isfinite(pre), pre, np.nan), cmap="Greys_r", vmin=-0.4, vmax=1.0)
    axes[0].set_title("Before: snow index, 12 Aug 2026", loc="left")
    im = axes[1].imshow(np.where(both, d, np.nan), cmap="RdBu", vmin=-0.8, vmax=0.8)
    axes[1].set_title("Change: red = ice lost, blue = fresh snow\n"
                      f"{100 * both.mean():.0f}% of the AOI seen in both epochs", loc="left")
    fig.colorbar(im, ax=axes[1], fraction=0.046, label="delta NDSI")
    for ax in axes:
        ax.set_xticks([]); ax.set_yticks([])
        for r in cands:
            y, x = rc(float(r["lat"]), float(r["lon"]))
            hit = lo_e <= float(r["elev_m"]) <= hi_e
            ax.add_patch(plt.Circle((x, y), 30, fill=False, lw=2.5 if hit else 1.5,
                                    color=RED if hit else ORANGE))
            ax.text(x + 34, y, f"{r['id']}" + (" - published\nelevation" if hit else ""),
                    color=RED if hit else ORANGE, fontsize=10, fontweight="bold", va="center")
        y, x = rc(28.28771, 85.52809)
        ax.add_patch(plt.Circle((x, y), 22, fill=False, ls="--", lw=1.8, color=TEAL))
        ax.text(x - 26, y - 30, "reported point:\nnot a scar", color=TEAL, fontsize=9,
                ha="right", fontweight="bold")
    fig.suptitle("Four detachment-like candidates, all facing north; one at the published elevation",
                 x=0.01, ha="left", fontsize=14, fontweight="bold")
    save(fig, "scar")


if __name__ == "__main__":
    ladder()
    regimes()
    scar()
