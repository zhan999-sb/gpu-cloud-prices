"""b15: canonical identity, buyer ordering and fail-closed summary publication."""

from dataclasses import replace
from pathlib import Path
import re
import urllib.request

import pytest

from pipelines.gpu_prices.audit import AuditError, Page, audit_site
from pipelines.gpu_prices.build_site import build_site
from pipelines.gpu_prices.i18n import LANGUAGES, load_catalogs
from pipelines.gpu_prices.models import canonical_model, category
from pipelines.gpu_prices.providers import clore
from pipelines.gpu_prices.providers.page_collector import normalize
from pipelines.gpu_prices.schema import Offer
from pipelines.gpu_prices.store import PriceStore

STAMP = '2026-11-12T18:00:00Z'
DAY = STAMP[:10]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError('Polish tests must never use the network')
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', refuse)


@pytest.mark.parametrize('raw,vram,expected', [
    ('NVIDIA GeForce RTX 4090', '23 GB', 'RTX 4090 24GB'),
    ('RTX 4090 24GB', None, 'RTX 4090 24GB'),
    ('L40S', '45 GB', 'L40S 48GB'),
    ('L40S 48GB', None, 'L40S 48GB'),
    ('Tesla V100PCIE32GB', '32 GB', 'V100 PCIe 32GB'),
    ('Tesla V100SXM232GB', '32 GB', 'V100 SXM2 32GB'),
    ('V100 32GB', None, 'V100 32GB'),
    ('P104100', '8 GB', 'P104-100 8GB'),
    ('P106100', '6 GB', 'P106-100 6GB'),
    ('RTX 4060 7GB', None, 'RTX 4060 8GB'),
    ('RTX 4060 Ti 7GB', None, 'RTX 4060 Ti 8GB'),
    ('RTX 4060 Ti 15GB', None, 'RTX 4060 Ti 16GB'),
    ('Tesla P40 23GB', None, 'P40 24GB'),
    ('RTX 4000 Ada Generation 19GB', None, 'RTX 4000 Ada 20GB'),
    ('RTX 4080 SUPER 32GB', None, 'RTX 4080 SUPER 32GB'),
    ('RTX 4090 48GB', None, 'RTX 4090 48GB'),
    ('RTX 5090 D 32GB', None, 'RTX 5090 D 32GB'),
    ('RTX 5090 32GB', None, 'RTX 5090 32GB'),
    ('RTX 4090 Laptop GPU 16GB', None, 'RTX 4090 Laptop GPU 16GB'),
    ('RTX 4090 20GB', None, 'RTX 4090 20GB'),
    ('RTX 4060 Ti 14GB', None, 'RTX 4060 Ti 14GB'),
    ('A6000 48GB', None, 'RTX A6000 48GB'),
    ('A5000 24GB', None, 'RTX A5000 24GB'),
    ('Unreviewed GPU 7GB', None, 'Unreviewed GPU 7GB'),
])
def test_canonical_examples_and_sku_boundaries(raw, vram, expected):
    assert canonical_model(raw, vram) == expected
    assert canonical_model(expected, vram) == expected  # Stable across read views.


@pytest.mark.parametrize('raw,vram,expected', [
    ('NVIDIA GeForce RTX 4090', 23, 'RTX 4090 24GB'),
    ('NVIDIA L40S', 45, 'L40S 48GB'),
    ('Tesla V100PCIE32GB', 32, 'V100 PCIe 32GB'),
    ('Tesla V100SXM232GB', 32, 'V100 SXM2 32GB'),
    ('P104100', 8, 'P104-100 8GB'),
    ('P106100', 6, 'P106-100 6GB'),
    ('RTX 4060', 7, 'RTX 4060 8GB'),
    ('RTX 4060 Ti', 7, 'RTX 4060 Ti 8GB'),
    ('RTX 4060 Ti', 15, 'RTX 4060 Ti 16GB'),
    ('Tesla P40', 23, 'P40 24GB'),
    ('RTX 4000 Ada Generation', 19, 'RTX 4000 Ada 20GB'),
])
def test_clore_adapter_retains_reported_name_and_capacity(raw, vram, expected):
    payload={'servers':[{'id':1,'rented':False,'specs':{'gpu':'1x '+raw,'gpuram':vram},
                         'price':{'spot':{'USD-Blockchain':24}}}]}
    row=clore.parse(payload,STAMP)[0]
    offer=normalize(row)
    assert row['gpu_model_raw']==offer.gpu_model_raw==raw
    assert row['vram_raw']==offer.vram_raw==f'{vram} GB'
    assert offer.gpu_model==expected


