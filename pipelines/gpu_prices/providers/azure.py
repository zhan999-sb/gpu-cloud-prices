"""Linux base-OS consumption + spot rates, all commercial Azure regions.

Official access contract:
https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices
"""

import logging
import time
from urllib.parse import urlencode
from dataclasses import replace

from ..catalog import lookup_azure_sku
from ..http import HttpClient, validate_url
from ..schema import Offer, parse_utc, utc_now, validate_offer

LOG = logging.getLogger(__name__)
API_URL = "https://prices.azure.com/api/retail/prices"
SOURCE_DOC = "https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices"
GPU_FILTER = ("serviceName eq 'Virtual Machines' and priceType eq 'Consumption' and "
              "(contains(armSkuName, 'Standard_NC') or "
              "contains(armSkuName, 'Standard_ND') or contains(armSkuName, 'Standard_NV'))")
# Retail Prices implements a subset of OData: use contains, then enforce exact
# ARM prefixes and Linux-only eligibility locally rather than unsupported startswith.


def initial_url() -> str:
    return API_URL + "?" + urlencode({"api-version": "2023-01-01-preview", "currencyCode": "'USD'",
                                     "meterRegion": "'primary'", "$filter": GPU_FILTER})


def normalize_item(item: dict, *, source_url: str, collected_at: str) -> Offer | None:
    """Defend against server-side filters drifting. No reservation amortization."""
    validate_url(source_url)
    if item.get("serviceName") != "Virtual Machines" or item.get("type") != "Consumption":
        return None
    product = item.get("productName", "")
    if not isinstance(product, str) or not product.startswith("Virtual Machines "):
        return None
    if any(word in product.casefold() for word in ("windows", "sql", "red hat", "rhel", "suse", "ubuntu pro")):
        return None
    if item.get("isPrimaryMeterRegion") is not True or item.get("unitOfMeasure") != "1 Hour":
        return None
    if item.get("currencyCode") != "USD" or item.get("tierMinimumUnits") != 0:
        return None
    sku = item.get("armSkuName")
    if not isinstance(sku, str) or not sku.startswith(("Standard_NC", "Standard_ND", "Standard_NV")):
        return None
    spec = lookup_azure_sku(sku)
    if spec is None:
        return None
    meter = " ".join(str(item.get(key, "")) for key in ("meterName", "skuName")).casefold()
    if "low priority" in meter or "lowpriority" in meter:
        return None  # Legacy low-priority meters are not current Spot offers.
    try:
        raw_price = item["retailPrice"]
        if isinstance(raw_price, bool) or not isinstance(raw_price, (int, float)):
            raise ValueError("retailPrice must be numeric")
        price = float(raw_price)
        offer = Offer("azure", sku, spec.gpu_model, spec.gpu_count, spec.vcpu, spec.ram_gb,
                      item.get("armRegionName", ""), "spot" if "spot" in meter else "on_demand",
                      "USD", price, price / spec.gpu_count, source_url, collected_at,
                      gpu_fraction=min(1.0, spec.gpu_count), gpu_model_raw=spec.gpu_model,
                      original_currency="USD", original_price=price)
        return validate_offer(offer)
    except (KeyError, ValueError, TypeError) as exc:
        LOG.warning("Invalid Azure meter skipped (%s): %s", sku, exc)
        return None


class AzureCollector:
    name = "azure"

    def __init__(self, *, client=None, sleep=time.sleep, page_delay: float = 1.0, clock=utc_now):
        if page_delay < 0:
            raise ValueError("page_delay must be nonnegative")
        self.client = client or HttpClient()
        self.sleep = sleep
        self.page_delay = page_delay
        self.clock = clock
        self.pages_fetched = 0
        self.complete = False
        self.raw_items = 0

    def collect(self, *, limit_pages: int | None = None) -> list[Offer]:
        if limit_pages is not None and (type(limit_pages) is not int or limit_pages < 1):
            raise ValueError("limit_pages must be a positive integer")
        self.pages_fetched, self.raw_items, self.complete = 0, 0, False
        collected_at = self.clock()  # One UTC observation day for the whole snapshot.
        observation_time = parse_utc(collected_at)
        url = initial_url()
        visited = set()
        selected = {}
        ambiguous = set()
        while url:
            validate_url(url)  # Also required when an injected offline client is used.
            if url in visited:
                raise ValueError("Azure paging cycle detected")
            visited.add(url)
            payload = self.client.get_json(url)
            items = payload.get("Items")
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ValueError("Azure page must contain an Items array of objects")
            self.pages_fetched += 1
            self.raw_items += len(items)
            for item in items:
                offer = normalize_item(item, source_url=url, collected_at=collected_at)
                if offer is None:
                    continue
                try:
                    effective = parse_utc(item["effectiveStartDate"])
                except (KeyError, ValueError, TypeError):
                    LOG.warning("Missing/invalid effectiveStartDate skipped: %s", offer.sku)
                    continue
                if effective > observation_time:
                    continue
                offer = replace(offer, effective_start_date=item["effectiveStartDate"], effective_from=item["effectiveStartDate"])
                key = (offer.sku, offer.region, offer.price_type)
                previous = selected.get(key)
                if previous is None or effective > previous[0]:
                    selected[key] = (effective, offer)
                    ambiguous.discard(key)
                elif effective == previous[0] and offer.price_per_hour != previous[1].price_per_hour:
                    ambiguous.add(key)
                    LOG.warning("Conflicting same-date meters skipped: %s", key)
            next_url = payload.get("NextPageLink")
            if next_url is not None and not isinstance(next_url, str):
                raise ValueError("NextPageLink must be a string or null")
            if next_url:
                validate_url(next_url)
            if not next_url:
                self.complete = True
                break
            if limit_pages is not None and self.pages_fetched >= limit_pages:
                LOG.warning("Collection truncated at %d pages; regional/model coverage is partial", limit_pages)
                break
            url = next_url
            self.sleep(self.page_delay)
        offers = [offer for key, (_, offer) in selected.items() if key not in ambiguous]
        offers.sort(key=lambda x: (x.gpu_model, x.sku, x.region, x.price_type))
        LOG.info("Azure: pages=%d raw_items=%d normalized_records=%d complete=%s models=%s",
                 self.pages_fetched, self.raw_items, len(offers), self.complete,
                 ", ".join(sorted({o.gpu_model for o in offers})) or "none")
        return offers
