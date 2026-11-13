"""Offline only. Hand-written tiny fixtures model Microsoft's documented shape."""

from dataclasses import replace
from datetime import datetime, timezone
from email.message import Message
from html.parser import HTMLParser
from html import unescape
from http.client import IncompleteRead
from io import BytesIO
import json
from pathlib import Path
import re
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlsplit
import xml.etree.ElementTree as ET

import pytest

from pipelines.gpu_prices import http
from pipelines.gpu_prices.build_site import DEFAULT_SITE_BASE_URL, build_site, model_slug, model_slugs, model_statistics
from pipelines.gpu_prices.catalog import AZURE_SKUS, lookup_azure_sku
from pipelines.gpu_prices.i18n import CATALOG_DIR, LANGUAGES, load_catalogs, placeholders
from pipelines.gpu_prices.providers.azure import API_URL, AzureCollector, initial_url, normalize_item
from pipelines.gpu_prices.schema import Offer, validate_offer
from pipelines.gpu_prices.store import PriceStore
from pipelines.gpu_prices.audit import AuditError, Page, audit_site, live_sample
from pipelines.gpu_prices.links import link_for
from pipelines.gpu_prices.regions import region_class

# Synthetic observations, never imported by production code or the live smoke run.
ITEM = json.loads(r'''{
  "currencyCode": "USD", "tierMinimumUnits": 0.0,
  "retailPrice": 24.0, "unitPrice": 24.0,
  "armRegionName": "eastus", "location": "US East",
  "effectiveStartDate": "2026-01-01T00:00:00Z",
  "meterId": "synthetic-meter", "meterName": "ND96isr H100 v5",
  "productName": "Virtual Machines NDH100v5 Series", "skuName": "ND96isr H100 v5",
  "serviceName": "Virtual Machines", "serviceFamily": "Compute",
  "unitOfMeasure": "1 Hour", "type": "Consumption",
  "isPrimaryMeterRegion": true, "armSkuName": "Standard_ND96isr_H100_v5"
}''')
STAMP = "2026-11-12T08:00:00Z"
TODAY = "2026-11-12"
NEXT = API_URL + "?$skip=1000"


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    import urllib.request
    def refuse(*args, **kwargs):
        raise AssertionError("Offline suite attempted a real network request")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)


def item(**changes):
    return {**ITEM, **changes}


def offer(**changes):
    value = normalize_item(item(), source_url=API_URL, collected_at=STAMP)
    return replace(value, **changes)


class Response(BytesIO):
    def __init__(self, payload, url=API_URL):
        super().__init__(json.dumps(payload).encode("utf-8"))
        self.url = url

    def geturl(self):
        return self.url


class FakeClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.urls = []

    def get_json(self, url):
        self.urls.append(url)
        return self.pages.pop(0)


def collector(pages, **kwargs):
    return AzureCollector(client=FakeClient(pages), clock=lambda: STAMP, **kwargs)


def test_known_unknown_mapping(caplog):
    spec = lookup_azure_sku("Standard_ND96isr_H100_v5")
    assert (spec.gpu_model, spec.gpu_count, spec.vcpu, spec.ram_gb) == ("H100 SXM 80GB", 8, 96, 1900)
    assert lookup_azure_sku("Standard_NC96ads_A100_v4").gpu_count == 4
    assert lookup_azure_sku("Standard_NC40ads_H100_v5").gpu_model == "H100 NVL 94GB"
    assert lookup_azure_sku("Standard_NV8ads_V710_v5").gpu_count == pytest.approx(1 / 3)
    assert lookup_azure_sku("Standard_NV12ads_V710_v5").gpu_count == .5
    assert lookup_azure_sku("Standard_NV24ads_V710_v5").gpu_count == 1
    assert lookup_azure_sku("Standard_Imaginary_L40S") is None
    assert "Unknown Azure GPU SKU" in caplog.text
    assert all(spec.source_url.startswith("https://learn.microsoft.com/") for spec in AZURE_SKUS.values())


def test_gpu_hour_normalization_and_fractional_gpu():
    assert offer().price_per_gpu_hour == 3
    fractional = normalize_item(item(armSkuName="Standard_NV6ads_A10_v5", retailPrice=.6),
                                source_url=API_URL, collected_at=STAMP)
    assert fractional.gpu_count == pytest.approx(1 / 6)
    assert fractional.price_per_gpu_hour == pytest.approx(3.6)
    assert fractional.gpu_model == "A10 24GB"
    spot = normalize_item(item(meterName="ND96isr H100 v5 Spot"), source_url=API_URL, collected_at=STAMP)
    assert spot.price_type == "spot"


@pytest.mark.parametrize("changes", [
    {"gpu_count": 0}, {"gpu_count": True}, {"vcpu": 1.5}, {"ram_gb": -1},
    {"price_per_hour": float("nan")}, {"price_per_gpu_hour": float("inf")},
    {"price_per_gpu_hour": 100}, {"currency": "usd"}, {"price_type": "free"},
    {"collected_at": "2026-11-12T08:00:00"}, {"collected_at": "2026-11-12T08:00:00+08:00"},
    {"source_url": "javascript:alert(1)"}, {"source_url": "https://user:pass@example.test/"},
    {"region": ""},
])
def test_validation(changes):
    with pytest.raises(ValueError):
        validate_offer(offer(**changes))


@pytest.mark.parametrize("changes", [
    {"productName": "Virtual Machines ND Series Windows"}, {"productName": "Virtual Machines RHEL"},
    {"type": "Reservation"}, {"type": "DevTestConsumption"}, {"serviceName": "Storage"},
    {"unitOfMeasure": "100 Hours"}, {"isPrimaryMeterRegion": False}, {"currencyCode": "EUR"},
    {"tierMinimumUnits": 1}, {"meterName": "ND96 Low Priority"}, {"retailPrice": 0},
    {"retailPrice": -1}, {"retailPrice": True}, {"retailPrice": "3.0"},
    {"armRegionName": ""}, {"armSkuName": "Standard_ND999_unknown"},
])
def test_normalizer_excludes_unsupported_meters(changes):
    assert normalize_item(item(**changes), source_url=API_URL, collected_at=STAMP) is None


def test_query_and_paging():
    query = parse_qs(urlsplit(initial_url()).query)
    expression = query["$filter"][0]
    assert "serviceName eq 'Virtual Machines'" in expression
    assert "priceType eq 'Consumption'" in expression
    assert all(prefix in expression for prefix in ("Standard_NC", "Standard_ND", "Standard_NV"))
    assert "armRegionName" not in expression
    assert "contains(armSkuName" in expression and "startswith(" not in expression
    assert query["currencyCode"] == ["'USD'"]
    delays = []
    client = collector([
        {"Items": [item()], "NextPageLink": NEXT},
        {"Items": [item(armRegionName="westus", retailPrice=8, meterName="H100 Spot")], "NextPageLink": None},
    ], sleep=delays.append)
    records = client.collect()
    assert len(records) == 2
    assert client.complete and client.pages_fetched == 2
    assert delays == [1.0]
    assert client.client.urls == [initial_url(), NEXT]
    assert {value.source_url for value in records} == {initial_url(), NEXT}


def test_page_limit_and_invalid_limits():
    client = collector([{"Items": [item()], "NextPageLink": NEXT}], sleep=lambda _: pytest.fail("extra page sleep"))
    assert len(client.collect(limit_pages=1)) == 1
    assert not client.complete and client.pages_fetched == 1
    with pytest.raises(ValueError):
        client.collect(limit_pages=0)


@pytest.mark.parametrize("url", [
    "https://example.test/api/retail/prices", "https://prices.azure.com.evil.test/api/retail/prices",
    "http://prices.azure.com/api/retail/prices", "https://prices.azure.com:444/api/retail/prices",
    "https://user:secret@prices.azure.com/api/retail/prices", "https://prices.azure.com/other",
    "file:///etc/passwd", "https://prices.azure.com/api/retail/prices#fragment",
    "https://prices.azure.com/api/retail/prices\n", "//prices.azure.com/api/retail/prices",
])
def test_host_allowlist_before_any_http(url):
    def forbid(*args, **kwargs):
        pytest.fail("unapproved URL reached HTTP boundary")
    with pytest.raises(ValueError):
        http.HttpClient(opener=forbid).get_json(url)
    client = collector([{"Items": [item()], "NextPageLink": url}])
    with pytest.raises(ValueError):
        client.collect()
    assert len(client.client.urls) == 1


def test_documented_port_and_redirect_refusal():
    assert http.validate_url("https://prices.azure.com:443/api/retail/prices?$skip=1000")
    with pytest.raises(ValueError, match="redirect"):
        http._NoRedirects().redirect_request(None, None, 302, "Found", {}, "https://evil.test/")


def test_paging_cycle_and_invalid_response():
    client = collector([{"Items": [], "NextPageLink": NEXT}, {"Items": [], "NextPageLink": NEXT}], sleep=lambda _: None)
    with pytest.raises(ValueError, match="cycle"):
        client.collect()
    for payload in ({}, {"Items": {}}, {"Items": ["bad"]}, {"Items": [], "NextPageLink": 4}):
        with pytest.raises(ValueError):
            collector([payload]).collect()


def test_effective_dates_and_ambiguous_rates(caplog):
    client = collector([{"Items": [item(retailPrice=16), item(retailPrice=8, effectiveStartDate="2025-01-01T00:00:00Z"),
                                    item(retailPrice=1, effectiveStartDate="2099-01-01T00:00:00Z"),
                                    item(armRegionName="missing-date", effectiveStartDate=None)], "NextPageLink": None}])
    assert [value.price_per_hour for value in client.collect()] == [16]
    ambiguous = collector([{"Items": [item(retailPrice=16), item(retailPrice=8)], "NextPageLink": None}])
    assert ambiguous.collect() == []
    assert "Conflicting" in caplog.text


def test_retry_timeout_ua_exponential_backoff_and_jitter():
    calls, delays = [], []
    def opener(request, timeout):
        calls.append((request, timeout))
        if len(calls) < 5:
            raise URLError("synthetic DNS failure")
        return Response({"Items": [], "NextPageLink": None})
    assert http.HttpClient(opener=opener, sleep=delays.append, jitter=lambda: .25).get_json(API_URL)["Items"] == []
    assert delays == [1.25, 2.25, 4.25, 8.25]
    assert len(calls) == 5
    assert all(timeout == 60 and request.get_header("User-agent") == http.USER_AGENT for request, timeout in calls)


