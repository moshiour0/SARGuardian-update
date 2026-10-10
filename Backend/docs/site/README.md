# The interactive site

An eight-station lab where a visitor runs the analysis themselves. The browser
does the arithmetic on the real measurements; it is not a recording.

    python docs/site/build_data.py        # extract, ~4 min with network
    python docs/site/render.py            # inject into the template

`build_data.py` emits `site_data.json`: the scene catalogue with cloud scored
over the AOI, the change grid as one RGB PNG, real offset-pixel samples, the
displacement series and per-pair floors, the classified clusters, the five
track geometries, and a block of **reference values computed by the project's
own functions**.

That last block is the point. Each live station checks its answer against the
Python and prints `verified` or `mismatch` on the page, so the site cannot
silently drift from the science it is describing.

## Which stations are live

| Station | | Why |
|---|---|---|
| 2 Find the imagery | LIVE | filtering the catalogue is arithmetic |
| 3 Find where it broke | LIVE | thresholding and connected components on the shipped grid |
| 4 Narrow it down | PRE-COMPUTED terrain | the browser cannot query a DEM |
| 5 Measure the floor | LIVE | MAD, sigma and the floor from real pixels |
| 6 Which way it faces | LIVE | full look-geometry, verified against `geometry_merge` |
| 7 Run the alarm | LIVE | velocities, per-pair gating, inverse-velocity fit, cutoff |
| 8 What you found | LIVE | assembled from the visitor's own results |

The change grid travels as a single RGB PNG rather than three arrays: delta
NDSI in red, pre-event NDSI in green, the valid-in-both mask in blue. PNG
compresses three correlated 602x457 planes well, and `getImageData` reads them
back exactly.

## Things that bit, and are now guarded

- **Units.** The AOI exports are in millimetres, not metres. Scaling again put
  the floor at 21,209 mm/day instead of 21.2. The reference block caught it
  immediately, which is what it is for.
- **Animation gating the result.** Station 5 used to set its readouts at the
  end of a `requestAnimationFrame` loop, so a throttled or hidden tab showed no
  answer at all. Numbers land first now; the scatter is decoration.
- **Recursion.** Station 8 repaints on every `save()`, and its own paint stores
  a result and calls `save()`. Unbounded, and the stack overflow killed the
  boot block that draws the progress rail. There is a re-entrancy guard.
- **"Best scene before the event"** meant best in the whole archive, which
  returned a beautifully clear image from May. It is now the clearest inside
  the 60 days before the collapse - the one the analysis actually used.
