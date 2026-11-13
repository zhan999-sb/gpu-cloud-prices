"""Public official catalogs, with persisted request budgets and strict scopes.

Contracts and policy evidence: docs/provider-specs-2026-11-13-hyperscalers.md.
Runtime uses only Python 3.12 stdlib. Fetch functions and clocks are injectable.
"""
import csv
from decimal import Decimal
import re
from urllib.parse import urlencode

from ..http import HttpClient, validate_url
from ..models import canonical_model
from ..schema import Offer, utc_now, validate_offer
from .common import number, unique

AWS_INDEX = 'https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/region_index.json'
AWS_SPOT = 'https://website.spot.ec2.aws.a2z.com/spot.json'
ORACLE = 'https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/?currencyCode=USD'
OVH = 'https://eu.api.ovh.com/1.0/order/catalog/public/cloud?ovhSubsidiary=FR'
VULTR = 'https://api.vultr.com/v2/'
HETZNER = 'https://www.hetzner.com/dedicated-rootserver/matrix-gpu/'

# Hardware families verified against AWS accelerated-computing documentation.
# GPU counts and capacities come from each CSV product, never its size suffix.
AWS_MODELS = {'g2': 'GRID K520', 'g3': 'Tesla M60', 'g3s': 'Tesla M60',
    'g4dn': 'T4', 'g4ad': 'Radeon Pro V520', 'g5': 'A10G', 'g5g': 'T4G',
    'g6': 'L4', 'gr6': 'L4', 'g6e': 'L40S', 'p2': 'Tesla K80',
    'p3': 'V100', 'p3dn': 'V100', 'p4d': 'A100', 'p4de': 'A100',
    'p5': 'H100', 'p5e': 'H200', 'p5en': 'H200', 'p6-b200': 'B200', 'p6-b300': 'B300', 'g7': 'RTX PRO 4500', 'g7e': 'RTX PRO 6000 Blackwell Server Edition'}

# part number -> (shape, GPU model, count, OCPU, memory GB).
# Old X7/V2/E3/E4, H100T, software, VMware and customer hardware are excluded.
ORACLE_SHAPES = {
    'B95907': ('BM.GPU.A100-v2.8', 'A100 80GB', 8, 128, 2048),
    'B95909': ('BM.GPU.A10.4', 'A10 24GB', 4, 64, 1024),
    'B98415': ('BM.GPU.H100.8', 'H100 80GB', 8, 112, 2048),
    'B109479': ('BM.GPU.L40S.4', 'L40S 48GB', 4, 112, 1024),
    'B109485': ('BM.GPU.MI300X.8', 'MI300X 192GB', 8, 112, 2048),
    'B110519': ('BM.GPU.H200.8', 'H200 141GB', 8, 112, 3072),
    'B110978': ('BM.GPU.B200.8', 'B200 180GB', 8, 128, 4096),
    'B112613': ('BM.GPU.RTXPRO.8', 'RTX PRO 6000 Blackwell 96GB', 8, 144, 3072),
}


def positive_int(value):
    result = number(value)
    if not result.is_integer():
        raise ValueError('integer hardware allocation required')
    return int(result)


def offer(provider, sku, model, count, price, stamp, source, *, region=None, currency='USD', **kw):
    return validate_offer(Offer(provider, sku, canonical_model(model), count, kw.pop('vcpu', None),
        kw.pop('ram_gb', None), region, kw.pop('price_type', 'on_demand'), currency, price,
        price / count, source, stamp, original_currency=currency, original_price=price, **kw))


class BudgetCollector:
    interval_hours = 6

    def __init__(self, *, store, fetch=None, client=None, clock=utc_now):
        self.store, self.client, self.clock = store, client or HttpClient(), clock
        self.fetch = fetch or self.client.get_json
        self.pages_fetched = self.raw_items = 0
        self.complete = self.skipped = False

    def begin(self, limit_pages):
        if limit_pages is not None and (type(limit_pages) is not int or limit_pages < 1):
            raise ValueError('limit_pages must be positive')
        self.pages_fetched = self.raw_items = 0
        self.complete = self.skipped = False
        stamp = self.clock()
        if not self.store.claim_page_fetch(self.name, stamp, interval_hours=self.interval_hours):
            self.skipped = True
            return None
        return stamp

    def get(self, url):
        validate_url(url)
        self.pages_fetched += 1  # Count attempts, including failed requests.
        return self.fetch(url)

    def finish(self, rows):
        if not rows or len(rows) > 20_000:
            raise ValueError('empty or oversized GPU snapshot')
        return unique(rows)


