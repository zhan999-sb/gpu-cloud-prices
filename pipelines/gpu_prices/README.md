# GPU price index ? provider expansion, phase 1

Python 3.12 standard library only; pytest is the test runner. All commands below run from the repository root. Collection and builds do not read credentials, change network settings, or deploy.

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m pipelines.gpu_prices.run collect
python -m pipelines.gpu_prices.run build --out site_i18n_preview --site-base-url https://gpu-cloud-prices.pages.dev
python -m pipelines.gpu_prices.run audit --out site_i18n_preview
python -m pytest tests/test_gpu_prices.py -q -p no:cacheprovider --basetemp=pipelines/gpu_prices/runtime/test_run_1
python -m pytest tests/test_gpu_prices.py -q -p no:cacheprovider --basetemp=pipelines/gpu_prices/runtime/test_run_2
```

Use a fresh `--basetemp` path for each test invocation: the repository pytest hook otherwise writes outside this pipeline. Tests block real HTTP and subprocess execution and use small hand-made fixtures. The basetemp directory must be pipeline-owned because pytest removes an existing basetemp.

Defaults are `pipelines/gpu_prices/runtime/gpu_prices.sqlite` and `site_i18n_preview/`. On first creation of the default database, the existing `data/gpu_prices.sqlite` is imported through SQLite's read-only backup API. The original database is not modified. The additive schema migration and transactional removal of old CPU/RAM NOT NULL constraints retain every original key, date and value. Unknown historical fields stay null. Unknown regions use an internal empty SQL sentinel to preserve upsert uniqueness and are restored to null in every public Offer/export.

Enabled collectors: Azure, Linode/Akamai, Scaleway, Verda/DataCrunch. `collect` collects all of them and commits each validated provider batch atomically. An individual failure is recorded and does not stop the other providers. Exit code 1 means at least one provider failed. A targeted retry is available, for example `collect --provider linode --provider verda`. `--limit-pages N` limits each paginated catalog resource and marks limited coverage. No partial batch is saved on a failed provider.

Only verified tier A APIs are enabled. B public-page adapters remain disabled in this phase; if enabled later they must be fetched no more than daily. B and C rows may be displayed but cannot enter JSON exports, including exported statistics/history. D providers have no collector: Vast.ai and Together are link-only cards. The registry is `providers/registry.py`; endpoint evidence and open P2 questions are in `PROVIDER_VERIFICATION.md`. The 37-provider policy source is `docs/provider-specs-2026-11-12.md`.

`providers/curated_mappings.json` contains exact SKU mappings with source URLs and review date. Unknown GPU SKUs, changed GPU counts/types, currency changes, inconsistent paging totals and missing required fields fail closed. Source catalog data never implies available stock. Linode joins the explicit availability feed to GPU-capable regions, applying regional hourly price overrides. Scaleway uses complete `/instance/server/` GPU hardware rows and excludes legacy aliases, integrated Apple cores, bare-metal/setup/monthly products. Verda retains unknown regions, raw CPU descriptions, RAM and VRAM descriptions without inventing vCPU or GiB semantics; it never queries authenticated availability endpoints.

Offer provenance includes price scope, fraction/raw GPU name, tax basis, starting-price flag, billing unit/minimum duration, effective interval, availability/time, tier/export flag and original currency/price plus FX rate/date/source. `price_per_hour` is an hourly amount in the stated scope. Instance rates divide by GPU count; per-GPU and GPU-component rates do not. GPU-component-only rates stay outside headline comparisons. Monthly billing units are rejected, never divided by 730. Minute/second rates are explicitly normalized and the source billing unit remains visible.

ECB reference rates come only from the approved daily XML URL. EUR to USD multiplies by the USD rate; CNY to USD multiplies by USD/CNY rates from the same dated snapshot. `runtime/ecb_rates.json` is replaced only by a valid newer/same-date snapshot. Refresh failure keeps the old date, and no cache means native-currency display without a USD comparison. Converted rows show original hourly currency/amount and ECB rate/date. A failed exchange-rate refresh is reported independently from successful catalog fetches.

Headline statistics require USD, exact prices (not advertised starting prices), whole GPUs, standard regions, a current effective interval, and resolved instance/per-GPU scope. Tax-included rows are separately labelled and excluded from headline/history minima; excluded and unknown tax bases are labelled. GPU variants and memory sizes retain distinct model names. Historical ambiguous model names remain separate from newly explicit variants. Availability badges describe observation, not a quota or deployment guarantee.

Each provider has its own collection status and complete-snapshot time interval. A failed/latest earlier-day fetch makes it stale and removes its rows from today's comparisons. A complete snapshot supersedes absent same-day keys without deleting historical rows. Today's history is recomputed from that current provider population. Old days are never filled. Headline discount, minimum/median/max and regional bars use raw precision before display rounding; price types remain separate.

Every build runs the independent `audit.py` gate against SQLite. It checks tier/export policy, hourly normalization, FX provenance and eligibility, recomputes its own median/statistics/history and verifies JSON, all localized numeric text, titles/descriptions, FAQ JSON-LD and chart geometry. HTML display facts and A-only export facts are audited separately. Corruption fails the build.

Eight complete UTF-8 catalogs are validated before output: English `/`, Simplified Chinese `/zh/`, Japanese `/ja/`, Korean `/ko/`, German `/de/`, French `/fr/`, Spanish `/es/`, Brazilian Portuguese `/pt/`. Model pages have provider/type/region-class filters, provider health, scope/tax/availability labels, source links and hourly/original currency values. Index coverage shows tier, observed model count, last successful fetch and status. Methodology pages explain each provider and the tier policy. Language-neutral A-only JSON downloads live under `/data/`; all language routes retain reciprocal hreflang, canonical, sitemap, structured data and contact disclosure.

Outbound links are reviewed in `links.py`. `AFFILIATE_VAST_REF` takes precedence at build time; when absent, `config/affiliates.json` is read without modification. An explicitly empty environment variable disables the referral. IDs are URL-encoded, and affiliate links carry sponsored/nofollow/noopener. Vast/Together prices never enter any comparison or export.

The preview can be served with `python -m http.server 8000 --directory site_i18n_preview`. Manifest cleanup touches only generated owned paths and preserves unrelated files. Files are replaced atomically; the whole directory is not a transactional deployment. This phase does not write `site/` or invoke deployment. The existing explicit deploy command remains the operator's separate responsibility and still targets the operator-built `site/` directory.
