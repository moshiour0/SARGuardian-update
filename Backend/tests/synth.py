"""
synth.py
--------
Build small NISAR-like L2 products with a displacement we chose ourselves.

Why fixtures and not real data
==============================
Every serious fault in this project produced plausible numbers instead of an
error: a reference in the wrong connected component, a mask read with inverted
polarity, a wrapped-grid coherence array paired with unwrapped-grid phase, a
truncated download that removed an epoch in silence. None of them crashed.
None could be caught by looking at the output, because the output looked fine.

They are all caught instantly by one question a real product can never answer:
put 50 mm in, does 50 mm come out?

So these files are built backwards from a known displacement field. The phase
is computed from the answer rather than the answer from the phase, which makes
every downstream step checkable against a number we already hold.

Faithfulness
============
Group layout, dataset names, dtypes and the three-digit mask encoding follow a
real NISAR L2 GUNW. In particular the decoy is reproduced: a
wrappedInterferogram coherence array on a much finer grid, which is what an
unanchored dataset search once matched against unwrapped phase. A fixture
without it cannot test the fix.

Grids are ~240 px instead of ~4300 so the suite runs in seconds.
"""

from __future__ import annotations

import numpy as np

try:
    import h5py
except ImportError:                                   # pragma: no cover
    raise SystemExit("h5py is required to build test fixtures: pip install h5py")

WAVELENGTH_M = 0.2439
SPEED_OF_LIGHT = 299_792_458.0
FRINGE_MM = WAVELENGTH_M / 2 * 1000.0                 # 122.0 mm per 2-pi cycle

GUNW_GRID = "science/LSAR/GUNW/grids/frequencyA"
UNW = f"{GUNW_GRID}/unwrappedInterferogram"
WRAP = f"{GUNW_GRID}/wrappedInterferogram"
GOFF_GRID = "science/LSAR/GOFF/grids/frequencyA/pixelOffsets"

# A box comfortably inside the Lhende AOI ring, and the grid we build around it.
LHENDE_INSIDE = (85.50, 28.40)                        # lon, lat
GRID_BOUNDS = (85.35, 28.25, 85.72, 28.57)            # lon0, lat0, lon1, lat1


def displacement_to_phase(disp_mm: np.ndarray, wavelength_m: float = WAVELENGTH_M):
    """Invert d = -(lambda / 4*pi) * phi, so a fixture states its own answer."""
    return -(disp_mm / 1000.0) * (4.0 * np.pi / wavelength_m)


def make_grid(n: int = 240):
    lon0, lat0, lon1, lat1 = GRID_BOUNDS
    xs = np.linspace(lon0, lon1, n)
    ys = np.linspace(lat1, lat0, n)                   # north-up, as delivered
    return xs, ys