def aws_rows(lines):
    """CSV streaming skips five metadata lines, validates schema, filters early."""
    it = iter(lines)
    for _ in range(12):
        line = next(it, '')
        if line.startswith('"SKU",'):
            break
    else:
        raise ValueError('missing AWS CSV header')
    fields = next(csv.reader([line]))
    required = {'SKU', 'TermType', 'Product Family', 'GPU', 'GPU Memory', 'Instance Type',
                'Operating System', 'Tenancy', 'Pre Installed S/W', 'CapacityStatus',
                'Region Code', 'Unit', 'PricePerUnit', 'Currency', 'EffectiveDate'}
    if not required <= set(fields):
        raise ValueError('AWS CSV schema changed')
    for row in csv.DictReader(it, fieldnames=fields):
        if None in row or None in row.values():
            raise ValueError('truncated AWS CSV record')
        if (row['TermType'] == 'OnDemand' and row['Product Family'] == 'Compute Instance'
                and row['GPU'] not in ('', '0') and row['Operating System'] == 'Linux'
                and row['Tenancy'] == 'Shared' and row['Pre Installed S/W'] == 'NA'
                and row['CapacityStatus'] == 'Used' and row.get('MarketOption') == 'OnDemand'
                and row.get('operation') == 'RunInstances'):
            yield row


def aws_normalize(row, stamp, source, expected_region):
    sku = row['Instance Type']
    family = sku.split('.')[0]
    if family not in AWS_MODELS:
        return None  # Includes fractional g6f and unreviewed future families.
    if (row['Region Code'] != expected_region or row['Location Type'] != 'AWS Region'
            or row['Unit'] != 'Hrs' or row['Currency'] != 'USD'
            or row['StartingRange'] != '0' or row['EndingRange'] != 'Inf'):
        raise ValueError('AWS region, currency or hourly range changed')
    count = positive_int(row['GPU'])
    memory = re.fullmatch(r'([\d,.]+) (?:GiB|GB)(?: HBM\w*)?', row['GPU Memory'])
    if not memory and row['GPU Memory'] != 'NA':
        raise ValueError('AWS GPU memory schema changed')
    capacity = number(memory[1].replace(',', '')) / count if memory else None
    # The live P5en price-list product reports 141 GB PER GPU, while other
    # products report aggregate memory. Official EC2 specifications explicitly
    # give P5e/P5en 8 x 141 GiB. Accept those two documented representations;
    # never divide the per-GPU value a second time or accept an unknown value.
    if family in {'p5e', 'p5en'}:
        amount = number(memory[1].replace(',', '')) if memory else None
        if amount not in {141, 141 * count}:
            raise ValueError('AWS H200 GPU memory differs from reviewed specifications')
        capacity = 141
    price = number(row['PricePerUnit'])
    ram = re.fullmatch(r'([\d,.]+) GiB', row['Memory'])
    model = AWS_MODELS[family] + (f' {capacity:g}GB' if capacity else '')
    effective = row['EffectiveDate']
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', effective):
        effective += 'T00:00:00Z'
    elif not effective.endswith('Z'):
        effective += 'Z'
    return offer('aws', sku, model, count, price, stamp, source,
        region=expected_region, vcpu=positive_int(row['vCPU']),
        ram_gb=number(ram[1].replace(',', '')) if ram else None,
        gpu_model_raw=AWS_MODELS[family], vram_raw=row['GPU Memory'],
        product=sku+' Linux shared tenancy', tax_basis='excluded', minimum_billing_seconds=60,
        effective_from=effective,
        price_conditions='Linux, shared tenancy, no preinstalled software; storage and data transfer extra.')


