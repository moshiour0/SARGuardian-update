# SARGuardian — On-demand architecture update (বাংলা)

## এই প্যাকেজে কী বদলানো হয়েছে

1. **Frontend**-এ `Check NISAR archive` অপশন যোগ হয়েছে। ব্যবহারকারী AOI polygon, তারিখ (সর্বোচ্চ 90 দিনের ব্যবধান) এবং একটি product family (`GUNW` অথবা `GOFF`) নির্বাচন করে ASF catalogue-এর metadata search করতে পারেন।
2. নতুন **metadata-only API** ASF Search API-তে NISAR product খোঁজে এবং granule name, available dates, direction/path metadata, `sizeMB` এবং পূর্ণ product ফাইলগুলোর আনুমানিক মোট volume দেখায়।
3. API-তে ডিফল্ট সীমা: AOI সর্বোচ্চ 250 km², সর্বোচ্চ 25 vertices, প্রতি query-তে সর্বোচ্চ 50 listed granules, 90-day window এবং প্রতি observed IP-তে best-effort 6 requests/minute.
4. ফলাফলে স্পষ্টভাবে জানানো হয়: এই ধাপে **কোনো satellite file download হয়নি এবং কোনো movement analysis হয়নি**।
5. পুরোনো `Reference demo` আলাদা রাখা হয়েছে এবং UI-তে বলা হয় যে সেটি precomputed fixture; live NISAR processing নয়।
6. Python API-র জন্য `api_service/README.md`, `render.yaml` ও mock-based tests যোগ হয়েছে; free pipeline architecture-এর বাংলা ব্যাখ্যা `docs/architecture/FREE_ON_DEMAND_PIPELINE_BN.md`-এ রয়েছে।

## এখনো কী বাস্তবায়িত হয়নি

এটি **সম্পূর্ণ live change-detection pipeline নয়**। NASA Harmony-এ job তৈরি/poll করা, selected variable GeoTIFF extraction, AOI raster processing, time-series calculation, job queue, durable job status এবং result cleanup এখনো পরবর্তী ধাপ। এগুলো সংযুক্ত না করে কোনো ফলাফলকে live deformation detection বা alert হিসেবে দেখানো হবে না।

## কেন 30 GB ডাউনলোডের সমস্যার সমাধান এটি?

এই সংস্করণে প্রথমে কেবল ছোট catalogue metadata আসে। তাই product search-এর সময় 30 GB ফাইল ডাউনলোড বা সংরক্ষণ হয় না। API ফলাফল থেকে full-product volume জানা যায় এবং processing stage তৈরির আগে কোন product group ও কতগুলো granule দরকার, তা দেখা যায়।

পরবর্তী processing stage-এ সব `.h5` নামানোর বদলে NASA Harmony-এর NISAR dataset extraction ব্যবহার করে শুধু প্রয়োজনীয় GUNW/GOFF layer(গুলো) নেওয়া হবে। Granule-গুলো ধারাবাহিকভাবে process হবে; AOI-র প্রয়োজনীয় values/summary বের করে intermediate files সঙ্গে সঙ্গে মুছে ফেলা হবে। স্থায়ীভাবে রাখা হবে কেবল ছোট JSON/time-series/QA/preview output।

## Local run

### 1) API

```bash
cd SARGuardian-main/api_service
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

`http://localhost:8000/health` খুলে health check করুন।

### 2) Frontend

```bash
cd SARGuardian-Frontend
cp .env.example .env.local
# .env.local-এ Firebase/Google Maps values এবং VITE_SARGUARDIAN_API_URL=http://localhost:8000 দিন
npm ci
npm run dev
```

ফ্রন্টএন্ডের Google login ও Google Maps কনফিগারেশন আলাদাভাবে লাগবে। Browser-এ `VITE_*` values public; কোনো NASA password/token এখানে দেবেন না।

### 3) Tests

```bash
cd SARGuardian-main/api_service
pip install pytest httpx
pytest -q
```

Backend API-র পাঁচটি mock-based test পাস করা হয়েছে। এখানে live ASF call বা satellite data download করা হয়নি। এই environment-এ npm packages পুনরায় নামানো যায়নি, তাই এই পরিবর্তনের পর frontend production build সফল হয়েছে বলে দাবি করছি না; deploy করার আগে `npm ci && npm run build && npm test` চালিয়ে নিন।

## Free deployment recommendation

- Frontend: Vercel Hobby বা GitHub Pages, তবে Google Maps billing/quota এবং Firebase settings আগে যাচাই করতে হবে।
- Metadata API: `SARGuardian-main/api_service`-কে Render Free web service হিসেবে deploy করা যায়। Render Free idle হলে sleep করে এবং local filesystem স্থায়ী নয়—এই API-র metadata-only কাজের জন্য সেটা গ্রহণযোগ্য, কিন্তু raster processor-এর জন্য নয়।
- Frontend Vercel environment variable `VITE_SARGUARDIAN_API_URL`-এ API origin দিন। API-র `FRONTEND_ORIGINS`-এ আপনার নির্দিষ্ট frontend origin দিন।
- এই metadata API public launch করার আগে platform-level rate limiting লাগবে; code-এর in-memory limiter কেবল best-effort এবং distributed/user-authenticated quota নয়।

## Public demo-র জন্য পরবর্তী ধাপের প্রস্তাবিত limits

- এক request-এ একটিমাত্র product family (GUNW অথবা GOFF)
- সর্বোচ্চ 90 দিনের date range; প্রয়োজন হলে user date range আরও ছোট করবেন
- সর্বোচ্চ 250 km² AOI (challenge/demo default; production science-এ AOI-specific review দরকার)
- একসঙ্গে সর্বোচ্চ 1 heavy analysis; পরেরগুলো queue-তে রাখা
- প্রতি account-এ 1 active job এবং শুরুর পর্যায়ে দৈনিক 3 job-এর প্রস্তাবিত quota
- একসঙ্গে download করা নয়; প্রতি granule/layer ধারাবাহিকভাবে process ও cleanup
- শুধু bounded-size result/presentation preview সংরক্ষণ; full source HDF5 নয়

এগুলো পরিকল্পিত product rules—এগুলো এখনো job queue-তে কার্যকর করা হয়নি এবং কোনো hosting provider-এর guaranteed concurrency নয়।
