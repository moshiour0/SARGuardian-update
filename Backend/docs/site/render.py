#!/usr/bin/env python3
"""Inject site_data.json into the template. Keeps the two separable."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
tpl = (ROOT / "index.tpl.html").read_text(encoding="utf-8")
data = (ROOT / "site_data.json").read_text(encoding="utf-8")
if "</script" in data:
    sys.exit("payload contains a closing script tag and would break out of it")
out = ROOT / "index.html"
out.write_text(tpl.replace("{{DATA}}", data), encoding="utf-8")
print(f"wrote {out}  ({out.stat().st_size/1e6:.2f} MB)")