class AwsCollector(BudgetCollector):
    name, interval_hours = 'aws', 24
    DEFAULT_REGIONS = ('us-east-1', 'eu-west-1')

    def __init__(self, *, stream=None, regions=DEFAULT_REGIONS, **kw):
        super().__init__(**kw)
        self.stream = stream or self.client.iter_lines
        self.regions = tuple(regions)
        if not 1 <= len(self.regions) <= 4 or len(set(self.regions)) != len(self.regions):
            raise ValueError('AWS requires one to four distinct regions')

    def collect(self, *, limit_pages=None):
        stamp = self.begin(limit_pages)
        if stamp is None:
            return []
        index = self.get(AWS_INDEX)
        rows = []
        for region in self.regions:
            if limit_pages and self.pages_fetched >= limit_pages:
                return self.finish(rows) if rows else []
            item = index['regions'][region]
            path = item['currentVersionUrl']
            if item['regionCode'] != region or not path.endswith('/'+region+'/index.json'):
                raise ValueError('AWS regional index join changed')
            url = 'https://pricing.us-east-1.amazonaws.com' + path.removesuffix('.json') + '.csv'
            validate_url(url)
            self.pages_fetched += 1
            for raw in aws_rows(self.stream(url)):
                self.raw_items += 1
                row = aws_normalize(raw, stamp, url, region)
                if row:
                    rows.append(row)
        if limit_pages and self.pages_fetched >= limit_pages:
            return self.finish(rows)
        hardware = {}
        for o in rows:
            previous = hardware.get(o.sku)
            if previous and (previous.gpu_model, previous.gpu_count, previous.vcpu, previous.ram_gb) != (o.gpu_model, o.gpu_count, o.vcpu, o.ram_gb):
                raise ValueError('AWS regional hardware specifications conflict')
            hardware[o.sku] = o
        spot_seen = {}
        spot = self.get(AWS_SPOT)['config']
        if spot['rate'] != 'perhr' or spot['currencies'] != ['USD']:
            raise ValueError('AWS Spot unit changed')
        for region in spot['regions']:
            code = region['region']
            if not re.fullmatch(r'(?:us|eu|ap|ca|sa|af|me|il|mx)-[a-z]+-[1-9]', code):
                continue
            for family in region['instanceTypes']:
                for raw in family['sizes']:
                    base = hardware.get(raw['size'])
                    if not base:
                        continue
                    for col in raw['valueColumns']:
                        if col['name'] != 'linux' or col['prices'].get('USD') in ('N/A', 'NA', '', None):
                            continue
                        self.raw_items += 1
                        key = (code, base.sku)
                        price = number(col['prices']['USD'])
                        if key in spot_seen:
                            if spot_seen[key] != price:
                                raise ValueError('conflicting duplicate AWS Spot price')
                            continue  # The real feed repeats identical rows in categories.
                        spot_seen[key] = price
                        rows.append(offer('aws', base.sku, base.gpu_model, base.gpu_count,
                            price, stamp, AWS_SPOT, region=code, price_type='spot',
                            vcpu=base.vcpu, ram_gb=base.ram_gb, gpu_model_raw=base.gpu_model_raw,
                            tax_basis='excluded', is_from_price=True,
                            price_conditions='Regional lowest Linux Spot price across availability zones; not an AZ quote or capacity guarantee.'))
        # Complete means selected coverage, never all AWS regions/families.
        self.complete = True
        return self.finish(rows)


class HetznerCollector(BudgetCollector):
    name, interval_hours = 'hetzner', 24

    def __init__(self, *, fetch=None, **kw):
        super().__init__(fetch=fetch, **kw)
        self.fetch = fetch or (lambda url: self.client.get_bytes(url, accept='text/html').decode('utf-8'))

    def collect(self, *, limit_pages=None):
        from .pages import Document, one, require
        stamp = self.begin(limit_pages)
        if stamp is None:
            return []
        doc = Document(self.get(HETZNER))
        cards = doc.root.find(cls='baremetal-product')
        rows, seen = [], set()
        for card in cards:
            text = card.text()
            m = re.search(r'\bGEX\d+\b', text)
            if not m:
                continue
            sku = m[0]
            require(sku not in seen, 'duplicate Hetzner GPU card')
            seen.add(sku)
            prices = card.find(tag='ho-price-container')
            hourly = one([n for n in prices if n.attrs.get('price-type') == 'hourly'], 'Hetzner hourly price')
            setup = one([n for n in prices if n.attrs.get('price-type') == 'setup'], 'Hetzner setup fee')
            month = one([n for n in prices if not n.attrs.get('price-type')], 'Hetzner monthly cap')
            h = re.fullmatch(r'€ ([\d,.]+) per hour, excl\. VAT', hourly.text())
            s = re.fullmatch(r'\+ € ([\d,.]+) one-time setup fee, excl\. VAT', setup.text())
            mo = re.fullmatch(r'€ ([\d,.]+) per month, excl\. VAT', month.text())
            require(h and s and mo, 'Hetzner price/currency/tax wording changed')
            # The matrix FAQ confirms one GPU on the three reviewed single-GPU
            # SKUs; GEX430 explicitly states 4x on the card. VRAM is per GPU.
            g = re.search(r'(?:([1-9]\d*)x )?NVIDIA RTX PRO™ (\d+) Blackwell (SFF Edition|Max-Q) .*?(\d+) GB GDDR7', text)
            require(g is not None, 'Hetzner GPU specification changed')
            count = int(g[1] or 1)
            model = f'RTX PRO {g[2]} Blackwell {g[3]} {g[4]}GB'
            from_price = 'from monthly' in text
            locations = {'GEX45': ('HEL1',), 'GEX63': ('HEL1',), 'GEX131': ('HEL1','FSN1'), 'GEX430': ('HEL1',)}
            require(sku in locations, 'unreviewed Hetzner location mapping')
            for region in locations[sku]:
                rows.append(offer('hetzner', sku, model, count, number(h[1].replace(',','')), stamp, HETZNER,
                    region=region, currency='EUR', tier='B', export_allowed=False, tax_basis='excluded',
                    product=sku, gpu_model_raw=g[0], vram_raw=g[4]+' GB/GPU', is_from_price=from_price,
                    price_conditions=f'Hourly billing, monthly cap EUR {mo[1]}; one-time setup EUR {s[1]}; configuration-dependent starting price.' if from_price else
                                     f'Hourly billing, monthly cap EUR {mo[1]}; one-time setup EUR {s[1]}.'))
        require(seen == {'GEX45','GEX63','GEX131','GEX430'}, 'Hetzner GPU card coverage changed')
        self.raw_items, self.complete = len(cards), True
        return self.finish(rows)


