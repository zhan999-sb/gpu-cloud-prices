"""Connect verified, network-free HTML parsers to hourly display collectors."""

from dataclasses import fields
import re

from ..http import HttpClient
from ..schema import Offer, utc_now, validate_offer
from ..models import canonical_model
from .common import unique
from .pages import featurize, nebius, lambda_, paperspace, baidu_gpu, anygpu, ai_galaxy, massedcompute

PARSERS = {module.__name__.rsplit(".", 1)[-1].rstrip("_"): module
           for module in (featurize, nebius, lambda_, paperspace, baidu_gpu, anygpu, ai_galaxy, massedcompute)}


def normalize(row: dict) -> Offer:
    """Retain source scope and unknowns; never invent an allocation or vCPU.

    Page parsers retain a vendor prefix for standalone use. The pipeline's
    canonical models omit that prefix, preserving every capacity/form variant.
    Only explicit GiB RAM is assigned to the pipeline's historical GiB field.
    """
    values = {field.name: row[field.name] for field in fields(Offer) if field.name in row}
    count, fraction = row["gpu_count"], row["gpu_fraction"]
    allocation = count or (fraction if fraction is not None and fraction < 1 else None)
    price = row["price_per_hour"]
    ram = re.fullmatch(r"(\d+(?:\.\d+)?)\s*GiB", row["ram_raw"] or "")
    values.update(
        gpu_model=row["gpu_model"].removeprefix("NVIDIA "),
        ram_gb=float(ram.group(1)) if ram else None,
        price_per_gpu_hour=(price / allocation if allocation else None) if row["price_scope"] == "instance" else price,
        collected_at=row["observed_at"], export_allowed=False,
        original_currency=row["currency"], original_price=price,
    )
    if row.get('notes'):
        values['price_conditions'] = ' '.join(row['notes'])
    if row['provider'] == 'clore':
        values.update(export_allowed=True, price_per_day=row['price_per_day'],
                      source_billing_unit=row['source_billing_unit'], price_basis=row['price_basis'])
    if row['provider'] in {'clore', 'anygpu', 'ai_galaxy'}:
        # These prefixes are branding, not hardware variants. Preserve Laptop,
        # Ti, SUPER, SXM/PCIe/NVL and every explicitly reported memory suffix.
        model = re.sub(r'^(?:NVIDIA\s+)?(?:GeForce\s+)?', '', row['gpu_model'])
        # Keep capacity in the identity, including larger modded cards. The
        # reviewed reporting-loss correction below keeps vram_raw unchanged.
        capacity = row.get('vram_raw')
        if capacity and not re.search(r'\d+(?:\.\d+)?\s*G(?:B)?\b', model, re.I):
            model += ' ' + re.sub(r'\s+', '', capacity)
        values['gpu_model'] = model
    # The Baidu header states cores, without certifying hardware threads.
    if row["provider"] == "baidu_gpu":
        values.update(vcpu=None, cpu_raw=row.get("vcpu_raw"))
    elif row.get("vcpu_raw"):
        values["cpu_raw"] = row["vcpu_raw"]
    values['gpu_model'] = canonical_model(values['gpu_model'], values.get('vram_raw'))
    return validate_offer(Offer(**values))


class PageCollector:
    """One approved URL and one request per provider per rolling hour.

    A store is required so direct use cannot silently bypass workflow cadence.
    Failed requests/parses consume the same persisted budget as successful IO.
    No blocked placeholder has a collector or an approved HTTP endpoint.
    """

    def __init__(self, name, *, store, client=None, clock=utc_now):
        self.name, self.parser = name, PARSERS[name]
        self.store, self.client, self.clock = store, client or HttpClient(), clock
        self.pages_fetched = self.raw_items = 0
        self.complete = self.skipped = False

    def collect(self, *, limit_pages=None):
        if limit_pages is not None and (type(limit_pages) is not int or limit_pages < 1):
            raise ValueError("limit_pages must be positive")
        stamp = self.clock()
        self.complete = self.skipped = False
        self.pages_fetched = self.raw_items = 0
        if not self.store.claim_page_fetch(self.name, stamp):
            self.skipped = True
            return []
        html = self.client.get_bytes(self.parser.URLS[0], accept="text/html").decode("utf-8")
        self.pages_fetched = 1
        rows = self.parser.parse(html, stamp)
        self.raw_items = len(rows)
        offers = unique([normalize(row) for row in rows])
        self.complete = True
        return offers
