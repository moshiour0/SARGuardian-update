# SARGuardian: storage-first, on-demand pipeline (Bangla)

## লক্ষ্য

প্রত্যেক ব্যবহারকারীর অনুরোধে সব NISAR পণ্য ডাউনলোড/সংরক্ষণ করা নয়। প্রথমে ASF-এর ক্যাটালগ থেকে **metadata only** খুঁজে পাওয়া, মোট সম্ভাব্য raw volume দেখানো, তারপর অনুমোদিত সীমার মধ্যে অল্প কিছু layer নিয়ে asynchronous processing করা। স্থায়ী স্টোরেজে মূল HDF5 রাখা হবে না।

## ধাপে ধাপে প্রস্তাবিত architecture

1. **Static frontend (Vercel/GitHub Pages):** মানচিত্র, AOI polygon, একক product type (`GUNW` অথবা `GOFF`), সর্বোচ্চ 90 দিনের সময়সীমা।
2. **Metadata preflight API:** ASF Search API-তে `platform=NISAR`, chosen `processingLevel`, AOI polygon এবং date range দিয়ে query। কেবল granule নাম, সময়, orbit/direction, processing level ও `sizeMB` ফেরত দেয়। এই ধাপে satellite product download হয় না।
3. **User confirmation / budget gate:** product count ও full-granule volume দেখিয়ে ব্যবহারকারীকে নিশ্চিত করতে হবে। অতিরিক্ত বড় বা বেশি product-এর অনুরোধ reject/ask-to-narrow হবে।
4. **NASA Harmony extraction:** সমর্থিত GUNW/GOFF product থেকে শুধু প্রয়োজনীয় dataset/layer GeoTIFF হিসেবে বের করা। Harmony ডেটা-রূপান্তরের কাজ NASA-side-এ করে এবং job status/result link দেয়। Exact AOI cropping/shape subsetting ওই product/service-এর বর্তমান capability দিয়ে integration-এর আগে যাচাই করতে হবে; শুধু dataset extraction-কে spatial cropping বলে ধরে নেওয়া যাবে না।
5. **Serial processor, not bulk downloader:** এক সময়ে সর্বোচ্চ একটি granule/layer process। AOI-র দরকারি window/summary বের করে পরের granule-এ যাওয়া। Raw `.h5`/intermediate GeoTIFF `finally` cleanup-এ মুছে ফেলতে হবে। Reference raster প্রয়োজন হলে শুধু AOI-clipped, bounded-size version অস্থায়ীভাবে রাখবে।
6. **Persist small outputs only:** JSON summary, time-series points, QA flags, provenance metadata, downsampled preview/COG tile—যা দরকার তাই রাখা। User-specific files স্বল্প সময় (যেমন 24 ঘণ্টা) পরে expire করা। Product HDF5 স্থায়ীভাবে cache করা যাবে না।
7. **Show scientifically valid result:** acquisition time, publication/processing time (যদি জানা যায়), last catalogue check, product maturity, units, QA, uncertainty, and “no data / insufficient data” states। নতুন product না এলে polling refresh হবে, কিন্তু fake live animation থাকবে না।

## কেন 90 দিনের সীমা একাই যথেষ্ট নয়?

NISAR L2 granule একটি বড় swath cover করতে পারে। AOI ছোট এবং 3 মাসের window হলেও, full HDF5 granule download করলে ফাইল কয়েকশ MB বা তারও বেশি হতে পারে। তাই date-range limit দরকার, কিন্তু storage savings-এর বড় অংশ আসে **metadata-first + dataset extraction + serial processing + immediate cleanup** থেকে। Harmony-এর বর্তমান NISAR guide-এ GUNW ও GOFF-সহ কিছু product-এর individual 2-D datasets GeoTIFF-এ extract করার service বর্ণিত আছে। এটি একবারে পুরো HDF5-এর সব variable নামানোর চেয়ে ভালো, কিন্তু ফাইলের আকার আগে থেকে ছোট হবে—এমন নিশ্চয়তা দেওয়া যায় না।

## 30 GB / 51 GB সমস্যা

বর্তমান গবেষণা repository-এর REPRODUCE_RESULTS.md-এ 16টি GOFF granule-এর জন্য প্রায় 17 GB উল্লেখ আছে; root README full set-এর জন্য 51 GB data product local-এ না রাখার কথা বলে। তাই এই research pipeline-কে সরাসরি public web request handler-এ চালানো উচিত নয়। Web API শুধু search/orchestration করবে; file-scale science processing আলাদা staged pipeline হবে।

## বিনামূল্যের deployment-এর বাস্তব সীমা