class OracleCollector(BudgetCollector):
    name = 'oracle'

    def collect(self, *, limit_pages=None):
        stamp = self.begin(limit_pages)
        if stamp is None:
            return []
        payload = self.get(ORACLE)
        if set(payload) != {'lastUpdated', 'items'} or not payload['lastUpdated'] or not isinstance(payload['items'], list):
            raise ValueError('Oracle complete-catalog envelope changed')
        rows, seen = [], set()
        self.raw_items = len(payload['items'])
        for raw in payload['items']:
            part = raw['partNumber']
            if part not in ORACLE_SHAPES:
                continue
            if part in seen or raw['metricName'] != 'GPU Per Hour' or raw['serviceCategory'] != 'Compute - GPU':
                raise ValueError('Oracle GPU SKU contract changed')
            seen.add(part)
            values = [p for c in raw['currencyCodeLocalizations'] if c['currencyCode'] == 'USD'
                      for p in c['prices'] if p['model'] == 'PAY_AS_YOU_GO']
            if len(values) != 1:
                raise ValueError('Oracle requires one USD PAYG price')
            shape, model, count, ocpu, ram = ORACLE_SHAPES[part]
            price = number(values[0]['value']) * count
            rows.append(offer('oracle', part+':'+shape, model, count, price, stamp, ORACLE,
                gpu_model_raw=raw['displayName'], product=shape, cpu_raw=f'{ocpu} OCPUs',
                ram_raw=f'{ram} GB', source_billing_unit='GPU-hour', price_basis='GPU rate multiplied by shape GPU count',
                price_conditions='Global public list rate; region and inventory unspecified. Shape CPU and memory included.'))
        if seen != set(ORACLE_SHAPES):
            raise ValueError('Oracle catalog missing reviewed GPU parts')
        self.complete = True
        return self.finish(rows)


