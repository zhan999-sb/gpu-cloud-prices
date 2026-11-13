# GCP Billing Catalog fixtures — b22, 2026-11-13

These are **constructed, trimmed offline responses**, not authenticated API
captures or current price evidence. No API key was available or used. The two
pages follow Google's documented `skus` / `nextPageToken`, `Category`,
`serviceRegions`, `PricingInfo`, `PricingExpression` and `Money` JSON fields:

- [Official services.skus.list response and resource schema](https://docs.cloud.google.com/billing/docs/reference/rest/v1/services.skus/list)
- [Official catalog access instructions](https://docs.cloud.google.com/billing/v1/how-tos/catalog-api)

The following real SKU identities and description shapes were checked against
Google's public SKU group tables. Prices, timestamps, region subsets and the
pagination token are test inputs, not observations from those tables.

| Fixture identities | Public primary source |
| --- | --- |
| T4 Spot `1A25-07A3-AB6D`, A100 80GB Spot `DD90-547C-2AAA`, A2 Core `3178-715E-CFB6` | [Spot Preemptible Compute](https://cloud.google.com/skus/sku-groups/spot-preemptible-compute) |
| H100 Spot `7A6D-F74C-B0A3`, A3 Core `EEE5-1DA5-D9CF`, A3 RAM `F619-87C3-D3BA` | [Spot Preemptible A3 VMs](https://cloud.google.com/skus/sku-groups/spot-preemptible-a3-vms) |
| H100 Plus Spot `8609-4BAD-F240` | [A3 Mega Spot Preemptible VMs](https://cloud.google.com/skus/sku-groups/a3-mega-spot-preemptible-vms) |
| L4 Taiwan `EDE5-960E-2433`, G2 Core `60FF-EC3A-2E13`, G2 RAM `3F9F-441A-EA47` | [On-Demand G2 VMs](https://cloud.google.com/skus/sku-groups/on-demand-g2-vms) |
| T4 Americas description | [Google's reservation billing example](https://docs.cloud.google.com/billing/docs/how-to/bq-examples) |

`FA01-*` SKU identifiers are synthetic. `88B8-C3ED-03F0` is a historically
published T4 ID; the fixture does not depend on that ID for hardware matching.
All matching uses descriptions and the official GPU category, not a fixed ID
price table. CPU/RAM and commitment rows exercise exclusions. The H100 timeline
contains a previous, current and future price to check effective-time selection.

[Official GPU pricing](https://cloud.google.com/products/compute/gpus-pricing)
documents A3/H100, A2 Standard/A100 40GB, A2 Ultra/A100 80GB and G2/L4. The
[GPU overview](https://docs.cloud.google.com/compute/docs/gpus/about-gpus)
documents T4 16GB and L4 24GB. Only those unambiguous missing capacities are
added before using the shared normalizer. Mega/Plus variants remain distinct.

The catalog provides regional GPU components without a machine configuration
allocation. A3/A2/G2 CPU/RAM components are deliberately not joined to invented
machine types. Every admitted record carries `price_scope=gpu_component`,
`GPU-hour`, and `GPU only; VM vCPU/RAM billed separately`. Allocation and
capacity remain unknown. Components cannot drive whole-VM headline minima.

The reviewed API and GPU price docs do not explicitly establish that this USD
catalog's list prices exclude tax; `tax_basis=unknown` is conservative. The
existing schema spells a proven tax-exclusive basis `excluded` (rather than
`excludes_tax`); no schema vocabulary change is needed here.
