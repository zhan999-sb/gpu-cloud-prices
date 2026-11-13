# GPU price index — API and page-provider integration

## b25 weekly price reports

Each build derives `/reports/<ISO-year>-W<week>/` and `/reports/` in all eight
languages from the retained SQLite observations. The current week uses today's
UTC snapshot; previous weeks use their last retained observation day. No dates
or prices are interpolated. The homepage links to the current week's report.

Current popular-model minima keep exact variants and on-demand/Spot separate.
Weekly comparisons require observations exactly seven calendar days apart in
the same provider/model/allocation/region/type/currency/scope/tax/billing and
conditions group. Each model/type selects its cheapest current matched group,
then compares that group's previous minimum. The unrestricted current minimum
is shown separately. Archived rows are daily group minima, not full catalogs or
the same machine. Historical offer counts are explicitly retained-group counts.
Original currency/FX, tax, tier, source, billing and marketplace qualifiers stay
visible. Listing differences describe observed coverage, never confirmed
provider launches, closures, stock or delistings; an initial import is not news.

A report needs at least three supported popular model/type comparisons to pass
the thin-content guard. Thin reports remain readable with explicit noindex and
are absent from all sitemaps. The index becomes indexable when a report
qualifies. Reports with observed popular-GPU prices enter the existing
trailing-month, fifty-item RSS feed with a week-stable GUID, including thin
reports whose descriptions explicitly explain the missing history.
Article modification dates are visible observation timestamps; an unrecorded
publication date is omitted. The index has breadcrumbs without Article markup.
`report_audit.py` independently
reconstructs the evidence from the SQL audit view, including table selection,
numbers, qualifiers, links, visibility, indexability, archives and feed entries.

Local acceptance (single process, fresh temporary directories, no collection):

```powershell
python -X utf8 -B pipelines/gpu_prices/runtime/b25/verify.py focused6 focused7 full1 build2 audit2
```

This writes the isolated preview to `runtime/b25/preview/` with canonical origin
`https://gpu-cloud-prices.pages.dev`, using a SQLite backup of b24's retained
observations. Phase logs and source/preservation hashes are retained in b25.

## b20 search intent, Dataset discovery and IndexNow

RTX 4090 desktop pages compare per-GPU hourly rates with the minimum billable
instance cost. H100 pages explicitly distinguish PCIe 80GB, SXM 80GB and NVL
94GB; A100 pages distinguish 40/80GB and PCIe/SXM/unspecified interfaces. Exact
model URLs remain separate. Their offer lists have separate on-demand and Spot
blocks, including continuation pages. Interruptible and reserved offers retain
their own separately labelled blocks as well. All eight catalogs supply the new copy.
Cards show the instance hourly total and, when documented, its initial minimum
billing duration/charge. Missing allocations, component-only CPU/RAM charges,
tax, fees, stock, promotional floors and display-only tiers retain their labels.

Each localized `/data/` has Dataset JSON-LD matching its visible name,
description, compilation creator, licence scope, observed date interval and
Tier A JSON download links. Temporal coverage uses retained export history and
observation dates, never a build date or an invented starting date. CC BY 4.0
applies only to our rights in the compilation, excluding upstream rights.

Builds generate the public IndexNow ownership key file and
`indexnow-manifest.json`. SHA-256 covers each sitemap HTML page, excluding only
the explicit `/built` clock element. Price, source/observation time, qualifiers,
styles and other page content affect the hash. Noindex continuations, embeds,
JSON, assets and the key file are not submitted. Removal from the indexable set
queues the old URL too. Independent auditing reconstructs every hash.

After a successful deployment, CI runs `python -m ci.indexnow --manifest
ci/runtime/site/indexnow-manifest.json --receipt ci/runtime/deploy-receipt.sqlite`
before caching the receipt. SQLite tables retain deployed hashes, pending
changed/deleted URLs and attempted URL counts per UTC day. One POST goes only to
`https://api.indexnow.org/indexnow`; at most 10,000 URLs may be attempted in a
day, including failed requests. Failures/overflow remain queued for a later
successful deployment. No pending changes means no HTTP request. A missing
baseline seeds hashes without a ping and conservatively closes that UTC day's
unknown budget; it never triggers a full-site resubmission. Receipt replacement
preserves these tables. CLI errors and HTTP failures do not fail the deploy job.
IndexNow receipt/acceptance does not establish indexing or ranking.

## b18 embeds, public data and daily observations

