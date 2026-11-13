"""Google's public Billing Catalog; runtime env key, GPU components only.

No machine-type allocation or Core/RAM join is present in this catalog contract.
A3/A2/G2 GPU-hour SKUs are therefore components, never whole-VM quotes.
Fixtures and primary source references: tests/fixtures/gcp/README.md.
"""
from decimal import Decimal
import logging
import os
import re
from urllib.error import HTTPError
from urllib.parse import urlencode

from ..http import GcpRequestError, validate_url
from ..models import canonical_model
from ..schema import Offer, parse_utc, validate_offer
from .hyperscalers import BudgetCollector

LOG = logging.getLogger(__name__)
API_URL = 'https://cloudbilling.googleapis.com/v1/services/6F81-5844-456A/skus'
SOURCE_URL = API_URL + '?currencyCode=USD&pageSize=5000'
KEY_ENV = 'GCP_BILLING_API_KEY'
COMPONENT_NOTE = 'GPU only; VM vCPU/RAM billed separately'
MAX_PAGES = 100

# These unqualified names have one documented GCP capacity. A100 without an
# 80GB suffix is the A2 Standard 40GB GPU; preserve explicit suffixes/variants.
CAPACITY = {'T4': '16GB', 'L4': '24GB', 'A100': '40GB'}
DESCRIPTION = re.compile(
    r'^(?:(?:Spot\s+)?Preemptible\s+|Spot\s+)?Nvidia\s+(?:Tesla\s+)?'
    r'(?P<model>.+?)\s+GPU\s+(?:running\s+in\s+|'
    r'attached\s+to\s+(?:Spot\s+)?Preemptible\s+VMs\s+running\s+in\s+).+$', re.I)
HARDWARE = re.compile(
    r'(?:T4|L4|A100|H100|H200|K80|P4|P100|V100)'
    r'(?:\s+\d+(?:\.\d+)?\s*GB)?(?:\s+(?:Mega|Plus|SXM|PCIe|NVL))?', re.I)


def _price(row, stamp):
    """Latest effective USD rate, using exact Money units + nanos arithmetic."""
    timeline = row.get('pricingInfo')
    if not isinstance(timeline, list) or not timeline:
        raise ValueError('GCP GPU pricing timeline missing')
    now, seen, active = parse_utc(stamp), set(), []
    for entry in timeline:
        if not isinstance(entry, dict):
            raise ValueError('GCP invalid pricing timeline entry')
        effective = parse_utc(entry['effectiveTime'])
        if effective in seen:
            raise ValueError('GCP duplicate effective price')
        seen.add(effective)
        if effective <= now:
            active.append((effective, entry))
    if not active:
        return None
    _, entry = max(active, key=lambda pair: pair[0])
    expression = entry['pricingExpression']
    if not isinstance(expression, dict):
        raise ValueError('GCP invalid pricing expression')
    rates = expression.get('tieredRates')
    if (expression.get('usageUnit') != 'h' or not isinstance(rates, list)
            or len(rates) != 1 or not isinstance(rates[0], dict)
            or type(rates[0].get('startUsageAmount')) not in {int, float}
            or rates[0]['startUsageAmount'] != 0):
        raise ValueError('GCP requires an untiered GPU-hour price starting at zero')
    money = rates[0]['unitPrice']
    if not isinstance(money, dict):
        raise ValueError('GCP invalid USD Money amount')
    units, nanos = money.get('units', '0'), money.get('nanos', 0)
    if (money.get('currencyCode') != 'USD' or not isinstance(units, str)
            or not re.fullmatch(r'\d{1,18}', units) or type(nanos) is not int
            or not 0 <= nanos < 1_000_000_000):
        raise ValueError('GCP invalid USD Money amount')
    amount = Decimal(units) + Decimal(nanos) / Decimal(1_000_000_000)
    if amount <= 0:
        raise ValueError('GCP nonpositive GPU-hour price')
    return float(amount), entry['effectiveTime']


SKIPPABLE_SKU_ERRORS = {'GCP nonpositive GPU-hour price', 'GCP requires an untiered GPU-hour price starting at zero'}


