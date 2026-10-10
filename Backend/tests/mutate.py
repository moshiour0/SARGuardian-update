"""
mutate.py
---------
Put each historical bug back and check the suite notices.

Why this exists
===============
A test that passes proves nothing on its own - it may assert something that
was never in danger. The only evidence a regression test works is that it
fails when the regression returns.

So this reintroduces each real fault by editing the source, runs the suite,
and checks that the test written for that fault is among the failures. Every
mutation is reverted in a finally block, and the run refuses to start if the
working tree is dirty, because a crash mid-mutation would otherwise leave a
sabotaged reader on disk. That nearly happened once; hence the guard.

    python tests/mutate.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

# (label, file, find, replace, test that must fail)
MUTATIONS = [
    ("reference may cross connected components",
     "gunw_reader.py",
     "comp_ok = (~cut_v | (cut_c == target_comp)).all(axis=(1, 3))",
     "comp_ok = np.ones_like(full)",
     "test_reference_never_crosses_a_connected_component"),

    ("mask polarity inverted",
     "gunw_reader.py",
     "usable = (water == 0) & (ref_sub > 0) & (sec_sub > 0) & (losmask != 255)",
     "usable = (losmask == 0)",
     "test_mask_polarity_rejects_water_and_missing_subswaths"),

    ("reference requires a fully-valid block",
     "gunw_reader.py",
     "for level in (1.0, 0.8, 0.6, 0.4):",
     "for level in (1.0,):",
     "test_reference_survives_a_partly_invalid_scene"),

    ("displacement sign flipped",
     "gunw_reader.py",
     "scale = -(wavelength / (4.0 * math.pi)) * 1000.0",
     "scale = (wavelength / (4.0 * math.pi)) * 1000.0",
     "test_round_trip_holds_across_sign_and_magnitude"),

    ("ionosphere screen not removed",
     "gunw_reader.py",
     "phase = phase - np.nan_to_num(iono, nan=0.0)",
     "phase = phase",
     "test_ionosphere_screen_is_subtracted"),

    ("consistency check reads the wrong keys",
     "gunw_reader.py",
     'groups.setdefault((r["reference"], r["secondary"]), []).append(r)',
     'groups.setdefault((r["reference_date"], r["secondary_date"]), []).append(r)',
     "test_consistency_check_runs_on_reader_output_keys"),

    ("broken products pass verification",
     "gunw_reader.py",
     "            good.append(f)",
     "            good.append(f)\n        except Exception:\n            good.append(f)",
     "test_truncated_product_is_reported_not_skipped"),

    ("velocity gate reads sign, not magnitude",
     "inverse_velocity.py",
     'usable = [w for w in vs if abs(w["v_mm_day"]) > w["gate"]]',
     'usable = [w for w in vs if w["v_mm_day"] > w["gate"]]',
     "test_alarm_does_not_depend_on_look_direction"),

    ("inverse-velocity fit uses signed velocity",
     "inverse_velocity.py",
     'v = np.abs(np.array([w["v_mm_day"] for w in win], dtype=float))',
     'v = np.array([w["v_mm_day"] for w in win], dtype=float)',
     "test_inverse_velocity_fit_uses_speed_not_signed_velocity"),

    ("reported peak is the signed maximum",
     "inverse_velocity.py",
     'wmax = max(vs, key=lambda w: abs(w["v_mm_day"]))',
     'wmax = max(vs, key=lambda w: w["v_mm_day"])',
     "test_reported_peak_is_the_largest_magnitude"),

    ("duplicate products averaged instead of resolved",
     "timeseries.py",
     "out.append(best[1])",
     "best[1].value = sum(p.value for _, p in cands) / len(cands)\n        out.append(best[1])",
     "test_duplicate_products_are_resolved_not_averaged"),

    ("export grid clipped to valid pixels, not to the AOI",
     "gunw_reader.py",
     "    if col_off > 0.01 or row_off > 0.01:",
     "    if False:",
     "test_a_product_off_the_lattice_is_refused_not_resampled"),

    ("AOI grid not anchored to the absolute lattice",
     "gunw_reader.py",
     "    x0 = math.floor(min(rx) / res_x) * res_x - pad_px * res_x",
     "    x0 = min(rx) - pad_px * res_x",
     "test_two_products_covering_different_ground_get_the_same_grid"),

    ("per-pair floor dropped, gate falls back to the scalar",
     "inverse_velocity.py",
     '"floor": (floors or {}).get((a["epoch"], b["epoch"])),',
     '"floor": None,',
     "test_each_interval_carries_the_floor_of_the_pair_that_produced_it"),

    ("duplicate processing improves the bound",
     "inverse_velocity.py",
     "floors[(a, b)] = max(f, floors.get((a, b), 0.0))",
     "floors[(a, b)] = f",
     "test_duplicate_processings_keep_the_larger_floor"),

    ("sign reversals counted across tracks instead of within one",
     "troposphere.py",
     '        by_track.setdefault(r.get("track", "?"), {}).setdefault(',
     '        by_track.setdefault("all", {}).setdefault(',
     "test_reversals_are_counted_within_a_track_not_across_the_file_list"),

    ("duplicate processings counted as two observations",
     "troposphere.py",
     '            r.get("reference", r["file"]), r)',
     '            r["file"], r)',
     "test_duplicate_processings_of_one_pair_count_once"),

    ("an undefined correlation reported as maximally significant",
     "troposphere.py",
     '        out["p_value"] = float("nan")',
     '        out["p_value"] = 0.0',
     "test_an_undefined_correlation_is_not_reported_as_significant"),

    ("impoundment ranks on volume-per-metre across different thresholds",
     "impoundment.py",
     '        ranked = sorted(sites,\n'
     '                        key=lambda r: (r["threshold_height_m"],\n'
     '                                       -r["efficiency_Mm3_per_m"]))',
     '        ranked = sorted(sites, key=lambda r: -r["efficiency_Mm3_per_m"])',
     "test_a_site_needing_a_hundred_metres_does_not_outrank_one_that_fills_at_ten"),

    ("--blatten declared but never read",
     "detectability.py",
     "    if args.blatten:\n        if args.precursor == [5, 10, 20, 40]:",
     "    if False:\n        if args.precursor == [5, 10, 20, 40]:",
     "test_blatten_preset_actually_changes_the_run"),

    ("detector gate ignores the noise it is guarding against",
     "detectability.py",
     '        return self.sig_multiple * 3.0 * self.noise_mm * math.sqrt(2.0) / max(dt, 1e-9)',
     '        return self.min_velocity_mm_day',
     "test_noise_gate_scales_with_the_velocity_noise_it_guards"),

    ("sweep shares one generator, so a cell cannot be reproduced alone",
     "detectability.py",
     "            rng = np.random.default_rng((seed, int(T * 1000), int(dt * 1000)))",
     "            pass",
     "test_a_cell_reproduces_regardless_of_what_it_was_swept_alongside"),

    ("phase ceiling ignores the spatial gradient assumption",
     "detectability.py",
     "            step = np.abs(np.diff(truth)) * gradient_fraction",
     "            step = np.abs(np.diff(truth))",
     "test_gradient_fraction_relaxes_the_phase_ceiling"),

    ("lead time measured from the interval midpoint",
     "inverse_velocity.py",
     '        last_obs = win[-1]["t1"]',
     '        last_obs = win[-1]["mid"]',
     "test_lead_time_runs_from_the_last_acquisition_not_the_midpoint"),

    ("summary reverts to the global scalar floor",
     "inverse_velocity.py",
     '            fastest, gate = r["max_velocity"], r["required"]',
     '            fastest, gate = r["max_velocity"], args.sig_multiple * args.noise_floor',
     "test_summary_reports_against_the_pairs_own_floor"),

    ("coverage measured against the frame, not the AOI",
     "gunw_reader.py",
     '"aoi_pct": round(100 * n / aoi_px, 2) if aoi_px else None,',
     '"aoi_pct": round(100 * n / total, 2) if aoi_px else None,',
     "test_coverage_is_reported_against_the_aoi_not_the_frame"),

    ("nodata decided after the ionosphere screen is removed",
     "gunw_reader.py",
     "    valid = np.isfinite(disp) & has_phase",
     "    valid = np.isfinite(disp) & (phase != 0)",
     "test_nodata_is_decided_on_raw_phase_not_the_ionosphere_corrected_array"),

    ("consistency check trusts every duplicate to carry a median",
     "gunw_reader.py",
     "        measured = [g for g in group if g.get(\"median\") is not None]",
     "        measured = list(group)",
     "test_a_duplicate_with_no_valid_pixels_is_reported_not_crashed_on"),

    ("fringe length taken from the constant, not the product",
     "gunw_reader.py",
     '        lam = group[0].get("wavelength_m") or NISAR_LAMBDA_M',
     '        lam = NISAR_LAMBDA_M',
     "test_consistency_sizes_a_fringe_from_the_products_own_wavelength"),

    ("Sentinel-1 modelled with NISAR's look side",
     "geometry_merge.py",
     'Track("S1 DESC 19", "Sentinel-1", False, 98.18, 39.0, 12, left_looking=False),',
     'Track("S1 DESC 19", "Sentinel-1", False, 98.18, 39.0, 12, left_looking=True),',
     "test_descending_sentinel1_puts_the_satellite_east_of_the_target"),

    ("look side reverses the vertical too, not just the horizontals",
     "geometry_merge.py",
     "    return np.array([side * -math.sin(t) * math.cos(h),\n"
     "                     side * math.sin(t) * math.sin(h),\n"
     "                     math.cos(t)])",
     "    return np.array([side * -math.sin(t) * math.cos(h),\n"
     "                     side * math.sin(t) * math.sin(h),\n"
     "                     side * math.cos(t)])",
     "test_flipping_the_side_reverses_only_the_horizontal_components"),

    ("geometry test decides the wrong way round",
     "candidate_check.py",
     "    if d_motion < d_delay:",
     "    if d_delay < d_motion:",
     "test_same_signed_rates_are_a_path_delay_where_sensitivities_oppose"),

    ("blind track still gets a verdict",
     "candidate_check.py",
     "    if abs(sens_asc) < MIN_SENS or abs(sens_desc) < MIN_SENS:",
     "    if False:",
     "test_a_blind_track_yields_no_verdict"),

    ("gentle terrain still gets a verdict",
     "candidate_check.py",
     "    if slope_deg is not None and slope_deg < MIN_SLOPE_DEG:",
     "    if False:",
     "test_gentle_terrain_yields_no_verdict"),

    ("indistinguishable geometry still gets a verdict",
     "candidate_check.py",
     "    if abs(ratio_motion - 1.0) < RATIO_MARGIN:",
     "    if False:",
     "test_no_verdict_when_the_two_hypotheses_predict_the_same_ratio"),

    ("elevation fit chases the outlier tails",
     "troposphere.py",
     '        if not robust:',
     '        if True:',
     "test_outliers_do_not_steer_the_slope"),

    ("trend removed in mm per metre, not mm per km",
     "troposphere.py",
     '    return disp_mm - fit["slope_mm_per_km"] * (elev_m / 1000.0)',
     '    return disp_mm - fit["slope_mm_per_km"] * elev_m',
     "test_removal_does_not_raise_the_core_scatter"),

    ("intercept subtracted along with the gradient",
     "troposphere.py",
     '    return disp_mm - fit["slope_mm_per_km"] * (elev_m / 1000.0)',
     '    return disp_mm - fit["slope_mm_per_km"] * (elev_m / 1000.0) - fit["intercept_mm"]',
     "test_intercept_is_not_subtracted"),

    ("extrapolated fit not flagged",
     "troposphere.py",
     '               leverage=(rng / iqr) if iqr > 0 else float("inf"),',
     '               leverage=1.0,',
     "test_leverage_is_flagged_when_the_fit_is_extrapolated"),

    ("flat terrain given a slope anyway",
     "troposphere.py",
     '    if np.ptp(e_all) <= 0:',
     '    if False:',
     "test_flat_terrain_is_refused"),

    ("local floor drops the span, reporting scatter as velocity",
     "local_floor.py",
     "    return 3.0 * robust_sigma(values_mm) / span_days",
     "    return 3.0 * robust_sigma(values_mm)",
     "test_floor_is_three_sigma_over_the_span"),

    ("window padded instead of clipped at the raster edge",
     "local_floor.py",
     "    r0, r1 = max(0, row - radius), min(arr.shape[0], row + radius + 1)",
     "    r0, r1 = row - radius, row + radius + 1",
     "test_window_is_clipped_at_the_edge_not_padded"),

    ("a two-pixel window still gets a noise floor",
     "local_floor.py",
     "    if a.size < MIN_PX or w.size < MIN_PX:",
     "    if False:",
     "test_too_few_valid_pixels_is_refused_not_averaged"),

    ("window sample count not reported with the floor",
     "local_floor.py",
     '           "window_total_px": int(win_mm.size),',
     '           "window_total_px": 0,',
     "test_valid_pixel_count_is_reported_alongside_the_floor"),

    ("post-event data leaks into the forecast",
     "inverse_velocity.py",
     '        leaked = [w for w in vs if w["t1"] >= cutoff]',
     '        leaked = []',
     "test_post_event_interval_is_dropped_from_the_fit"),

    ("cutoff boundary is exclusive, so the event pair survives",
     "inverse_velocity.py",
     '        leaked = [w for w in vs if w["t1"] >= cutoff]',
     '        leaked = [w for w in vs if w["t1"] > cutoff]',
     "test_an_interval_ending_exactly_on_the_event_is_excluded"),
    # ---- restart2: fixes found in the 9 Sep 2026 audit --------------------
    ("rates divide by requested trials, not trials that ran",
     "detectability.py",
     'sat_rate = saturated / n_eligible if n_eligible else float("nan")',
     'sat_rate = saturated / n_trials',
     "test_skipped_trials_leave_the_denominator"),

    ("detector does not report how many windows it tested",
     "detectability.py",
     "            tested += 1",
     "            tested += 0",
     "test_the_detector_reports_how_many_windows_it_tested"),

    ("failure date bounded even when the slope is not resolved",
     "inverse_velocity.py",
     "    if A <= 0:",
     "    if False:",
     "test_a_three_point_fit_on_noise_does_not_bound_a_failure_date"),

    ("non-finite per-pair floor reaches the gate",
     "inverse_velocity.py",
     "            if not math.isfinite(probe) or probe <= 0:",
     "            if False:",
     "test_a_non_finite_floor_is_rejected_not_parsed"),

    ("local floor reported without a sampling interval",
     "local_floor.py",
     "    return (float(np.percentile(good, 2.5)), float(np.percentile(good, 97.5)))",
     "    return (float(np.median(good)), float(np.median(good)))",
     "test_the_bootstrap_interval_brackets_the_point_estimate"),

    ("track dropped from the derived statistics",
     "gunw_reader.py",
     "    m = TRACK_RE.search(str(name))",
     "    m = None",
     "test_track_is_recovered_from_the_granule_name"),
    # ---- scar mapping from optical -------------------------------------
    ("snow masked out with the clouds, removing the whole signal",
     "scar_map.py",
     "UNUSABLE = {0, 1, 3, 8, 9, 10}",
     "UNUSABLE = {0, 1, 3, 8, 9, 10, 11}",
     "test_snow_is_not_treated_as_unusable"),

    ("grid transform at the wrong scale, placing the scar 600 m out",
     "scar_map.py",
     "    return tf_from_bounds(*b, width=GRID[1], height=GRID[0])",
     "    return tf_from_bounds(*b, width=GRID[1]*2, height=GRID[0]*2)",
     "test_the_grid_transform_comes_from_the_aoi_not_a_band"),

    ("an unobserved location reported as observed",
     "scar_map.py",
     '        return {"lat": lat, "lon": lon, "n_usable": 0, "observed": False}',
     '        return {"lat": lat, "lon": lon, "n_usable": 0, "observed": True}',
     "test_an_unobserved_point_is_reported_as_untested_not_as_clean"),

    ("centroid not weighted by the strength of the change",
     "scar_map.py",
     "    w = np.where(np.isfinite(dv), -dv, 0.0)",
     "    w = np.ones_like(dv)",
     "test_measure_reports_extent_and_a_change_weighted_centroid"),
    # ---- the demo ------------------------------------------------------
    ("demo lets an event-spanning interval into its pre-event window",
     "../demo.py",
     "              if spans(r)[1] < EVENT and (EVENT - spans(r)[0]).days <= 60]",
     "              if spans(r)[0] < EVENT and (EVENT - spans(r)[0]).days <= 60]",
     "test_no_pre_event_interval_may_end_after_the_collapse"),

    ("demo quotes the median interval rather than the weakest",
     "../demo.py",
     "    worst = max(float(r[\"local_floor_mm_day\"]) for r in summer) if summer else float(\"nan\")",
     "    worst = min(float(r[\"local_floor_mm_day\"]) for r in summer) if summer else float(\"nan\")",
     "test_the_bound_is_the_weakest_pre_event_interval"),
    # ---- 11 Sep 2026 review: like-for-like headline --------------------
    ("headline divides the downslope bound by a line-of-sight precursor",
     "bound.py",
     '        "ratio_los_pixel": round(px / PRECURSOR_MM_DAY, 1) if g else float("nan"),',
     '        "ratio_los_pixel": round(px / abs(sens) / PRECURSOR_MM_DAY, 1) if g else float("nan"),',
     "test_the_ratio_is_line_of_sight_against_line_of_sight"),

    ("bound's pre-event window judged on the reference date",
     "bound.py",
     "    return sec < EVENT and (EVENT - ref).days <= COVER_DAYS",
     "    return ref < EVENT and (EVENT - ref).days <= COVER_DAYS",
     "test_the_pre_event_window_closes_at_the_collapse"),

    ("an unobserved candidate is given a bound anyway",
     "bound.py",
     '        "downslope_bound_mm_day": round(px / abs(sens), 1) if g else float("nan"),',
     '        "downslope_bound_mm_day": round(32.0 / abs(sens), 1),',
     "test_an_unobserved_candidate_gets_no_bound"),

    ("window-median floor built from pixels, not from block medians",
     "local_floor.py",
     "                meds.append(float(np.median(v)))",
     "                meds.extend(v.tolist())",
     "test_block_floor_is_below_the_pixel_floor_for_independent_noise"),

    ("common datum allowed onto the target",
     "common_ref.py",
     "        out |= (yy - r) ** 2 + (xx - c) ** 2 <= radius_px ** 2",
     "        pass",
     "test_the_datum_is_never_set_on_the_target"),

    ("timeseries common datum allowed onto the moving target",
     "timeseries.py",
     "            stable &= ~buffer_mask(stable.shape, [centre], int(round(buffer_km * 1000 / px)))",
     "            pass",
     "test_common_datum_is_not_set_on_the_moving_target"),

    ("a floors file mixes two targets' noise",
     "local_floor.py",
     '        raise ValueError("--as-floors needs a single target")',
     "        pass",
     "test_floors_from_two_targets_are_refused"),

    ("a pair straddling the release gap is called BETA",
     "gunw_reader.py",
     "    if lo <= ref <= hi and lo <= sec <= hi:",
     "    if lo <= ref <= hi:",
     "test_a_pair_straddling_the_release_gap_is_not_rounded_to_either"),
]


def failing_tests() -> set[str]:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(ROOT / "tests"), "-q", "--no-header",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=ROOT)
    out = r.stdout + r.stderr
    names = set()
    for line in out.splitlines():
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            part = line.split(" ", 1)[1].split(" ")[0]
            if "::" in part:
                names.add(part.split("::")[-1].split("[")[0])
    return names


def main() -> int:
    dirty = subprocess.run(["git", "status", "--porcelain", "src"],
                           capture_output=True, text=True, cwd=ROOT).stdout.strip()
    if dirty:
        print("src/ has uncommitted changes. Commit or stash first - this script\n"
              "edits source in place and must be able to restore it exactly.\n")
        print(dirty)
        return 2

    baseline = failing_tests()
    if baseline:
        print(f"Suite is not green to begin with: {sorted(baseline)}")
        return 2
    print("baseline: suite green\n")
    print(f"  {'MUTATION':<46}{'CAUGHT BY':<12}RESULT")
    print("  " + "-" * 74)

    escaped = []
    for label, fname, find, repl, expect in MUTATIONS:
        path = SRC / fname
        # newline="" on both read and write, so the file goes back byte for
        # byte. write_text() uses the platform newline, which on Windows turns
        # every LF into CRLF - the content is identical but git sees the file
        # as modified, the tree is left dirty, and the NEXT run of this script
        # refuses to start because it insists on a clean tree. A harness that
        # sabotages the next invocation of itself is worse than no harness.
        with open(path, encoding="utf-8", newline="") as fh:
            original = fh.read()
        # Match the file's own line endings. With git core.autocrlf=true a
        # Windows checkout is CRLF, so every multi-line target written with
        # "\n" silently failed to match and was reported as an escape - three
        # of them, on this machine only, while a Linux clone caught all.
        if find not in original and "\n" in find:
            crlf = find.replace("\n", "\r\n")
            if crlf in original:
                find, repl = crlf, repl.replace("\n", "\r\n")
        try:
            if find not in original:
                print(f"  {label:<46}{'-':<12}TARGET GONE - update mutate.py")
                escaped.append(label)
                continue
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(original.replace(find, repl, 1))
            failed = failing_tests()
            if expect in failed:
                print(f"  {label:<46}{'yes':<12}caught ({len(failed)} failed)")
            else:
                print(f"  {label:<46}{'NO':<12}ESCAPED")
                escaped.append(label)
        finally:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(original)
            for junk in SRC.rglob("__pycache__"):
                for f in junk.glob("*"):
                    f.unlink()

    print()
    if escaped:
        print(f"{len(escaped)} mutation(s) escaped - those tests do not protect "
              f"what they claim:")
        for e in escaped:
            print(f"  - {e}")
        return 1
    print(f"All {len(MUTATIONS)} mutations caught. The suite bites.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