@pytest.mark.parametrize('name,expected', [
    ('H100 SXM 80GB','data_centre'),('H200 141GB','data_centre'),
    ('B300 SXM6 268GB','data_centre'),('GB300 SXM6 288GB','data_centre'),
    ('A100 80GB','data_centre'),('L40S 48GB','data_centre'),
    ('L4 24GB','data_centre'),('A10 24GB','data_centre'),
    ('A30 24GB','data_centre'),('T4 16GB','data_centre'),
    ('V100 PCIe 32GB','data_centre'),('MI300X 192GB','data_centre'),
    ('RTX 6000 Ada 48GB','workstation'),('RTX PRO 6000 96GB','workstation'),
    ('RTX A6000 48GB','workstation'),('RTX A5000 24GB','workstation'),
    ('A6000 48GB','workstation'),('A5000 24GB','workstation'),
    ('P4000 8GB','workstation'),('RTX 4090 24GB','consumer'),
    ('RTX 3060 12GB','consumer'),('RTX 5090 D 32GB','consumer'),
    ('GTX 1070 8GB','legacy'),('GTX 1660 SUPER 6GB','legacy'),
    ('CMP 30HX 6GB','legacy'),('P104-100 8GB','legacy'),
    ('P106-100 6GB','legacy'),('Tesla M40 24GB','legacy'),
    ('P40 24GB','legacy'),('GT 1030 2GB','legacy'),
])
def test_buyer_taxonomy(name, expected):
    assert category(name)==expected


def make_offer(model, price, **changes):
    return replace(Offer(provider='verda',sku=model,gpu_model=model,gpu_count=1,
                         vcpu=None,ram_gb=None,region=None,price_type='spot',currency='USD',
                         price_per_hour=price,price_per_gpu_hour=price,
                         source_url='https://verda.com/pricing',collected_at=STAMP), **changes)


@pytest.fixture(scope='module')
def polished_site(tmp_path_factory):
    directory=tmp_path_factory.mktemp('polish')
    db=directory/'prices.sqlite'
    with PriceStore(db) as store:
        models=[('GTX 1660 SUPER 6GB',.001),('P104100 8GB',.002),('T4 16GB',.01),
                ('RTX 4060 7GB',.005),('RTX 3060 12GB',.5),('RTX A5000 24GB',.6),
                ('H100 SXM 80GB',2),('H200 141GB',3),('B200 SXM6 180GB',4),
                ('A100 80GB',1),('L40S 45GB',.4),('L40S 48GB',.8),
                ('RTX 4090 23GB',.2),('RTX 4090 24GB',.3),('RTX 5090 32GB',.25),
                ('RTX 6000 Ada 48GB',.9),('RTX PRO 6000 96GB',1.2),
                ('RTX 4080 SUPER 32GB',.7),('RTX 5090 D 32GB',.8),
                ('RTX 4090 Laptop GPU 16GB',.1)]
        rows=[make_offer(model,price) for model,price in models]
        rows += [make_offer('H100 SXM 80GB',.0001,sku='promo',is_from_price=True),
                 make_offer('H100 SXM 80GB',.0002,sku='component',price_scope='gpu_component'),
                 make_offer('H100 SXM 80GB',.0003,sku='included',tax_basis='included'),
                 make_offer('H100 SXM 80GB',.0004,sku='fraction',gpu_count=.5,gpu_fraction=.5,price_per_hour=.0002),
                 make_offer('H100 SXM 80GB',.0005,sku='native',currency='EUR'),
                 make_offer('H100 SXM 80GB',.0006,sku='edge',region='attdallas1'),
                 make_offer('H100 SXM 80GB',.0007,sku='future',effective_from='2026-11-16T00:00:00Z'),
                 make_offer('H100 SXM 80GB',.0008,sku='failed-provider',provider='linode'),
                 make_offer('H100 SXM 80GB',.0009,sku='stale',collected_at='2026-11-11T18:00:00Z'),
                 make_offer('RTX 4090 23GB',.18,sku='raw',gpu_model_raw='NVIDIA GeForce RTX 4090',vram_raw='23 GB'),
                 make_offer('A100 80GB',.7,sku='display',provider='paperspace',tier='C',export_allowed=False)]
        store.upsert(rows)
        store.record_run(provider='linode',started_at=STAMP,status='failed',pages=0,records=0)
        # Native data remains available; no canonicalization rewrites SQL.
        assert store.connection.execute("SELECT gpu_model,vram_raw FROM offers WHERE sku='raw'").fetchone()[:]==('RTX 4090 23GB','23 GB')
        build_site(store,directory,today=DAY,built_at=STAMP,site_base_url='https://gpu-cloud-prices.pages.dev')
    return db,directory


