"""Verda public instance catalog; never query authenticated availability APIs."""
from ..schema import Offer, validate_offer
from .common import CatalogCollector, number, unique
from .mappings import VERDA_SKUS

API = "https://api.verda.com/v1/instance-types"


def normalize(item, stamp):
    count = item["gpu"]["number_of_gpus"]
    if count == 0:
        return []
    mapping = VERDA_SKUS.get(item["instance_type"])
    if mapping is None:
        raise ValueError("unreviewed Verda GPU SKU: " + item["instance_type"])
    model, expected_count, expected_description = mapping
    if count != expected_count or item["gpu"]["description"] != expected_description:
        raise ValueError("Verda GPU specification changed")
    rows = []
    for field, kind in (("price_per_hour", "on_demand"), ("spot_price", "spot")):
        price = number(item[field])
        currency = item["currency"].upper()
        if currency != "USD":
            raise ValueError("Verda currency changed")
        rows.append(validate_offer(Offer("verda", item["instance_type"], model, count, None, None, None,
            kind, currency, price, price / count, API, stamp,
            gpu_model_raw=item["gpu"]["description"], original_currency=currency, original_price=price,
            product=item.get("name"), cpu_raw=item["cpu"]["description"], ram_raw=item["memory"]["description"],
            vram_raw=item["gpu_memory"]["description"])))
    return rows


class VerdaCollector(CatalogCollector):
    name = "verda"

    def collect(self, *, limit_pages=None):
        stamp = self.clock()
        payload = self.get(API)
        if not isinstance(payload, list) or any(not isinstance(x, dict) for x in payload):
            raise ValueError("Verda catalog must be an array of objects")
        self.raw_items = len(payload)
        rows = unique([o for item in payload for o in normalize(item, stamp)])
        if not rows:
            raise ValueError("empty Verda GPU snapshot")
        self.complete = True
        return rows
