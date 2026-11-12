"""Public HTML tables: C, attributed display only. No duplicate mobile cards."""
import re
from . import Document, finish, match, memory, money, normalize_gpu, one, record, require, unsupported
from ...models import canonical_model

URLS = ("https://vm.massedcompute.com/pricing",)
TIER = "C"


def parse(html: str, fetched_at: str) -> list[dict]:
    doc = Document(html)
    tables = doc.root.find(tag='table')
    if not tables:
        unsupported(html, 'massedcompute', 'no verified public hourly table')
    rows = []
    for table in tables:
        require(table.end is not None, 'Unclosed Massed Compute table')
        require([n.text() for n in table.find(tag='th')] == ['Qty', 'vCPU', 'RAM', 'Storage', 'Price', ''], 'Massed Compute columns changed')
        group = table.parent.parent
        title = one([n for n in group.children if hasattr(n, 'tag') and n.tag == 'div' and n.has_class('bg-pricing-bone')], 'Massed Compute GPU heading').text()
        capacity = re.search(r'\((\d+(?:\.\d+)?)GB\)', title)
        vram = capacity.group(1)+'GB' if capacity else None
        model = re.sub(r'\s*\[[^]]+\]|\s*\(\d+(?:\.\d+)?GB\)|\s+NVLink$', '', title)
        model = re.sub(r'^H100 SXM5$', 'H100 SXM', model)
        model = canonical_model(normalize_gpu(model, vram).removeprefix('NVIDIA '), vram)
        for tr in one(table.find(tag='tbody'), 'Massed Compute table body').find(tag='tr'):
            cells = [n.text() for n in tr.find(tag='td')]
            require(len(cells) == 6, 'Massed Compute row width changed')
            count = int(match(r'×\s*([1-9]\d*)', cells[0], 'GPU count').group(1))
            if cells[4] == 'Request':
                continue
            price = money(match(r'\$(\d+(?:\.\d+)?)\s*/hr', cells[4], 'instance hourly unit').group(1))
            cpu = int(match(r'[1-9]\d*', cells[1], 'vCPU').group())
            rows.append(record('massedcompute', TIER, URLS[0], fetched_at,
                product=title, sku=f'{title} | {count} GPU | {cells[3]} storage',
                gpu_model_raw=title, gpu_model=model, gpu_count=count, gpu_fraction=1.0,
                vram_raw=vram, vcpu=cpu, ram_raw=memory(cells[2]), price_per_hour=price,
                currency='USD', price_type='on_demand', price_scope='instance',
                billing_unit='instance-hour'))
    return finish(rows)
