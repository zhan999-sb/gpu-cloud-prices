# GPU price index — API and page-provider integration

Python 3.12 standard library only; pytest is the test runner. All commands below run from the repository root. Collection and builds do not read credentials, change network settings, or deploy.

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m pipelines.gpu_prices.run collect
python -m pipelines.gpu_prices.run build --out pipelines/gpu_prices/site_i18n_preview --site-base-url https://gpu-cloud-prices.pages.dev
python -m pipelines.gpu_prices.run audit --out pipelines/gpu_prices/site_i18n_preview
python -B -m pytest --noconftest tests/test_gpu_prices.py tests/test_gpu_provider_pages.py tests/test_gpu_new_sources.py tests/test_gpu_polish.py -q -p no:cacheprovider --basetemp=pipelines/gpu_prices/runtime/test_run_1
python -B -m pytest --noconftest tests/test_gpu_prices.py tests/test_gpu_provider_pages.py tests/test_gpu_new_sources.py tests/test_gpu_polish.py -q -p no:cacheprovider --basetemp=pipelines/gpu_prices/runtime/test_run_2
```

Use a fresh `--basetemp` path for each test invocation, disable plugin autoload with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, and use `--noconftest`: the repository pytest hook otherwise writes outside this pipeline. Tests block real HTTP and subprocess execution and use API fixtures plus captured, trimmed official HTML. The basetemp directory must be pipeline-owned because pytest removes an existing basetemp.

Defaults are `pipelines/gpu_prices/runtime/gpu_prices.sqlite` and `site_i18n_preview/`. On first creation of the default database, the existing `data/gpu_prices.sqlite` is imported through SQLite's read-only backup API. The original database is not modified. The additive schema migration and transactional removal of old CPU/RAM NOT NULL constraints retain every original key, date and value. Unknown historical fields stay null. Unknown regions use an internal empty SQL sentinel to preserve upsert uniqueness and are restored to null in every public Offer/export.

Enabled collectors: Clore.ai, AnyGPU 算力云, 智星云, Azure, Linode/Akamai, Scaleway, Verda/DataCrunch, Featurize, Nebius, Lambda, Paperspace and Baidu GPU. `collect` visits all enabled sources and commits each validated provider batch atomically. An individual failure is recorded and does not stop the other providers. Exit code 1 means at least one provider failed. A targeted collection is available, for example `collect --provider featurize --provider baidu_gpu`. `--limit-pages N` limits each paginated catalog resource and marks limited coverage. No partial batch is saved on a failed provider.

Tier A APIs may be exported. Nebius and Lambda are B; Featurize, Paperspace and Baidu GPU are C. All B/C rows are display only and excluded from JSON, including exported statistics/history. Featurize agreement 7.1/7.3 restricts reuse of site materials; the integration task's C classification supersedes the old spec's provisional B. RunPod, DigitalOcean GPU, Salad, Thunder Compute and Massed Compute returned 403 to ordinary requests and remain link-only cards with no collector or HTTP allowlist entry. Vast.ai and Together remain D and link only. The registry is `providers/registry.py`; the research source is `docs/provider-specs-2026-11-12.md`.

Clore and every page provider have a rolling 60-minute request budget in SQLite `meta` (`page_last_fetch:<provider>`). A transaction reserves the time before network IO, including failed attempts, and serializes overlapping workflows. A newly reserved request invalidates only `saved_fingerprint`, forcing the existing data-branch save gate to persist the budget even when prices are unchanged or the fetch fails. Price fingerprints and deployment receipts remain unchanged, so this does not force a deployment. Requests within the hour are skipped without changing observations or provider runs. Page HTTP requests do not retry automatically, so a workflow every 15 minutes cannot exceed one request per provider per hour. Other API retry/backoff behavior is unchanged. No credentials, cookies, browser impersonation or 403 bypass is used.

Featurize gets `gpu_count=1` only when visible text explicitly states per-card rental; its current fixture does not, so the count and per-GPU price remain null and the instance price is displayed. Nebius is labelled “GPU only; CPU/RAM billed separately” and excluded from headlines. Lambda's 22 observed instance prices are stored with `effective_from=2026-11-16T00:00:00Z` and do not appear in current offers before that time. Paperspace's conditional prices retain `price_conditions` and `is_from_price`. Baidu retains explicit whole-GPU counts and fractional allocations; CPU core descriptions are not certified vCPU counts. Unknown allocations have no invented per-GPU price. The schema migration preserves old data while permitting null GPU counts/rates, including databases whose previous SQLite migration quoted the table name.

`providers/curated_mappings.json` contains exact SKU mappings with source URLs and review date. Unknown GPU SKUs, changed GPU counts/types, currency changes, inconsistent paging totals and missing required fields fail closed. Source catalog data never implies available stock. Linode joins the explicit availability feed to GPU-capable regions, applying regional hourly price overrides. Scaleway uses complete `/instance/server/` GPU hardware rows and excludes legacy aliases, integrated Apple cores, bare-metal/setup/monthly products. Verda retains unknown regions, raw CPU descriptions, RAM and VRAM descriptions without inventing vCPU or GiB semantics; it never queries authenticated availability endpoints.

Offer provenance includes price scope, fraction/raw GPU name, tax basis, starting-price flag, billing unit/minimum duration, effective interval, availability/time, tier/export flag and original currency/price plus FX rate/date/source. `price_per_hour` is an hourly amount in the stated scope. Instance rates divide by GPU count; per-GPU and GPU-component rates do not. GPU-component-only rates stay outside headline comparisons. Monthly billing units are rejected, never divided by 730. Minute/second rates are explicitly normalized and the source billing unit remains visible.

ECB reference rates come only from the approved daily XML URL. EUR to USD multiplies by the USD rate; CNY to USD multiplies by USD/CNY rates from the same dated snapshot. `runtime/ecb_rates.json` is replaced only by a valid newer/same-date snapshot. Refresh failure keeps the old date, and no cache means native-currency display without a USD comparison. Converted rows show original hourly currency/amount and ECB rate/date. A failed exchange-rate refresh is reported independently from successful catalog fetches.

Headline statistics require USD, exact prices (not advertised starting prices), whole GPUs, standard or unspecified regions, a current effective interval, and resolved instance/per-GPU scope. An unspecified region is eligible and labelled "Region not specified by provider"; it counts as one unspecified-location group. Edge and restricted regions remain outside headlines and history. Tax-included rows are separately labelled and excluded from headline/history minima; excluded and unknown tax bases are labelled, and unknown tax is eligible. GPU variants and memory sizes retain distinct model names. Historical ambiguous model names remain separate from newly explicit variants. Availability badges describe observation, not a quota or deployment guarantee.

Each provider has its own collection status and complete-snapshot time interval. A failed/latest earlier-day fetch makes it stale and removes its rows from today's comparisons. A complete snapshot supersedes absent same-day keys without deleting historical rows. Today's history is recomputed from that current provider population. Old days are never filled. Headline discount, minimum/median/max and regional bars use raw precision before display rounding; price types remain separate.

Every build runs the independent `audit.py` gate against SQLite. It checks tier/export policy, hourly normalization, FX provenance and eligibility, recomputes its own median/statistics/history and verifies JSON, all localized numeric text, titles/descriptions, FAQ JSON-LD and chart geometry. HTML display facts and A-only export facts are audited separately. Corruption fails the build.

Eight complete UTF-8 catalogs are validated before output: English `/`, Simplified Chinese `/zh/`, Japanese `/ja/`, Korean `/ko/`, German `/de/`, French `/fr/`, Spanish `/es/`, Brazilian Portuguese `/pt/`. The compact index opens with the cheapest eligible offer per model across all providers and price types, ordered by raw USD per GPU-hour. Full offer cards follow, capped at 100 with up to 20 reserved fractional offers and links to every uncapped model page. Each card states its GPU count/model, source VRAM, vCPU/RAM where normalized, provider, region, tax/type/scope, instance-hour amount, per-GPU-hour amount, original currency where applicable and checked UTC time. A per-GPU-scoped source is multiplied by GPU count for the instance total; GPU-component-only rates never imply a whole-instance price. Native currencies without FX remain visible, outside USD comparisons, after comparable USD rates. Model/type/count/provider/region filters and GPU-hour/instance-hour sorting are local; without JavaScript all generated cards stay visible. Definitions and collection health are collapsed below prices; region charts, history and FAQ follow the model offers. The footer is retained. Language-neutral A-only JSON downloads live under `/data/`; all language routes retain reciprocal hreflang, canonical, sitemap, structured data and contact disclosure.

Outbound links are reviewed in `links.py`. `AFFILIATE_VAST_REF` takes precedence at build time; when absent, `config/affiliates.json` is read without modification. An explicitly empty environment variable disables the referral. IDs are URL-encoded, and affiliate links carry sponsored/nofollow/noopener. Vast/Together prices never enter any comparison or export.

The preview can be served with `python -m http.server 8000 --directory site_i18n_preview`. Manifest cleanup touches only generated owned paths and preserves unrelated files. Files are replaced atomically; the whole directory is not a transactional deployment. This phase does not write `site/` or invoke deployment. The existing explicit deploy command remains the operator's separate responsibility and still targets the operator-built `site/` directory.

## Production refresh policy

The production schedule runs every 15 minutes. Both workflows create
`ci/runtime` immediately before the pytest step, including on a fresh checkout.
Offline checks include both GPU test modules and `ci/tests`.

`ci/refresh_policy.py` fingerprints the **current** provider snapshot with
canonical sorted JSON and SHA-256. All substantive offer fields participate,
including provider/SKU/region/type, hourly and per-GPU prices, currency, original
price/FX inputs, tier, scope and effective intervals. Observation timestamps are
excluded. Missing offers, failed providers and expired prices alter the current
population and therefore its fingerprint; old history is never hashed as current.

A build and deployment are skipped only when the current fingerprint matches
the last successful deployment and that deployment is less than six hours old.
An unsuccessful build, audit or deploy never advances the deployment receipt.
The deployment records the fingerprint of the actual build snapshot even if it
finishes across UTC midnight. A changed price, missing receipt or six-hour expiry
requires a fresh audited build.

History is saved to `data` only for a fingerprint change or the first save of a
new UTC day. SQLite's `meta` table stores `saved_fingerprint`,
`last_saved_utc_day`, `deployed_fingerprint` and `last_deploy_at`. The existing
branch validator still allows exactly `README.md` and `gpu_prices.sqlite`.
Successful observations can still be saved after later pipeline failures.

A freshness-only deployment need not save the price DB. Its two deployment
metadata keys are also stored in a tiny SQLite receipt restored/saved through
Actions cache. Only a valid newer receipt is merged into the price DB. Cache
eviction, missing/corrupt receipts and future timestamps cause an extra build;
they cannot suppress a required deployment. No metadata JSON is committed to
the data branch. The cache is an efficiency aid, not a price-history store.

## SVG history and partner disclosure

Python 3.12.12 added escapable raw-text handling for `title`/`textarea`
(CPython gh-118350). An older local Python parsed the renderer's nested history
`span` elements, while Python 3.12.16 passed them to the audit as literal text.
History tooltips now contain plain text, with a semantic pointer on the `title`
itself. The audit independently checks the entire date/statistic/price/count
label as well as the exact history table and polyline geometry. Regression tests
force the newer parser behavior even on older Python and reject corrupt tooltip
dates, prices, counts and removed markers.

Surflare uses only the public URL, label and promo code from
`config/affiliates.json`. Its secondary index card follows **both** offer lists;
every localized page also has a compact footer link and adjacent
referral disclosure. Eight catalogs describe owner use, commission disclosure,
the buyer's 5% discount and code, without performance or access claims. The
numeric audit accepts only the exact localized disclosure; changed discount or
promo-code numbers fail. Links use `target="_blank"` and
`rel="sponsored nofollow noopener"`. Partner data never enters price statistics.


## b15 compact listings and canonical models

The b15 layout/naming verification is recorded in `runtime/b15/verification.md`.
The new `tests/test_gpu_polish.py` belongs to the GPU suite. Screenshots and the
32-case browser measurements are under `runtime/ui/`.

`models.py` defines the buyer taxonomy and conservative canonical model names.
Read views apply it to old observations and compressed history without changing
SQLite source records. Reporting loss up to 6.25% is corrected only for reviewed
models; the specified integer-truncated 7 GB RTX 4060/4060 Ti reports map to 8 GB.
Raw model/VRAM fields remain in offer details and exports. Modded capacities,
Laptop, D, PCIe and SXM variants stay distinct. Headline cards are unique per
canonical model. Popular families have a fixed order; other categories use price
order, with 12 GB+ consumers first. Legacy/mining lists use native collapsed
`details` on the index and expand without JavaScript. Model pages retain all
observations and filters retain all current model options.

The summary quotes only eligible current whole-GPU USD offers. It names the exact
canonical model, price type, provider and host region, and labels B/C display
restrictions when applicable. Counts and the update clock use the same eligible
population. The independent audit re-selects each quote, checks every number and
qualifier, verifies canonical uniqueness/category order, and rejects opened
legacy sections or missing raw-source values.

## b14 marketplace integration and storage budget

Clore is tier A, with a persisted rolling hourly budget including failures.
The registered collector uses HttpClient, not the prep adapter's standalone
retry helper. It retains at most five currently available hosts per
(model, GPU count, price type, host country); Laptop variants stay separate.
The offer contains the original server day price, source billing unit and price
basis. The independent audit divides that day price by 24 and by GPU count again.
Individual-host reliability and stablecoin/platform-fee qualifiers accompany
all Clore cards. On-demand and Spot retain separate headline comparisons.

AnyGPU and ai_galaxy are C and display-only. AnyGPU uses normal hourly prices;
member prices remain notes. ai_galaxy is always a promotional floor/from-price,
excluded from headlines. Unknown allocation counts remain null. CNY originals,
dated ECB conversions and raw USD/EUR plus CNY/EUR rates are stored so the audit
can independently recompute the cross-rate. All eight catalogs include labels.
Gpushare docs remains unregistered because the table's billing period is unclear.
The three new outbound links use the exact official destination without query
parameters or invented affiliate contracts.

`offers` holds each provider's most recent observation day. On rollover,
`history_snapshots(provider,day,row_count,payload)` stores zlib-compressed UTF-8
JSON containing one cheapest offer per model/allocation/type/region (currencies
and scopes are kept separate). It represents grouped minima of the latest per-offer daily observations;
it is not an hourly time series or a count of all marketplace hosts. The current
day still displays all retained current offers. History distributions use the
archived group representatives. Clore replaces disappeared host listings within
the day, rather than accumulating SKUs. SQL `offers` alone is no longer the full
history: use PriceStore's history/observation APIs. The independent audit decodes
and rechecks the archive itself, without importing storage helpers.

Before migrating any existing SQLite file, SQLite's backup API creates
`runtime/gpu_prices.backup-<UTC timestamp>.sqlite`. `legacy_archive` also retains
all original offers and collection runs as lossless compressed JSON. Recovery:
`json.loads(zlib.decompress(payload))` for either named archive. The backup keeps
the original schema as well. New collection runs retain only the latest outcome
and latest successful outcome per provider, preserving freshness/health without
unbounded logs. Migration is idempotent and is marked in `meta`.

Run `python -B -m pipelines.gpu_prices.growth --db pipelines/gpu_prices/runtime/gpu_prices.sqlite`
for an offline thirty-day SQLite page-allocation estimate at a 15-minute cadence.
The fixture stress case expands Clore to p4's full response count while treating
every expanded offer as a distinct history group; the optional DB estimate uses
all stored provider populations, including stale/failed providers. Both include
actual SQLite pages/index overhead and deterministic daily price drift. Current
listings and health records are bounded and do not multiply by scheduled runs.
The estimate assumes comparable provider/group cardinality in the next month.
