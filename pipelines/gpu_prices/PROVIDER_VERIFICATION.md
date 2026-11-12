# Provider expansion: live verification, 2026-11-12

Implementation source: `docs/provider-specs-2026-11-12.md`. Every endpoint below was requested with a real unauthenticated GET through `HttpClient`, with its allowlist, normal TLS verification, retries/backoff and no network changes. Full responses and timestamped evidence remain under `runtime/live/`; offline tests use independent tiny hand-made fixtures.

| Provider | Verified response | Phase 1 decision |
|---|---|---|
| Linode / Akamai | `data`, `page`, `pages`, `results`; types: 75 records, regions: 33, availability: 2475. `page_size=500` verified for all three endpoints, availability spans five pages. A test request at 1000 returned HTTP 400 with `Must be 25-500`; corrected before successful ingestion. | Enabled A. 13 curated GPU SKUs; 286 plan/region records. Exclude VPU devices and regions lacking GPU capability; join explicit availability; apply `region_prices[].hourly`. |
| Scaleway | `products`, `total_count=5672`; six pages at 1000. Exact `price.retail_price.{currency_code,units,nanos}`, `unit_of_measure.{unit,size}`, `properties.hardware.{gpu,cpu,ram}`, `locality.zone`. | Enabled A, 33 complete instance records. GPU minute prices explicitly normalize to hours. Legacy aliases without hardware, Apple cores, bare metal, monthly/setup and non-instance products excluded. |
| Verda / DataCrunch | Top-level array, 84 instance types; 57 GPU types. `gpu.number_of_gpus`, exact descriptions, string `price_per_hour`, `spot_price`, lowercase `currency`. | Enabled A, 114 on-demand/Spot records. No region inferred. Original CPU/RAM/VRAM text retained; unknown vCPU/GiB fields null. No other Verda endpoint queried. |
| Vultr | Two real cursor pages: 100 + 40 plans, `meta.total=140`, `meta.links.next` is a cursor token. `hourly_cost`, `monthly_cost`, `location_cost`, deployment flags exist. Fifteen GPU rows; only three L40S rows have explicit GPU count/type; these have empty locations and regional cost maps. | Disabled A. TODO: verify regional pricing structure and remaining GPU/fraction semantics. No monthly division or GPU-count inference from SKU text. |
| Oracle Cloud | `lastUpdated`, `items` (668), no paging metadata; products include `partNumber`, `displayName`, `metricName`, `serviceCategory`, `currencyCodeLocalizations[].prices[]`. GPU-hour metrics and AI Enterprise software rows coexist. | Disabled A. TODO: prove complete-catalog/pagination contract and curate verified part-number?shape/GPU-count/OCPU-vCPU joins. No GPU-hour price is presented as an instance price. |
| OVHcloud | `catalogId`, `locale`, `plans` (6), `products` (800), `addons` (2802), `planFamilies`. FR locale specifies EUR and taxRate 20. Prices have integer `price`/`tax`, rounded `formattedPrice`, and consumption/blob technical metadata. | Disabled A. TODO: document/verify amount multiplier, exact consumption-unit paths and region-category joins. Rounded display strings alone do not prove an exact monetary multiplier. |
| Vast.ai / Together | No pricing GET performed. Link-only policy from the supplied spec. | D: cards only, no collector/export. Vast referral resolves env first, config fallback second. |

Official API URLs:

- https://api.linode.com/v4/linode/types?page=1&page_size=100
- https://api.linode.com/v4/regions?page=1&page_size=100
- https://api.linode.com/v4/regions/availability?page=1&page_size=100
- https://api.scaleway.com/product-catalog/v2alpha1/public-catalog/products?page=1&page_size=100
- https://api.verda.com/v1/instance-types
- https://api.vultr.com/v2/plans
- https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/
- https://eu.api.ovh.com/1.0/order/catalog/public/cloud?ovhSubsidiary=FR
- https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml

SKU mappings and their per-entry official sources are in `providers/curated_mappings.json`. The Linode pricing page and Scaleway API documentation were also checked; catalog parsing always uses the direct HTTP response rather than search-result prices.

## Final real collection and preview

The first default all-provider collection completed Azure (9 pages, 8013 raw rows, 1330 offers) and Scaleway, but recorded the invalid Linode page-size request and a Verda transport failure. After correcting the Linode parameter using the server error and real 500-row GETs, a targeted real retry completed all P1 providers. Final provider run states are complete. No failed route or older failure is overwritten in `collection_runs`.