class OvhCollector(BudgetCollector):
    name = 'ovh'

    def collect(self, *, limit_pages=None):
        stamp = self.begin(limit_pages)
        if stamp is None:
            return []
        payload = self.get(OVH)
        if payload['locale']['currencyCode'] != 'EUR' or payload['locale']['subsidiary'] != 'FR':
            raise ValueError('OVH currency/subsidiary changed')
        rows = []
        self.raw_items = len(payload['addons'])
        for raw in payload['addons']:
            tech = (raw.get('blobs') or {}).get('technical') or {}
            if (raw['product'] != 'publiccloud-instance' or not tech.get('gpu')
                    or tech.get('os', {}).get('family') != 'linux'
                    or not raw['planCode'].endswith('.consumption')):
                continue
            ps = [p for p in raw['pricings'] if p['type'] == 'consumption' and p['mode'] == 'default'
                  and p['commitment'] == 0 and p['interval'] == 1 and p['intervalUnit'] == 'hour'
                  and p['quantity']['min'] == 1 and not p['promotions']]
            if len(ps) != 1 or raw['consumptionConfiguration']['prorataUnit'] != 'second':
                raise ValueError('OVH hourly consumption contract changed')
            gpu = tech['gpu']
            count = positive_int(gpu['number'])
            memory = number(gpu['memory']['size'])
            # API schema: integer microcents = 1e-8 currency units, excluding tax.
            price = number(Decimal(str(ps[0]['price'])) / Decimal(100_000_000))
            if type(ps[0]['price']) is not int or ps[0]['tax'] < 0:
                raise ValueError('OVH integer money contract changed')
            cpu = tech.get('cpu', {})
            rows.append(offer('ovh', raw['planCode'], f"{gpu['model']} {memory:g}GB", count,
                price, stamp, OVH, currency='EUR', gpu_model_raw=gpu['model'],
                vram_raw=f'{memory:g} GB/GPU', product=raw['invoiceName'], tax_basis='excluded',
                vcpu=positive_int(cpu['cores']) if cpu.get('type') == 'vCore' else None,
                # Public Cloud billing guide: second-based usage, initial
                # one-minute minimum. Proration resolution is not the minimum.
                ram_raw=str(tech.get('memory', {}).get('size'))+' GB', minimum_billing_seconds=60,
                price_conditions='FR subsidiary base Linux public-cloud catalog; exact region and capacity unspecified; local/3-AZ prices not inferred.'))
        self.complete = True
        return self.finish(rows)


class VultrCollector(BudgetCollector):
    name = 'vultr'

    def collect(self, *, limit_pages=None):
        stamp = self.begin(limit_pages)
        if stamp is None:
            return []
        rows = []
        for endpoint, key in [('plans', 'plans'), ('plans-metal', 'plans_metal')]:
            cursor, seen, received = '', set(), 0
            for _ in range(10):
                if limit_pages and self.pages_fetched >= limit_pages:
                    return self.finish(rows) if rows else []
                url = VULTR+endpoint+'?'+urlencode({'per_page': 500, **({'cursor': cursor} if cursor else {})})
                payload = self.get(url)
                plans = payload[key]
                received += len(plans)
                self.raw_items += len(plans)
                for raw in plans:
                    if not raw.get('gpu_count') or not raw.get('gpu_type'):
                        continue  # vGPU SKU names are not proof of whole GPUs.
                    count = positive_int(raw['gpu_count'])
                    memory = number(raw['gpu_vram_gb']) / count  # Source total VRAM.
                    model = raw['gpu_type'].removeprefix('NVIDIA_').removeprefix('AMD_').replace('_', ' ')
                    if raw.get('invoice_type') != 'hourly':
                        raise ValueError('Vultr invoice unit changed')
                    costs = raw.get('location_cost', {})
                    if costs:
                        raise ValueError('Vultr regional override needs reviewed schema')
                    for kind, flag, field in [('on_demand','deploy_ondemand','hourly_cost'),
                                              ('interruptible','deploy_preemptible','hourly_cost_preemptible')]:
                        if type(raw[flag]) is not bool:
                            raise ValueError('Vultr deployment flag changed')
                        if not raw[flag]:
                            continue
                        for region in raw['locations'] or [None]:
                            rows.append(offer('vultr', raw['id'], f'{model} {memory:g}GB', count,
                                number(raw[field]), stamp, url, region=region, price_type=kind,
                                vcpu=positive_int(raw['vcpu_count']) if raw.get('vcpu_count') else None,
                                cpu_raw=f"{raw['cpu_threads']} threads" if raw.get('cpu_threads') else None,
                                ram_gb=number(raw['ram'])/1024, gpu_model_raw=raw['gpu_type'],
                                vram_raw=f"{raw['gpu_vram_gb']} GB total", product=raw['id'],
                                minimum_billing_seconds=3600,
                                price_conditions='Published hourly catalog rate; empty locations mean region/capacity unknown. Preemptible deployment may be reclaimed.'))
                nxt = payload['meta']['links']['next']
                if not nxt:
                    if received != payload['meta']['total']:
                        raise ValueError('Vultr catalog total mismatch')
                    break
                if not plans or nxt in seen or not isinstance(nxt, str):
                    raise ValueError('Vultr cursor loop')
                seen.add(nxt)
                cursor = nxt
            else:
                raise ValueError('Vultr exceeds ten-page budget')
        self.complete = True
        return self.finish(rows)