def normalize_item(row, stamp):
    """One official SKU may cover several serviceRegions; emit one per region."""
    category = row.get('category', {})
    if not isinstance(category, dict):
        raise ValueError('GCP invalid SKU category')
    if category.get('resourceGroup') != 'GPU':
        return []  # Core/Ram, software, disks and bundled VM rows are not GPUs.
    description = row.get('description', '')
    if not isinstance(description, str):
        raise ValueError('GCP invalid GPU description')
    usage = category.get('usageType')
    if usage not in {'OnDemand', 'Preemptible'}:
        return []  # Commitments are never amortized into an hourly quote.
    match = DESCRIPTION.fullmatch(description)
    if not match or not HARDWARE.fullmatch(match['model']):
        return []  # Includes workstation licences and unreviewed new shapes.
    sku = row['skuId']
    if not isinstance(sku, str) or not re.fullmatch(r'[A-Z0-9]{4}(?:-[A-Z0-9]{4}){2}', sku):
        raise ValueError('GCP invalid SKU identifier')
    if row.get('name') != f'services/6F81-5844-456A/skus/{sku}':
        raise ValueError('GCP SKU belongs to an unexpected service')
    regions = row.get('serviceRegions')
    if (not isinstance(regions, list) or any(not isinstance(r, str) for r in regions)
            or len(regions) != len(set(regions))):
        raise ValueError('GCP invalid serviceRegions')
    if any(r != 'global' and not re.fullmatch(r'[a-z]+(?:-[a-z]+)+[1-9][0-9]*', r) for r in regions):
        raise ValueError('GCP serviceRegions must contain regions, not descriptions or zones')
    regions = list(dict.fromkeys(None if r == 'global' else r for r in regions)) or [None]
    current = _price(row, stamp)
    if current is None:
        return []
    price, effective = current
    raw_model = match['model']
    # Correct casing for known hardware without dropping source capacity/form.
    model = re.sub(r'^(t4|l4|a100|h100|h200|k80|p4|p100|v100)',
                   lambda m: m[0].upper(), raw_model, flags=re.I)
    if model in CAPACITY:
        model += ' ' + CAPACITY[model]
    model = canonical_model(model)
    memory = re.search(r'\d+(?:\.\d+)?\s*GB\b', raw_model, re.I)
    spot = usage == 'Preemptible' or bool(re.search(r'\b(?:Spot|Preemptible)\b', description, re.I))
    return [validate_offer(Offer(
        'gcp', sku, model, None, None, None, region,
        'spot' if spot else 'on_demand', 'USD', price, price, SOURCE_URL, stamp,
        price_scope='gpu_component', gpu_fraction=None, gpu_model_raw=description,
        # The reviewed catalog/GPU docs do not explicitly establish a tax basis.
        tax_basis='unknown', billing_unit='GPU-hour', source_billing_unit='GPU-hour',
        effective_from=effective, availability='unknown', tier='A', export_allowed=True,
        original_currency='USD', original_price=price, product=description,
        vram_raw=memory[0] if memory else None, price_conditions=COMPONENT_NOTE,
    )) for region in regions]


class GcpCollector(BudgetCollector):
    name = 'gcp'
    interval_hours = 6

    def get(self, url):
        validate_url(url)
        self.pages_fetched += 1
        try:
            return self.fetch(url)
        except GcpRequestError:
            raise
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise GcpRequestError(status) from None
        except Exception:
            # Injected transports must have the same credential-safe boundary.
            raise GcpRequestError() from None

    def collect(self, *, limit_pages=None):
        if limit_pages is not None and (type(limit_pages) is not int or limit_pages < 1):
            raise ValueError('limit_pages must be positive')
        self.pages_fetched = self.raw_items = 0
        self.complete = self.skipped = False
        self.skip_reason = None
        key = os.environ.get(KEY_ENV, '').strip()
        if not key:
            self.skipped = True
            self.skip_reason = f'{KEY_ENV} is not set; Google Cloud collection skipped'
            LOG.info('%s', self.skip_reason)
            return []  # No HTTP, no consumed budget, no replacement run record.
        stamp = self.begin(limit_pages)
        if stamp is None:
            self.skip_reason = 'source attempted within 6 hours'
            return []
        token, seen_tokens, seen_skus, offers = '', set(), set(), []
        while self.pages_fetched < MAX_PAGES:
            query = {'currencyCode': 'USD', 'pageSize': 5000, 'key': key}
            if token:
                query['pageToken'] = token
            payload = self.get(API_URL + '?' + urlencode(query))
            if (not isinstance(payload, dict) or not isinstance(payload.get('skus'), list)
                    or 'error' in payload):
                raise ValueError('GCP catalog response shape changed')
            rows = payload['skus']
            if len(rows) > 5000 or any(not isinstance(row, dict) for row in rows):
                raise ValueError('GCP invalid catalog page')
            self.raw_items += len(rows)
            for row in rows:
                sku = row.get('skuId')
                if not isinstance(sku, str) or sku in seen_skus:
                    raise ValueError('GCP missing or duplicate catalog SKU')
                seen_skus.add(sku)
                try:
                    offers.extend(normalize_item(row, stamp))
                except ValueError as exc:
                    # The live catalogue contains GPU SKUs that are zero-priced or tiered (e.g. promotional or
                    # commitment-related rows). Skip those individual SKUs instead of failing the whole run;
                    # structural problems (pagination, Money format, regions) still fail closed below.
                    if str(exc) not in SKIPPABLE_SKU_ERRORS:
                        raise
                    self.skipped_skus = getattr(self, 'skipped_skus', 0) + 1
            token = payload.get('nextPageToken', '')
            if not isinstance(token, str) or token in seen_tokens:
                raise ValueError('GCP invalid or repeated pagination token')
            if not token:
                result = self.finish(offers)
                self.complete = True
                return result
            seen_tokens.add(token)
            if limit_pages and self.pages_fetched >= limit_pages:
                return self.finish(offers)
        raise ValueError('GCP catalog exceeds page budget')