def test_canonical_headlines_grouping_summary_and_raw_transparency(polished_site):
    db,directory=polished_site
    catalogs=load_catalogs()
    for lang in LANGUAGES:
        raw=(directory/lang.prefix/'index.html').read_text(encoding='utf-8')
        page=Page(raw);tr=catalogs[lang.code]
        cards=[c for c in page.cards if c['attrs']['data-list']=='cheapest']
        names=[c['attrs']['data-gpu-model'] for c in cards]
        assert len(names)==len(set(names))
        assert names[:4]==['H100 SXM 80GB','H200 141GB','B200 SXM6 180GB','A100 80GB']
        assert names.index('T4 16GB')<names.index('RTX A5000 24GB')<names.index('RTX 3060 12GB')<names.index('RTX 4060 8GB')<names.index('GTX 1660 SUPER 6GB')
        assert names.count('L40S 48GB')==names.count('RTX 4090 24GB')==1
        assert 'RTX 4080 SUPER 32GB' in names and 'RTX 5090 D 32GB' in names
        assert all('RTX 4090 23GB' not in name for name in names)
        assert re.search(r'<details class="legacy-gpus" data-model-group="legacy">',raw)
        assert tr.text('unknown_value') not in cards[0]['roles'].get('hardware','')
        assert tr.text('tax_unknown') in cards[0]['roles']['offer-badges']
        assert 'H100 SXM 80GB' in page.summary_quotes[0][1] and '$2.00' in page.summary_quotes[0][1]
        assert '$0.180' in page.summary_quotes[2][1]
        assert tr.text('tier_c') in page.summary_quotes[1][1]
        assert all('Laptop' not in text for _,text in page.summary_quotes)
        assert tr.text('reported_model') in raw and 'NVIDIA GeForce RTX 4090' in raw and '23 GB' in raw
        assert 'C' in next(c['roles']['offer-badges'] for c in cards if c['attrs']['data-provider']=='paperspace')
        assert tr.text('from_price') in raw
        assert '<span class="price-unit">' in raw and '<span class="checked">' in raw
    assert audit_site(db,directory)['status']=='passed'
    with PriceStore(db,read_only=True) as store:
        assert len(store.daily_series('H100 SXM 80GB',price_type='spot'))==2
        assert len([m for m in store.models() if m.startswith('L40S')])==1


@pytest.mark.parametrize('damage', ['price','model','type','provider','region','offers','providers','clock','marker','remove','duplicate','legacy-open','wrong-group','raw-name'])
def test_audit_rejects_polish_semantic_corruption(polished_site,damage):
    db,directory=polished_site
    path=directory/'index.html';original=path.read_text(encoding='utf-8')
    strip=re.search(r'<aside class="summary-strip".*?</aside>',original).group()
    if damage in {'price','model','type','provider','region','offers','providers','clock','marker'}:
        before,after={
            'price':('data-format="money">$2.00','data-format="money">$0.001'),
            'model':('>H100 SXM 80GB</span>','>T4 16GB</span>'),
            'type':('Spot','On-demand'), 'provider':('Verda / DataCrunch','Azure'),
            'region':('Region not specified by provider','US host'),
            'offers':('/summary/offers" data-format="text">','/summary/offers" data-format="text">9'),
            'providers':('/summary/providers" data-format="text">','/summary/providers" data-format="text">9'),
            'clock':('18:00 UTC','19:00 UTC'),
            'marker':('data-stat="/summary/offers"','data-hidden="/summary/offers"'),
        }[damage]
        assert before in strip
        changed=original.replace(strip,strip.replace(before,after,1),1)
    elif damage=='remove':changed=original.replace(strip,'',1)
    elif damage=='duplicate':
        card=re.search(r'<article class="offer-card".*?</article>',original).group()
        changed=original.replace(card,card+card,1)
    elif damage=='legacy-open':changed=original.replace('class="legacy-gpus"','class="legacy-gpus" open',1)
    elif damage=='wrong-group':changed=original.replace('data-model-group="consumer"','data-model-group="workstation"',1)
    else:changed=original.replace('>NVIDIA GeForce RTX 4090</span>','>NVIDIA H100</span>',1)
    assert changed!=original
    try:
        path.write_text(changed,encoding='utf-8')
        with pytest.raises(AuditError,match='Statistics audit FAILED'):
            audit_site(db,directory)
    finally:
        path.write_text(original,encoding='utf-8')