`/embed/<model-slug>.html` is a self-contained, script-free, noindex widget below
15,000 UTF-8 bytes. Locale variants use `/embed/<locale>/<model-slug>.html`, so
the single generated `_headers` rule for `/embed/*` covers all eight languages
without changing other routes. Its CSP permits framing and authorizes the one
inline stylesheet by SHA-256, with no external resources or JavaScript. The
attribution link uses `nofollow noopener`; it is not an affiliate link. Each
model page has a readonly, keyboard-copyable iframe snippet and preview link.
Widgets redistribute only current, eligible, export-allowed Tier A quotes;
Spot, region, tax, availability, FX and community-marketplace qualifiers remain
visible. Empty, stale or B/C-only models show an unavailable message.

`/data/` and its seven localized equivalents document language-neutral Tier A
exports, field semantics, refresh limits, observation/build timestamps and
attribution. `/data/latest.json` lists the existing export URLs and their exact
timestamps; it exists with an empty list when no models are exportable. CC BY
4.0 applies only to our rights in compiled data and excludes provider content,
trademarks and other third-party rights. Credit the index, link the data page
and licence, retain sources/dates and identify changes.

`/feed.xml` and each locale's `/feed.xml` contain at most 50 RSS items from the
trailing 30 UTC days. Tier A eligible history supplies adjacent-day changes:
an observed daily-minimum drop of at least 15%, or a new low in retained history
for a reviewed popular family. Exact model variants and price types stay
separate. A new provider means its first retained eligible observation after
a prior-day baseline; initial imports are not announcements. Coverage changes
can change minima. GUIDs, dates, ordering and content are deterministic for an
identical observation snapshot; rebuild timestamps never create news.

Homepage model cards show a price-drop badge only when today's selected minimum
for the same price type is at least 10% below the median of the preceding seven
daily minima. All seven preceding calendar days and today must be observed.
This uses the existing displayed-history eligibility, including labelled B/C
display-only quotes; those quotes never enter widgets, exports or feeds.

`audience_audit.py` independently derives prices/events/badges from the SQL
audit snapshot and checks all locale outputs, exact attribution/snippets,
licence boundaries, export timestamps, header scope, CSS hash, script/resource
absence, size, sitemap exclusion and manifest cleanup. It never imports the
feature renderer. Offline regression and adversarial tests are in
`tests/test_gpu_audience.py`. Browser QA helpers and retained reports are in
`runtime/b18/`; they introduce no generator dependencies.

```powershell
python -B pipelines/gpu_prices/runtime/b18/run_checks.py tests3
python -B pipelines/gpu_prices/runtime/b18/run_checks.py tests4
python -B pipelines/gpu_prices/runtime/b18/run_checks.py build
python -B pipelines/gpu_prices/runtime/b18/run_checks.py audit
```

The wrapper replays b17's database at `2026-11-12T22:16:41.760705Z` into
`runtime/b18/preview/`, with `--site-base-url https://gpu-cloud-prices.pages.dev`.
It never collects, pushes or deploys. Use new test phase names for reruns.
Cloudflare header syntax: <https://developers.cloudflare.com/pages/configuration/headers/>.
The framing directive requires HTTP headers, not a meta element:
<https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/frame-ancestors>.
Licence text: <https://creativecommons.org/licenses/by/4.0/>.

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

## b16 search landing pages and technical SEO

Existing `/<model-slug>/` routes remain the search targets in all eight locales.
Their title/H1 uses the eligible observation date, not today's build clock.
Build-time introductions give the unweighted eligible USD minimum/median,
offer/provider counts and on-demand/Spot counts. The fourth visible FAQ describes
the observed daily-minimum change inside a trailing 30-day window, with actual
endpoints and coverage warnings; fewer than two observed days produces an
explicit unavailable answer. No missing days or prices are estimated. A replay
does not include future-dated observations or history after its observation day.

The landing page shows the first 12 price-ordered records. Complete current
lists continue at `/<model-slug>/offers/2/`, etc., with original values, labels,
source links and static HTML. Every continuation is noindex and linked from its
model page; its filters apply to that page's records. Both landing pages and
continuations have a hard 300,000-byte UTF-8 HTML build/audit limit. The audit
reconstructs the complete partition from SQLite and rejects missing, duplicated
or mislabelled cards. This supersedes the b15 single-document model list.

Tracked providers have `/providers/<provider>/` pages (`clore-ai` for Clore.ai),
including coverage/tier/source notes, current and eligible counts per model,
cheapest eligible quotes, original offer cards and reviewed outbound links.
Only these comparison routes are generated: `/compare/h100-vs-a100/`,
`/compare/a100-vs-l40s/`, `/compare/rtx-4090-vs-rtx-5090/`,
`/compare/h100-vs-h200/`. Prices retain exact configuration rows. Reference
hardware specifications are curated in `search_specs.py`, with official URLs
shown on each comparison. Sparse Tensor Core peaks and GeForce FP32 accumulation
are explicit; the H200 preliminary-specification qualifier is retained. There
are no workload benchmarks or subjective performance/rental recommendations.

