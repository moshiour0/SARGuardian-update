"""
organise.py
-----------
Sort a flat pile of NISAR L2 downloads into a structure a team can share, and
write a manifest so anyone can see what is present without opening 20 GB.

NISAR granule names encode everything needed:

    NISAR_L2_PR_GUNW_006_048_D_074_007_2000_SH_20251125T125813_..._20251207T...
              |   |    |   |  |            |   reference date   secondary date
              |   |    |   |  |            polarisation
              |   |    |   |  frame
              |   |    |   orbit direction
              |   |    track/path
              |   product type
              processing type: PR = routine, UR = urgent response

Layout produced:

    data/nisar_l2/
        GUNW/2025-11_winter/...
        GOFF/2026-07_summer/...
        GOFF/co_event/...          anything whose secondary date is after the event
        MANIFEST.csv

Moves by default (these files are 1-2 GB; copying wastes disk). Dry-run unless
--apply is given.

Usage
-----
    python src/organise.py                 # find the files wherever they are
    python src/organise.py --apply         # then actually move them
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import ROOT, NISAR, find_products, resolve, describe_layout  # noqa: E402

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("organise")

EVENT_DATE = date(2026, 8, 26)
DEST_DEFAULT = NISAR

NAME = re.compile(
    r"NISAR_L2_(?P<proc>[A-Z]{2})_(?P<product>[A-Z]{4})_"
    r"(?P<cycle>\d+)_(?P<path>\d+)_(?P<direction>[AD])_(?P<frame>\d+)_"
    r"(?P<sub>\d+)_(?P<bw>\d+)_(?P<pol>[A-Z]{2})_"
)


def parse(path: Path) -> dict | None:
    m = NAME.match(path.name)
    if not m:
        return None
    stamps = re.findall(r"_(\d{8})T\d{6}", path.name)
    if len(stamps) < 4:
        return None
    ref = datetime.strptime(stamps[0], "%Y%m%d").date()
    sec = datetime.strptime(stamps[2], "%Y%m%d").date()
    d = m.groupdict()
    return {
        "file": path.name,
        "product": d["product"],
        "processing": "urgent" if d["proc"] == "UR" else "routine",
        "path": int(d["path"]),
        "direction": d["direction"],
        "polarisation": d["pol"],
        "reference": ref,
        "secondary": sec,
        "span_days": (sec - ref).days,
        "days_before_event": (EVENT_DATE - sec).days,
        "size_mb": round(path.stat().st_size / 1024 / 1024),
        "source": path,
    }


def bucket(rec: dict) -> str:
    """Which subfolder this pair belongs in."""
    if rec["secondary"] > EVENT_DATE:
        return "co_event"
    month = rec["reference"].month
    season = "winter" if month in (11, 12, 1, 2, 3) else "summer"
    return f"{rec['reference']:%Y-%m}_{season}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Organise NISAR L2 downloads")
    ap.add_argument("--src", help="folder holding the .h5 files. "
                                  "Omit to search the whole repository.")
    ap.add_argument("--dest", default=str(DEST_DEFAULT))
    ap.add_argument("--apply", action="store_true", help="actually move (default: dry run)")
    ap.add_argument("--copy", action="store_true", help="copy instead of move")
    args = ap.parse_args()

    dest = resolve(args.dest)

    if args.src:
        src = resolve(args.src)
        if not src.is_dir():
            logger.error("No such folder: %s", src)
            print()
            print(describe_layout())
            return 1
        # Recursive: files often sit in per-date subfolders, and a
        # non-recursive glob silently finds nothing.
        files = sorted(src.rglob("*.h5"))
        if not files:
            logger.error("No .h5 files under %s", src)
            print()
            print(describe_layout())
            return 1
    else:
        logger.info("No --src given; searching the repository for NISAR products...")
        files = find_products()
        if not files:
            logger.error("No .h5 files found anywhere under %s", ROOT)
            print()
            print(describe_layout())
            print()
            print("Put the downloads anywhere under the repository and re-run,")
            print("or pass --src <folder>.")
            return 1
        src = None
        logger.info("Found %d file(s)", len(files))

    # Skip only what is ALREADY filed in a product/season bucket. The staging
    # folder data/nisar_l2/_incoming/ lives under dest too, and excluding
    # everything below dest silently threw away every fresh download - which is
    # precisely where downloads land.
    dest_resolved = dest.resolve()
    incoming_resolved = (dest / "_incoming").resolve()

    def already_filed(f: Path) -> bool:
        parents = f.resolve().parents
        return dest_resolved in parents and incoming_resolved not in parents

    n_filed = sum(1 for f in files if already_filed(f))
    files = [f for f in files if not already_filed(f)]

    recs, skipped = [], []
    for fp in files:
        r = parse(fp)
        (recs if r else skipped).append(r or fp.name)

    for s in skipped:
        logger.warning("Unparsed, left in place: %s", s)

    if not recs:
        if n_filed and not files:
            # Everything is already in a product/season bucket. Normal state,
            # not a failure - re-running organise.py must always be safe.
            logger.info("Nothing to do: all %d product(s) are already filed.", n_filed)
            return 0
        logger.error("Found %d .h5 file(s) but none are NISAR L2 granules.", len(files))
        print()
        print("Expected names like:")
        print("  NISAR_L2_PR_GUNW_026_048_D_074_028_4000_SH_20260723T...h5")
        print()
        print("Downloads belong in data/nisar_l2/_incoming/ (any subfolder is fine).")
        print(describe_layout())
        return 1

    recs.sort(key=lambda r: (r["product"], r["reference"], r["path"]))
    total_gb = sum(r["size_mb"] for r in recs) / 1024

    print(f"\n{len(recs)} products, {total_gb:.1f} GB\n")
    print(f"{'PRODUCT':<8}{'PROC':<9}{'DIR':>4}{'PATH':>6}{'POL':>5}  "
          f"{'REFERENCE':<12}{'SECONDARY':<12}{'SPAN':>6}{'PRE-EV':>8}{'MB':>7}  -> BUCKET")
    print("-" * 104)
    for r in recs:
        b = bucket(r)
        flag = "  <== SPANS THE COLLAPSE" if b == "co_event" else ""
        print(f"{r['product']:<8}{r['processing']:<9}{r['direction']:>4}{r['path']:>6}"
              f"{r['polarisation']:>5}  {str(r['reference']):<12}{str(r['secondary']):<12}"
              f"{r['span_days']:>4} d{r['days_before_event']:>6} d{r['size_mb']:>7}"
              f"  -> {r['product']}/{b}{flag}")

    # summary by product and bucket
    from collections import Counter
    print()
    for prod in sorted({r["product"] for r in recs}):
        sub = [r for r in recs if r["product"] == prod]
        gb = sum(r["size_mb"] for r in sub) / 1024
        buckets = Counter(bucket(r) for r in sub)
        print(f"  {prod}: {len(sub)} files, {gb:.1f} GB  "
              + ", ".join(f"{k}={v}" for k, v in sorted(buckets.items())))

    if not args.apply:
        print(f"\nDRY RUN. Nothing moved. Re-run with --apply to {'copy' if args.copy else 'move'}.")
        return 0

    dest.mkdir(parents=True, exist_ok=True)
    moved = 0
    for r in recs:
        target_dir = dest / r["product"] / bucket(r)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / r["file"]
        if target.exists():
            logger.info("Already there: %s", r["file"][:60])
            continue
        if args.copy:
            shutil.copy2(r["source"], target)
        else:
            shutil.move(str(r["source"]), str(target))
        moved += 1
        logger.info("%s -> %s", r["file"][:52], target_dir)

    manifest = dest / "MANIFEST.csv"
    keys = ["product", "processing", "direction", "path", "polarisation",
            "reference", "secondary", "span_days", "days_before_event",
            "size_mb", "bucket", "file"]
    with open(manifest, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in recs:
            w.writerow({**r, "bucket": bucket(r),
                        "reference": str(r["reference"]), "secondary": str(r["secondary"])})
    logger.info("Moved %d files. Manifest: %s", moved, manifest)

    if src is not None and src.exists() and src.is_dir():
        leftovers = [f for f in src.rglob("*") if f.is_file()]
        if not leftovers:
            logger.info("Source folder is now empty - safe to delete: %s", src)
    return 0


if __name__ == "__main__":
    sys.exit(main())
