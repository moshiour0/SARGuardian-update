# 30-second video script

Space Apps gives a 30-second slot. This is written to land the whole argument
inside it, with the terminal doing the work rather than a voice describing it.

**What to record:** one screen capture of `python demo.py` running at its
default pace, which finishes in 29 seconds. Narration over the top. No slides,
no title card - the demo *is* the title card, and a judge who has watched forty
mockups will notice that this one is computing.

```bash
python demo.py                    # 29 s, paced
python demo.py --plain            # no ANSI colour, if your recorder mangles it
python demo.py --pace 0.5         # 23 s, if you need headroom for narration
```

Record at 1280x720 or larger, terminal font 16 pt or larger. Dark background.

---

## The script

Word counts assume ~150 wpm, which is an unhurried speaking pace. Total 74
words - deliberately under the slot, because the numbers need a beat to land.

| Time | On screen | Narration |
|------|-----------|-----------|
| **0:00-0:05** | Title block, then the event | "On the 26th of August, a mountainside in Nepal collapsed and killed more than a thousand people." |
| **0:06-0:11** | Section 1, the four candidates | "Satellite imagery shows four places it could have detached - and the one at the reported height is a kilometre from where the reports said." |
| **0:12-0:18** | Section 2, the floors scrolling | "Then we measured what NISAR radar could actually see on that exact slope." |
| **0:19-0:23** | Section 3, no alarm | "Our detector found nothing." |
| **0:24-0:29** | Section 4, the three numbers | "Because the warning signal was there - and it was thirty to a hundred times too small for this product to see. That gap is the answer." |

---

## If you only get one sentence

> The precursor was 0.33 millimetres a day. NISAR's offset floor there was 9 to
> 32. It could not have warned, and now we know by how much.

---

## What to cut if you overrun

In order, cut:

1. The `--pace` down to 0.5 (saves 6 s, no content lost)
2. Section 3, the detector run (saves 4 s) - the null is implied by section 4
3. The scar area and extent lines in section 1 (saves 2 s)

**Do not cut** the last lines of section 4. The comparison between 0.33 and
9.2-32.0 mm/day is the whole argument; everything before it is setup.

---

## What not to say

- **Do not say "we failed to detect it".** The project detected the limit, which
  is a result. The framing that scores is "we measured the requirement".
- **Do not claim a warning system.** This is an instrument-capability study with
  a measured bound. Claiming an operational early-warning system invites a
  judge to ask for the operational evidence, and there is none.
- **Do not quote 45, 40.4, 33.4, 68, 60-119 mm/day or "180-360x".** All are
  superseded. 45 assumed the wrong aspect; 40.4 and 33.4 were measured off any
  candidate; 68 treated one candidate as the answer; 60-119 and 180-360x
  divided a downslope bound by a line-of-sight rate and took their upper end
  from a candidate with no valid data. The live numbers are **9.2-32.0 mm/day
  line of sight, 28x-96x the precursor** - from `python src/bound.py`, and
  said as a range, because the range is the honest part.
- **Do not say "phase would have caught it" about NISAR.** At the candidate at
  the published elevation NISAR phase has no valid pixel in any pair.

---

## Disclosure

Space Apps permits AI assistance and requires it to be declared. Put it in the
project page's tools list, not in the video - the slot is too short and the
submission form is where the rule actually applies.