def aoi_box_mask(xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Cells inside the Lhende ring, computed here rather than imported."""
    from gunw_reader import LHENDE_RING
    lons = [p[0] for p in LHENDE_RING]
    lats = [p[1] for p in LHENDE_RING]
    gx, gy = np.meshgrid(xs, ys)
    return ((gx >= min(lons)) & (gx <= max(lons))
            & (gy >= min(lats)) & (gy <= max(lats)))


def encode_mask(water=0, ref_sub=1, sec_sub=1) -> int:
    """
    The three-digit LOS mask: water * 100 + ref_subswath * 10 + sec_subswath.

    A pixel is usable only when water == 0 and BOTH subswath digits are
    non-zero. Reading this as a simple `mask == 0` keeps precisely the pixels
    that were invalid in both acquisitions - the inversion that once left 0.5%
    of a scene standing instead of 39.5%.
    """
    return water * 100 + ref_sub * 10 + sec_sub


def write_gunw(
    path,
    truth_mm: float = 50.0,
    n: int = 240,
    coherence: float = 0.9,
    aoi_component: int = 1,
    far_component: int | None = None,
    far_fringes: float = 0.0,
    iono_mm: float = 0.0,
    water_stripe: bool = False,
    unusable_stripe: bool = False,
    hole_fraction: float = 0.0,
    nodata_fraction: float = 0.0,
    include_wrapped_decoy: bool = True,
    stable_ring_px: int = 40,
    wavelength_m: float = WAVELENGTH_M,
    noise_mm: float = 2.0,
    seed: int = 0,
) -> dict:
    """
    Write one fixture and return the ground truth needed to check it.

    truth_mm         displacement inside the AOI; everything else is 0
    far_component    component id for ground far from the AOI
    far_fringes      whole cycles added there, the cross-component trap
    iono_mm          constant ionospheric screen, in mm of LOS
    water_stripe     a band coded as water, which must be rejected
    unusable_stripe  a band with a zero subswath digit, also rejected
    nodata_fraction  cells left as EXACT ZERO phase, the way a real GUNW
                     fills what it could not unwrap. Their mask digits
                     stay valid on purpose, so `phase == 0` is the only
                     thing that can reject them - which is what makes
                     them able to test the nodata gate at all
    stable_ring_px   width of same-component stable ground around the AOI
    """
    xs, ys = make_grid(n)
    aoi = aoi_box_mask(xs, ys)

    # Stable ground carries a little atmosphere, never an exact zero. This is
    # not decoration: the reader treats phase == 0 as no-data, exactly as a
    # real product's unfilled cells are, so a noiseless fixture marks its own
    # stable ground invalid and every reference search then fails inside the
    # AOI. The fixture has to be as noisy as reality to exercise the same path.
    rng = np.random.default_rng(seed)
    disp = rng.normal(0.0, noise_mm, size=(n, n)) if noise_mm else np.zeros((n, n))
    disp[aoi] += truth_mm

    comp = np.full((n, n), aoi_component, dtype=np.uint16)
    if far_component is not None:
        # Everything beyond a stable ring around the AOI belongs to another
        # component, and carries its own arbitrary constant.
        far = ~_dilate(aoi, stable_ring_px)
        comp[far] = far_component
        disp[far] += far_fringes * FRINGE_MM

    phase = displacement_to_phase(disp, wavelength_m)

    iono_phase = np.zeros((n, n), dtype=np.float32)
    if iono_mm:
        # A GRADIENT, not a constant. Referencing subtracts one number from the
        # whole scene, so a constant screen vanishes whether the reader removes
        # it or not - a fixture built that way cannot tell a working
        # ionosphere correction from a missing one.
        ramp = np.linspace(-1.0, 1.0, n)[None, :] * np.ones((n, 1))
        iono_phase[:] = displacement_to_phase(ramp * iono_mm, wavelength_m)
        phase = phase + iono_phase                     # reader must remove it

    # Unfilled cells, written last so they are exactly zero in the stored
    # array - including where the ionosphere screen is not zero. That is the
    # real product's convention, and it is the only thing that can catch a
    # reader which decides nodata AFTER subtracting the screen.
    nodata = np.zeros((n, n), dtype=bool)
    if nodata_fraction:
        # Scattered across the WHOLE grid, the AOI included. Keeping them
        # outside would let the AOI clip remove them before the nodata gate
        # was ever exercised, which is a fixture that cannot fail.
        nodata = rng.random((n, n)) < nodata_fraction
        phase[nodata] = 0.0

    coh = np.full((n, n), coherence, dtype=np.float32)
    mask = np.full((n, n), encode_mask(), dtype=np.uint8)
    if hole_fraction:
        # Scattered invalid pixels, the way real decorrelation arrives. Stripes
        # leave whole clean blocks standing, so they never exercise the relaxed
        # reference search; holes spread everywhere are what force it.
        holes = rng.random((n, n)) < hole_fraction
        mask[holes] = encode_mask(sec_sub=0)
    if water_stripe:
        mask[:12, :] = encode_mask(water=1)
    if unusable_stripe:
        mask[-12:, :] = encode_mask(ref_sub=0)

    usable = ~nodata
    if hole_fraction:
        usable &= ~holes
    if water_stripe:
        usable[:12, :] = False
    if unusable_stripe:
        usable[-12:, :] = False

    with h5py.File(path, "w") as f:
        g = f.create_group(f"{UNW}/HH")
        g.create_dataset("unwrappedPhase", data=phase.astype(np.float32))
        g.create_dataset("coherenceMagnitude", data=coh)
        g.create_dataset("connectedComponents", data=comp)
        g.create_dataset("ionospherePhaseScreen", data=iono_phase)
        g.create_dataset("xCoordinates", data=xs)
        g.create_dataset("yCoordinates", data=ys)
        g.create_dataset("projection", data=np.uint32(4326))
        f[UNW].create_dataset("mask", data=mask)
        f[UNW].create_dataset("xCoordinates", data=xs)
        f[UNW].create_dataset("yCoordinates", data=ys)
        f[GUNW_GRID].create_dataset(
            "centerFrequency", data=np.float64(SPEED_OF_LIGHT / wavelength_m))

        if include_wrapped_decoy:
            # The trap. A coherence array of the same name on a four-times
            # finer grid, at a shorter path than the unwrapped one. A search
            # that ranks by path length picks THIS, and every quality gate is
            # then applied to the wrong pixels.
            m = n * 4
            w = f.create_group(f"{WRAP}/HH")
            w.create_dataset("coherenceMagnitude",
                             data=np.full((m, m), 0.99, dtype=np.float32))
            w.create_dataset("xCoordinates", data=np.linspace(xs[0], xs[-1], m))
            w.create_dataset("yCoordinates", data=np.linspace(ys[0], ys[-1], m))
            f[WRAP].create_dataset("mask", data=np.zeros((m, m), dtype=np.uint8))

    return {"truth_mm": truth_mm, "n": n, "xs": xs, "ys": ys, "aoi": aoi,
            "usable": usable, "components": comp, "fringe_mm": FRINGE_MM,
            "nodata": nodata, "n_nodata": int(nodata.sum()),
            "n_aoi_usable": int((aoi & usable).sum())}


def _dilate(mask: np.ndarray, px: int) -> np.ndarray:
    """Square dilation by `px`, without pulling in scipy for one call."""
    if px <= 0:
        return mask
    out = mask.copy()
    for shift in range(1, px + 1):
        out[:-shift, :] |= mask[shift:, :]
        out[shift:, :] |= mask[:-shift, :]
    side = out.copy()
    for shift in range(1, px + 1):
        out[:, :-shift] |= side[:, shift:]
        out[:, shift:] |= side[:, :-shift]
    return out


def write_goff(path, truth_mm: float = 200.0, n: int = 240,
               empty_vv: bool = True, correlation: float = 0.8) -> dict:
    """
    A GOFF fixture whose only job is the polarisation-key collision.

    Quad-pol products carry HH/layer1..3 and VV/layer1..3. Keying layers by
    name alone lets an empty VV overwrite a populated HH and the read collapses
    to nothing - diagnosed at the time as a data problem rather than a
    dictionary key.
    """
    xs, ys = make_grid(n)
    aoi = aoi_box_mask(xs, ys)
    rng = np.zeros((n, n)); rng[aoi] = truth_mm / 1000.0        # metres

    with h5py.File(path, "w") as f:
        for pol in ("HH", "VV"):
            dead = empty_vv and pol == "VV"
            for layer in ("layer1", "layer2", "layer3"):
                g = f.create_group(f"{GOFF_GRID}/{pol}/{layer}")
                g.create_dataset("slantRangeOffset",
                                 data=(np.zeros((n, n)) if dead else rng))
                g.create_dataset("alongTrackOffset", data=np.zeros((n, n)))
                g.create_dataset("correlationSurfacePeak",
                                 data=np.full((n, n), 0.0 if dead else correlation,
                                              dtype=np.float32))
                g.create_dataset("snr", data=np.full((n, n), 0.0 if dead else 10.0,
                                                     dtype=np.float32))
                g.create_dataset("xCoordinates", data=xs)
                g.create_dataset("yCoordinates", data=ys)
                g.create_dataset("projection", data=np.uint32(4326))
    return {"truth_mm": truth_mm, "aoi": aoi, "n_aoi": int(aoi.sum())}


def granule_name(product="GUNW", proc="PR", path=48, direction="D",
                 ref="20260101", sec="20260113") -> str:
    """A filename the organiser, the date parser and the exporter all accept."""
    return (f"NISAR_L2_{proc}_{product}_006_{path:03d}_{direction}_074_007_2000_SH_"
            f"{ref}T125813_{ref}T125848_{sec}T125814_{sec}T125849_X05010_N_F_J_001.h5")
