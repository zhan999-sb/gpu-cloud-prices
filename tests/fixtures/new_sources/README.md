# New-source preparation fixtures, 2026-11-12

These are real anonymous GET responses, not invented pricing examples. Each of
the four source fetches succeeded on its first attempt. No credentials, cookies,
login, browser impersonation, network-setting changes, or block bypass were used.

`fetch_manifest.json` records the original response URL, time, byte length and
SHA-256, plus the fixture's length, SHA-256 and trimming method. The individual
`*.fetch.json` files preserve the original fetch receipts. `*.live_summary.json`
records results computed from each **complete response before trimming**, with
concrete examples and separate on-demand/spot minima. They are derived reports,
not API response fixtures. They exclude Laptop variants from desktop RTX 4090
minima.

| Source | Full live results | Fixture contents | Fixture results |
|---|---:|---|---:|
| Clore | 2,112 offers (1,077 on-demand, 1,035 spot) | 30 real servers, original order, projected public price/spec/rental fields | 28 |
| AnyGPU | 2 offers | Three original GPU package card fragments | 2 |
| Gpushare docs | 0 offers | Complete original HTML, including the separate GPU information table | 0 |
| ai-galaxy | 7 offers | Eight original GPU card fragments, including the view-more button | 7 |

Clore's complete response contained 2,828 servers, 1,494 explicitly unrented.
Only the cheapest five offers per output model/count/type/country are retained.
USD-Blockchain prices have priority. USD conversions, when needed, are labelled
as Clore's own conversions. Prices are whole-server daily amounts divided by
24, then by the explicitly listed full GPU count; partially occupied servers are
not represented as whole-server availability. All tax treatment remains unknown.

AnyGPU's RTX 5090 normal hourly amount is literally `¥2.72.0`. It is rejected
without silently correcting the source or using its member price. RTX 4090's
normal price is CNY 2.24/hour; CNY 1.97/hour is retained only as a member note.
There are only two valid real offers, so a third example cannot be supplied.

The Gpushare bronze header is `青铜会员9.5折` and its cells say only `元`. A later
unrelated example says `¥2.0/小时`; it does **not** supply the price table's unit.
The adapter therefore emits no hourly records from the real response. Tests
explicitly label a changed header as synthetic to exercise bronze-only prices,
rowspans and the VRAM checks: RTX 3090 Ti is incorrectly listed as 8GB instead of
24GB, and RTX 3080 Ti as 10GB instead of 12GB. Conflicting offer VRAM becomes
null, while reported and canonical capacities and a conflict flag are retained.

ai-galaxy cards explicitly say `元/小时` and are always promotional from-prices.
Neither AnyGPU nor ai-galaxy specifies the GPU allocation count in these cards:
their package prices remain hourly observations, with `gpu_count=null` and
`price_per_gpu_hour=null`. No single-GPU allocation is assumed.

The modules expose `parse(payload_or_html, fetched_at) -> list[dict]`. Clore also
exposes `collect(fetch=callable, checked_at=...)`; the callable accepts its single
official API URL and returns decoded JSON. Clore returns the same dictionary
record family as the page parsers, with checked_at, collected_at, explicit
per-GPU-hour values and pricing provenance added. It is not registered and does
not return schema.Offer objects. Integration is left to the concurrent job.

`anygpu.gpu_fields` is a shared pure helper housed in an allowed new module. It
uses the existing `normalize_gpu` and reviewed `CURATED` mappings, without adding
to them. Unmapped models remain raw and receive `unmapped_gpu_model`; the separate
`gpu_model_normalized` is a spelling candidate, not an approved mapping. Page
parsers do not import the provider registry, HTTP transport, or offer schema.

Targeted test commands (no existing conftest/cache writes or full-suite run):

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -B -m pytest --noconftest -p no:cacheprovider tests/test_gpu_new_sources.py --basetemp=ci/runtime/gpu_new_sources_prep_1 -q
python -B -m pytest --noconftest -p no:cacheprovider tests/test_gpu_new_sources.py --basetemp=ci/runtime/gpu_new_sources_prep_2 -q
```
