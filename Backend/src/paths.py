"""
paths.py
--------
One place that decides where things live, so no script depends on the
directory you happen to be standing in.

The problem this solves
=======================
Every default like Path("data/nisar_l2") resolves against the CURRENT working
directory. Run a script from the repository root and it means <repo>/data; run
the same script from src/ and it silently means <repo>/src/data, which does not
exist. Downloads then land in the wrong place, batch jobs find nothing, and the
error says "no files" rather than "you are in the wrong folder".

Everything here is anchored to the repository root instead - the parent of the
directory holding this file - so the same command works from anywhere.

    python src/organise.py            # from the repo root
    cd src && python organise.py      # identical behaviour

Finding the data
================
People put downloads wherever the browser or the download tool left them.
find_products() searches the sensible places rather than demanding an exact
path, so a teammate who does not know where their files went can still run the
pipeline.
"""

from __future__ import annotations

from pathlib import Path

# <repo>/src/paths.py -> <repo>
ROOT = Path(__file__).resolve().parent.parent

DATA = ROOT / "data"
NISAR = DATA / "nisar_l2"
INCOMING = NISAR / "_incoming"
OUTPUTS = ROOT / "outputs"
DEM = DATA / "dem"

# Places downloads plausibly end up, in the order we should trust them.
SEARCH_ROOTS = [
    NISAR,
    INCOMING,
    DATA,
    ROOT / "src",          # the old layout, before organise.py existed
    ROOT,
]

# Directories that must never be walked. A virtualenv inside the repository is
# the dangerous one: h5py ships test fixtures called things like
# vlen_string_dset.h5 and compound-dtype-complex.h5, and a naive recursive
# search happily "finds" them as NISAR products.
EXCLUDE_DIRS = {
    ".venv", "venv", "env", ".env",
    "site-packages", "dist-packages", "node_modules",
    ".git", "__pycache__", ".ipynb_checkpoints", ".tox", ".mypy_cache",
}

# A real NISAR L2 granule. Anything not matching this is not our data.
NISAR_NAME = "NISAR_"


def _is_excluded(path: Path) -> bool:
    return any(part in EXCLUDE_DIRS for part in path.parts)


def resolve(p: str | Path) -> Path:
    """
    Interpret a user-supplied path sensibly.

    Absolute paths are honoured. A relative path is tried against the current
    directory first, then against the repository root, so both of these work
    no matter where you are:

        --dir data/nisar_l2/GUNW
        --dir ../data/nisar_l2/GUNW
    """
    p = Path(p)
    if p.is_absolute():
        return p
    if p.exists():
        return p.resolve()
    candidate = ROOT / p
    if candidate.exists():
        return candidate
    return p            # let the caller report a clean "not found"


def find_products(product: str | None = None, roots=None,
                  strict: bool = True) -> list[Path]:
    """
    Every NISAR .h5 under the repository, deduplicated, newest layout first.

    product filters on the granule name (GUNW, GOFF, RSLC...). Case-insensitive.

    strict keeps only files whose name starts with NISAR_, and skips
    virtualenvs and caches entirely. Without it a repo containing a .venv
    reports h5py's own test fixtures as products.
    """
    seen: dict[Path, None] = {}
    for root in (roots or SEARCH_ROOTS):
        if not root.exists():
            continue
        for f in sorted(root.rglob("*.h5")):
            rel = f.relative_to(root) if root in f.parents else f
            if _is_excluded(rel) or _is_excluded(f):
                continue
            if strict and not f.name.upper().startswith(NISAR_NAME):
                continue
            if product and product.upper() not in f.name.upper():
                continue
            seen.setdefault(f.resolve(), None)
    return list(seen)


def describe_layout() -> str:
    """Human-readable summary of what exists, for error messages."""
    lines = [f"repository root : {ROOT}"]
    for label, path in (("data", DATA), ("nisar_l2", NISAR), ("outputs", OUTPUTS)):
        mark = "exists" if path.exists() else "missing"
        n = len(list(path.rglob("*.h5"))) if path.exists() else 0
        extra = f", {n} .h5" if n else ""
        lines.append(f"  {label:<9} {mark}{extra}  ({path})")
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe_layout())
    for prod in ("GUNW", "GOFF"):
        files = find_products(prod)
        print(f"\n{prod}: {len(files)} file(s)")
        for f in files[:5]:
            print(f"   {f.relative_to(ROOT) if ROOT in f.parents else f}")
        if len(files) > 5:
            print(f"   ... and {len(files)-5} more")
