"""Offline GCP catalog contracts, credential boundaries and eight-language audit."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from io import BytesIO
import json
from pathlib import Path
import traceback
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from functools import partial

import pytest

from pipelines.gpu_prices import http, run
from pipelines.gpu_prices.audit import audit_site
from pipelines.gpu_prices.build_site import build_site
from pipelines.gpu_prices.i18n import LANGUAGES, load_catalogs
from pipelines.gpu_prices.providers import gcp
from pipelines.gpu_prices.providers.registry import PROVIDERS
from pipelines.gpu_prices.regions import eligible
from pipelines.gpu_prices.schema import parse_utc
from pipelines.gpu_prices.store import PriceStore

# This module tests its feature in all eight languages. The complete workload
# matrix and its independent audit remain in test_gpu_workloads.py.
build_site = partial(build_site, include_workloads=False)
audit_site = partial(audit_site, require_workloads=False)

FIXTURES = Path(__file__).parent / 'fixtures/gcp'
STAMP = '2026-11-13T12:00:00Z'
DUMMY = 'synthetic-not-a-credential/+?&='


def page(number):
    return json.loads((FIXTURES / f'page{number}.json').read_text(encoding='utf-8'))


def fetch(url):
    return page(2 if 'pageToken=' in url else 1)


@pytest.fixture(autouse=True)
def fake_key(monkeypatch):
    # No test reads or sends the operator's real runtime environment value.
    monkeypatch.setenv(gcp.KEY_ENV, DUMMY)


def collector(store, **kw):
    return gcp.GcpCollector(store=store, fetch=kw.pop('fetch', fetch),
                            clock=kw.pop('clock', lambda: STAMP), **kw)


def test_catalog_pagination_math_regions_and_provenance():
    urls = []
    with PriceStore(':memory:') as store:
        c = collector(store, fetch=lambda url: (urls.append(url), fetch(url))[1])
        offers = c.collect()
        assert c.complete and not c.skipped and c.pages_fetched == 2 and c.raw_items == 15
        assert len(offers) == 10
        queries = [parse_qs(urlsplit(u).query) for u in urls]
        assert queries[0] == {'currencyCode': ['USD'], 'pageSize': ['5000'], 'key': [DUMMY]}
        assert queries[1] == {**queries[0], 'pageToken': ['fixture-page-2']}
        assert len({(o.sku, o.region, o.price_type) for o in offers}) == 10
        t4 = [o for o in offers if o.sku == '88B8-C3ED-03F0']
        assert {o.region for o in t4} == {'us-central1', 'us-east1'}
        assert all(o.gpu_model == 'T4 16GB' and o.price_per_gpu_hour == .35 for o in t4)
        h100 = next(o for o in offers if o.sku == 'FA01-0000-0001')
        assert h100.gpu_model == 'H100 80GB' and h100.price_per_gpu_hour == 9.8
        assert h100.effective_from == '2026-11-12T00:00:00Z'
        assert next(o for o in offers if o.sku == '1A25-07A3-AB6D').price_per_gpu_hour == .110000001
        assert next(o for o in offers if o.sku == 'EDE5-960E-2433').region == 'asia-east1'
        assert {'A100 40GB', 'A100 80GB', 'L4 24GB', 'H100 80GB Plus'} <= {o.gpu_model for o in offers}
        for offer in offers:
            assert offer.price_scope == 'gpu_component' and offer.gpu_count is None
            assert offer.gpu_fraction is offer.vcpu is offer.ram_gb is None
            assert offer.billing_unit == offer.source_billing_unit == 'GPU-hour'
            assert offer.price_per_hour == offer.price_per_gpu_hour == offer.original_price
            assert offer.original_currency == offer.currency == 'USD'
            assert offer.price_conditions == gcp.COMPONENT_NOTE
            assert offer.tax_basis == offer.availability == 'unknown'
            assert offer.tier == 'A' and offer.export_allowed and not eligible(offer)
            assert offer.source_url == gcp.SOURCE_URL and 'key=' not in offer.source_url
        assert store.upsert(offers) == 10
        assert store.upsert(offers) == 0
        before = list(store.connection.execute('SELECT * FROM meta'))
        assert c.collect() == [] and c.skipped and c.pages_fetched == 0
        assert c.skip_reason == 'source attempted within 6 hours'
        assert before == list(store.connection.execute('SELECT * FROM meta'))


def test_limit_pages_is_truthfully_partial():
    with PriceStore(':memory:') as store:
        c = collector(store)
        assert len(c.collect(limit_pages=1)) == 4
        assert not c.complete and not c.skipped and c.pages_fetched == 1


@pytest.mark.parametrize('limit', [0, -1, True, 1.5])
def test_invalid_page_limit_never_reserves_budget(limit):
    with PriceStore(':memory:') as store:
        before = list(store.connection.execute('SELECT * FROM meta'))
        with pytest.raises(ValueError, match='limit_pages'):
            collector(store).collect(limit_pages=limit)
        assert before == list(store.connection.execute('SELECT * FROM meta'))


@pytest.mark.parametrize('value', [None, '', '  '])
def test_missing_key_skip_without_http_or_budget(monkeypatch, caplog, value):
    if value is None:
        monkeypatch.delenv(gcp.KEY_ENV)
    else:
        monkeypatch.setenv(gcp.KEY_ENV, value)
    with PriceStore(':memory:') as store, caplog.at_level('INFO'):
        before = list(store.connection.execute('SELECT * FROM meta'))
        c = collector(store, fetch=lambda u: pytest.fail('unexpected HTTP'))
        assert c.collect() == [] and c.skipped and not c.complete
        assert c.pages_fetched == c.raw_items == 0
        assert gcp.KEY_ENV in c.skip_reason and 'skipped' in caplog.text
        assert before == list(store.connection.execute('SELECT * FROM meta'))
        assert not store.provider_runs()


@pytest.mark.parametrize('status', [400, 403])
def test_http_errors_never_expose_url_body_or_key(status, caplog):
    calls = []
    def fail(req, timeout):
        calls.append(1)
        raise HTTPError(req.full_url, status, 'echo ' + DUMMY, {}, BytesIO(DUMMY.encode()))
    client = http.HttpClient(opener=fail, sleep=lambda _: pytest.fail('unexpected retry'))
    url = gcp.API_URL + '?key=' + DUMMY
    with caplog.at_level('DEBUG'), pytest.raises(http.GcpRequestError) as caught:
        client.get_json(url)
    exposed = caplog.text + ''.join(traceback.format_exception(caught.value))
    assert len(calls) == 1 and caught.value.status == status
    assert f'HTTP {status}' in exposed
    assert DUMMY not in exposed and 'key=' not in exposed and url not in exposed


@pytest.mark.parametrize('error', [URLError, ValueError, RuntimeError])
def test_transport_errors_are_fixed_and_chained_secrets_suppressed(error, caplog):
    def fail(*args, **kw):
        raise error(gcp.API_URL + '?key=' + DUMMY)
    with pytest.raises(http.GcpRequestError) as caught:
        http.HttpClient(opener=fail).get_bytes(gcp.API_URL + '?key=' + DUMMY)
    assert DUMMY not in caplog.text + ''.join(traceback.format_exception(caught.value))
    with PriceStore(':memory:') as store, pytest.raises(http.GcpRequestError) as injected:
        collector(store, fetch=fail).collect()
    assert DUMMY not in ''.join(traceback.format_exception(injected.value))


@pytest.mark.parametrize('body', [b'not JSON', b'"not an object"'])
def test_invalid_http_json_has_no_private_body(body):
    class Response(BytesIO):
        def geturl(self):
            return gcp.API_URL
    client = http.HttpClient(opener=lambda *a, **kw: Response(body))
    with pytest.raises((ValueError, http.GcpRequestError)) as caught:
        client.get_json(gcp.API_URL + '?key=' + DUMMY)
    assert DUMMY not in ''.join(traceback.format_exception(caught.value))


@pytest.mark.parametrize('status', [400, 403])
def test_failed_attempt_budget_persists_for_six_hours(tmp_path, status):
    calls = []
    def fail(url):
        calls.append(1)
        raise HTTPError(url, status, DUMMY, {}, BytesIO(DUMMY.encode()))
    path = tmp_path / 'budget.sqlite'
    with PriceStore(path) as store:
        c = collector(store, fetch=fail)
        with pytest.raises(http.GcpRequestError):
            c.collect()
        assert not store.latest_prices() and c.pages_fetched == 1
    with PriceStore(path) as store:
        almost = (parse_utc(STAMP) + timedelta(hours=6, microseconds=-1)).isoformat()
        c = collector(store, fetch=fail, clock=lambda: almost)
        assert c.collect() == [] and c.skipped and len(calls) == 1
        ready = (parse_utc(STAMP) + timedelta(hours=6)).isoformat()
        c = collector(store, fetch=fail, clock=lambda: ready)
        with pytest.raises(http.GcpRequestError):
            c.collect()
        assert len(calls) == 2


@pytest.mark.parametrize('change', ['tiered', 'start', 'unit', 'currency', 'units',
                                  'negative', 'overflow', 'boolean', 'zero', 'timeline'])
def test_unsafe_gpu_price_expressions_fail_closed(change):
    row = page(1)['skus'][0]
    expression = row['pricingInfo'][0]['pricingExpression']
    rate = expression['tieredRates'][0]
    money = rate['unitPrice']
    if change == 'tiered': expression['tieredRates'].append(deepcopy(rate))
    if change == 'start': rate['startUsageAmount'] = 1
    if change == 'unit': expression['usageUnit'] = 's'
    if change == 'currency': money['currencyCode'] = 'EUR'
    if change == 'units': money['units'] = 1
    if change == 'negative': money['nanos'] = -1
    if change == 'overflow': money['nanos'] = 1_000_000_000
    if change == 'boolean': money['nanos'] = True
    if change == 'zero': money.update(units='0', nanos=0)
    if change == 'timeline': row['pricingInfo'].append(deepcopy(row['pricingInfo'][0]))
    with pytest.raises(ValueError):
        gcp.normalize_item(row, STAMP)


def test_future_prices_do_not_replace_current_or_create_early_offers():
    row = page(2)['skus'][0]
    assert gcp.normalize_item(row, STAMP)[0].price_per_hour == 9.8
    row['pricingInfo'] = row['pricingInfo'][-1:]
    assert gcp.normalize_item(row, STAMP) == []


@pytest.mark.parametrize('regions', [[], ['global']])
def test_unknown_region_does_not_guess_from_description(regions):
    row = page(1)['skus'][0]
    row['serviceRegions'] = regions
    assert gcp.normalize_item(row, STAMP)[0].region is None


@pytest.mark.parametrize('regions', [['Americas'], ['us-central1-a'], ['us-central1'] * 2, None])
def test_invalid_regions_are_refused(regions):
    row = page(1)['skus'][0]
    row['serviceRegions'] = regions
    with pytest.raises(ValueError):
        gcp.normalize_item(row, STAMP)


@pytest.mark.parametrize('description', [
    'Spot Preemptible Nvidia Tesla T4 GPU running in Americas',
    'Preemptible Nvidia Tesla T4 GPU running in Americas',
    'Nvidia Tesla T4 GPU attached to Spot Preemptible VMs running in Americas',
])
def test_description_also_identifies_spot(description):
    row = page(1)['skus'][0]
    row['description'] = description
    assert gcp.normalize_item(row, STAMP)[0].price_type == 'spot'


def test_core_ram_commitments_workstation_and_unreviewed_shapes_are_excluded():
    rows = page(2)['skus']
    assert all(gcp.normalize_item(r, STAMP) == [] for r in rows[6:])
    for description in ['Nvidia Tesla T4 Virtual Workstation GPU running in Americas',
                        'Nvidia Future X GPU running in Americas', 'A3 Instance Core running in Americas']:
        row = page(1)['skus'][0]
        row['description'] = description
        assert gcp.normalize_item(row, STAMP) == []
    row = page(1)['skus'][0]
    row['name'] = row['name'].replace('6F81-5844-456A', '152E-C115-5142')
    with pytest.raises(ValueError, match='service'):
        gcp.normalize_item(row, STAMP)


@pytest.mark.parametrize('change', ['cycle', 'duplicate', 'bad_token', 'bad_rows', 'error', 'empty'])
def test_failed_pagination_never_saves_partial_batch(change):
    def broken(url):
        data = fetch(url)
        if 'pageToken=' in url:
            if change == 'cycle': data['nextPageToken'] = 'fixture-page-2'
            if change == 'duplicate': data['skus'].append(page(1)['skus'][0])
            if change == 'bad_token': data['nextPageToken'] = 3
            if change == 'bad_rows': data['skus'].append('bad row')
            if change == 'error': data['error'] = {'message': 'failure'}
        if change == 'empty': return {'skus': []}
        return data
    with PriceStore(':memory:') as store:
        c = collector(store, fetch=broken)
        with pytest.raises(ValueError): c.collect()
        assert not c.complete and not store.latest_prices()


def test_http_endpoint_and_redirect_boundary():
    assert http.validate_url(gcp.SOURCE_URL) == gcp.SOURCE_URL
    for url in [gcp.API_URL.replace('6F81-5844-456A', 'other-service'),
                gcp.API_URL.replace('https:', 'http:'),
                gcp.API_URL.replace('cloudbilling.googleapis.com', 'example.com')]:
        with pytest.raises(ValueError): http.validate_url(url)
    with pytest.raises(ValueError):
        http._NoRedirects().redirect_request(None, None, 302, '', {}, 'https://example.com')


def test_page_budget_stops_unbounded_distinct_tokens(monkeypatch):
    monkeypatch.setattr(gcp, 'MAX_PAGES', 2)
    calls = []
    def endless(url):
        calls.append(1)
        data = page(1)
        data['skus'] = data['skus'][:1]
        row = data['skus'][0]
        row['skuId'] = f'FA02-0000-000{len(calls)}'
        row['name'] = 'services/6F81-5844-456A/skus/' + row['skuId']
        data['nextPageToken'] = f'page-{len(calls)}'
        return data
    with PriceStore(':memory:') as store, pytest.raises(ValueError, match='page budget'):
        collector(store, fetch=endless).collect()
    assert len(calls) == 2


@pytest.mark.parametrize('field', ['category', 'pricingInfo', 'pricingExpression', 'unitPrice'])
def test_malformed_nested_contract_is_a_provider_error(field):
    row = page(1)['skus'][0]
    if field == 'category': row['category'] = None
    if field == 'pricingInfo': row['pricingInfo'] = [None]
    if field == 'pricingExpression': row['pricingInfo'][0]['pricingExpression'] = None
    if field == 'unitPrice': row['pricingInfo'][0]['pricingExpression']['tieredRates'][0]['unitPrice'] = None
    with pytest.raises(ValueError):
        gcp.normalize_item(row, STAMP)


def test_cli_missing_key_preserves_skip_contract(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(gcp.KEY_ENV)
    monkeypatch.setattr(run, 'configure_logging', lambda: None)
    path = tmp_path / 'skip.sqlite'
    assert run.main(['collect', '--provider', 'gcp', '--db', str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['collections'] == {}
    assert report['catalog_skips']['gcp']['status'] == 'skipped'
    assert gcp.KEY_ENV in report['catalog_skips']['gcp']['reason']
    assert report['collection']['status'] == 'skipped'
    with PriceStore(path) as store:
        assert not store.provider_runs() and not store.latest_prices()


@pytest.mark.parametrize('status', [400, 403])
def test_cli_failure_is_partial_credential_safe_and_does_not_replace_prices(
        tmp_path, monkeypatch, capsys, caplog, status):
    from ci.collection_gate import classify
    path = tmp_path / 'partial.sqlite'
    with PriceStore(path) as store:
        old = gcp.normalize_item(page(1)['skus'][0], STAMP)
        store.upsert(old)
    def failed_factory(store):
        def fail(url):
            if 'pageToken=' in url:
                raise HTTPError(url, status, DUMMY, {}, BytesIO(DUMMY.encode()))
            return page(1)
        return collector(store, fetch=fail)
    class Azure:
        pages_fetched = raw_items = 1
        complete = True
        def collect(self, **kw):
            return [replace(old[0], provider='azure', sku='offline-success')]
    monkeypatch.setattr(run, 'configure_logging', lambda: None)
    monkeypatch.setattr(run, 'GcpCollector', failed_factory)
    monkeypatch.setattr(run, 'AzureCollector', Azure)
    with caplog.at_level('INFO'):
        code = run.main(['collect', '--provider', 'azure', '--provider', 'gcp', '--db', str(path)])
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert code == 1 and report['collections']['gcp']['status'] == 'failed'
    assert report['collections']['gcp']['pages'] == 2 and report['collections']['gcp']['records'] == 0
    assert report['collections']['azure']['status'] == 'complete'
    assert classify(report, code)[0]  # Existing CI permits a genuinely partial failure.
    exposed = output.out + output.err + caplog.text
    assert f'HTTP {status}' in exposed and DUMMY not in exposed and 'key=' not in exposed
    assert 'Traceback' not in caplog.text
    with PriceStore(path) as store:
        assert store.provider_runs()['gcp']['status'] == 'failed'
        assert len([o for o in store.latest_prices() if o.provider == 'gcp']) == 2


def test_eight_languages_exports_component_scope_and_independent_audit(tmp_path, monkeypatch):
    monkeypatch.setattr('pipelines.gpu_prices.store.utc_now', lambda: STAMP)
    assert PROVIDERS['gcp']['enabled'] and PROVIDERS['gcp']['tier'] == 'A'
    with PriceStore(tmp_path / 'audit.sqlite') as store:
        offers = collector(store).collect()
        store.upsert(offers)
        store.record_run(provider='gcp', started_at=STAMP, status='complete', pages=2, records=10)
        out = tmp_path / 'preview'
        result = build_site(store, out, site_base_url='https://gpu-cloud-prices.pages.dev',
                            built_at=STAMP, today=STAMP[:10])
        assert result['offers_today'] == 10
        audited = audit_site(store.connection, out)
        assert audited['status'] == 'passed' and audited['language_sitemaps'] == 8
        catalogs = load_catalogs()
        assert len(LANGUAGES) == 8
        for language in LANGUAGES:
            tr = catalogs[language.code]
            method = (out / language.prefix / 'methodology.html').read_text(encoding='utf-8')
            model = (out / language.prefix / 't4-16gb/index.html').read_text(encoding='utf-8')
            assert tr.html('note_gcp') in method
            assert tr.html('gpu_component_note') in model
            assert tr.html('scope_gpu_component') in model
        exported = json.loads((out / 'data/t4-16gb.json').read_text(encoding='utf-8'))
        assert exported['offers'] and all(o['price_scope'] == 'gpu_component' for o in exported['offers'])
        assert DUMMY not in json.dumps(exported) and 'key=' not in json.dumps(exported)
        # Corrupt a row to masquerade as a full VM: the independent audit refuses it.
        store.connection.execute("UPDATE offers SET price_scope='instance' WHERE provider='gcp'")
        store.connection.commit()
        with pytest.raises(ValueError, match='GCP GPU component provenance mismatch'):
            audit_site(store.connection, out)


def test_ci_key_mapping_is_confined_to_collect_step():
    text = (Path(__file__).resolve().parents[1] / '.github/workflows/gpu-site.yml').read_text(encoding='utf-8')
    mapping = 'GCP_BILLING_API_KEY: ${{ secrets.GCP_BILLING_API_KEY }}'
    assert text.count(mapping) == 1
    step = text.split('- name: Collect prices and apply provider failure policy', 1)[1]
    assert mapping in step.split('\n      - name:', 1)[0]
