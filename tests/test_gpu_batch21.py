"""b21 admission holds: no network, invented quotes, ingestion or export."""
from dataclasses import replace
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from pipelines.gpu_prices import http, links, run
from pipelines.gpu_prices.audit import AuditError, audit_site
from pipelines.gpu_prices.build_site import build_site
from pipelines.gpu_prices.i18n import LANGUAGES, load_catalogs
from pipelines.gpu_prices.providers.page_collector import PageCollector, PARSERS
from pipelines.gpu_prices.providers.pages import Document
from pipelines.gpu_prices.providers.registry import PROVIDERS
from pipelines.gpu_prices.schema import Offer, validate_offer
from pipelines.gpu_prices.store import PriceStore

NEW = ('sesterce', 'seeweb', 'gcore', 'jarvislabs', 'modal', 'koyeb')
STAMP = '2026-11-13T04:00:00Z'
REVIEW = json.loads((Path(__file__).parent/'fixtures/batch21/admission.json').read_text(encoding='utf-8'))


def allowed_offer():
    return Offer(provider='azure', sku='Standard_ND96isr_H100_v5',
                 gpu_model='H100 SXM 80GB', gpu_count=8, vcpu=96, ram_gb=1900,
                 region='eastus', price_type='on_demand', currency='USD',
                 price_per_hour=32.0, price_per_gpu_hour=4.0,
                 source_url='https://prices.azure.com/api/retail/prices',
                 collected_at=STAMP)


@pytest.mark.parametrize('name', NEW)
def test_hold_cannot_make_price_requests_or_be_selected_by_cli(name, tmp_path, monkeypatch):
    calls = []
    def refuse(*args, **kwargs):
        calls.append(args)
        raise AssertionError('Held provider must never reach the transport')
    assert REVIEW['providers'][name]['hold']
    source = PROVIDERS[name]['source']
    with pytest.raises(ValueError, match='allowlist'):
        http.HttpClient(opener=refuse).get_bytes(source)
    with PriceStore(':memory:') as store:
        before = [tuple(row) for row in store.connection.execute('SELECT * FROM meta')]
        with pytest.raises(KeyError):
            PageCollector(name, store=store, client=http.HttpClient(opener=refuse))
        assert [tuple(row) for row in store.connection.execute('SELECT * FROM meta')] == before
    monkeypatch.setattr(run, 'configure_logging', refuse)
    db = tmp_path/'must-not-exist.sqlite'
    with pytest.raises(SystemExit) as error:
        run.main(['collect', '--provider', name, '--db', str(db)])
    assert error.value.code == 2 and not db.exists() and calls == []


@pytest.mark.parametrize('name', NEW)
@pytest.mark.parametrize('forged_tier', ['A', 'B', 'C'])
def test_row_flags_cannot_override_provider_hold_and_batch_stays_atomic(name, forged_tier):
    good = allowed_offer()
    forged = replace(good, provider=name, tier=forged_tier,
                     export_allowed=forged_tier == 'A', source_url=PROVIDERS[name]['source'])
    with pytest.raises(ValueError, match='link-only provider'):
        validate_offer(forged)
    with PriceStore(':memory:') as store:
        with pytest.raises(ValueError, match='link-only provider'):
            store.upsert([good, forged])
        assert store.connection.execute('SELECT COUNT(*) FROM offers').fetchone()[0] == 0


@pytest.fixture(scope='module')
def preview(tmp_path_factory):
    folder = tmp_path_factory.mktemp('b21-preview')
    db, out = folder/'prices.sqlite', folder/'site'
    with PriceStore(db) as store:
        store.upsert([allowed_offer()])
        result = build_site(store, out, today=STAMP[:10], built_at=STAMP,
                            site_base_url='https://gpu-cloud-prices.pages.dev')
        assert audit_site(store.connection, out)['status'] == 'passed'
    return db, out, result


@pytest.mark.parametrize('language', LANGUAGES, ids=lambda lang: lang.code)
def test_all_languages_show_plain_official_cards_with_no_invented_prices(preview, language, monkeypatch):
    db, out, result = preview
    tr = load_catalogs()[language.code]
    home = out/language.path('/').lstrip('/')/'index.html'
    doc = Document(home.read_text(encoding='utf-8'))
    for name in NEW:
        monkeypatch.setenv('AFFILIATE_'+name.upper()+'_REF', 'ignored-unreviewed-id')
        matches = doc.root.find(tag='a', **{'data-provider-link':name})
        assert len(matches) == 1
        assert matches[0].attrs['href'] == PROVIDERS[name]['source']
        node = matches[0]
        while node and node.tag != 'article':
            node = node.parent
        assert node and PROVIDERS[name]['name'] in node.text()
        assert tr.text('note_'+name) in node.text()
        assert not any('data-stat' in part.attrs for part in node.walk())
        assert not any(part.has_class('offer-price') for part in node.walk())
        link = links.link_for(name, '', '', '')
        assert not link['is_affiliate'] and link['url'] == PROVIDERS[name]['source']
        assert not urlsplit(link['url']).query
    for file in (out/'data').glob('*.json'):
        payload = json.loads(file.read_text(encoding='utf-8'))
        assert all(row['provider'] not in NEW for row in payload.get('offers', []))


@pytest.mark.parametrize('name', NEW)
def test_independent_audit_rejects_sql_injection_of_a_held_provider(preview, name):
    db, out, result = preview
    # Bypass the writer deliberately: audit must also reject tampered/stale DBs.
    with PriceStore(db, read_only=True) as source, PriceStore(':memory:') as store:
        source.connection.backup(store.connection)
        store.connection.execute("UPDATE offers SET provider=?, tier='A', export_allowed=1", (name,))
        store.connection.commit()
        with pytest.raises(AuditError, match='disabled or link-only provider has observations'):
            audit_site(store.connection, out)


def test_hold_reasons_preserve_scope_unknowns_and_serverless_labels():
    # Missing robots is unknown, never falsely labelled an affirmative ban.
    assert all(REVIEW['providers'][name]['robots'] == 'unknown'
               for name in ('sesterce', 'jarvislabs', 'modal', 'koyeb'))
    assert REVIEW['providers']['seeweb']['robots'] == REVIEW['providers']['gcore']['robots'] == 'allowed'
    assert REVIEW['providers']['jarvislabs']['quote'] is None
    catalogs = load_catalogs()
    for tr in catalogs.values():
        assert all(tr.text('note_'+name).strip() for name in NEW)
    for name in ('modal', 'koyeb'):
        assert 'Serverless, per-second billed' in catalogs['en'].text('note_'+name)
        assert name not in PARSERS