def test_incomplete_body_retries():
    calls, delays = [], []
    def opener(request, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise IncompleteRead(b"truncated", 100)
        return Response({"Items": []})
    assert http.HttpClient(opener=opener, sleep=delays.append, jitter=lambda: 0).get_json(API_URL) == {"Items": []}
    assert len(calls) == 2 and delays == [1]


def test_exhausted_retries_and_permanent_failure():
    calls, delays = [], []
    def fail(request, timeout):
        calls.append(1)
        raise TimeoutError("synthetic timeout")
    with pytest.raises(TimeoutError):
        http.HttpClient(opener=fail, sleep=delays.append, jitter=lambda: 0).get_json(API_URL)
    assert len(calls) == 5 and delays == [1, 2, 4, 8]
    def bad_request(request, timeout):
        raise HTTPError(API_URL, 400, "Bad request", Message(), None)
    with pytest.raises(HTTPError):
        http.HttpClient(opener=bad_request, sleep=lambda _: pytest.fail("400 retried")).get_json(API_URL)


@pytest.mark.parametrize("retry_after,expected", [("12", 12), ("Thu, 12 Nov 2026 08:00:15 GMT", 15), ("invalid", 1.5)])
def test_retry_after_seconds_and_http_date(retry_after, expected):
    delays, calls = [], []
    def opener(request, timeout):
        calls.append(1)
        if len(calls) == 1:
            headers = Message()
            headers["Retry-After"] = retry_after
            raise HTTPError(API_URL, 429, "Slow down", headers, None)
        return Response({"Items": []})
    client = http.HttpClient(opener=opener, sleep=delays.append, jitter=lambda: .5,
                             now=lambda: datetime(2026, 11, 12, 8, tzinfo=timezone.utc))
    client.get_json(API_URL)
    assert delays == [expected]


def test_store_idempotency_newer_wins_and_daily_series(tmp_path):
    path = tmp_path / "data" / "prices.sqlite"
    with PriceStore(path) as store:
        assert store.upsert([offer()]) == 1
        assert store.upsert([offer()]) == 0
        earlier = offer(price_per_hour=8, price_per_gpu_hour=1, collected_at="2026-11-12T07:00:00+00:00")
        assert store.upsert([earlier]) == 0
        newer = offer(price_per_hour=16, price_per_gpu_hour=2, collected_at="2026-11-12T09:00:00Z")
        assert store.upsert([newer]) == 1
        yesterday = offer(price_per_hour=8, price_per_gpu_hour=1, collected_at="2026-11-11T09:00:00Z")
        west = offer(region="westus", price_per_hour=32, price_per_gpu_hour=4)
        store.upsert([yesterday, west])
        latest = store.latest_prices()
        assert len(latest) == 2 and latest[0].price_per_gpu_hour == 2
        assert len(store.latest_prices(day="2026-11-11")) == 1
        assert store.latest_prices(day="2026-11-13") == []
        series = store.daily_series("H100 SXM 80GB", price_type="on_demand")
        assert [(p["min"], p["median"], p["max"], p["count"]) for p in series] == [(1, 1, 1, 1), (2, 3, 4, 2)]
    with PriceStore(path) as store:
        assert store.models() == ["H100 SXM 80GB"]
        assert store.last_updated().startswith("2026-11-12T09:")


def test_store_batch_validation_is_atomic_and_currencies_separate():
    with PriceStore(":memory:") as store:
        with pytest.raises(ValueError):
            store.upsert([offer(), offer(region="invalid", price_per_hour=-1)])
        assert store.latest_prices() == []
        store.upsert([offer(), offer(region="eu", currency="EUR", price_per_hour=800, price_per_gpu_hour=100, original_currency="EUR", original_price=800)])
        assert store.daily_series("H100 SXM 80GB")[0]["max"] == 3
        assert store.daily_series("H100 SXM 80GB", currency="EUR")[0]["max"] == 100
        store.record_run(provider="azure", started_at=STAMP, status="limited", pages=3, records=2)
        assert store.latest_run()["status"] == "limited"


class StrictHTML(HTMLParser):
    """Explicitly verify matching tags (HTMLParser by itself is only permissive)."""
    VOID = {"meta", "br", "hr", "link", "input", "img", "area", "base", "col", "embed", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.tags, self.links = [], [], []
        self.ids, self.attributes, self.text = set(), [], []
        self.language_links = []
        self.language_nav_depth = None

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        attributes = dict(attrs)
        self.attributes.append((tag, attributes))
        if "id" in attributes:
            assert attributes["id"] not in self.ids
            self.ids.add(attributes["id"])
        if tag == "nav" and attributes.get("class") == "languages":
            self.language_nav_depth = len(self.stack)
        if tag == "a" and self.language_nav_depth is not None:
            self.language_links.append(attributes)
        if tag in {"a", "script", "link", "img", "iframe"}:
            self.links.append((tag, attributes))
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag, f"Mismatched closing {tag}"
        if self.language_nav_depth == len(self.stack):
            self.language_nav_depth = None

    def handle_data(self, data):
        self.text.append(data)


def test_site_from_fixtures_cheapest_html_json_and_history(tmp_path):
    with PriceStore(":memory:") as store:
        store.upsert([offer(), offer(region="westus", price_type="spot", price_per_hour=8, price_per_gpu_hour=1),
                      offer(collected_at="2026-11-11T08:00:00Z", price_per_hour=40, price_per_gpu_hour=5)])
        store.record_run(provider="azure", started_at=STAMP, status="limited", pages=3, records=3)
        result = build_site(store, tmp_path, today=TODAY, built_at=STAMP)
    assert result["offers_today"] == 2 and result["html_pages"] == 19 * len(LANGUAGES)
    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert '/statistics/cheapest/on_demand" data-format="money">$3.00</span>' in index
    assert '/statistics/cheapest/spot" data-format="money">$1.00</span>' in index
    assert "Spot" in index and "H100 SXM 80GB" in index
    assert "Partial or failed collection" in index
    slug = model_slug("H100 SXM 80GB")
    model_html = (tmp_path / slug / "index.html").read_text(encoding="utf-8")
    offers_html = model_html[model_html.index("Today&#x27;s observed offers"):]
    assert offers_html.index('data-price-block="on_demand"') < offers_html.index('data-price-block="spot"')
    parsed=Page(model_html)
    assert [c['attrs']['data-price-type'] for c in parsed.cards]==['on_demand','spot']
    assert '$3.00' in parsed.cards[0]['roles']['per-gpu'] and '$1.00' in parsed.cards[1]['roles']['per-gpu']
    assert "On-demand" in model_html and "Spot" in model_html
    assert "<svg" in model_html and "Exact daily history values" in model_html
    svg = model_html[model_html.index("<svg"):model_html.index("</svg>") + 6]
    assert ET.fromstring(svg).tag.endswith("svg")
    exported = json.loads((tmp_path / "data" / (slug + ".json")).read_text(encoding="utf-8"))
    assert exported["offers"][0]["price_per_gpu_hour"] == 1
    assert len(exported["history"]["on_demand"]) == 2
    for path in tmp_path.rglob("*.html"):
        content = path.read_text(encoding="utf-8")
        parser = StrictHTML()
        parser.feed(content)
        parser.close()
        assert not parser.stack
        assert all(attrs.get("type") in {"application/ld+json", "module"} and "src" not in attrs
                   for tag, attrs in parser.links if tag == "script")
        relative = path.relative_to(tmp_path).parts
        if relative[0] == 'embed':
            assert len(content.encode('utf-8')) < 15_000
            assert 'content="noindex"' in content and not any(tag=='script' for tag,_ in parser.links)
            continue
        lang = next((lang for lang in LANGUAGES if lang.prefix and lang.prefix == relative[0]), LANGUAGES[0])
        catalog = load_catalogs()[lang.code]
        assert f'<html lang="{lang.code}" dir="ltr">' in content and 'id="main"' in content
        assert catalog.html("source_name") in content and catalog.html("updated", date="").split(":")[0] in content
        assert 'data-slot="future-affiliate"></div>' in content
        assert all(tag in {"a", "link", "script"} for tag, attrs in parser.links)
        for tag, attrs in parser.links:
            href = attrs.get("href", "")
            if href and not href.startswith(("https:", "#")):
                local = tmp_path / urlsplit(href).path.lstrip("/")
                assert local.exists() or local.with_suffix(".html").exists(), href


@pytest.mark.parametrize('corruption', [None, 'day', 'price', 'count', 'marker'])
def test_history_titles_are_plain_audited_text_with_new_python_parser(tmp_path, monkeypatch, corruption):
    """Reproduce gh-118350 (3.12.12+) even on an older Windows Python.

    Older HTMLParser versions parse span children inside title. New versions
    deliver them as literal RCDATA. Use that text-only behavior on every host;
    timezone changes cannot reproduce this patch-version difference.
    """
    from pipelines.gpu_prices import audit as audit_module

    class TextTitlePage(audit_module.Page):
        CDATA_CONTENT_ELEMENTS = (*HTMLParser.CDATA_CONTENT_ELEMENTS, 'title')

        def handle_data(self, data):
            if self.stack and self.stack[-1][0] == 'title':
                data = unescape(data)
            super().handle_data(data)

    # Prove the regression harness observes exactly the Actions failure mode.
    legacy = TextTitlePage('<svg><title><span data-stat="/day">2026-11-12</span></title></svg>')
    assert any('<span data-stat=' in text for text in legacy.unmarked)
    monkeypatch.setattr(audit_module, 'Page', TextTitlePage)
    with PriceStore(':memory:') as store:
        store.upsert([offer(), offer(collected_at='2026-11-11T08:00:00Z',
                                    price_per_hour=40, price_per_gpu_hour=5)])
        result = build_site(store, tmp_path, today=TODAY, built_at=STAMP)
        assert result['audit']['status'] == 'passed'
        for lang in LANGUAGES:
            path = tmp_path / lang.path('/h100-sxm-80gb/').lstrip('/') / 'index.html'
            text = path.read_text(encoding='utf-8')
            parser = StrictHTML()
            parser.feed(text)
            assert all('<span' not in str(value) for _, attrs in parser.attributes for value in attrs.values())
            for svg in re.findall(r'<svg\b.*?</svg>', text, re.S):
                element = ET.fromstring(svg)
                for title in element.findall('.//{http://www.w3.org/2000/svg}title'):
                    assert len(title) == 0 and '<span' not in (title.text or '')
            assert '&lt;span' not in text
        if corruption:
            path = tmp_path / 'h100-sxm-80gb/index.html'
            text = path.read_text(encoding='utf-8')
            pattern = r'(<title data-stat="[^"]+" data-format="history-median">)(.*?)(</title>)'
            def corrupt(match):
                label = match[2]
                if corruption == 'day':
                    label = label.replace('2026-11-11', '2026-11-10')
                elif corruption == 'price':
                    label = label.replace('$5.00', '$99.00')
                elif corruption == 'count':
                    label = label.replace('n=1', 'n=9')
                return ('<title>' if corruption == 'marker' else match[1]) + label + match[3]
            path.write_text(re.sub(pattern, corrupt, text, count=1), encoding='utf-8')
            with pytest.raises(AuditError):
                audit_site(store.connection, tmp_path)


def test_surflare_partner_placement_localization_and_numeric_audit(tmp_path):
    partner = json.loads((Path(__file__).resolve().parents[1] / 'config/affiliates.json').read_text(encoding='utf-8'))['surflare']
    catalogs = load_catalogs()
    with PriceStore(':memory:') as store:
        store.upsert([offer()])
        build_site(store, tmp_path, today=TODAY, built_at=STAMP)
        for path in tmp_path.rglob('*.html'):
            if path.relative_to(tmp_path).parts[0]=='embed':
                assert partner['url'] not in path.read_text(encoding='utf-8')
                continue
            text = path.read_text(encoding='utf-8')
            lang = next(lang for lang in LANGUAGES if f'<html lang="{lang.code}"' in text)
            tr = catalogs[lang.code]
            assert '?' not in tr.text('partner_heading')
            parser = StrictHTML()
            parser.feed(text)
            links = [attrs for tag, attrs in parser.links if tag == 'a' and attrs.get('href') == partner['url']]
            is_home = path == tmp_path / lang.path().lstrip('/') / 'index.html'
            assert len(links) == (2 if is_home else 1)
            assert all(a['target'] == '_blank' and a['rel'] == 'sponsored nofollow noopener' for a in links)
            footer = text[text.index('<footer>'):]
            assert partner['url'] in footer
            assert tr.html('partner_footer', code=partner['promo_code']) in footer
            if is_home:
                assert text.index('data-list="cheapest"') < text.index('data-offer-browser') < text.index('<aside class="partner-card"')
                assert text.index('</section><script type="module">') < text.index('<aside class="partner-card"')
                assert tr.html('partner_copy', label=partner['label'], code=partner['promo_code']) in text
            else:
                assert '<aside class="partner-card"' not in text
            for table in re.findall(r'<table\b.*?</table>', text, re.S):
                assert partner['url'] not in table
        # Exact disclosure text is allowed; an altered discount or code is not.
        home = tmp_path / 'index.html'
        original = home.read_text(encoding='utf-8')
        for before, after in [('5%', '6%'), (partner['promo_code'], 'wrong123')]:
            home.write_text(original.replace(before, after), encoding='utf-8')
            with pytest.raises(AuditError, match='unaccounted numeric text'):
                audit_site(store.connection, tmp_path)
        home.write_text(original, encoding='utf-8')
        assert audit_site(store.connection, tmp_path)['status'] == 'passed'


def test_build_cli_uses_injected_utc_snapshot_clock(tmp_path, capsys):
    from pipelines.gpu_prices.run import main
    db = tmp_path / 'prices.sqlite'
    with PriceStore(db) as store:
        store.upsert([offer()])
    out = tmp_path / 'site'
    assert main(['build', '--db', str(db), '--out', str(out), '--as-of', STAMP,
                 '--site-base-url', 'https://gpu-cloud-prices.pages.dev']) == 0
    result = json.loads(capsys.readouterr().out)['build']
    assert result['offers_today'] == 1
    manifest = json.loads((out / 'gpu-prices-manifest.json').read_text(encoding='utf-8'))
    assert manifest['observation_day'] == TODAY
    assert datetime.fromisoformat(manifest['generated_at']) == datetime.fromisoformat(STAMP.replace('Z', '+00:00'))


def test_site_empty_stale_escape_and_owned_cleanup(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("preserve", encoding="utf-8")
    unrelated = tmp_path / "unrelated.html"
    unrelated.write_text("preserve", encoding="utf-8")
    with PriceStore(":memory:") as store:
        build_site(store, tmp_path, today=TODAY, built_at=STAMP)
        assert "No real USD observations" in (tmp_path / "index.html").read_text(encoding="utf-8")
        assert not list(tmp_path.glob("h100*.json"))
        store.upsert([offer(region='<east & "west">')])
        build_site(store, tmp_path, today=TODAY, built_at=STAMP)
        slug = model_slug("H100 SXM 80GB")
        html = (tmp_path / slug / "index.html").read_text(encoding="utf-8")
        assert '&lt;east &amp; &quot;west&quot;&gt;' in html
        assert 'data-series=' not in html  # A one-day snapshot has only the regional chart.
        build_site(store, tmp_path, today="2026-11-13", built_at=STAMP)
        assert "No real USD observations" in (tmp_path / "index.html").read_text(encoding="utf-8")
        assert "Historical prices below are not current offers" in (tmp_path / slug / "index.html").read_text(encoding="utf-8")
    with PriceStore(":memory:") as store:
        build_site(store, tmp_path, today=TODAY, built_at=STAMP)
    assert not (tmp_path / slug / "index.html").exists()
    assert not (tmp_path / "data" / (slug + ".json")).exists()
    assert readme.read_text() == unrelated.read_text() == "preserve"


def test_cli_all_network_failure_builds_honest_empty_site(tmp_path, monkeypatch, capsys):
    from pipelines.gpu_prices import run
    class FailingCollector:
        pages_fetched = 0
        def __init__(self, *args, **kwargs):
            pass
        def collect(self, **kwargs):
            raise URLError("synthetic network outage")
    for name in ("AzureCollector", "LinodeCollector", "ScalewayCollector", "VerdaCollector", "PageCollector", "CloreCollector", "MonthlyCollector",
                 "AwsCollector", "OracleCollector", "OvhCollector", "VultrCollector", "HetznerCollector", "GcpCollector"):
        monkeypatch.setattr(run, name, FailingCollector)
    monkeypatch.setattr(run, "refresh", lambda path: (None, "unavailable"))
    monkeypatch.setattr(run, "configure_logging", lambda: None)
    result = run.main(["all", "--db", str(tmp_path / "prices.sqlite"), "--out", str(tmp_path / "site"), "--limit-pages", "3"])
    assert result == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["collection"]["records"] == 0 and summary["collection"]["status"] == "failed"
    assert summary["build"]["offers_today"] == 0
    html = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "No real USD observations" in html and "failed" in html


class Metadata(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.meta, self.canonicals, self.json_ld, self.titles = {}, [], [], []
        self.alternates, self.html_attrs = [], {}
        self.active, self.buffer = None, []
        self.feed(content)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "html":
            self.html_attrs = attrs
        if tag == "meta":
            self.meta[attrs.get("name", attrs.get("property"))] = attrs.get("content")
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonicals.append(attrs["href"])
        if tag == "link" and attrs.get("rel") == "alternate" and "hreflang" in attrs:
            self.alternates.append((attrs["hreflang"], attrs["href"]))
        if tag == "title" or (tag == "script" and attrs.get("type") == "application/ld+json"):
            self.active, self.buffer = tag, []

    def handle_data(self, data):
        if self.active:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if self.active == tag:
            text = ''.join(self.buffer)
            if tag == "script":
                self.json_ld.append(json.loads(text))
            else:
                self.titles.append(text)
            self.active = None


@pytest.mark.parametrize("model,slug", [
    ("H100 SXM 80GB", "h100-sxm-80gb"), ("A100 80GB", "a100-80gb"),
    ("H200 141GB", "h200-141gb"), ("MI300X 192GB", "mi300x-192gb"),
    ("  A/B + C  ", "a-b-c"), ("中文", "gpu"),
])
def test_readable_slugs(model, slug):
    assert model_slug(model) == slug


def test_slug_collisions_are_deterministic_and_reserve_routes(tmp_path):
    names = ["GPU", "gpu", "GPU!", "GPU 2", "Data", "中文"]
    slugs = model_slugs(names)
    assert slugs == model_slugs(reversed(names))
    assert len(set(slugs.values())) == len(names)
    assert slugs["GPU"] == "gpu" and slugs["GPU 2"] == "gpu-2"
    assert slugs["gpu"] == "gpu-3" and slugs["GPU!"] == "gpu-4"
    assert slugs["Data"] == "data-2"
    with PriceStore(":memory:") as store:
        store.upsert([offer(gpu_model=name, region=f"region-{i}") for i, name in enumerate(names)])
        build_site(store, tmp_path, today=TODAY, built_at=STAMP)
    for name, slug in slugs.items():
        assert (tmp_path / slug / "index.html").is_file()
        exported = json.loads((tmp_path / "data" / f"{slug}.json").read_text(encoding="utf-8"))
        assert exported["gpu_model"] == name


def test_seo_sharing_structured_data_and_sitemap(tmp_path):
    origin = "https://gpu.example.test"
    with PriceStore(":memory:") as store:
        store.upsert([offer(), offer(region="westus", price_per_hour=40, price_per_gpu_hour=5),
                      offer(region="eastus", price_type="spot", price_per_hour=8, price_per_gpu_hour=1)])
        result = build_site(store, tmp_path, today=TODAY, built_at="2026-11-13T00:00:00Z", site_base_url=origin + "/")
    index = Metadata((tmp_path / "index.html").read_text(encoding="utf-8"))
    model = Metadata((tmp_path / "h100-sxm-80gb" / "index.html").read_text(encoding="utf-8"))
    assert index.json_ld[0]["@type"] == "WebSite" and index.json_ld[0]["url"] == origin + "/"
    assert model.canonicals == [origin + "/h100-sxm-80gb/"]
    assert "H100 Cloud Rental Prices — PCIe vs SXM, Spot vs On-Demand (H100 SXM 80GB)" in model.titles[0]
    assert "minimum $1.00, median $3.00. Eligible offers: 3; providers: 1" in model.meta["description"]
    for page in (index, model):
        assert page.meta["og:url"] == page.canonicals[0]
        assert page.meta["og:type"] == "website"
        assert page.meta["og:title"] and page.meta["twitter:title"]
        assert page.meta["twitter:card"] == ("summary_large_image" if page is model else "summary")
        assert page.meta["og:description"] == page.meta["twitter:description"] == page.meta["description"]
        assert "robots" not in page.meta
    dataset = model.json_ld[0]
    assert dataset["@context"] == "https://schema.org" and dataset["@type"] == "Dataset"
    assert dataset["url"] == origin + "/h100-sxm-80gb/"
    assert dataset["distribution"]["contentUrl"] == origin + "/data/h100-sxm-80gb.json"
    assert dataset["temporalCoverage"] == TODAY
    assert {place["name"] for place in dataset["spatialCoverage"]} == {"eastus", "westus"}
    namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    sitemap = ET.fromstring((tmp_path / "sitemap.xml").read_text(encoding="utf-8"))
    urls = {entry.find("s:loc", namespace).text: entry.find("s:lastmod", namespace).text for entry in sitemap}
    assert urls == {origin + lang.path(route): TODAY for lang in LANGUAGES
                    for route in ("/", "/methodology", "/data/", "/h100-sxm-80gb/", "/providers/azure/",
                                  "/guides/", "/guides/choose-gpu-cloud-llm-inference/",
                                  "/guides/per-gpu-hour-vs-instance/", "/guides/gpu-cloud-price-glossary/",
                                  "/guides/cheapest-gpu-per-vram-gb/")}
    # A single matched Spot pair cannot bypass the guide's thin-content guard.
    assert all(origin+lang.path('/guides/spot-vs-on-demand/') not in urls for lang in LANGUAGES)
    assert set(result["sitemap_urls"]) == set(urls)
    assert (tmp_path / "robots.txt").read_text() == f"User-agent: *\nAllow: /\n\nSitemap: {origin}/sitemap.xml\n"+''.join(f'Sitemap: {origin}/sitemap-{l.code}.xml\n' for l in LANGUAGES)
    assert "Page not found" in (tmp_path / "404.html").read_text(encoding="utf-8")
    assert not list(tmp_path.glob("h100*.html")) and not list(tmp_path.glob("h100*.json"))


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_thin_content_noindex_and_sitemap_boundary(tmp_path, count):
    with PriceStore(":memory:") as store:
        store.upsert([offer(collected_at="2026-11-11T08:00:00Z")])
        store.upsert([offer(region=f"region-{i}") for i in range(count)])
        build_site(store, tmp_path, today=TODAY, built_at=STAMP)
    tree = ET.fromstring((tmp_path / "sitemap.xml").read_text(encoding="utf-8"))
    locations = [e.text for e in tree.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
    for lang in LANGUAGES:
        content = (tmp_path / lang.prefix / "h100-sxm-80gb" / "index.html").read_text(encoding="utf-8")
        page = Metadata(content)
        assert (page.meta.get("robots") == "noindex") == (count < 3)
        assert (DEFAULT_SITE_BASE_URL + lang.path("/h100-sxm-80gb/") in locations) == (count >= 3)
    content = (tmp_path / "h100-sxm-80gb" / "index.html").read_text(encoding="utf-8")
    page = Metadata(content)
    if count == 0:
        assert load_catalogs()['en'].text('search_empty_intro') in page.meta['description']
        assert "temporalCoverage" not in page.json_ld[0]
        assert "Historical prices below are not current offers" in content


def test_data_comparisons_regions_missing_and_negative_discount(tmp_path):
    values = [offer(), offer(region="westus", price_per_hour=40, price_per_gpu_hour=5),
              offer(region="eastus", price_type="spot", price_per_hour=8, price_per_gpu_hour=1),
              offer(region="eastus", sku="other-sku", price_per_hour=56, price_per_gpu_hour=7)]
    stats = model_statistics(values)
    assert stats["cheapest"] == {"on_demand": 3, "spot": 1}
    assert stats["spot_discount_percent"] == pytest.approx(100 * 2 / 3)
    assert stats["distributions"]["on_demand"] == {"count": 3, "min": 3, "median": 5, "max": 7}
    assert stats["distributions"]["spot"]["count"] == 1
    assert stats["regions"][1]["spot_min"] is None
    with PriceStore(":memory:") as store:
        store.upsert(values)
        build_site(store, tmp_path, today=TODAY, built_at=STAMP)
    content = (tmp_path / "h100-sxm-80gb" / "index.html").read_text(encoding="utf-8")
    assert "66.67% below" in content and "regions and SKUs may differ" in content
    assert "Observed prices for H100 SXM 80GB" in content and "n=" in content
    assert "Prices by region" in content and "Not observed" in content
    exported = json.loads((tmp_path / "data" / "h100-sxm-80gb.json").read_text(encoding="utf-8"))
    assert exported["statistics"] == stats
    assert model_statistics([offer()])["spot_discount_percent"] is None
    assert model_statistics([offer(price_type="spot")])["cheapest"]["on_demand"] is None
    assert model_statistics([offer(), offer(price_type="spot", price_per_hour=48, price_per_gpu_hour=6)])["spot_discount_percent"] is None


def test_complete_snapshot_excludes_earlier_same_day_keys_without_deleting_history(tmp_path, monkeypatch):
    from pipelines.gpu_prices import store as store_module
    monkeypatch.setattr(store_module, "utc_now", lambda: "2026-11-12T09:00:00Z")
    with PriceStore(":memory:") as store:
        store.upsert([offer(region="old-region", price_per_hour=.8, price_per_gpu_hour=.1, collected_at="2026-11-12T07:00:00Z"),
                      offer(), offer(region="westus", price_type="spot", price_per_hour=8, price_per_gpu_hour=1),
                      offer(region="north", price_per_hour=40, price_per_gpu_hour=5)])
        store.record_run(provider="azure", started_at=STAMP, status="complete", pages=9, records=3)
        result = build_site(store, tmp_path, today=TODAY, built_at=STAMP)
        assert len(store.latest_prices(day=TODAY)) == 4  # Original observations survive.
    assert result["offers_today"] == 3
    exported = json.loads((tmp_path / "data" / "h100-sxm-80gb.json").read_text(encoding="utf-8"))
    assert len(exported["offers"]) == 3 and exported["statistics"]["region_count"] == 3
    assert exported["history"]["on_demand"][0]["min"] == 3
    assert exported["history"]["on_demand"][0]["count"] == 2
    page = Metadata((tmp_path / "h100-sxm-80gb" / "index.html").read_text(encoding="utf-8"))
    assert "H100 Cloud Rental Prices — PCIe vs SXM, Spot vs On-Demand (H100 SXM 80GB)" in page.titles[0]
    assert "minimum $1.00, median $3.00. Eligible offers: 3" in page.meta['description']
    assert "old-region" not in {place["name"] for place in page.json_ld[0]["spatialCoverage"]}


def test_json_ld_escaping_and_manifest_cleanup(tmp_path):
    name = 'GPU </script><script>bad & "quoted"'
    (tmp_path / "legacy-hash.html").write_text("old", encoding="utf-8")
    # A manifest cannot delete a parent file or an unrelated README.
    preserved = tmp_path / "README.md"
    preserved.write_text("keep", encoding="utf-8")
    (tmp_path / "gpu-prices-manifest.json").write_text(json.dumps({"files": ["legacy-hash.html", "../outside.html", "README.md"]}), encoding="utf-8")
    with PriceStore(":memory:") as store:
        store.upsert([offer(gpu_model=name, region='region </script> & "quote"')])
        build_site(store, tmp_path, today=TODAY, built_at=STAMP)
    content = (tmp_path / model_slug(name) / "index.html").read_text(encoding="utf-8")
    assert content.count('<script') == 4  # Inert Dataset/FAQ/breadcrumbs plus local filter module.
    assert Metadata(content).json_ld[0]["name"] == name + " observed GPU prices"
    assert not (tmp_path / "legacy-hash.html").exists() and preserved.read_text() == "keep"


def test_site_origin_environment_and_cli_override(tmp_path, monkeypatch, capsys):
    from pipelines.gpu_prices import run
    monkeypatch.setenv("SITE_BASE_URL", "https://environment.example.test/")
    with PriceStore(":memory:") as store:
        result = build_site(store, tmp_path / "env", today=TODAY, built_at=STAMP)
    assert result["site_base_url"] == "https://environment.example.test"
    monkeypatch.setattr(run, "configure_logging", lambda: None)
    assert run.main(["build", "--db", str(tmp_path / "db.sqlite"), "--out", str(tmp_path / "cli"),
                     "--site-base-url", "https://cli.example.test"]) == 0
    assert json.loads(capsys.readouterr().out)["build"]["site_base_url"] == "https://cli.example.test"
    monkeypatch.delenv("SITE_BASE_URL")
    with PriceStore(":memory:") as store:
        result = build_site(store, tmp_path / "placeholder", today=TODAY, built_at=STAMP)
    assert result["placeholder_base_url"]
    assert "Placeholder site URL" in (tmp_path / "placeholder" / "index.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("origin", ["", "http://example.test", "https://u:p@example.test", "https://example.test/path",
                                   "https://example.test?x=1", "https://example.test#x", "https://example.test\n", "https://example.test:bad"])
def test_invalid_site_origin_is_rejected(tmp_path, origin):
    with PriceStore(":memory:") as store, pytest.raises(ValueError, match="SITE_BASE_URL"):
        build_site(store, tmp_path, today=TODAY, site_base_url=origin)


def test_catalog_keys_named_placeholders_and_utf8():
    catalogs = load_catalogs()
    assert [(lang.code, lang.prefix) for lang in LANGUAGES] == [
        ("en", ""), ("zh-Hans", "zh"), ("ja", "ja"), ("ko", "ko"),
        ("de", "de"), ("fr", "fr"), ("es", "es"), ("pt-BR", "pt")]
    english = catalogs["en"].messages
    assert {path.stem for path in CATALOG_DIR.glob("*.json")} == set(catalogs)
    for code, catalog in catalogs.items():
        assert set(catalog.messages) == set(english), code
        for key, text in catalog.messages.items():
            assert placeholders(text) == placeholders(english[key]), (code, key)
            assert text.strip() and "\ufffd" not in text
            assert "<" not in text and ">" not in text, (code, key)
        if code != "en":
            for key in ("home_heading", "home_description", "model_title", "method_history", "not_found"):
                assert catalog.messages[key] != english[key], (code, key)


@pytest.mark.parametrize("damage", ["missing", "extra", "placeholder", "positional", "attribute", "conversion", "format", "empty"])
def test_catalog_validation_fails_before_writing_output(tmp_path, monkeypatch, damage):
    from pipelines.gpu_prices import i18n
    messages = json.loads((CATALOG_DIR / "en.json").read_text(encoding="utf-8"))
    folder = tmp_path / "catalogs"
    folder.mkdir()
    (folder / "en.json").write_text(json.dumps(messages), encoding="utf-8")
    if damage == "missing":
        del messages["model_title"]
    elif damage == "extra":
        messages["extra"] = "extra"
    elif damage == "placeholder":
        messages["model_title"] += " {unknown}"
    elif damage == "empty":
        messages["model_title"] = ""
    else:
        messages["model_title"] = {"positional": "{}", "attribute": "{model.name}",
                                    "conversion": "{model!r}", "format": "{price:.2f}"}[damage]
    for lang in LANGUAGES[1:]:
        (folder / (lang.code + ".json")).write_text(json.dumps(messages), encoding="utf-8")
    monkeypatch.setattr(i18n, "CATALOG_DIR", folder)
    with PriceStore(":memory:") as store, pytest.raises(ValueError):
        build_site(store, tmp_path / "output", today=TODAY, built_at=STAMP)
    assert not (tmp_path / "output").exists()


def local_target(root, url):
    """Resolve clean Pages URLs, directory indexes and ordinary static files."""
    path = root / urlsplit(url).path.lstrip("/")
    if path.is_dir():
        path /= "index.html"
    elif not path.suffix:
        path = path.with_suffix(".html")
    assert path.is_file(), url
    assert path.resolve().is_relative_to(root.resolve()), url
    return path


def assert_multilingual_site(root, origin, models):
    """Audit every generated page, including noindex models and all 404s."""
    catalogs = load_catalogs()
    routes = {"/", "/methodology", "/404.html", "/data/"} | {"/" + slug + "/" for slug in models.values()}
    from pipelines.gpu_prices.search_specs import PAIRS
    routes |= ({'/providers/azure/'} if models else set()) | {'/compare/'+slug+'/' for slug in PAIRS}
    from pipelines.gpu_prices.guide_specs import GUIDES
    routes |= {'/guides/'} | {'/guides/'+slug+'/' for slug in GUIDES}
    # These fixtures retain only dates in ISO week 46; both new routes are thin.
    routes |= {'/reports/', '/reports/2026-W46/'}
    all_pages = {origin + lang.path(route): (lang, route) for lang in LANGUAGES for route in routes}
    assert len(list(root.rglob("*.html"))) == len(all_pages) + len(models) * len(LANGUAGES)
    parsed, metadata, contact_bytes = {}, {}, set()
    original_contact = (b'<span>&#122;&#104;&#97;&#110;&#57;&#57;&#57; [at] '
                        b'&#116;&#117;&#116;&#97;&#109;&#97;&#105;&#108; [dot] '
                        b'&#99;&#111;&#109;</span> &middot; ')
    for url, (lang, route) in all_pages.items():
        path = local_target(root, url)
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        parser = StrictHTML()
        parser.feed(text)
        parser.close()
        assert not parser.stack, url
        page = Metadata(text)
        parsed[url], metadata[url] = parser, page
        assert page.html_attrs == {"lang": lang.code, "dir": "ltr"}
        assert page.canonicals == [url]
        expected = {other.code: origin + other.path(route) for other in LANGUAGES}
        expected["x-default"] = origin + route
        assert len(page.alternates) == len(expected) and dict(page.alternates) == expected
        assert page.json_ld and all(data["inLanguage"] == lang.code and data["url"] == url for data in page.json_ld)
        assert page.meta["og:url"] == url and page.meta["description"]
        assert len(parser.language_links) == len(LANGUAGES)
        assert {a["hreflang"]: urljoin(url, a["href"]) for a in parser.language_links} == {
            code: href for code, href in expected.items() if code != "x-default"}
        assert [a["hreflang"] for a in parser.language_links if a.get("aria-current") == "page"] == [lang.code]
        assert all(a["lang"] == a["hreflang"] for a in parser.language_links)
        assert all(attrs.get("type") in {"application/ld+json", "module"} and "src" not in attrs
                   for tag, attrs in parser.links if tag == "script")
        assert not any(key.lower().startswith("on") for _, attrs in parser.attributes for key in attrs)
        assert "\ufffd" not in text and 'data-slot="future-affiliate"></div>' in text
        contact = re.search(rb'<p id="contact">.*?(<span>.*?</span> &middot; )', raw)
        assert contact and contact.group(1) == original_contact
        contact_bytes.add(contact.group(1))
        assert catalogs[lang.code].html("contact") in text
        for tag, attrs in parser.attributes:
            if tag == "time":
                assert re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T.*)?", attrs["datetime"])
        for stamp, visible in re.findall(r'<time datetime="([^"]+)">([^<]+)</time>', text):
            assert visible == stamp[:10] and re.fullmatch(r"\d{4}-\d{2}-\d{2}", visible)
        if route == "/404.html":
            assert page.meta["robots"] == "noindex"
            assert catalogs[lang.code].text("not_found") in ''.join(parser.text)
            homes = {a["href"] for tag, a in parser.links if tag == "a"}
            assert {other.path() for other in LANGUAGES} <= homes
        if route == "/methodology":
            assert catalogs[lang.code].html("method_history") in text
        if route in {'/'+slug+'/' for slug in models.values()}:
            assert page.json_ld[0]["distribution"]["contentUrl"] == origin + "/data/" + route.strip('/') + ".json"
    assert len(contact_bytes) == 1
    for url, parser in parsed.items():
        page = metadata[url]
        for code, alternate in page.alternates:
            assert alternate in all_pages
            assert dict(metadata[alternate].alternates) == dict(page.alternates), (url, alternate)
        for _, attrs in parser.links:
            href = attrs.get("href")
            if not href:
                continue
            target_url = urljoin(url, href)
            parts = urlsplit(target_url)
            assert parts.scheme == "https", href
            if parts.netloc != urlsplit(origin).netloc:
                continue
            target = local_target(root, target_url)
            if parts.fragment and target.suffix == ".html":
                destination = parsed[target_url.split('#', 1)[0]]
                assert parts.fragment in destination.ids, href
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9", "x": "http://www.w3.org/1999/xhtml"}
    entries = ET.parse(root / "sitemap.xml").getroot().findall("s:url", ns)
    expected_indexed = {url for url, page in metadata.items() if page.meta.get("robots") != "noindex"}
    locations = [entry.find("s:loc", ns).text for entry in entries]
    assert len(locations) == len(set(locations)) and set(locations) == expected_indexed
    for entry in entries:
        url = entry.find("s:loc", ns).text
        alternates = entry.findall("x:link", ns)
        assert len(alternates) == len(LANGUAGES) + 1
        assert all(a.attrib["rel"] == "alternate" for a in alternates)
        assert {a.attrib["hreflang"]: a.attrib["href"] for a in alternates} == dict(metadata[url].alternates)
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry.find("s:lastmod", ns).text)
    assert (root / "robots.txt").read_text(encoding="utf-8") == f"User-agent: *\nAllow: /\n\nSitemap: {origin}/sitemap.xml\n"+''.join(f'Sitemap: {origin}/sitemap-{l.code}.xml\n' for l in LANGUAGES)


@pytest.mark.parametrize("origin", ["https://one.example.test", "https://two.example.test:443"])
def test_every_language_page_reciprocal_metadata_links_sitemap_and_contact(tmp_path, monkeypatch, origin):
    monkeypatch.setenv("SITE_BASE_URL", origin)
    names = ["H100 SXM 80GB", "A100 80GB", "H200 141GB", 'GPU </script><script>bad & "quoted"']
    with PriceStore(":memory:") as store:
        store.upsert([offer(), offer(region="westus"), offer(region="north", price_type="spot"),
                      offer(collected_at="2026-11-11T08:00:00Z"),
                      offer(gpu_model=names[1], sku="thin", price_per_hour=9.84, price_per_gpu_hour=1.23),
                      offer(gpu_model=names[2], sku="stale", collected_at="2026-11-11T08:00:00Z"),
                      offer(gpu_model=names[3], sku="escaping", region='region <&> "quoted"',
                            price_per_hour=9876.48, price_per_gpu_hour=1234.56)])
        store.record_run(provider="azure", started_at=STAMP, status="limited", pages=3, records=6)
        result = build_site(store, tmp_path, today=TODAY, built_at=STAMP)
    assert_multilingual_site(tmp_path, origin, model_slugs(names))
    assert result["html_pages_by_language"] == {lang.code: 25 for lang in LANGUAGES}
    assert len(list((tmp_path / "data").glob("*.json"))) == len(names) + 1
    catalogs = load_catalogs()
    for lang in LANGUAGES:
        if lang.prefix:
            assert (tmp_path / lang.prefix / 'data/index.html').is_file()
            assert not list((tmp_path / lang.prefix / 'data').glob('*.json'))
        content = (tmp_path / lang.prefix / "a100-80gb/index.html").read_text(encoding="utf-8")
        assert '$1.23' in content and '$1,23' not in content
        page = Metadata(content)
        assert "1.23" in page.meta['description'] and TODAY in content
        singular = {
            "en": "1 offer across 1 Azure region", "zh-Hans": "1 条报价、1 个 Azure 区域",
            "ja": "1 件、Azure 1 リージョン", "ko": "요금 1건, Azure 지역 1곳",
            "de": "1 Angebot in 1 Azure-Region", "fr": "1 offre dans 1 région Azure",
            "es": "1 oferta en 1 región de Azure", "pt-BR": "1 oferta em 1 região do Azure",
        }
        assert page.meta['og:title'] == catalogs[lang.code].text('seo_a100_title',model='A100 80GB')
        homepage = Metadata((tmp_path / lang.prefix / "index.html").read_text(encoding="utf-8"))
        assert "5" in homepage.titles[0] and "5" in homepage.meta["description"]
        assert "3" in homepage.titles[0] and "3" in homepage.meta["description"]
        assert catalogs[lang.code].text("partial") in ''.join(_html_text(content))
        expensive = local_target(tmp_path, origin + lang.path('/' + model_slugs(names)[names[3]] + '/'))
        content = expensive.read_text(encoding="utf-8")
        assert '$1234.56' in content and '$1,234.56' not in content and '$1234,56' not in content


def _html_text(content):
    parser = StrictHTML()
    parser.feed(content)
    parser.close()
    return parser.text


def test_language_prefixes_reserved_and_multilingual_cleanup(tmp_path):
    names = [lang.prefix.upper() for lang in LANGUAGES if lang.prefix]
    slugs = model_slugs(names)
    assert all(slug not in {lang.prefix for lang in LANGUAGES} for slug in slugs.values())
    with PriceStore(":memory:") as store:
        store.upsert([offer(gpu_model=name, sku="sku-" + name) for name in names])
        build_site(store, tmp_path, today=TODAY, built_at=STAMP)
    for lang in LANGUAGES:
        for slug in slugs.values():
            assert (tmp_path / lang.prefix / slug / "index.html").is_file()
    with PriceStore(":memory:") as store:
        build_site(store, tmp_path, today=TODAY, built_at=STAMP)
    assert [p.name for p in (tmp_path / 'data').glob('*.json')] == ['latest.json']
    assert json.loads((tmp_path/'data/latest.json').read_text(encoding='utf-8'))['exports'] == []
    for lang in LANGUAGES:
        for slug in slugs.values():
            assert not (tmp_path / lang.prefix / slug / "index.html").exists()
    assert_multilingual_site(tmp_path, DEFAULT_SITE_BASE_URL, {})


def deployment_files(root):
    (root / "site").mkdir()
    for name in ("index.html", "sitemap.xml"):
        (root / "site" / name).write_text("test artifact", encoding="utf-8")


def test_deploy_pinned_command_credentials_and_secret_free_logs(tmp_path, caplog, capsys):
    from pipelines.gpu_prices.deploy import WRANGLER_VERSION, deploy_site
    deployment_files(tmp_path)
    environment = {"PATH": "test-path", "CLOUDFLARE_API_TOKEN": "test-token-secret", "CLOUDFLARE_ACCOUNT_ID": "test-account-secret"}
    calls = []
    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="test-token-secret", stderr="test-account-secret")
    def forbid_dotenv(*args):
        pytest.fail("complete process credentials must not read .env")
    with caplog.at_level("INFO"):
        result = deploy_site("gpu-prices", root=tmp_path, environ=environment, runner=runner, dotenv_loader=forbid_dotenv)
    assert result["status"] == "complete" and WRANGLER_VERSION == "4.45.0"
    command, options = calls[0]
    assert command == ["npx", "--yes", "wrangler@4.45.0", "pages", "deploy", "site", "--project-name", "gpu-prices", "--branch", "main"]
    assert options["cwd"] == tmp_path and options["shell"] is False and options["capture_output"] is True
    assert options["env"]["PATH"] == "test-path"
    assert options["env"]["CLOUDFLARE_API_TOKEN"] == environment["CLOUDFLARE_API_TOKEN"]
    assert options["env"]["CLOUDFLARE_ACCOUNT_ID"] == environment["CLOUDFLARE_ACCOUNT_ID"]
    assert options["env"] is not environment
    output = caplog.text + str(result) + repr(command) + str(capsys.readouterr())
    assert "test-token-secret" not in output and "test-account-secret" not in output


def test_deploy_dotenv_runtime_loading_and_environment_precedence(tmp_path):
    from orchestrator.dotenv import parse_file
    from pipelines.gpu_prices.deploy import deploy_site
    deployment_files(tmp_path)
    (tmp_path / ".env").write_text('CLOUDFLARE_API_TOKEN="file-token"\nCLOUDFLARE_ACCOUNT_ID=file-account\nUNRELATED_SECRET=not-forwarded\n', encoding="utf-8")
    calls = []
    def loader(path):
        assert path == tmp_path / ".env"
        return parse_file(path)
    def runner(command, **kwargs):
        calls.append(kwargs["env"])
        return subprocess.CompletedProcess(command, 0)
    deploy_site("gpu", root=tmp_path, environ={"CLOUDFLARE_API_TOKEN": "process-token"}, runner=runner, dotenv_loader=loader)
    assert calls[0]["CLOUDFLARE_API_TOKEN"] == "process-token"
    assert calls[0]["CLOUDFLARE_ACCOUNT_ID"] == "file-account" and "UNRELATED_SECRET" not in calls[0]


@pytest.mark.parametrize("missing", ["index.html", "sitemap.xml", "credentials", "token", "account", "unreadable", "malformed"])
def test_deploy_refuses_missing_inputs_and_unreadable_dotenv(tmp_path, missing):
    from pipelines.gpu_prices.deploy import DeployError, deploy_site
    deployment_files(tmp_path)
    environment = {"CLOUDFLARE_API_TOKEN": "secret", "CLOUDFLARE_ACCOUNT_ID": "account"}
    if missing.endswith(".html") or missing.endswith(".xml"):
        (tmp_path / "site" / missing).unlink()
    else:
        environment = {"CLOUDFLARE_ACCOUNT_ID": "account"} if missing == "token" else ({"CLOUDFLARE_API_TOKEN": "secret"} if missing == "account" else {})
    def loader(path):
        if missing == "unreadable":
            raise PermissionError("do not leak this secret")
        if missing == "malformed":
            raise ValueError("do not leak this secret")
        return {}
    with pytest.raises(DeployError) as caught:
        deploy_site("gpu", root=tmp_path, environ=environment,
                    dotenv_loader=loader, runner=lambda *args, **kwargs: pytest.fail("runner reached despite missing inputs"))
    assert "do not leak" not in str(caught.value)


@pytest.mark.parametrize("mode", ["exit", "exception"])
def test_deploy_child_failures_never_log_or_print_credentials(tmp_path, caplog, capsys, monkeypatch, mode):
    from pipelines.gpu_prices import run
    from pipelines.gpu_prices.deploy import deploy_site
    deployment_files(tmp_path)
    def runner(command, **kwargs):
        if mode == "exception":
            raise subprocess.CalledProcessError(1, command, output="token-private", stderr="account-private")
        return subprocess.CompletedProcess(command, 1, stdout="token-private", stderr="account-private")
    monkeypatch.setattr(run, "configure_logging", lambda: None)
    monkeypatch.setattr(run, "deploy_site", lambda project: deploy_site(project, root=tmp_path, runner=runner,
                        environ={"CLOUDFLARE_API_TOKEN": "token-private", "CLOUDFLARE_ACCOUNT_ID": "account-private"}))
    assert run.main(["deploy", "--project", "gpu"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["deploy"]["status"] == "failed"
    assert "token-private" not in output.out + output.err + caplog.text
    assert "account-private" not in output.out + output.err + caplog.text


@pytest.mark.parametrize("project", ["", "--bad", "a;echo hi", "a&b", "UPPER", "a/../../b", "a" * 64])
def test_deploy_project_argument_is_validated(project):
    from pipelines.gpu_prices.deploy import DeployError, deploy_command
    with pytest.raises(DeployError):
        deploy_command(project)


def test_headlines_keep_demand_and_spot_separate_and_audit_raw_precision(tmp_path):
    values = [offer(price_per_hour=98.32, price_per_gpu_hour=12.29),
              offer(price_type="spot", price_per_hour=18.169536, price_per_gpu_hour=2.271192),
              offer(region="attdallas1", price_per_hour=.08, price_per_gpu_hour=.01),
              offer(region="switzerlandwest", price_per_hour=.16, price_per_gpu_hour=.02)]
    with PriceStore(":memory:") as store:
        store.upsert(values)
        result = build_site(store, tmp_path, today=TODAY, built_at=STAMP)
        assert result["audit"]["status"] == "passed"
        assert audit_site(store.connection, tmp_path)["html_pages"] == 152
    text = (tmp_path / "h100-sxm-80gb/index.html").read_text(encoding="utf-8")
    page = Metadata(text)
    assert "$2.27" in page.meta['description'] and "H100 Cloud Rental Prices" in page.titles[0]
    assert "median $7.28" in page.meta["description"] and "on-demand: 1; Spot: 1" in page.meta['description']
    assert "Edge zone" in text and "Restricted region" in text
    assert "FAQPage" in text and "theme-color" in text and "og:image" in text
    assert page.json_ld[1]["mainEntity"][0]["acceptedAnswer"]["text"].startswith("On-demand: $12.29/GPU-hour. Spot: $2.27")
    index = Metadata((tmp_path / "index.html").read_text(encoding="utf-8"))
    assert "2 offers" in index.titles[0] and "1 region" in index.titles[0]


@pytest.mark.parametrize("region", ["australiacentral2", "francesouth", "germanynorth", "norwaywest",
    "switzerlandwest", "uaecentral", "southafricawest", "brazilsoutheast", "jioindiacentral", "jioindiawest", "westindia",
    "attdallas1", "attfuturecity9", "sgxsingapore1", "losangeles", "usgovvirginia"])
def test_special_regions_never_enter_comparison_statistics(region):
    stats = model_statistics([offer(), offer(region=region, price_per_hour=.8, price_per_gpu_hour=.1)])
    assert stats["cheapest"]["on_demand"] == 3
    assert stats["region_count"] == 1 and stats["offers"] == 1
    assert stats["all_offers"] == 2
    assert any(r["region"] == region and r["classification"] != "standard" for r in stats["regions"])


def test_unknown_region_defaults_to_standard_and_is_logged(caplog):
    assert region_class("new-test-unreviewed-region") == "standard"
    assert "Unknown Azure region defaults to standard" in caplog.text


def test_fractional_prices_are_separate_and_medians_are_across_offers(tmp_path):
    fractional = offer(sku="Standard_NV12ads_A10_v5", gpu_model="A10 24GB", gpu_count=1/3,
                       price_per_hour=.02, price_per_gpu_hour=.06)
    whole = offer(sku="Standard_NV36ads_A10_v5", gpu_model="A10 24GB", gpu_count=1,
                  price_per_hour=1.23456, price_per_gpu_hour=1.23456)
    values = [fractional, whole, replace(whole, sku="second", price_per_hour=3.4, price_per_gpu_hour=3.4),
              replace(whole, region="westus", price_per_hour=8.8, price_per_gpu_hour=8.8)]
    stats = model_statistics(values)
    assert stats["cheapest"]["on_demand"] == 1.23456
    assert stats["distributions"]["on_demand"]["median"] == 3.4
    assert stats["distributions"]["on_demand"]["count"] == 3
    with PriceStore(":memory:") as store:
        store.upsert(values)
        build_site(store, tmp_path, today=TODAY, built_at=STAMP)
    text = (tmp_path / "a10-24gb/index.html").read_text(encoding="utf-8")
    assert "Fractional GPU, 1/3 share; price normalized to a full-GPU equivalent" in text
    assert "$0.020" in text and "$0.060" in text and "$1.23" in text
    assert 'data-format="text">3</span>)' in text
    assert "minimum $1.23" in Metadata(text).meta['description']


@pytest.mark.parametrize("spot", [84.80, 90.00])
def test_no_zero_or_negative_spot_discount_is_published(tmp_path, spot):
    values = [offer(gpu_model="H200 141GB", price_per_hour=84.80, price_per_gpu_hour=10.6),
              offer(gpu_model="H200 141GB", price_type="spot", price_per_hour=spot, price_per_gpu_hour=spot/8)]
    assert model_statistics(values)["spot_discount_percent"] is None
    with PriceStore(":memory:") as store:
        store.upsert(values)
        build_site(store, tmp_path, today=TODAY, built_at=STAMP)
    text = (tmp_path / "h200-141gb/index.html").read_text(encoding="utf-8")
    assert "Spot: no discount in list price" in text
    assert not re.search(r'(?:0\.00%|-\d+(?:\.\d+)?%)', text)


@pytest.mark.parametrize("damage", ["price", "title", "metadata", "faq", "json", "bar", "unmarked", "missing"])
def test_independent_audit_rejects_corrupt_published_numbers(tmp_path, damage):
    with PriceStore(":memory:") as store:
        store.upsert([offer(), offer(price_type="spot", price_per_hour=8, price_per_gpu_hour=1)])
        build_site(store, tmp_path, today=TODAY, built_at=STAMP)
        path = tmp_path / "h100-sxm-80gb/index.html"
        text = path.read_text(encoding="utf-8")
        if damage == "json":
            target = tmp_path / "data/h100-sxm-80gb.json"
            data = json.loads(target.read_text(encoding="utf-8"))
            data["statistics"]["cheapest"]["spot"] = .123
            target.write_text(json.dumps(data), encoding="utf-8")
        else:
            if damage == "price":
                text = text.replace('data-format="money">$3.00', 'data-format="money">$0.03', 1)
            elif damage == "title":
                text = text.replace("<title>H100", "<title>H200", 1)
            elif damage == "metadata":
                text = text.replace('content="H100 Cloud Rental Prices', 'content="H200 Cloud Rental Prices', 1)
            elif damage == "faq":
                text = text.replace('data-faq="0">On-demand: $3.00', 'data-faq="0">On-demand: $0.30', 1)
            elif damage == "bar":
                text = text.replace('width="450.000"', 'width="499.000"', 1)
            elif damage == "missing":
                text = re.sub(r'<span data-stat="/models/H100 SXM 80GB/offers/0/price_per_hour".*?</span>', '', text, count=1)
            else:
                text = text.replace('</main>', '<p>Only $0.01 per GPU hour</p></main>')
            assert text != path.read_text(encoding='utf-8'), 'Corruption must change the artifact'
            path.write_text(text, encoding="utf-8")
        with pytest.raises(AuditError, match="Statistics audit FAILED"):
            audit_site(store.connection, tmp_path)


def test_history_uses_same_eligible_population_and_audit_detects_curve_corruption(tmp_path):
    with PriceStore(":memory:") as store:
        store.upsert([offer(), offer(collected_at="2026-11-11T08:00:00Z"),
            offer(region="attdallas1", price_per_hour=.8, price_per_gpu_hour=.1),
            offer(sku="fraction", gpu_count=.5, price_per_hour=.01, price_per_gpu_hour=.02)])
        assert store.daily_series("H100 SXM 80GB", price_type="on_demand")[-1]["min"] == 3
        build_site(store, tmp_path, today=TODAY, built_at=STAMP)
        path = tmp_path / "h100-sxm-80gb/index.html"
        text = path.read_text(encoding="utf-8")
        text = re.sub(r'points="[^"]+" data-series=', 'points="1,2 3,4" data-series=', text, count=1)
        path.write_text(text, encoding="utf-8")
        with pytest.raises(AuditError, match="history polyline"):
            audit_site(store.connection, tmp_path)


def test_outbound_links_official_destinations_env_only_affiliates_and_disclosure(tmp_path, monkeypatch):
    monkeypatch.delenv("AFFILIATE_VAST_REF", raising=False)
    plain = link_for("azure", "Standard_ND96isr_H100_v5", "eastus2", "spot")
    assert urlsplit(plain["url"]).path.endswith("/ndh100v5-series")
    assert not plain["is_affiliate"]
    assert parse_qs(urlsplit(plain["url"]).query)["utm_campaign"] == ["H100 SXM 80GB"]
    monkeypatch.setenv("AFFILIATE_VAST_REF", "test-ref&escaped=1")
    affiliate = link_for("vast", "", "", "", model="H100")
    assert affiliate["is_affiliate"] and urlsplit(affiliate["url"]).hostname == "cloud.vast.ai"
    assert parse_qs(urlsplit(affiliate["url"]).query)["ref_id"] == ["test-ref&escaped=1"]
    with pytest.raises(ValueError, match="No reviewed"):
        link_for("imaginary", "", "", "")
    with PriceStore(":memory:") as store:
        store.upsert([offer()])
        build_site(store, tmp_path, today=TODAY, built_at=STAMP)
    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    model = (tmp_path / "h100-sxm-80gb/index.html").read_text(encoding="utf-8")
    assert 'rel="sponsored nofollow noopener"' in index
    assert 'target="_blank" rel="nofollow noopener"' in model
    assert "Live price on provider site; not tracked here" in index
    for lang, tr in load_catalogs().items():
        assert tr.html("disclosure") in (tmp_path / tr.language.prefix / "index.html").read_text(encoding="utf-8")
        assert tr.html("disclosure") in (tmp_path / tr.language.prefix / "methodology.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("kind", ["matched", "mismatch", "newer_effective_date", "unverified_missing"])
def test_live_sample_tolerance_updates_and_missing_rates_are_reported(kind):
    price = 24.0000001 if kind == "matched" else 26
    items = [] if kind == "unverified_missing" else [item(retailPrice=price, effectiveStartDate="2026-11-13T00:00:00Z" if kind == "newer_effective_date" else "2026-01-01T00:00:00Z")]
    from pipelines.gpu_prices import audit
    # A known stored effective date lets a source update be detected even when
    # it predates collection, unlike the explicitly limited old snapshot path.
    if kind == "newer_effective_date":
        items[0]["effectiveStartDate"] = "2026-02-01T00:00:00Z"
    with PriceStore(":memory:") as store:
        store.upsert([offer(effective_start_date="2026-01-01T00:00:00Z")])
        result = live_sample(store.connection, 1, client=FakeClient([{"Items": items, "NextPageLink": None}]))
    assert result["results"][0]["status"] == kind
    assert result["status"] == ("failed" if kind == "mismatch" else "incomplete" if kind == "unverified_missing" else "passed")


def test_design_remains_local_responsive_and_all_new_strings_translated():
    from pipelines.gpu_prices.build_site import CSS
    assert "prefers-color-scheme:dark" in CSS and "system-ui" in CSS
    assert ".table-wrap{overflow-x:auto;max-width:100%;min-width:0;" in CSS
    assert "grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr))" in CSS
    assert not re.search(r'(?<!max-)(?<!min-)width\s*:\s*(?:3[6-9]\d|[4-9]\d\d|\d{4,})px', CSS)
    assert "@import" not in CSS and "url(" not in CSS
    catalogs = load_catalogs()
    new_keys = {"basis", "disclosure", "fraction_note", "edge", "restricted", "no_discount", "faq_cheapest_a", "method_audit", "check_live",
                "home_heading", "home_intro", "view_provider", "cheapest_models", "how_compare", "sort_gpu", "sort_instance",
                "checked_at", "unknown_region", "see_model", "no_results", "no_usd_conversion", "full_vram", "offer_details"}
    for code, catalog in catalogs.items():
        assert new_keys <= catalog.messages.keys()
        for key in new_keys:
            assert '??' not in catalog.messages[key]
            if code != "en":
                assert catalog.messages[key] != catalogs["en"].messages[key]


# Tiny hand-made fixtures from the response contracts observed on 2026-11-12.
# No production response files or cached prices are used by this offline suite.
def linode_plan():
    return {'id':'g1-gpu-rtx6000-1','label':'Dedicated 32GB + RTX6000 GPU x1',
            'gpus':1,'vcpus':8,'memory':32768,'accelerated_devices':0,
            'price':{'hourly':1.5,'monthly':None},
            'region_prices':[{'id':'gb-lon','hourly':1.7,'monthly':None}]}


def scaleway_product():
    return {'sku':'/instance/server/l4_1_24g/fr-par-2','product':'L4-1-24G',
            'locality':{'zone':'fr-par-2'},'status':'general_availability',
            'price':{'retail_price':{'currency_code':'EUR','units':0,'nanos':13000000}},
            'unit_of_measure':{'unit':'minute','size':1},
            'properties':{'instance':{'range':'GPU'},'hardware':{
                'gpu':{'type':'L4','count':1},'cpu':{'virtual':{'count':8},'description':'vCPUs: 8'},
                'ram':{'size':51539607552,'description':'48 GiB'}}}}


def verda_type():
    return {'instance_type':'1H100.80S.30V','name':'H100 SXM5 80GB',
            'gpu':{'description':'1x H100 SXM5 80GB','number_of_gpus':1},
            'cpu':{'description':'30 CPU','number_of_cores':30},
            'memory':{'description':'120GB RAM','size_in_gigabytes':120},
            'gpu_memory':{'description':'80GB GPU RAM','size_in_gigabytes':80},
            'price_per_hour':'2.71','spot_price':'1.23','currency':'usd'}


def test_linode_verified_region_join_override_vpu_and_pagination():
    from pipelines.gpu_prices.providers.linode import LinodeCollector, normalize
    plan = linode_plan()
    regions = {'gb-lon':{'capabilities':['GPU Linodes']},'us-central':{'capabilities':[]}}
    rows = normalize(plan, regions, {('gb-lon',plan['id']):True,('us-central',plan['id']):False}, STAMP)
    assert len(rows)==1 and rows[0].price_per_hour==1.7 and rows[0].availability=='available'
    assert rows[0].ram_gb==32 and rows[0].minimum_billing_seconds==3600
    assert normalize({**plan,'gpus':0,'accelerated_devices':2},regions,{},STAMP)==[]
    def page(data):return {'data':data,'page':1,'pages':1,'results':len(data)}
    fake=FakeClient([page([plan]),page([{'id':key,**value} for key,value in regions.items()]),
                     page([{'region':'gb-lon','plan':plan['id'],'available':True}])])
    collector=LinodeCollector(client=fake,clock=lambda:STAMP,sleep=lambda _:None)
    assert collector.collect()==rows and collector.complete
    assert len(fake.urls)==3 and all('page_size=500' in u for u in fake.urls)
    with pytest.raises(ValueError,match='unreviewed'):
        normalize({**plan,'id':'new-gpu'},regions,{},STAMP)


def test_scaleway_verified_money_minutes_hardware_aliases_and_paging():
    from pipelines.gpu_prices.providers.scaleway import normalize, ScalewayCollector
    item=scaleway_product()
    row=normalize(item,STAMP)
    assert row.price_per_hour==pytest.approx(.78) and row.price_per_gpu_hour==pytest.approx(.78)
    assert row.currency=='EUR' and row.billing_unit=='instance-minute' and row.ram_gb==48
    assert row.availability=='unknown' and row.availability_observed_at is None
    assert normalize({**item,'sku':'/apple-silicon/m4-s/run_par1'},STAMP) is None
    assert normalize({**item,'sku':'/compute/old_alias/run_fr-par-2'},STAMP) is None
    for unit in ('month','gigabyte'):
        with pytest.raises(ValueError,match='non-hourly'):
            normalize({**item,'unit_of_measure':{'unit':unit,'size':1}},STAMP)
    collector=ScalewayCollector(client=FakeClient([{'products':[item],'total_count':2},
        {'products':[{**item,'sku':'/unrelated/storage'}],'total_count':2}]),clock=lambda:STAMP,sleep=lambda _:None)
    assert len(collector.collect())==1 and collector.pages_fetched==2 and collector.complete


def test_verda_verified_array_decimal_spot_and_unknown_region():
    from pipelines.gpu_prices.providers.verda import VerdaCollector
    collector=VerdaCollector(client=FakeClient([[verda_type()]]),clock=lambda:STAMP)
    rows=collector.collect()
    assert len(rows)==2 and {r.price_type for r in rows}=={'on_demand','spot'}
    assert rows[0].gpu_model=='H100 SXM 80GB' and rows[0].price_per_hour==2.71
    assert all(r.region is None and r.vcpu is None and r.ram_gb is None for r in rows)
    assert model_statistics(rows)['offers']==2
    with PriceStore(':memory:') as store:
        assert store.upsert(rows)==2 and store.upsert(rows)==0
        assert store.latest_prices()[0].region is None
    with pytest.raises(ValueError,match='array'):
        VerdaCollector(client=FakeClient([{'items':[]}])).collect()


def test_fx_ecb_cross_rates_dated_cache_and_native_fallback(tmp_path):
    from pipelines.gpu_prices.fx import parse_rates, refresh, to_usd, URL
    xml=b'''<Envelope xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref"><Cube><Cube time="2026-11-11"><Cube currency="USD" rate="1.2"/><Cube currency="CNY" rate="8.0"/></Cube></Cube></Envelope>'''
    rates=parse_rates(xml)
    euro=offer(currency='EUR',price_per_hour=80,price_per_gpu_hour=10,original_currency='EUR',original_price=80)
    converted=to_usd(euro,rates)
    assert converted.price_per_hour==96 and converted.fx_rate==1.2 and converted.fx_date=='2026-11-11'
    yuan=replace(euro,currency='CNY',original_currency='CNY')
    assert to_usd(yuan,rates).price_per_hour==pytest.approx(12)
    class Client:
        def get_bytes(self,url,**kwargs):assert url==URL;return xml
    path=tmp_path/'fx.json'
    assert refresh(path,client=Client())[1]=='fresh'
    before=path.read_bytes()
    class Failure:
        def get_bytes(self,*a,**k):raise URLError('synthetic offline')
    cached,status=refresh(path,client=Failure())
    assert status=='cached' and cached['date']=='2026-11-11' and path.read_bytes()==before
    assert to_usd(euro,None)==euro
    with pytest.raises(ValueError,match='FX'):
        validate_offer(replace(converted,fx_date='2026-11-13'))


@pytest.mark.parametrize('changes',[
    {'is_from_price':True},{'gpu_fraction':.5},{'tax_basis':'included'},
    {'price_scope':'gpu_component','price_per_hour':3},
    {'effective_from':'2026-11-13T00:00:00Z'}, {'effective_to':'2026-11-12T00:00:00Z'}])
def test_headline_policy_refuses_noncomparable_rows(changes):
    good=offer()
    bad=offer(sku='excluded',price_per_hour=.08,price_per_gpu_hour=.01,**{k:v for k,v in changes.items() if k!='price_per_hour'})
    if changes.get('price_scope')=='gpu_component':bad=replace(bad,price_per_hour=.01)
    stats=model_statistics([good,bad])
    assert stats['cheapest']['on_demand']==3 and stats['offers']==1


def test_scope_monthly_export_and_variant_contracts():
    assert validate_offer(offer(price_scope='per_gpu',price_per_hour=3)).price_per_gpu_hour==3
    for changes in ({'billing_unit':'instance-month'},{'tier':'C'},{'tier':'D'}):
        with pytest.raises(ValueError):validate_offer(offer(**changes))
    assert len({model_slug(s) for s in ['H100 PCIe 80GB','H100 SXM 80GB','H100 NVL 94GB',
        'A100 40GB','A100 80GB','RTX 4090 24GB','RTX 4090 48GB']})==7


@pytest.mark.parametrize('quoted_table', [False, True])
def test_additive_legacy_migration_keeps_every_original_value(tmp_path, quoted_table):
    import sqlite3
    path=tmp_path/'legacy.sqlite'
    fields='provider sku gpu_model gpu_count vcpu ram_gb region price_type currency price_per_hour price_per_gpu_hour source_url collected_at day'.split()
    row=offer().to_dict();row['day']=TODAY
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE offers (provider TEXT NOT NULL,sku TEXT NOT NULL,gpu_model TEXT NOT NULL,gpu_count REAL NOT NULL,vcpu INTEGER NOT NULL,ram_gb REAL NOT NULL,region TEXT NOT NULL,price_type TEXT NOT NULL,currency TEXT NOT NULL,price_per_hour REAL NOT NULL,price_per_gpu_hour REAL NOT NULL,source_url TEXT NOT NULL,collected_at TEXT NOT NULL,day TEXT NOT NULL,PRIMARY KEY(provider,sku,region,price_type,day))')
        conn.execute('INSERT INTO offers VALUES ('+','.join('?' for _ in fields)+')',[row[k] for k in fields])
        if quoted_table:
            conn.execute('ALTER TABLE offers RENAME TO original_offers')
            conn.execute('ALTER TABLE original_offers RENAME TO offers')
    with PriceStore(path) as store:
        actual=dict(store.connection.execute('SELECT * FROM offers').fetchone())
        assert {k:actual[k] for k in fields}=={k:row[k] for k in fields}
        assert actual['tier']=='A' and actual['effective_from'] is None
        from pipelines.gpu_prices.providers.verda import normalize
        assert store.upsert(normalize(verda_type(),STAMP))==2
    with PriceStore(path) as store:assert len(store.latest_prices())==3


def test_multitier_exports_stale_providers_filters_and_native_currency(tmp_path):
    from pipelines.gpu_prices.providers.scaleway import normalize
    with PriceStore(':memory:') as store:
        store.upsert([offer(),offer(provider='linode',tier='B',export_allowed=False,sku='public',price_per_hour=8,price_per_gpu_hour=1),
            offer(provider='verda',tier='C',export_allowed=False,sku='display',price_per_hour=4,price_per_gpu_hour=.5),
            offer(provider='verda',tier='C',export_allowed=False,sku='display-only-model',gpu_model='RTX 4090 48GB'),normalize(scaleway_product(),STAMP)])
        result=build_site(store,tmp_path,today=TODAY,built_at=STAMP)
        data=json.loads((tmp_path/'data/h100-sxm-80gb.json').read_text(encoding='utf-8'))
        assert {r['provider'] for r in data['offers']}=={'azure'} and data['statistics']['cheapest']['on_demand']==3
        assert not (tmp_path/'data/rtx-4090-48gb.json').exists()
        display_only=(tmp_path/'rtx-4090-48gb/index.html').read_text(encoding='utf-8')
        assert 'DataDownload' not in display_only and 'Download this model as JSON' not in display_only
        html=(tmp_path/'h100-sxm-80gb/index.html').read_text(encoding='utf-8')
        assert 'data-filter="provider"' in html and 'data-filter="price-type"' in html and 'data-filter="region-class"' in html
        assert 'data-format="money">$0.500' in html and result['audit']['status']=='passed'
        assert 'EUR ' in (tmp_path/'l4-24gb/index.html').read_text(encoding='utf-8')
        store.record_run(provider='verda',started_at=STAMP,status='failed',pages=0,records=0)
        build_site(store,tmp_path,today=TODAY,built_at=STAMP)
        html=(tmp_path/'h100-sxm-80gb/index.html').read_text(encoding='utf-8')
        assert 'data-provider="verda"' not in html
        assert 'Stale' in (tmp_path/'index.html').read_text(encoding='utf-8')


def test_vast_config_fallback_and_empty_environment_override(monkeypatch):
    monkeypatch.delenv('AFFILIATE_VAST_REF',raising=False)
    expected=json.loads((Path(__file__).resolve().parents[1]/'config/affiliates.json').read_text(encoding='utf-8'))['vast']['ref_id']
    assert parse_qs(urlsplit(link_for('vast','','','')['url']).query)['ref_id']==[expected]
    monkeypatch.setenv('AFFILIATE_VAST_REF','')
    assert not link_for('vast','','','')['is_affiliate']
    assert not link_for('together','','','')['is_affiliate']


def test_prices_first_unknown_region_and_instance_scope_cards(tmp_path):
    from pipelines.gpu_prices.audit import Page
    values=[offer(gpu_count=1,price_per_hour=2,price_per_gpu_hour=2),
            offer(provider='verda',sku='unspecified',region=None,gpu_count=2,price_scope='per_gpu',
                  price_per_hour=.406,price_per_gpu_hour=.406,tax_basis='unknown'),
            offer(sku='edge',region='attdallas1',price_per_hour=.08,price_per_gpu_hour=.01),
            offer(sku='tax-included',price_per_hour=.16,price_per_gpu_hour=.02,tax_basis='included'),
            offer(sku='fraction',gpu_count=.5,gpu_fraction=.5,price_per_hour=.01,price_per_gpu_hour=.02)]
    with PriceStore(':memory:') as store:
        store.upsert(values)
        build_site(store,tmp_path,today=TODAY,built_at=STAMP)
        assert audit_site(store.connection,tmp_path)['status']=='passed'
    html=(tmp_path/'index.html').read_text(encoding='utf-8')
    cards=Page(html).cards
    cheapest=[c for c in cards if c['attrs']['data-list']=='cheapest']
    assert len(cheapest)==1 and cheapest[0]['attrs']['data-provider']=='verda'
    assert cheapest[0]['roles']['offer-price']=='USD $0.812/hr'
    assert cheapest[0]['roles']['per-gpu']=='$0.406 per GPU-hour'
    assert cheapest[0]['roles']['checked']=='checked 08:00 UTC'
    assert cheapest[0]['roles']['offer-region']=='Region not specified by provider'
    assert cheapest[0]['roles']['cta']=='View on Verda / DataCrunch →'
    assert 'Tax unknown' in cheapest[0]['roles']['offer-badges']
    assert html.index('data-list="cheapest"')<html.index('data-offer-browser')<html.index('class="partner-card"')<html.index('class="comparison"')
    assert '<div class="filters" hidden>' in html
    assert all('hidden' not in c['attrs'] for c in cards)  # Server output is usable with JS disabled.
    for field in ('gpu-model','gpu-count','provider','price-type','region-class'):
        assert f'data-filter="{field}"' in html
    model=(tmp_path/'h100-sxm-80gb/index.html').read_text(encoding='utf-8')
    tr=load_catalogs()['en']
    assert model.index('data-offer-browser')<model.index(tr.html('regions_heading'))<model.index(tr.html('history_heading'))<model.index('class="faq"')
    exported=json.loads((tmp_path/'data/h100-sxm-80gb.json').read_text(encoding='utf-8'))
    assert exported['statistics']['cheapest']['on_demand']==.406
    assert exported['history']['on_demand'][-1]['min']==.406


def test_home_offer_cap_and_model_page_keeps_all_observations(tmp_path):
    from pipelines.gpu_prices.audit import Page
    with PriceStore(':memory:') as store:
        store.upsert([offer(sku=f'gpu-{i}',gpu_count=1,price_per_hour=(i+1)/100,price_per_gpu_hour=(i+1)/100) for i in range(105)])
        build_site(store,tmp_path,today=TODAY,built_at=STAMP)
    home=Page((tmp_path/'index.html').read_text(encoding='utf-8'))
    model=Page((tmp_path/'h100-sxm-80gb/index.html').read_text(encoding='utf-8'))
    all_model_cards=[c for path in (tmp_path/'h100-sxm-80gb').rglob('*.html') for c in Page(path.read_text(encoding='utf-8')).cards]
    assert len(home.cards)==101 and len(model.cards)==12 and len(all_model_cards)==105
    assert len({c['attrs']['data-offer'] for c in all_model_cards})==105
    rates=[float(c['attrs']['data-sort-gpu']) for c in home.cards if c['attrs']['data-list']=='offers']
    assert rates==sorted(rates) and rates[0]==.01 and rates[-1]==1
    assert 'See all for H100 SXM 80GB' in home.text and 'up to 100 offers' in home.text


def test_capped_home_keeps_fractional_group_and_native_prices_are_not_usd(tmp_path):
    from pipelines.gpu_prices.audit import Page
    from pipelines.gpu_prices.fx import to_usd, URL
    euro=offer(provider='scaleway',sku='converted',gpu_model='H200 141GB',gpu_count=2,
               currency='EUR',price_per_hour=3,price_per_gpu_hour=1.5,original_currency='EUR',original_price=3)
    converted=to_usd(euro,{'date':TODAY,'rates':{'USD':1.2,'EUR':1},'source':URL})
    with PriceStore(':memory:') as store:
        store.upsert([offer(sku=f'whole-{i}') for i in range(105)]+[
            offer(sku='partial',gpu_count=.5,gpu_fraction=.5,price_per_hour=.01,price_per_gpu_hour=.02),
            converted,replace(euro,sku='native',gpu_model='L4 24GB')])
        build_site(store,tmp_path,today=TODAY,built_at=STAMP)
        assert audit_site(store.connection,tmp_path)['status']=='passed'
    home=Page((tmp_path/'index.html').read_text(encoding='utf-8'))
    assert len([c for c in home.cards if c['attrs']['data-list'] in {'offers','fractional'}])==100
    assert len([c for c in home.cards if c['attrs']['data-list']=='fractional'])==1
    h200=Page((tmp_path/'h200-141gb/index.html').read_text(encoding='utf-8'))
    assert h200.cards[0]['roles']['offer-price']=='USD $3.60/hr'
    assert h200.cards[0]['roles']['native-price']=='Original hourly price: EUR 3/hr · Instance'
    native=Page((tmp_path/'l4-24gb/index.html').read_text(encoding='utf-8'))
    assert native.cards[0]['roles']['offer-price']=='EUR 3/hr'
    assert native.cards[0]['attrs']['data-sort-gpu']=='Infinity'
    assert 'USD conversion unavailable' in native.text
    assert not any(c['attrs']['data-gpu-model']=='L4 24GB' and c['attrs']['data-list']=='cheapest' for c in home.cards)


@pytest.mark.parametrize('damage',['selection','currency','region','clock','sort','cta','badge','derived-total'])
def test_audit_rejects_corrupt_offer_card_semantics(tmp_path,damage):
    with PriceStore(':memory:') as store:
        store.upsert([offer(provider='verda',region=None,gpu_count=2,price_scope='per_gpu',price_per_hour=.406,price_per_gpu_hour=.406)])
        build_site(store,tmp_path,today=TODAY,built_at=STAMP)
        home=tmp_path/'index.html';original=home.read_text(encoding='utf-8')
        before,after={
            'selection':('data-list="cheapest"','data-list="offers"'),
            'currency':('<span class="currency">USD','<span class="currency">EUR'),
            'region':('class="offer-region">Region not specified by provider','class="offer-region">eastus'),
            'clock':('data-format="clock">08:00 UTC','data-format="clock">09:00 UTC'),
            'sort':('data-sort-gpu="0.406"','data-sort-gpu="0.00406"'),
            'cta':('View on Verda / DataCrunch →','View on Azure →'),
            'badge':('class="tag tax_unknown">Tax unknown','class="tag tax_unknown">Tax excluded'),
            'derived-total':('data-format="instance-money">$0.812','data-format="instance-money">$0.406'),
        }[damage]
        assert before in original
        home.write_text(original.replace(before,after,1),encoding='utf-8')
        with pytest.raises(AuditError,match='Statistics audit FAILED'):
            audit_site(store.connection,tmp_path)


PAGE_COUNTS = {'featurize': 11, 'nebius': 14, 'lambda': 22, 'paperspace': 13, 'baidu_gpu': 31}


def page_html(name):
    name = 'lambda_' if name == 'lambda' else name
    return (Path(__file__).parent / 'fixtures/provider_pages' / (name + '.html')).read_bytes()


class PageClient:
    def __init__(self, name, failure=None):
        self.name, self.failure, self.calls = name, failure, []

    def get_bytes(self, url, *, accept):
        self.calls.append((url, accept))
        if self.failure == 'network':
            raise URLError('synthetic outage')
        return b'<h1>changed page</h1>' if self.failure == 'parse' else page_html(self.name)


def fixture_page_offers(name, stamp=STAMP):
    from pipelines.gpu_prices.providers.page_collector import PARSERS, normalize
    return [normalize(row) for row in PARSERS[name].parse(page_html(name).decode('utf-8'), stamp)]


@pytest.mark.parametrize('name', PAGE_COUNTS)
def test_page_collectors_normalize_real_fixtures_without_inventing_allocations(name):
    from pipelines.gpu_prices.providers.page_collector import PageCollector
    from pipelines.gpu_prices.providers.registry import PROVIDERS
    client = PageClient(name)
    with PriceStore(':memory:') as store:
        collector = PageCollector(name, store=store, client=client, clock=lambda: STAMP)
        rows = collector.collect()
        assert collector.complete and collector.pages_fetched == 1
        assert len(rows) == collector.raw_items == PAGE_COUNTS[name]
        assert client.calls == [(PROVIDERS[name]['source'], 'text/html')]
        assert all(o.tier == PROVIDERS[name]['tier'] and not o.export_allowed for o in rows)
        assert all(not o.gpu_model.startswith('NVIDIA ') for o in rows)
        assert store.upsert(rows) == PAGE_COUNTS[name]
        expected = [replace(o, collected_at='2026-11-12T08:00:00.000000Z') for o in rows]
        assert store.latest_prices() == sorted(expected, key=lambda o: (o.currency, o.price_per_gpu_hour is not None, o.price_per_gpu_hour or 0, o.provider, o.region or '', o.sku, o.price_type))
        if name in {'featurize', 'paperspace'}:
            assert all(o.gpu_count is None and o.price_per_gpu_hour is None for o in rows)
            assert model_statistics(rows)['offers'] == 0
        if name == 'nebius':
            assert all(o.gpu_count is None and o.price_scope == 'gpu_component' for o in rows)
            assert all(o.price_per_gpu_hour == o.price_per_hour for o in rows)
        if name == 'baidu_gpu':
            assert all(o.vcpu is None and o.cpu_raw for o in rows)
            fractional = next(o for o in rows if o.gpu_fraction == .25)
            assert fractional.gpu_count is None
            assert fractional.price_per_gpu_hour == fractional.price_per_hour * 4


@pytest.mark.parametrize('failure', [None, 'network', 'parse'])
def test_hourly_page_budget_survives_reopen_failure_and_workflow_overlap(tmp_path, failure):
    from pipelines.gpu_prices.providers.page_collector import PageCollector
    path, client = tmp_path / 'hourly.sqlite', PageClient('featurize', failure)
    with PriceStore(path) as store:
        collector = PageCollector('featurize', store=store, client=client, clock=lambda: STAMP)
        if failure:
            with pytest.raises((URLError, ValueError)):
                collector.collect()
        else:
            collector.collect()
        assert store.connection.execute("SELECT value FROM meta WHERE key='page_last_fetch:featurize'").fetchone()[0] == STAMP
    with PriceStore(path) as store, PriceStore(path) as overlapping:
        for stamp in ('2026-11-12T08:15:00Z', '2026-11-12T08:59:59Z'):
            skipped = PageCollector('featurize', store=overlapping, client=client, clock=lambda: stamp)
            assert skipped.collect() == [] and skipped.skipped
        assert len(client.calls) == 1
        # Another provider has a separate budget, and exactly 60 minutes is due.
        assert store.claim_page_fetch('nebius', '2026-11-12T08:15:00Z')
        assert store.claim_page_fetch('featurize', '2026-11-12T09:00:00Z')
        assert not overlapping.claim_page_fetch('featurize', '2026-11-12T09:00:00Z')


@pytest.mark.parametrize('status', [403, 429, 500])
def test_page_http_errors_never_retry_inside_the_hour(status):
    calls, sleeps = [], []
    url = 'https://featurize.cn/vm/available'
    def opener(req, timeout):
        calls.append(req)
        assert req.get_header('Accept') == 'text/html'
        assert req.get_header('Authorization') is None and req.get_header('Cookie') is None
        raise HTTPError(url, status, 'synthetic failure', {'Retry-After': '60'}, BytesIO(b'failed'))
    with pytest.raises(HTTPError):
        http.HttpClient(opener=opener, sleep=sleeps.append).get_bytes(url, accept='text/html')
    assert len(calls) == 1 and not sleeps


def test_blocked_page_providers_remain_link_only_and_outside_http_allowlist():
    from pipelines.gpu_prices.providers.page_collector import PARSERS
    from pipelines.gpu_prices.providers.registry import PROVIDERS
    from pipelines.gpu_prices.links import PROVIDER_ONLY
    for name in ('runpod', 'digitalocean_gpu', 'salad', 'thunder'):
        assert not PROVIDERS[name]['enabled'] and PROVIDERS[name]['link_only']
        assert name not in PARSERS
        assert any(p['provider'] == name for p in PROVIDER_ONLY)
        with pytest.raises(ValueError, match='allowlist'):
            http.validate_url(PROVIDERS[name]['source'])


def test_page_cli_persists_hourly_skips_without_replacing_success(tmp_path, monkeypatch, capsys):
    from pipelines.gpu_prices import run
    from pipelines.gpu_prices.providers.page_collector import PageCollector
    from pipelines.gpu_prices.fx import URL
    clients = {name: PageClient(name) for name in ('featurize', 'baidu_gpu')}
    monkeypatch.setattr(run, 'PageCollector', lambda name, store: PageCollector(name, store=store, client=clients[name], clock=lambda: STAMP))
    monkeypatch.setattr(run, 'configure_logging', lambda: None)
    rates = {'date': '2026-11-11', 'source': URL, 'rates': {'EUR': 1, 'USD': 1.2, 'CNY': 8}}
    monkeypatch.setattr(run, 'refresh', lambda path: (rates, 'fresh'))
    args = ['collect', '--db', str(tmp_path/'cli.sqlite'), '--provider', 'featurize', '--provider', 'baidu_gpu']
    assert run.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert {name: entry['records'] for name, entry in first['collections'].items()} == {'featurize': 11, 'baidu_gpu': 31}
    assert first['fx']['status'] == 'fresh'
    assert run.main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert all(entry['status'] == 'skipped' and entry['pages'] == 0 for entry in second['collections'].values())
    with PriceStore(tmp_path/'cli.sqlite') as store:
        assert store.connection.execute('SELECT COUNT(*) FROM collection_runs').fetchone()[0] == 2
        assert all(o.original_currency == 'CNY' and o.fx_date == rates['date'] for o in store.latest_prices())
        assert all(o.collected_at == '2026-11-12T08:00:00.000000Z' for o in store.latest_prices())
    assert all(len(client.calls) == 1 for client in clients.values())


def test_lambda_effective_boundary_and_all_page_rows_across_eight_languages(tmp_path):
    from pipelines.gpu_prices.audit import Page
    from pipelines.gpu_prices.fx import to_usd, URL
    from pipelines.gpu_prices.regions import eligible
    rates = {'date': '2026-11-11', 'source': URL, 'rates': {'EUR': 1, 'USD': 1.2, 'CNY': 8}}
    page_rows = [to_usd(o, rates) for name in PAGE_COUNTS for o in fixture_page_offers(name)]
    future = [o for o in page_rows if o.provider == 'lambda']
    assert not any(o.current_at('2026-11-15T23:59:59.999999Z') for o in future)
    assert all(o.current_at('2026-11-16T00:00:00Z') for o in future)
    assert model_statistics(future, at='2026-11-15T23:59:59Z')['offers'] == 0
    assert model_statistics(future, at='2026-11-16T00:00:00Z')['offers'] == 22
    baseline = offer(gpu_model='V100 16GB', sku='shared-model', gpu_count=1, price_per_hour=2.4, price_per_gpu_hour=2.4)
    with PriceStore(':memory:') as store:
        assert store.upsert(page_rows + [baseline]) == 92
        built = build_site(store, tmp_path, today=TODAY, built_at=STAMP)
        assert built['offers_today'] == 70 and built['audit']['status'] == 'passed'
        assert audit_site(store.connection, tmp_path)['status'] == 'passed'
        current = store.current_prices(TODAY, STAMP)
        for lang in LANGUAGES:
            tr = load_catalogs()[lang.code]
            home = Page((tmp_path/lang.path().lstrip('/')/'index.html').read_text(encoding='utf-8'))
            cheapest = [c for c in home.cards if c['attrs']['data-list'] == 'cheapest']
            assert len(cheapest) == len({o.gpu_model for o in current if eligible(o)})
            assert all(c['attrs']['data-provider'] in {'azure', 'baidu_gpu'} for c in cheapest)
            nebius_page = tmp_path/lang.path('/hgx-h100/').lstrip('/')/'index.html'
            text = nebius_page.read_text(encoding='utf-8')
            assert tr.text('gpu_component_note') in unescape(text)
            assert tr.text('tier_b') in unescape(text)
            assert all(c['attrs']['data-provider'] != 'lambda' for c in home.cards)
            # The standalone component prices remain display only, without downloads.
            assert 'DataDownload' not in text
        exports = [p for p in (tmp_path/'data').glob('*.json') if p.name != 'latest.json']
        assert len(exports) == 1
        data = json.loads(exports[0].read_text(encoding='utf-8'))
        assert {r['provider'] for r in data['offers']} == {'azure'}
        assert 'price_conditions' in data['offers'][0]
        # A second build after the exact activation boundary admits Lambda.
        store.upsert(fixture_page_offers('lambda', '2026-11-16T00:00:00Z'))
        after = build_site(store, tmp_path, today='2026-11-16', built_at='2026-11-16T00:00:00Z')
        assert after['offers_today'] == 22 and after['audit']['status'] == 'passed'


@pytest.mark.parametrize('damage', ['component', 'tier', 'conditions', 'unknown-rate'])
def test_independent_audit_detects_page_price_qualifier_corruption(tmp_path, damage):
    rows = [fixture_page_offers('nebius')[0], fixture_page_offers('paperspace')[0]]
    conditional = next(o for o in fixture_page_offers('paperspace') if o.price_type == 'reserved')
    rows.append(conditional)
    with PriceStore(':memory:') as store:
        store.upsert(rows)
        build_site(store, tmp_path, today=TODAY, built_at=STAMP)
        home = tmp_path/'index.html'
        text = home.read_text(encoding='utf-8')
        old, new = {
            'component': ('GPU only; CPU/RAM billed separately', 'GPU includes CPU/RAM'),
            'tier': ('C · Display only', 'A · Official API'),
            'conditions': ('3-year commitment', '1-year commitment'),
            'unknown-rate': ('Per-GPU rate unavailable: allocation unknown', '$0.001 per GPU-hour'),
        }[damage]
        assert old in text
        home.write_text(text.replace(old, new), encoding='utf-8')
        with pytest.raises(AuditError):
            audit_site(store.connection, tmp_path)


def test_fx_missing_cny_rate_keeps_native_instance_price():
    from pipelines.gpu_prices.fx import to_usd, URL
    original = fixture_page_offers('featurize')[0]
    rates = {'date': '2026-11-11', 'source': URL, 'rates': {'EUR': 1, 'USD': 1.2}}
    assert to_usd(original, rates) == original
