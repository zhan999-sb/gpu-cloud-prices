"""Monthly commitments kept separate from hourly offers and rankings."""
import re
from . import Document, match, money, one, record, require
from ...models import canonical_model

URLS = ('https://www.cloudclusters.io/server/gpu',)
TIER = 'B'


def parse(html, fetched_at):
    record('cloudclusters', TIER, URLS[0], fetched_at)
    doc = Document(html)
    require('Single-GPU configurations' in doc.root.text(), 'Single-GPU allocation statement missing')
    cards = doc.root.find(cls='d-plan-y')
    require(bool(cards), 'Cloud Clusters monthly cards missing')
    rows = []
    for card in cards:
        require(card.end is not None, 'Unclosed Cloud Clusters card')
        title = one(card.find(tag='p'), 'Cloud Clusters plan title').text()
        price_node = one([n for n in card.find(tag='div') if n.has_class('d-fs-40')], 'monthly price')
        price = money(match(r'\$\s*(\d+(?:\.\d+)?)\s*/mo', price_node.text(), 'monthly unit').group(1))
        selected = one(card.find(cls='price-item-active'), 'selected commitment').text()
        months = int(match(r'([1-9]\d*)mo', selected, 'commitment months').group(1))
        features = {}
        for li in card.find(tag='li'):
            field, sep, value = li.text().partition(':')
            if sep:
                features[field.strip()] = re.sub(r'[\ue000-\uf8ff]', '', value).strip()
        require(all(k in features for k in ('GPU', 'GPU Memory', 'Location', 'CPU', 'Memory')), 'Cloud Clusters specification missing')
        raw = features['GPU']; capacity = re.match(r'(\d+(?:\.\d+)?\s*GB)\b', features['GPU Memory'])
        require(capacity is not None, 'Cloud Clusters GPU memory missing')
        vram = capacity.group(1)
        count = None if '×' in features['GPU Memory'] else 1
        model = canonical_model(raw, vram)
        if not re.search(r'\d+(?:\.\d+)?GB\b', model):
            model += ' '+vram.replace(' ', '')
        rows.append(dict(provider='cloudclusters', tier=TIER, export_allowed=False,
            source_url=URLS[0], collected_at=fetched_at, sku=title, gpu_model_raw=raw,
            gpu_model=model, gpu_count=count, vram_raw=features['GPU Memory'],
            cpu_raw=features['CPU'], ram_raw=features['Memory'], region=features['Location'],
            currency='USD', price_per_month=price, commitment_months=months,
            tax_basis='unknown', availability='unknown', billing_unit='instance-month',
            is_promotion=card.has_class('plan-y-promotion')))
    require(len({r['sku'] for r in rows}) == len(rows), 'Duplicate Cloud Clusters quote')
    return rows
