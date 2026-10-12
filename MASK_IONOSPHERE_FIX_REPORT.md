# SARGuardian — Mask & Ionosphere Fix (বাংলা নির্দেশিকা)

## কোন সমস্যাগুলো ঠিক করা হয়েছে

1. **Packed GUNW mask decoding:** `mask`-কে আর decimal three-digit পূর্ণ value হিসেবে decode করা হয় না। Actual `uint32` রাখা হয়; WRS/water code low byte থেকে, reference/secondary exception bytes আলাদা, bit 24 আলাদা করে পরীক্ষা হয়।
2. **Precision quarantine:** `2^24` বা তার বেশি mask value strict acceptance থেকে বাদ থাকে; অনুমান করে byte “repair” করা হয় না। ISCE3-এর float32-geocoding issue থাকার কারণে upstream mask reliability এখনও independent evidence বা corrected product দিয়ে যাচাই করতে হবে।
3. **Native dtype preservation:** remote COG window পড়ার সময় layer-কে আগেভাগে `float64` করা হয় না; `uint32` mask conversion/rint থেকে রক্ষা পায়। Non-`uint32` mask পাওয়া গেলে সেটিকে unresolved দেখিয়ে zero pixel strict gate পার করতে দেওয়া হয়।
4. **Ionosphere correction:** correction-এর জন্য phase screen এবং matching `ionospherePhaseScreenUncertainty` দুটিই লাগবে। Missing/invalid screen বা uncertainty-তে zero substitute করে pixel qualify করা হয় না। uncertainty-এর জন্য মনগড়া numeric threshold যোগ করা হয়নি।
5. **UI:** mask dtype, decode status, upstream encoding independently verified কি না, precision quarantine count এবং screen uncertainty-এর valid/gate counts দেখা যায়।
6. **Regression tests:** mask rounding, packed flags, unknown mask dtype, missing uncertainty, invalid uncertainty samples—এই পরিস্থিতির tests যোগ করা হয়েছে।

## Validation

- Backend full test suite: **226 passed**.
- Python syntax compilation: passed (`python -m compileall -q api_service src tests`).
- দুইটি বিদ্যমান, non-fatal NumPy warning troposphere correlation test-এ থাকে: correlation undefined হলে NumPy division warning দেয়।
- Frontend local production build: এই environment-এ verify করা যায়নি। `npm run build` চালালে `google.maps` এবং `vite/client` type definition পাওয়া যায়নি; প্রয়োজনীয় npm package cache-এ ছিল না এবং offline install সম্পূর্ণ করা যায়নি। Vercel-এর connected build log-এ production build যাচাই করতে হবে।

## Deploy করার আগে মনে রাখবে

- ZIP extract করে repository root-এ file merge/replace করবে। ZIP ফাইলটিকে নিজেই repository-তে upload করলে deploy হবে না।
- `.env`, NASA Earthdata token, Firebase private configuration বা অন্য credential ZIP/GitHub-এ যোগ করবে না। বর্তমান Render/Vercel environment variables যেমন আছে, সেভাবেই রাখবে।
- GitHub-এ commit/push করার পরে Render backend এবং Vercel frontend-এর deployment শেষ হলে একই AOI/গ্রানুলে quality analysis আবার চালাবে।
- নতুন code বেশি pixel force করে pass করাবে না। যদি source product upstream-এ float32 mask bug-এ আক্রান্ত হয়, সঠিকভাবে regenerated source product/independent validity evidence দরকার হতে পারে।

## Deploy ধাপ

1. Full-project ZIP চাইলে extract করে সব content repository root-এ merge করবে; patch ZIP চাইলে শুধুমাত্র তার `Backend/`, `Frontend/` এবং README paths overlay করবে।
2. GitHub-এ পরিবর্তিত source files commit ও push করবে। Secrets বা ZIP archive নিজে commit করবে না।
3. Render-এর বর্তমান connected service-এ **Manual Deploy → Deploy latest commit** চালাবে, যদি auto deploy না হয়। বিদ্যমান root-directory/environment settings অকারণে বদলাবে না।
4. Vercel-এ বর্তমান project deploy হবে; Vite root directory `Frontend` এবং build command `npm run build`, output `dist`—যদি বর্তমান Vercel setting এভাবেই configured থাকে।
5. UI-তে phase job, exact granule, companion layer job IDs (uncertainty-সহ), তারপর `Analyze aligned AOI quality gates` চালাবে.
6. Result-এ `Mask format & scientific safety`, uncertainty valid count এবং warnings দেখবে। `jointly qualified` কম হলে এটিকে success displacement হিসেবে উপস্থাপন করবে না।

## বৈজ্ঞানিক interpretation

- Native source HDF5 mask এবং Harmony COG-এর 1:1 equality transfer correctness দেখায়; upstream encoding correctness নিশ্চিত করে না।
- এই patch landslide detection/prediction বা operational warning চালু করে না। বর্তমান live AOI output এখনও phase-domain quality diagnostic; LOS displacement এখনও হিসাব করা হয়নি।
- Exact `uint32` mask, screen এবং uncertainty পাওয়া গেলেও stable reference, verified source metadata এবং পর্যাপ্ত quality-qualified spatial coverage ছাড়া displacement report করা যাবে না।

## Official technical reference

- ASF NISAR GUNW guide: https://nisar-docs.asf.alaska.edu/gunw/
- ISCE3 mask-geocoding precision fix: https://github.com/isce-framework/isce3/pull/381
- ISCE3 polarization-specific validDataMask change: https://github.com/isce-framework/isce3/pull/379