| Provider | Final records | Pages |
|---|---:|---:|
| Azure | 1330 | 9 |
| Linode / Akamai | 286 | 7 |
| Scaleway | 33 | 6 |
| Verda / DataCrunch | 114 | 1 |

Preview: **1763 current records, 31 models, 272 HTML pages, 31 A-only JSON exports**. Eight languages each have 34 HTML pages. Build uses `--site-base-url https://gpu-cloud-prices.pages.dev`. The operator-built `site/` was not written or deployed.

Audit: **passed**, 105991 values checked. Final full offline suite: **163 passed in 60.69s** and **163 passed in 63.01s**, each with `-q -p no:cacheprovider` and a fresh pipeline-owned basetemp.

ECB XML requests failed after retries with actual TLS validation, transport and DNS errors. There was no valid cached rate; therefore Scaleway remains in EUR. No date or USD conversion was fabricated. Verda unknown-region records and unconverted EUR records remain visible but cannot drive USD standard-region headline minima.

## Headline on-demand USD / GPU-hour

Minima below obey the whole-GPU, current-effective, standard-region, exact-price and resolved-scope rules. Tax bases are excluded/unknown; tax-included rows are kept separate. These are observed catalog list-price minima, not capacity guarantees or market-wide cheapest claims.

| GPU model | USD / GPU-hour | Provider | Region |
|---|---:|---|---|
| A10 24GB | 3.2 | azure | centralus |
| A100 40GB | 3.399625 | azure | eastus |
| A100 80GB | 4.09625 | azure | eastus |
| A100 SXM4 40GB | ? | No eligible USD offer | ? |
| A100 SXM4 80GB | ? | No eligible USD offer | ? |
| B200 CC SXM6 180GB | ? | No eligible USD offer | ? |
| B200 SXM6 180GB | ? | No eligible USD offer | ? |
| B300 CC SXM6 268GB | ? | No eligible USD offer | ? |
| B300 SXM 288GB | ? | No eligible USD offer | ? |
| B300 SXM6 268GB | ? | No eligible USD offer | ? |
| GB300 SXM6 288GB | ? | No eligible USD offer | ? |
| H100 NVL 94GB | 6.98 | azure | eastus |
| H100 PCIe 80GB | ? | No eligible USD offer | ? |
| H100 SXM 80GB | 12.29 | azure | eastus |
| H200 141GB | 10.6 | azure | eastus2 |
| H200 SXM5 141GB | ? | No eligible USD offer | ? |
| L4 24GB | ? | No eligible USD offer | ? |
| L40S 48GB | ? | No eligible USD offer | ? |
| MI300X 192GB | 6 | azure | eastus2 |
| P100 16GB | ? | No eligible USD offer | ? |
| Quadro RTX 6000 24GB | 1.5 | linode | ap-south |
| RTX 4000 Ada 20GB | 0.52 | linode | ap-south |
| RTX 6000 Ada 48GB | ? | No eligible USD offer | ? |
| RTX A6000 48GB | ? | No eligible USD offer | ? |
| RTX PRO 6000 96GB | ? | No eligible USD offer | ? |
| RTX PRO 6000 Blackwell 96GB | ? | No eligible USD offer | ? |
| RTX PRO 6000 CC 96GB | ? | No eligible USD offer | ? |
| Radeon Pro V710 24GB | 2 | azure | eastus |
| T4 16GB | 0.526 | azure | eastus |
| V100 16GB | 3.06 | azure | eastus |
| V100 32GB | 2.754 | azure | eastus |

## Remaining verification limits

- ECB daily XML unavailable after retries (TLS validation/transport/DNS failures); no valid local FX cache. Scaleway displayed in EUR and excluded from USD headlines.
- Verda provides no verified region and stock feed; CPU core and RAM unit semantics remain unverified. Raw descriptions are retained; unknown fields stay null.
- Vultr, Oracle, OVHcloud remain disabled pending the contract details in PROVIDER_VERIFICATION.md.
- Real browser interaction/visual QA not completed: browser access host unavailable and native Chrome access denied by automatic approval. Static HTML, numeric audit and tests passed.

Machine-readable evidence: `runtime/PHASE1_VERIFICATION.json`; complete command outputs: `runtime/collection_phase1.json`, `runtime/collection_retry_p1.json`, `runtime/build_verified.json`, `runtime/pytest_delivery_1.txt`, `runtime/pytest_delivery_2.txt`.
