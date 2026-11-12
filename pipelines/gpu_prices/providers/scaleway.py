"""Scaleway public catalog; exact Money and explicit time-unit conversion."""
from ..schema import Offer, validate_offer
from .common import CatalogCollector, number, unique
from .mappings import SCALEWAY_SKUS

API = "https://api.scaleway.com/product-catalog/v2alpha1/public-catalog/products"


def normalize(item, stamp):
    props = item.get("properties", {})
    if props.get("instance", {}).get("range") != "GPU" or not item.get("sku", "").startswith("/instance/server/"):
        return None  # Apple integrated GPU cores, storage and bare metal excluded.
    mapping = SCALEWAY_SKUS.get(item["sku"])
    if mapping is None:
        raise ValueError("unreviewed Scaleway GPU SKU: " + item["sku"])
    model, expected_count, expected_type = mapping
    hw = props["hardware"]
    if hw["gpu"]["count"] != expected_count or hw["gpu"]["type"] != expected_type:
        raise ValueError("Scaleway GPU specification changed")
    money = item["price"]["retail_price"]
    if money["currency_code"] != "EUR" or not 0 <= money["nanos"] < 1_000_000_000:
        raise ValueError("Scaleway currency or Money changed")
    from decimal import Decimal
    price = Decimal(str(money["units"])) + Decimal(str(money["nanos"])) / Decimal(1_000_000_000)
    unit = item["unit_of_measure"]
    if unit["unit"] not in {"hour", "minute", "second"}:
        raise ValueError("non-hourly Scaleway GPU price refused")
    seconds = {"hour": 3600, "minute": 60, "second": 1}[unit["unit"]]
    price = number(price * 3600 / Decimal(str(number(unit["size"]) * seconds)))
    zone = item["locality"]["zone"]
    if zone not in {"fr-par-1", "fr-par-2", "fr-par-3", "nl-ams-1", "nl-ams-2", "nl-ams-3", "pl-waw-1", "pl-waw-2", "pl-waw-3"}:
        raise ValueError("unreviewed Scaleway zone")
    return validate_offer(Offer("scaleway", item["sku"], model, expected_count, hw["cpu"]["virtual"]["count"],
        number(hw["ram"]["size"]) / 2**30, zone, "on_demand", "EUR", price, price / expected_count, API, stamp,
        gpu_model_raw=hw["gpu"]["type"], tax_basis="excluded", zone=zone,
        billing_unit="instance-" + unit["unit"], original_currency="EUR", original_price=price,
        product=item["product"], cpu_raw=hw["cpu"]["description"], ram_raw=hw["ram"]["description"] ))


class ScalewayCollector(CatalogCollector):
    name = "scaleway"

    def collect(self, *, limit_pages=None):
        stamp = self.clock()
        rows = []
        for page in self.pages(API, "products", limit_pages=limit_pages):
            for item in page:
                offer = normalize(item, stamp)
                if offer:
                    rows.append(offer)
        if not rows:
            raise ValueError("empty Scaleway GPU snapshot")
        self.complete = limit_pages is None
        return unique(rows)
