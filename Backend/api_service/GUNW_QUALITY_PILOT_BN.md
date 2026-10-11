# SARGuardian — Multi-layer GUNW AOI quality pilot (বাংলা)

## এই আপডেটে কী যোগ হয়েছে

- `POST /api/harmony/gunw-quality/start`: বিদ্যমান সফল HH/VV unwrapped-phase job পুনর্ব্যবহার করে একই granule/polarization-এর coherence, connected components, ionosphere phase screen, shared GUNW mask এবং ঐচ্ছিক ionosphere-screen uncertainty layer-এর আলাদা Harmony jobs তৈরি করে।
- `POST /api/harmony/gunw-quality/analyze-aoi`: সব প্রয়োজনীয় job successful হলে remote COG থেকে কেবল নির্বাচিত AOI window পড়ে, raster grid alignment যাচাই করে এবং `src/gunw_reader.py`-এর মূল quality gates-এর সঙ্গে সামঞ্জস্যপূর্ণ diagnostics দেয়।
- GeoTIFF স্থায়ীভাবে সংরক্ষণ করা হয় না। Signed URLs, authorization tokens, অথবা pixel arrays API response/log-এ ফেরত দেওয়া হয় না।

## গুরুত্বপূর্ণ সীমাবদ্ধতা

এই ধাপটি এখনো `src/gunw_reader.py`-এর HDF5-only `read_gunw()` function-কে সরাসরি কল করে না। এটি একই মূল gates (phase non-zero, coherence threshold, connected component > 0, GUNW mask decoding, ionosphere subtraction) আলigned AOI arrays-এ প্রয়োগ করে। Stable reference region এবং verified source metadata ছাড়া LOS displacement প্রকাশ করা হয় না। এটি landslide prediction বা alert নয়।

বর্তমান `net2cog` extraction-এ AOI server-side crop নিশ্চিত নয়। Companion jobs full-granule layer তৈরি করতে পারে; SARGuardian শুধু remote COG থেকে ছোট AOI window পড়ে। তাই job bundle-টি একবারের pilot হিসেবে ব্যবহার করো, বারবার বোতাম চাপবে না। প্রতি client/API process-এ bundle starts সীমিত করা হয়েছে, তবে production-এ platform-level rate limit-ও ব্যবহার করা উচিত।

## ওয়েব UI দিয়ে ব্যবহার

1. বিদ্যমান সফল phase job ID আগের Harmony Job ID ঘরে রাখো।
2. নিচের নতুন field-এ সেই job-এর exact GUNW granule name বসাও। উদাহরণ: `NISAR_L2_PR_GUNW_027_149_D_074_028_2000_SH_20260811T130632_20260811T130706_20260823T130631_20260823T130706_P05023_N_F_J_001`।
3. AOI polygon বন্ধ করে **Start companion quality-layer jobs** চাপো। এতে কয়েকটি Harmony asynchronous jobs তৈরি হবে; এই সময় কোনো TIFF সার্ভারে নামানো হয় না।
4. **Check all layer jobs** চাপতে থাকো যতক্ষণ phase, coherence, connected components, ionosphere screen এবং mask সব `successful` না হয়।
5. সব required job সফল হলে **Analyze aligned AOI quality gates** চাপো।
6. ফলাফলে jointly qualified pixel count, coherence pass, component gate, land/subswath mask এবং ionosphere-corrected phase statistics দেখা যাবে। Grid misalignment পেলে analyzer ইচ্ছাকৃতভাবে থেমে যাবে।

## API / Swagger ব্যবহার

`POST /api/harmony/gunw-quality/start` body:

```json
{
  "granule_name": "NISAR_L2_PR_GUNW_027_149_D_074_028_2000_SH_20260811T130632_20260811T130706_20260823T130631_20260823T130706_P05023_N_F_J_001",
  "phase_job_id": "YOUR_SUCCESSFUL_PHASE_JOB_ID",
  "coordinates": [[85.4, 28.2], [85.5, 28.2], [85.5, 28.3], [85.4, 28.3]],
  "maturity": "PROVISIONAL",
  "polarization": "HH",
  "confirm_full_granule_layers": true,
  "include_iono_uncertainty": true
}
```

Start response থেকে `jobs` object-এর job IDs রাখো। তারপর `GET /api/harmony/jobs/{job_id}` দিয়ে প্রতিটি required job successful কি না পরীক্ষা করো।

`POST /api/harmony/gunw-quality/analyze-aoi` body:

```json
{
  "granule_name": "NISAR_L2_PR_GUNW_027_149_D_074_028_2000_SH_20260811T130632_20260811T130706_20260823T130631_20260823T130706_P05023_N_F_J_001",
  "polarization": "HH",
  "coordinates": [[85.4, 28.2], [85.5, 28.2], [85.5, 28.3], [85.4, 28.3]],
  "phase_job_id": "PHASE_JOB_ID",
  "coherence_job_id": "COHERENCE_JOB_ID",
  "connected_components_job_id": "COMPONENTS_JOB_ID",
  "ionosphere_phase_screen_job_id": "IONOSPHERE_JOB_ID",
  "mask_job_id": "MASK_JOB_ID",
  "ionosphere_phase_screen_uncertainty_job_id": "OPTIONAL_UNCERTAINTY_JOB_ID",
  "coherence_threshold": 0.3
}
```

Coordinates অবশ্যই তোমার বর্তমান Polygon-এর বাস্তব WGS84 `[longitude, latitude]` vertices দিয়ে বদলাবে। Example coordinates নিজের AOI-র বদলে ব্যবহার করবে না।

## Deployment notes

- Backend patch-এর ফাইলগুলো `Backend/`-এর একই relative path-এ রাখতে হবে।
- Frontend patch-এর ফাইলগুলো `Frontend/`-এর একই relative path-এ রাখতে হবে।
- GitHub-এ push করার পর Render এবং Vercel-এর Build status সফল হয়েছে কি না যাচাই করতে হবে।
- Backend-এ `EARTHDATA_TOKEN` শুধু Render Secret হিসেবে থাকবে; GitHub বা `VITE_` frontend variable-এ নয়।
- Live Harmony outputs এবং host-side remote range access সব hosting environment-এ একইভাবে কাজ করবে—এটি এই runtime-এ end-to-end যাচাই করা সম্ভব হয়নি।
