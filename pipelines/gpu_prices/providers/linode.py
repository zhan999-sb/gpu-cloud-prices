"""Akamai public API, verified 2026-11-12. Region/plan availability is separate."""
from ..schema import Offer, validate_offer
from .common import CatalogCollector, number, unique
from .mappings import LINODE_SKUS

API = "https://api.linode.com/v4"


def normalize(plan, regions, availability, stamp):
    if not plan.get("gpus"):
        return []  # NETINT VPU accelerated_devices are not GPUs.
    mapping = LINODE_SKUS.get(plan["id"])
    if mapping is None:
        raise ValueError("unreviewed Linode GPU SKU: " + plan["id"])
    model, count = mapping
    if plan["gpus"] != count:
        raise ValueError("Linode mapped GPU count changed")
    overrides = {r["id"]: number(r["hourly"]) for r in plan["region_prices"]}
    rows = []
    for (region, sku), available in availability.items():
        if sku != plan["id"]:
            continue
        if region not in regions:
            raise ValueError("availability references an unknown region")
        if "GPU Linodes" not in regions[region]["capabilities"]:
            continue
        price = overrides.get(region, number(plan["price"]["hourly"]))
        rows.append(validate_offer(Offer("linode", sku, model, count, plan["vcpus"], number(plan["memory"]) / 1024,
            region, "on_demand", "USD", price, price / count, API + "/linode/types", stamp,
            gpu_model_raw=plan["label"], minimum_billing_seconds=3600,
            availability="available" if available else "unavailable", availability_observed_at=stamp,
            original_currency="USD", original_price=price, product=plan["label"])))
    if not rows:
        raise ValueError("no verified region/plan join for Linode GPU SKU")
    return rows


class LinodeCollector(CatalogCollector):
    name = "linode"

    def collect(self, *, limit_pages=None):
        stamp = self.clock()
        plans = [x for page in self.pages(API + "/linode/types", "data", size=500, linode=True, limit_pages=limit_pages) for x in page]
        regions = {x["id"]: x for page in self.pages(API + "/regions", "data", size=500, linode=True, limit_pages=limit_pages) for x in page}
        availability = {}
        for page in self.pages(API + "/regions/availability", "data", size=500, linode=True, limit_pages=limit_pages):
            for row in page:
                if type(row["available"]) is not bool:
                    raise ValueError("invalid availability flag")
                availability[row["region"], row["plan"]] = row["available"]
        rows = unique([o for plan in plans for o in normalize(plan, regions, availability, stamp)])
        self.complete = limit_pages is None
        if not rows:
            raise ValueError("empty Linode GPU snapshot")
        return rows
