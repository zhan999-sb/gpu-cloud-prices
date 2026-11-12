# Official pricing page fixtures, observed 2026-11-12

These are trimmed fragments of actual plain GET responses, not reconstructed
tables or prices copied from the provider spec. All fragments are below
200,000 UTF-8 bytes. The SHA-256 of each full response and each retained fixture,
UTC fetch time, request headers, outcome, and request attempt are recorded in
`fetch_manifest.json`. Each provider received exactly one GET in this run.

The request used `User-Agent: Python-urllib/3.12`, `Accept: text/html`, no cookies,
and no authentication. All requests completed without transient network errors.
HTTP 403 sources were stopped immediately according to the supplied spec; there
was no browser impersonation, network-setting change, login, secondary pricing
URL, or undocumented API request. In particular, a 403 is **not** evidence that
the price page is JS-only.

| Module | Outcome | Parsed rows | Fixture bytes |
|---|---|---:|---:|
| `featurize` | parsed | 11 | 66,675 |
| `nebius` | parsed | 14 | 10,749 |
| `lambda_` | parsed | 22 | 17,361 |
| `runpod` | blocked, HTTP 403 | 0 | 882 |
| `digitalocean_gpu` | blocked, HTTP 403 | 0 | 114 |
| `paperspace` | parsed | 13 | 45,614 |
| `salad` | blocked, HTTP 403 | 0 | 874 |
| `thunder` | blocked, HTTP 403 | 0 | 900 |
| `massedcompute` | blocked, HTTP 403 | 0 | 896 |
| `baidu_gpu` | parsed | 31 | 8,641 |

For all five supported sources, parsing the original response and parsing its
trimmed fixture produced identical records at preparation time (91 total rows).
The original full responses were kept in a temporary directory for that check;
they are not required to run the offline tests. Blocked fixtures contain only
the returned error title/headings, with request identifiers and addresses omitted.

The five blocked adapters are explicit fail-closed placeholders. On their saved
error HTML they raise `NotSupported` (a subclass of `ParseError`); arbitrary
invented price tables raise `ParseError`. They do not implement an unverified
price format or manufacture sample rows from the spec. Their tests verify this
behavior, and their concrete-price tests are skipped with the HTTP 403 reason.
No JS-only outcome was observed in this fetch.

## Interpretation and pitfalls

- All modules are independent of the existing registry, schema, store, and HTTP
  code. They perform no network or filesystem access while parsing. Import
  `ParseError` and `NotSupported` from the new `providers.pages` package.
- All B/C rows have `export_allowed=False`. The supplied spec grants none of the
  B providers permission to export. These files do not register or enable any
  production collection task.
- Unknown allocation counts/fractions and regions remain `None`. Missing tax
  evidence is `unknown`; absent explicit inventory is `unknown`. Provenance adds
  `observed_at` and, only for explicit inventory, `availability_observed_at`.
- `gpu_count` is the explicitly stated integer number of whole GPUs;
  `gpu_fraction=1.0` accompanies those rows. For explicitly fractional GPU
  allocations, `gpu_count=None` and `gpu_fraction` is 0.25 or 0.5. Per-GPU billing
  is never evidence for a single-GPU allocation.
- **Featurize:** visible cards report 1 RTX 5090 available and zero for the other
  GPU types at fetch time. This is page-reported inventory, not a live inventory
  API. The 4090 allocation displays exactly `25.2 GB`; it is not rewritten to
  24GB. Its normalized name conservatively retains that observed capacity.
  The 48G variant stays separate. Hydration contains physical-host GPU totals
  and internal host metadata; it is intentionally omitted from the fixture.
- **Nebius:** prices are `gpu_component`, denominated per GPU-hour. GPU count is
  unknown. Spot lower bounds have `is_from_price=True`. Both L40S preemptible
  rows are explicitly excluded from dynamic spot pricing, so they are
  `interruptible`. CPU ranges remain `vcpu=None` with the range in `vcpu_raw`;
  RAM ranges remain intact in `ram_raw`. Contact-sales GB200/GB300 rows are
  explicitly nonnumeric and omitted. The page explicitly excludes applicable
  taxes, including VAT.
- **Lambda:** only Instances tabs are emitted, with `price_scope=per_gpu` and
  explicit counts 1/2/4/8. Each tab's footnote must state the expected future
  date: `effective_from=2026-11-16T00:00:00Z`. These are not November 12 current
  prices. The committed 1-Click Clusters section is retained as a negative
  control. H100 PCIe/SXM, A100 40GB/80GB and HGX variants remain distinct.
- **Paperspace:** only visible hourly GPU compute cards are parsed. The HTML
  retains a hidden H100 `$3.09` price with old A100 specs: it must be ignored.
  `$2.24*` H100 and `$1.15**` A100 require three-year commitments and are
  `reserved` with `is_from_price=True`. A separate explicit footnote quotes
  H100 on-demand at `$5.95/hour` under a special promo; this is also conditional
  and carries `is_from_price=True` plus `price_conditions`. There are 12 visible
  GPU cards and this additional on-demand footnote, hence 13 records. GPU counts
  are not invented from nearby multi-GPU variants. VDI, monthly plans, hidden
  two-GPU cards, platform subscriptions and CPU instances are excluded.
- **Baidu:** all four tables are parsed, selecting the post-paid column by
  header. The GN5 post-paid column has a different index from the other tables.
  Monthly/yearly prepaid cells are never converted. The original GPU-memory
  string is aggregate allocation VRAM; normalized per-GPU capacity is computed
  only from explicitly stated count/fraction. E.g. 8 GPUs / `320GB` is the A100
  NVSwitch 40GB variant; quarter V100 / `8GB` is a 32GB base GPU with a 0.25
  allocation. `CPU` is labeled in cores by the source and retained as
  `vcpu_raw`; it has not been independently certified as a hardware-thread count.
- **Blocked sources:** RunPod must eventually distinguish Secure/Community,
  Pods/Serverless and MIG fractions; DigitalOcean must retain second billing
  and the 300-second minimum; Salad must retain priority and interruptibility;
  Thunder must verify the included vCPU/RAM and extra charges separately;
  Massed must retain whole-instance row prices and nonlinear multi-GPU specs.
  These constraints are documented in each placeholder, not asserted as live
  observations.

## Offline verification

From the repository root, using Python 3.12 and pytest:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -B -m pytest --noconftest -p no:cacheprovider tests/test_gpu_provider_pages.py -q
```

`--noconftest`, `-B` and disabling the cache provider avoid the shared repository
conftest's run directories, existing cache files and bytecode writes. No tests
fetch the network. Tests cover concrete official values, every output field,
provenance/hash/size, variant separation, count/fraction semantics, future
dates, conditional prices, column reordering, missing/changed price structures,
truncation, duplicates, and blocked-source refusal.