- **Vercel:** frontend static hosting-এর জন্য উপযোগী; metadata API-ও lightweight হলে ব্যবহারযোগ্য। Hobby Functions-এর বর্তমান maximum duration 300 seconds এবং memory/CPU 2 GB / 1 vCPU। এগুলো বহু GB HDF5/raster processing করার গ্যারান্টি নয়।
- **Cloudflare Workers Free:** lightweight request orchestration/rate limiting/metadata proxy করতে পারে, কিন্তু Free CPU limit 10 ms per request ও 128 MB memory—এতে Python `h5py`/`rasterio` raster pipeline চালানো উপযুক্ত নয়।
- **Render Free:** metadata-only Python API চালাতে পারে, কিন্তু service idle হলে spin-down হয় এবং filesystem ephemeral। Heavy processing বা persistent intermediate file-এর জায়গা হিসেবে ব্যবহার করা যাবে না।
- **Cloudflare R2:** optional short-lived, small outputs caching-এর জন্য বিবেচনা করা যায় (free allowance সর্বোচ্চ 10 GB-month, usage অনুযায়ী)। 17–51 GB source-products সেখানে স্থায়ীভাবে রাখা পরিকল্পনা নয়।

## একসঙ্গে কতজন user?

কোনো host-এর “maximum concurrent users” একক সংখ্যায় বলা যায় না—API latency, rate limits, dataset count, Harmony queue, browser/device, cache hit rate এবং load test-এর ফলাফলের ওপর নির্ভর করে। Static frontend অনেক দর্শককে serve করতে পারে; ব্যয়বহুল analysis capacity আলাদা সীমাবদ্ধতা। শুরুতে free demo-তে **1 heavy analysis globally at a time**, **3 queued jobs**, **প্রতি user 1 active job**, **প্রতি user দিনে 3 analysis**, **এক request-এ 1 product family**, **max 90 days**, **AOI ≤ 250 km²**, **max 50 granules preflight** প্রস্তাব করা হচ্ছে। এগুলো product-design limits, কোনো hosting provider-এর guaranteed capacity নয়। Real load test ছাড়া নির্দিষ্ট user count প্রতিশ্রুতি দেওয়া যাবে না।

## গুরুত্বপূর্ণ security/operational rules

- Earthdata credentials বা bearer token কখনও `VITE_*` variable, frontend JS, GitHub বা browser local storage-এ রাখবে না। `VITE_` variables browser bundle-এ প্রকাশ্য।
- API rate-limit শুধু frontend-এর button disable করে হবে না; backend/platform স্তরেও limit লাগবে। In-memory limiter horizontal scaling-এ যথেষ্ট নয়।
- Job queue durable না হলে deploy/sleep/restart-এ job হারাতে পারে। Heavy pipeline-এর আগে durable queue/status store ঠিক করতে হবে।
- Data licence/citation, product maturity (`BETA`, `PROVISIONAL`, validated) এবং processing version per result রাখতে হবে। Product maturity মিশিয়ে time series করলে explicit warning দেখাবে।
- Full product deletion job সফল/ব্যর্থ/timeout সব অবস্থায় `finally` cleanup-এ হতে হবে। Provider-side Harmony result expiry-ও কাজে লাগাতে হবে; result URLs-এর মেয়াদ থাকতে পারে।

## বর্তমান codebase-এর অবস্থা

- `SARGuardian-Frontend` Vite/React frontend-এ বর্তমান landslide adapter fixture ব্যবহার করে; তা live NISAR fetch/process করে না।
- `SARGuardian-main` Python repository গবেষণার reproducibility CLI; `.h5` granule download ও local filesystem-এ রাখা ধরে নেয়। `requirements.txt`-এ `h5py`, `rasterio`, `numpy`-এর মতো ভারী geospatial dependency আছে।
- যুক্ত করা `api_service` কেবল metadata preflight-এর জন্য। এটি deliberate first phase—live product downloads/measurements নেই।
- পরবর্তী ধাপ: authentication strategy, Harmony collection/job integration, durable job queue, bounded AOI output, and science validation। ঐ অংশে credentials/hosting choices নিশ্চিত না করে “real-time change analysis complete” দাবি করা যাবে না।

## Official references checked (10 October 2026)

- NISAR data availability and nominal latency: https://nisar-docs.asf.alaska.edu/availability-overview/
- NISAR Harmony dataset extraction capabilities: https://nisar-docs.asf.alaska.edu/harmony/
- Harmony API/jobs/temporary results: https://harmony.earthdata.nasa.gov/docs
- Vercel current platform limits: https://vercel.com/docs/limits
- Vercel Hobby plan usage: https://vercel.com/docs/plans/hobby
- Cloudflare Workers limits: https://developers.cloudflare.com/workers/platform/limits/
- Cloudflare R2 pricing/free allowance: https://developers.cloudflare.com/r2/pricing/
- Render free-service limits: https://render.com/docs/free