Indexing requires at least three **current eligible** offers, not merely three
display records. Comparisons additionally require three per side. Home and
methodology use the site's eligible population; localized 404s and all offer
continuations are always noindex. Every noindex page is excluded from the root
and per-language sitemaps. `robots.txt` advertises all nine sitemaps. Every HTML
route has self-canonical URLs, eight reciprocal hreflang alternatives plus
x-default, visible breadcrumbs with BreadcrumbList JSON-LD, and social metadata.
Each model gets a 1200x630 PNG, shared across locale equivalents with localized
meta text/alt text. The PNG writer uses an original bitmap font, struct and zlib:
no Pillow, converter, font installation or new CI dependency is needed.

FAQPage data must match the visible questions and answers. Dataset markup only
describes A/export-allowed JSON data, including A-only source/region/summary and
actual currency units; B/C-only pages emit WebPage instead. Source/price/tax,
community-marketplace and stablecoin/platform-fee disclosures remain visible.
`search_audit.py` uses the main audit's independent SQL snapshot, rather than
renderer statistics. It checks search copy, tables/labels, official references,
all links/canonicals/hreflang, exact sitemap membership and lastmod, pagination,
and PNG content/CRCs/dimensions; it also checks thin pages that are noindex.

Local reproducible b16 checks (fresh names for test temp directories):

```powershell
python -B pipelines/gpu_prices/runtime/b16/run_checks.py tests1
python -B pipelines/gpu_prices/runtime/b16/run_checks.py tests2
python -B pipelines/gpu_prices/runtime/b16/run_checks.py build
python -B pipelines/gpu_prices/runtime/b16/run_checks.py audit
```

The build wrapper uses `--site-base-url https://gpu-cloud-prices.pages.dev` and
replays `--as-of 2026-11-12T17:43:29.976595Z` from the read-only database. This is
an existing observation snapshot, not a new collection. Logs, final source/data
hashes, size/count evidence and the delivery report stay in `runtime/b16/`.
No push or deployment is part of these checks.

### Evergreen guides (b23)

`/guides/` and five article routes are generated in all eight languages.
The footer links to the guide index; the articles link to their exact-model,
provider and comparison pages. Visible articles carry Article and hierarchical
BreadcrumbList JSON-LD. There is no synthetic FAQ markup. Root and localized
sitemaps include only articles with sufficient current eligible observations.

The same whole-GPU, USD, current-day, effective-date and provider-health rules
apply. Standard/unspecified regions and excluded/unknown tax bases qualify;
fractional, component-only, included-tax, stale and from-price records do not.
Price types remain separate. The per-GB table uses only explicit decimal GB
capacity in the canonical database read view; unknown capacity or GiB-only
unknown names have no inferred denominator. Popular models appear first, with
the cheapest observed quote for each exact model/type and its full offer card.

Spot discounts use matching model/provider/SKU/region/count/fraction/scope/
currency/tax/CPU/RAM/minimum-duration pairs. Compute each pair's percentage
reduction, then take its model's median. Price medians use that same matched
cohort; their ratio is not the discount statistic. Negative discounts remain
negative. No matches means unavailable, not an estimated or regional-floor
discount. At least three matched pairs are required to index the Spot guide.
Other guides require three relevant eligible offers; the memory guide also
requires the cited model references. Missing-data articles stay available with
noindex, outside every sitemap and the IndexNow manifest.

Writable database opens add `guide_reference_models` separately from market
observations. These are the official model card's nominal parameter counts,
not collected rental data or measured runtime memory. Build-time weight-storage
arithmetic reads those rows: nominal parameter count times bytes per weight.
The page distinguishes ideal weight-only decimal GB from runtime requirements,
including quantization metadata, KV cache, activations and concurrency. Existing
read-only databases without the additive table are supported without a guessed
reference fallback. To populate references, open an isolated DB copy with
`PriceStore` once before a read-only build. Original observations need no edits.

`guide_audit.py` selects and recomputes facts from the independent SQL snapshot.
It does not import the guide renderer, statistics or store helpers. It verifies
reviewed reference rows, every table cell and numeric marker, complete offer
cards, honesty labels, visible explanatory text, official sources, internal
links, Article metadata, breadcrumbs, thin-content decisions and sitemap
membership. The source list contains official provider/model documentation;
no throughput or interruption-frequency estimate is inferred from price data.

Task-local verification is in `runtime/b23/`. Its pytest entry point runs in a
single process with plugin autoload/cache disabled and fresh temporary roots.
On this restricted Windows token, it replaces only newly requested private
temporary-directory modes with inherited workspace ACLs for the local test
process. It does not edit existing ACLs or machine settings. Use fresh phase
names with `verify.py focusedN` or `verify.py testsN`; `build` and `audit` use
the isolated b21 database copy and the requested public canonical origin.
