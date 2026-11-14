"""Offline parser and integration tests using real fixtures and synthetic cases.

Use python -B -m pytest --noconftest -p no:cacheprovider to avoid writes by the
repository's shared conftest/cache while another job edits the existing suite.
No test uses the network or deployment code.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

from functools import partial

import pytest

from pipelines.gpu_prices.providers import clore
from pipelines.gpu_prices.providers.pages import Document, ParseError
from pipelines.gpu_prices.providers.pages import ai_galaxy, anygpu, gpushare_docs


ROOT = Path(__file__).parent / "fixtures" / "new_sources"
STAMP = "2026-11-12T15:27:54Z"
MANIFEST = json.loads((ROOT / "fetch_manifest.json").read_text(encoding="utf-8"))
PARSERS = {"clore_marketplace": clore, "anygpu": anygpu,
           "gpushare_docs": gpushare_docs, "ai_galaxy": ai_galaxy}


def source(name):
    data = (ROOT / MANIFEST["providers"][name]["fixture"]).read_text(encoding="utf-8")
    return json.loads(data) if name == "clore_marketplace" else data


def parsed(name):
    return PARSERS[name].parse(source(name), STAMP)


def server(ident=1, *, gpu="2x NVIDIA GeForce RTX 4090", daily=48, spot=24,
           country="US", rented=False):
    """Synthetic server using the exact paths verified in the real fixture."""
    return {"id": ident, "rented": rented,
            "specs": {"gpu": gpu, "gpuram": 24, "cpus": "8/16", "ram": 64,
                      "net": {"cc": country}},
            "partial_gpu_rental": False,
            "price": {"on_demand": {"USD-Blockchain": daily},
                      "spot": {"USD-Blockchain": spot},
                      "usd": {"on_demand_clore": 999, "spot": 999}}}


def parse_servers(*servers):
    return clore.parse({"code": 0, "servers": list(servers)}, STAMP)


@pytest.mark.parametrize("name", PARSERS)
def test_real_fixtures_provenance(name):
    meta = MANIFEST["providers"][name]
    blob = (ROOT / meta["fixture"]).read_bytes()
    assert meta["succeeded"] is True
    assert meta["attempts"] == 1
    assert hashlib.sha256(blob).hexdigest() == meta["fixture_sha256"]
    assert len(blob) == meta["fixture_bytes"] < 60_000
    assert len(meta["sha256"]) == 64  # Original complete response hash.
    assert len(parsed(name)) == meta["fixture_record_count"]
    assert MANIFEST["request"]["authentication"] is False
    assert MANIFEST["request"]["cookies"] is False
    assert MANIFEST["request"]["method"] == "GET"


@pytest.mark.parametrize("name", PARSERS)
def test_record_contract(name):
    for row in parsed(name):
        assert {"provider", "gpu_model", "gpu_model_raw", "gpu_count", "vram_raw",
                "price_per_hour", "price_per_gpu_hour", "price_scope", "currency",
                "price_type", "region", "tier", "tax_basis", "is_from_price",
                "source_url", "checked_at", "observed_at", "export_allowed"} <= row.keys()
        assert row["checked_at"] == row["observed_at"] == row["collected_at"] == STAMP
        assert row["tax_basis"] == "unknown"
        assert row["price_per_hour"] > 0
        assert row["tier"] == ("A" if name == "clore_marketplace" else "C")
        assert row["export_allowed"] is (name == "clore_marketplace")
        if row["gpu_count"] is None:
            assert row["price_per_gpu_hour"] is None
        elif row["price_scope"] == "instance":
            assert row["price_per_gpu_hour"] == pytest.approx(row["price_per_hour"] / row["gpu_count"])
        if "unmapped_gpu_model" in row["flags"]:
            assert row["gpu_model"] == row["gpu_model_raw"]


def test_clore_real_sample_concrete_values():
    data = source("clore_marketplace")
    assert len(data["servers"]) == 30
    rows = parsed("clore_marketplace")
    assert len(rows) == 28
    row = next(r for r in rows if r["sku"] == "122309" and r["price_type"] == "spot")
    assert row["gpu_model_raw"] == "NVIDIA H100 NVL"
    assert row["gpu_count"] == 2 and row["region"] == "US"
    assert row["price_per_day"] == 28
    assert row["price_per_hour"] == pytest.approx(28 / 24)
    assert row["price_per_gpu_hour"] == pytest.approx(28 / 24 / 2)
    assert row["vram_raw"] == "94 GB"
    assert row["source_url"] == row["rent_url"] == "https://clore.ai/marketplace"
    rented = {str(s["id"]) for s in data["servers"] if s["rented"]}
    assert rented and not rented.intersection(r["sku"] for r in rows)


def test_clore_injected_fetch_called_once_and_whole_server_daily_conversion():
    calls = []
    def fetch(url):
        calls.append(url)
        return {"servers": [server()]}
    rows = clore.collect(fetch=fetch, checked_at=STAMP)
    assert calls == [clore.API]
    demand = next(r for r in rows if r["price_type"] == "on_demand")
    spot = next(r for r in rows if r["price_type"] == "spot")
    assert (demand["price_per_hour"], demand["price_per_gpu_hour"]) == (2, 1)
    assert (spot["price_per_hour"], spot["price_per_gpu_hour"]) == (1, 0.5)
    assert demand["original_price"] == 2
    assert demand["price_basis"] == "USD-Blockchain"
    assert any("Stablecoin" in note for note in demand["notes"])
    assert any("fees may apply" in note for note in demand["notes"])
    assert demand["region_country"] == "US" and demand["zone"] is None
    assert demand["cpu_raw"] == "8/16" and demand["vcpu"] is None


@pytest.mark.parametrize("value", [0, -1, "nan", "Infinity", "1e1000", 4800.01, True, "bad"])
def test_clore_absurd_or_invalid_prices_are_skipped(value):
    assert parse_servers(server(daily=value, spot=value)) == []


def test_clore_price_limit_is_per_gpu_and_inclusive():
    rows = parse_servers(server(daily=4800, spot=4800))
    assert len(rows) == 2 and all(r["price_per_gpu_hour"] == 100 for r in rows)


@pytest.mark.parametrize("rented", [True, None, "false", 0])
def test_clore_rental_state_must_be_explicitly_false(rented):
    assert parse_servers(server(rented=rented)) == []


@pytest.mark.parametrize("gpu", ["RTX 4090", "0x RTX 4090", "2x", "2x Mixed GPUs", "2x unknown", "2x No GPU", None])
def test_clore_missing_count_or_model_is_skipped(gpu):
    assert parse_servers(server(gpu=gpu)) == []


def test_clore_partial_gpu_count_never_changes_whole_server_denominator():
    full = server()
    full["partial_gpu_rental"] = {"available_gpus": 2, "total_gpus": 2}
    assert all(r["gpu_count"] == 2 and r["available_gpu_count"] == 2 for r in parse_servers(full))
    for available, total in [(1, 2), (0, 2), (2, 3), (True, 2)]:
        changed = deepcopy(full)
        changed["partial_gpu_rental"] = {"available_gpus": available, "total_gpus": total}
        assert parse_servers(changed) == []


def test_clore_dedupe_keeps_cheapest_five_in_each_required_group():
    servers = [server(i, daily=24 * i, spot=12 * i) for i in range(9, 0, -1)]
    servers += [deepcopy(servers[-1]), server(20, country="DE"),
                server(21, gpu="1x NVIDIA GeForce RTX 4090"),
                server(22, gpu="2x NVIDIA H100 NVL")]
    # A genuinely different 48 GB mod stays in its own canonical group.
    servers[0]["specs"]["gpuram"] = 48
    rows = parse_servers(*servers)
    groups = Counter((r["gpu_model"], r["gpu_count"], r["price_type"], r["region"]) for r in rows)
    assert max(groups.values()) == 5
    demand = [r for r in rows if r["gpu_model_raw"] == "NVIDIA GeForce RTX 4090"
              and r["gpu_model"] == "RTX 4090 24GB"
              and r["gpu_count"] == 2 and r["region"] == "US" and r["price_type"] == "on_demand"]
    assert [r["sku"] for r in demand] == ["1", "2", "3", "4", "5"]
    assert any(r['sku']=='9' and r['gpu_model']=='RTX 4090 48GB' for r in rows)
    assert len({(r["sku"], r["price_type"]) for r in rows}) == len(rows)
    assert {r["region"] for r in rows} == {"US", "DE"}


def test_clore_usd_fallback_is_provider_conversion_and_not_a_second_offer():
    item = server()
    del item["price"]["on_demand"]["USD-Blockchain"]
    del item["price"]["spot"]["USD-Blockchain"]
    item["price"]["usd"] = {"on_demand_usd": None, "on_demand_clore": 48,
                             "on_demand_btc": 96, "spot": 24}
    rows = parse_servers(item)
    assert len(rows) == 2
    demand = next(r for r in rows if r["price_type"] == "on_demand")
    assert demand["price_per_hour"] == 2
    assert demand["price_basis"] == "price.usd.on_demand_clore"
    assert any("Clore's own USD conversion" in note for note in demand["notes"])
    assert all(r["currency"] == "USD" for r in rows)


def test_clore_crypto_amounts_are_not_dollars_and_missing_conversion_is_skipped():
    item = server()
    item["price"] = {"on_demand": {"bitcoin": 0.00001, "CLORE-Blockchain": 100},
                     "spot": {"bitcoin": 0.00001}}
    assert parse_servers(item) == []


@pytest.mark.parametrize("payload", [{}, [], {"code": 1, "servers": []}, {"servers": {}}])
def test_clore_invalid_envelopes_fail(payload):
    with pytest.raises(ParseError):
        clore.parse(payload, STAMP)


def test_unknown_gpu_names_survive_and_existing_mappings_are_reused():
    unknown = clore.gpu_fields("Future Q9000", "64GB")
    assert unknown["gpu_model"] == unknown["gpu_model_raw"] == "Future Q9000"
    assert unknown["flags"] == ["unmapped_gpu_model"]
    known = clore.gpu_fields("NVIDIA A100-SXM4", "40GB")
    assert known["gpu_model"] == "NVIDIA A100 SXM4 40GB" and known["flags"] == []
    vague = clore.gpu_fields("NVIDIA A100", "40GB")
    assert vague["gpu_model"] == "NVIDIA A100" and vague["flags"]


def test_anygpu_fixture_uses_normal_hourly_price_and_preserves_specs():
    rows = parsed("anygpu")
    assert len(rows) == 2
    row = next(r for r in rows if r["gpu_model_raw"] == "RTX 4090")
    assert row["price_per_hour"] == 2.24 and row["member_price_per_hour"] == 1.97
    assert row["currency"] == "CNY" and row["region"] is None
    assert row["gpu_count"] is None and row["price_per_gpu_hour"] is None
    assert row["vram_raw"] == "24GB" and row["ram_raw"] == "64GB"
    assert row["cpu_raw"] == "8核CPU" and row["disk_raw"] == "50GB+200GB SSD"
    assert row["is_from_price"] is False
    assert any("1.97" in n for n in row["notes"])
    assert not any("5090" in r["gpu_model_raw"] for r in rows)
    assert "¥2.72.0" in source("anygpu")
    assert next(r for r in rows if r["gpu_model_raw"] == "RTX 3090")["price_per_hour"] == 1.55


def test_anygpu_explicit_count_only_and_no_monthly_substitution():
    html = source("anygpu").replace("RTX 4090", "2x RTX 4090")
    row = next(r for r in anygpu.parse(html, STAMP) if r["gpu_model_raw"] == "RTX 4090")
    assert row["gpu_count"] == 2 and row["price_per_gpu_hour"] == 1.12
    changed = source("anygpu").replace("¥2.24", "¥0.0")
    # A malformed/nonpositive price cannot fall through to a monthly/member one.
    with pytest.raises(ParseError):
        anygpu.parse(changed, STAMP)


def test_gpushare_real_table_refuses_ambiguous_period_despite_unrelated_hourly_example():
    html = source("gpushare_docs")
    assert "青铜会员9.5折" in html and "活动价¥2.0/小时" in html
    assert gpushare_docs.parse(html, STAMP) == []
    doc = Document(html)
    table = next(t for t in doc.root.find(tag="table") if "青铜会员" in t.text())
    assert "元/小时" not in table.text()


def test_gpushare_explicit_hourly_bronze_and_vram_anomalies_synthetic_header():
    # Only this test changes a unit. The saved real fixture is never doctored.
    html = source("gpushare_docs").replace("青铜会员9.5折", "青铜会员9.5折（元/小时）")
    rows = gpushare_docs.parse(html, STAMP)
    assert len(rows) == 20
    assert rows[0]["gpu_model_raw"] == "Tesla P4" and rows[0]["price_per_hour"] == 0.57
    assert all(r["is_from_price"] and not r["export_allowed"] for r in rows)
    assert all(r["gpu_count"] is None and r["price_scope"] == "gpu_component" for r in rows)
    anomaly = next(r for r in rows if r["gpu_model_raw"] == "GeForce RTX 3090 Ti")
    assert anomaly["reported_vram_raw"] == "8GB"
    assert anomaly["vram_raw"] is None and anomaly["canonical_vram_raw"] == "24GB"
    assert "vram_conflict" in anomaly["flags"]
    assert anomaly["gpu_model_normalized"].endswith("24GB")
    assert anomaly["price_per_hour"] == anomaly["price_per_gpu_hour"] == 2.28
    second = next(r for r in rows if r["gpu_model_raw"] == "GeForce RTX 3080 Ti")
    assert second["reported_vram_raw"] == "10GB" and second["canonical_vram_raw"] == "12GB"
    assert second["vram_raw"] is None
    a100 = next(r for r in rows if r["gpu_model_raw"] == "NVIDIA A100")
    assert a100["price_per_hour"] == 3.04 and a100["vram_raw"] == "40GB"


def test_ai_galaxy_real_explicit_hourly_promos_and_separate_memory_variants():
    rows = parsed("ai_galaxy")
    assert len(rows) == 7
    r4090 = next(r for r in rows if r["sku"] == "RTX 4090 24G")
    assert r4090["price_per_hour"] == 1.65 and r4090["vram_raw"] == "24GB"
    r48 = next(r for r in rows if r["sku"] == "RTX 4090 48G")
    assert r48["price_per_hour"] == 2.53 and r48["vram_raw"] == "48GB"
    a100 = next(r for r in rows if r["sku"] == "A100-SXM4 40G")
    assert a100["gpu_model"] == "NVIDIA A100 SXM4 40GB" and a100["price_per_hour"] == 2.75
    assert all(r["is_from_price"] and r["region"] is None and r["gpu_count"] is None for r in rows)
    assert all(r["price_per_gpu_hour"] is None for r in rows)


def test_ai_galaxy_ambiguous_cards_are_skipped_and_h_unit_is_accepted():
    html = source("ai_galaxy")
    assert ai_galaxy.parse(html.replace("元/小时", "元"), STAMP) == []
    assert len(ai_galaxy.parse(html.replace("元/小时", "/h"), STAMP)) == 7
    first_only = html.replace("元/小时", "元", 1)
    rows = ai_galaxy.parse(first_only, STAMP)
    assert len(rows) == 6 and all(r["sku"] != "A100-SXM4 80G" for r in rows)


class Response:
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return b'{"code":0,"servers":[]}'


def test_clore_transport_retries_anonymous_get_with_long_timeout(monkeypatch):
    calls, sleeps = [], []
    def open_request(request, timeout):
        calls.append((request, timeout))
        if len(calls) == 1:
            raise URLError("synthetic timeout")
        return Response()
    monkeypatch.setattr(clore, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    monkeypatch.setattr(clore.time, "sleep", sleeps.append)
    assert clore.fetch_json(clore.API) == {"code": 0, "servers": []}
    assert len(calls) == 2 and sleeps == [2]
    for request, timeout in calls:
        assert request.get_method() == "GET" and request.full_url == clore.API
        assert request.data is None and timeout == 120
        assert request.get_header("Authorization") is None and request.get_header("Cookie") is None
        assert request.get_header("Accept") == "application/json"
    with pytest.raises(ValueError):
        clore.fetch_json("https://example.org/")
    assert len(calls) == 2


@pytest.mark.parametrize("status,expected_calls", [(403, 1), (401, 1), (500, 3), (429, 3)])
def test_clore_transport_stops_on_blocks_and_bounds_retries(monkeypatch, status, expected_calls):
    calls, sleeps = [], []
    def open_request(request, timeout):
        calls.append(request)
        raise HTTPError(request.full_url, status, "synthetic", {"Retry-After": "7"}, None)
    monkeypatch.setattr(clore, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    monkeypatch.setattr(clore.time, "sleep", sleeps.append)
    with pytest.raises(HTTPError):
        clore.fetch_json(clore.API)
    assert len(calls) == expected_calls
    assert sleeps == ([7, 7] if expected_calls == 3 else [])


def test_clore_transport_refuses_redirects():
    with pytest.raises(ValueError, match="redirects are refused"):
        clore._NoRedirects().redirect_request(None, None, 302, "login", {}, "https://clore.ai/login")


@pytest.mark.parametrize("name", PARSERS)
def test_invalid_timestamps_fail_even_for_empty_results(name):
    with pytest.raises(ParseError):
        PARSERS[name].parse(source(name), "2026-11-12")


@pytest.mark.parametrize("name", ["anygpu", "gpushare_docs", "ai_galaxy"])
def test_html_structure_changes_fail(name):
    with pytest.raises(ParseError):
        PARSERS[name].parse("<html><body>Loading</body></html>", STAMP)


# b14 integration: adapters, hourly transport, compressed history and build gate.
from dataclasses import replace
import sqlite3
from pipelines.gpu_prices.store import PriceStore, unpack_rows
from pipelines.gpu_prices.providers.clore_collector import CloreCollector
from pipelines.gpu_prices.providers.page_collector import PageCollector, normalize
from pipelines.gpu_prices.providers.registry import PROVIDERS
from pipelines.gpu_prices.fx import to_usd, URL as FX_URL
from pipelines.gpu_prices.build_site import build_site
from pipelines.gpu_prices.audit import audit_site, AuditError, Page

# This module tests its feature in all eight languages. The complete workload
# matrix and its independent audit remain in test_gpu_workloads.py.
build_site = partial(build_site, include_workloads=False)
audit_site = partial(audit_site, require_workloads=False)
from pipelines.gpu_prices.i18n import load_catalogs
from pipelines.gpu_prices.links import link_for


def integrated(name):
    return [normalize(r) for r in parsed('clore_marketplace' if name=='clore' else name)]


def test_registry_outbound_links_and_all_localizations(monkeypatch):
    catalogs = load_catalogs()
    expected = {'clore': ('A','https://clore.ai/marketplace'),
                'anygpu': ('C','https://www.anygpu.cn/'),
                'ai_galaxy': ('C','https://www.ai-galaxy.cn/')}
    for name, (tier, url) in expected.items():
        assert PROVIDERS[name]['enabled'] and PROVIDERS[name]['tier']==tier
        monkeypatch.setenv('AFFILIATE_'+name.upper()+'_REF', 'must-not-be-added')
        link = link_for(name, 'sku', 'US', 'spot')
        assert link['url']==url and not link['is_affiliate']
    assert 'gpushare_docs' not in PROVIDERS
    assert len(catalogs)==8
    for catalog in catalogs.values():
        for key in ('community_marketplace','clore_price_note','promo_floor_price',
                    'note_clore','note_anygpu','note_ai_galaxy','history_aggregation_note'):
            assert catalog.text(key).strip()
            assert '?' not in catalog.text(key)
        assert '\u2022' in catalog.text('clore_price_note')
    assert catalogs['zh-Hans'].text('community_marketplace') == '\u793e\u533a\u5e02\u573a'
    assert catalogs['ja'].text('community_marketplace') == '\u30b3\u30df\u30e5\u30cb\u30c6\u30a3\u5e02\u5834'


@pytest.mark.parametrize('name', ['clore','anygpu','ai_galaxy'])
@pytest.mark.parametrize('fail', [False, True])
def test_new_sources_hourly_budget_persisted_even_on_failure(tmp_path, name, fail):
    calls = []
    class Client:
        def get_json(self, url):
            calls.append(url)
            if fail: raise URLError('synthetic outage')
            return source('clore_marketplace')
        def get_bytes(self, url, **kwargs):
            calls.append(url)
            if fail: raise URLError('synthetic outage')
            return source(name).encode('utf-8')
    def collector(store, stamp):
        kwargs = dict(store=store, client=Client(), clock=lambda:stamp)
        return CloreCollector(**kwargs) if name=='clore' else PageCollector(name, **kwargs)
    path = tmp_path/'hourly.sqlite'
    with PriceStore(path) as store:
        first = collector(store, STAMP)
        if fail:
            with pytest.raises(URLError): first.collect()
        else:
            rows=first.collect()
            assert rows and all(o.tier==PROVIDERS[name]['tier'] for o in rows)
    # Three subsequent 15-minute workflow invocations, including a new process.
    from datetime import timedelta
    from pipelines.gpu_prices.schema import parse_utc
    for minutes in (15,30,45):
        stamp=(parse_utc(STAMP)+timedelta(minutes=minutes)).isoformat()
        with PriceStore(path) as store:
            skipped=collector(store,stamp)
            assert skipped.collect()==[] and skipped.skipped
    assert len(calls)==1
    with PriceStore(path) as store:
        next_hour=collector(store,(parse_utc(STAMP)+timedelta(hours=1)).isoformat())
        if fail:
            with pytest.raises(URLError): next_hour.collect()
        else: assert next_hour.collect()
    assert len(calls)==2


def test_new_hourly_endpoints_send_only_one_attempt_on_transient_failure():
    from pipelines.gpu_prices.http import HttpClient, validate_url
    for url in (clore.API, anygpu.URLS[0], ai_galaxy.URLS[0]):
        calls=[]
        def fail(request, **kwargs):
            calls.append(request.full_url)
            raise URLError('synthetic transient failure')
        with pytest.raises(URLError):
            HttpClient(opener=fail, sleep=lambda _:pytest.fail('hourly source retried')).get_bytes(url)
        assert calls==[url]
        validate_url(url)
    with pytest.raises(ValueError):
        validate_url(gpushare_docs.URLS[0])


def test_clore_current_top_five_history_top_one_and_laptop_isolation(tmp_path):
    payload={'code':0,'servers':[server(i, daily=24+i, spot=12+i) for i in range(10)]}
    payload['servers'].append(server(50,gpu='1x NVIDIA GeForce RTX 4090 Laptop GPU'))
    rows=[normalize(r) for r in clore.parse(payload,STAMP)]
    assert len(rows)==12  # two types, five desktop plus one Laptop per type
    assert len({r.gpu_model for r in rows})==2
    assert any('Laptop' in r.gpu_model for r in rows)
    with PriceStore(tmp_path/'prices.sqlite') as store:
        store.upsert(rows)
        for hour in range(16,24):
            batch=[replace(o, sku=str(int(o.sku)+hour*100),
                           collected_at=f'2026-11-12T{hour:02}:00:00Z') for o in rows]
            store.upsert(batch)
            assert store.connection.execute("SELECT COUNT(*) FROM offers WHERE provider='clore'").fetchone()[0]==12
        store.upsert([replace(o,collected_at='2026-11-13T00:00:00Z') for o in rows])
        history=store.connection.execute('SELECT row_count,payload FROM history_snapshots').fetchone()
        assert history[0]==4
        archived=unpack_rows(history[1])
        assert sum('Laptop' in r['gpu_model'] for r in archived)==2
        assert min(r['price_per_gpu_hour'] for r in archived if r['price_type']=='on_demand')==pytest.approx(.5)
        assert len(store.latest_prices(day='2026-11-12'))==4
        assert len(store.current_prices('2026-11-13','2026-11-13T01:00:00Z'))==12


def test_legacy_migration_backup_and_lossless_recoverable_archive(tmp_path):
    path=tmp_path/'gpu_prices.sqlite'
    with PriceStore(path) as store:
        # Remove only the new schema marker to emulate an older daily-SKU DB.
        store.connection.execute("DELETE FROM meta WHERE key='compact_history_v1'")
        store.connection.commit()
        rows=integrated('anygpu')
        store.upsert(rows)
        store.record_run(provider='anygpu', started_at=STAMP,status='complete',pages=1,records=2)
        before=[dict(r) for r in store.connection.execute('SELECT * FROM offers')]
    with PriceStore(path) as store:
        assert store.backup_path.exists()
        with sqlite3.connect(store.backup_path) as backup:
            backup.row_factory=sqlite3.Row
            assert [dict(r) for r in backup.execute('SELECT * FROM offers')]==before
        archived=store.connection.execute("SELECT payload FROM legacy_archive WHERE name='offers'").fetchone()[0]
        assert unpack_rows(archived)==before
        assert [dict(r) for r in store.connection.execute('SELECT * FROM offers')]==before
        assert store.connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    with PriceStore(path) as store:
        assert store.backup_path is None
    assert len(list(tmp_path.glob('gpu_prices.backup-*.sqlite')))==1


def test_other_providers_compact_and_run_health_is_bounded(tmp_path):
    # Reuse a valid page offer under a synthetic API provider identity.
    row=replace(integrated('clore')[0], provider='azure', price_per_day=None,
                source_billing_unit=None, price_basis=None)
    rows=[replace(row,sku='sku-'+str(i),price_per_hour=row.price_per_hour+i,
                  price_per_gpu_hour=(row.price_per_hour+i)/row.gpu_count) for i in range(30)]
    with PriceStore(tmp_path/'prices.sqlite') as store:
        store.upsert(rows)
        store.upsert([replace(o,collected_at='2026-11-13T00:00:00Z') for o in rows])
        assert store.connection.execute('SELECT COUNT(*) FROM offers').fetchone()[0]==30
        assert store.connection.execute('SELECT SUM(row_count) FROM history_snapshots').fetchone()[0]==1
        for _ in range(96):
            store.record_run(provider='azure',started_at=STAMP,status='complete',pages=1,records=30)
        store.record_run(provider='azure',started_at=STAMP,status='failed',pages=0,records=0)
        assert store.connection.execute('SELECT COUNT(*) FROM collection_runs').fetchone()[0]==2
        assert store.provider_runs()['azure']['status']=='failed'
        assert store.connection.execute("SELECT COUNT(*) FROM collection_runs WHERE status='complete'").fetchone()[0]==1
        result=build_site(store,tmp_path/'site',today='2026-11-13',built_at='2026-11-13T00:30:00Z')
        assert result['audit']['status']=='passed'


def test_fixture_monthly_growth_below_five_megabytes():
    from pipelines.gpu_prices.growth import estimate, fixture_offers
    result=estimate(fixture_offers())
    assert result['scheduled_runs']==2880
    assert result['current_records']>2100
    assert result['under_5_mb'], result


def test_integrated_native_prices_notes_headlines_and_eight_language_audit(tmp_path,integrated_sources_site):
    rates={'date':'2026-11-11','source':FX_URL,'rates':{'EUR':1,'USD':1.1,'CNY':7.9}}
    rows=[to_usd(o,rates) for name in ('clore','anygpu','ai_galaxy') for o in integrated(name)]
    member=next(o for o in rows if o.provider=='anygpu' and o.gpu_model_raw=='RTX 4090')
    assert member.original_price==2.24 and '1.97' in member.price_conditions
    assert member.price_per_hour==pytest.approx(2.24*1.1/7.9)
    assert member.gpu_count is member.price_per_gpu_hour is None
    galaxy_4090={o.gpu_model for o in rows if o.provider=='ai_galaxy' and o.gpu_model_raw=='RTX 4090'}
    assert galaxy_4090=={'RTX 4090 24GB','RTX 4090 48GB'}
    store,tmp_path,result=integrated_sources_site
    assert result['audit']['status']=='passed'
    page=Page((tmp_path/'index.html').read_text(encoding='utf-8'))
    headlines=[c for c in page.cards if c['attrs']['data-list']=='cheapest']
    assert headlines and {c['attrs']['data-provider'] for c in headlines}=={'clore'}
    assert any(c['attrs']['data-price-type']=='on_demand' for c in headlines)
    assert any(c['attrs']['data-price-type']=='spot' and 'Spot' in c['roles']['offer-badges'] for c in headlines)
    assert 'Community marketplace' in page.text
    assert 'USD (stablecoin) \u2022 platform fees may apply' in page.text
    assert 'promo floor price' in page.text
    assert 'CNY 2.24/hr' in page.text
    assert audit_site(store.connection,tmp_path)['status']=='passed'


@pytest.fixture(scope='module')
def integrated_sources_site(tmp_path_factory):
    folder=tmp_path_factory.mktemp('integrated_sources_site')
    rates={'date':'2026-11-11','source':FX_URL,'rates':{'EUR':1,'USD':1.1,'CNY':7.9}}
    rows=[to_usd(o,rates) for name in ('clore','anygpu','ai_galaxy') for o in integrated(name)]
    store=PriceStore(':memory:')
    store.upsert(rows)
    result = build_site(store,folder,today='2026-11-12',built_at=STAMP)
    yield store,folder,result
    store.close()


@pytest.mark.parametrize('corrupt', ['daily','gpu','fx','floor','marketplace_label'])
def test_independent_audit_rejects_new_source_corruption(tmp_path, corrupt,integrated_sources_site,gpu_site_copy):
    store,tmp_path,_=gpu_site_copy(integrated_sources_site)
    with store:
        if corrupt=='daily':
            store.connection.execute("UPDATE offers SET price_per_day=price_per_day*2 WHERE provider='clore'")
        elif corrupt=='gpu':
            store.connection.execute("UPDATE offers SET price_per_gpu_hour=price_per_gpu_hour*2 WHERE provider='clore'")
        elif corrupt=='fx':
            # Preserve hour/GPU and original*fx consistency; the independent
            # raw ECB numerator/denominator check must still detect corruption.
            store.connection.execute("UPDATE offers SET fx_rate=fx_rate*2,price_per_hour=price_per_hour*2 WHERE provider='anygpu'")
        elif corrupt=='floor':
            store.connection.execute("UPDATE offers SET is_from_price=0 WHERE provider='ai_galaxy'")
        else:
            path=tmp_path/'index.html'
            path.write_text(path.read_text(encoding='utf-8').replace('Community marketplace','Unqualified market'),encoding='utf-8')
        with pytest.raises(AuditError): audit_site(store.connection,tmp_path)



def test_hourly_budget_survives_unchanged_price_data_branch_restore(tmp_path):
    from datetime import timedelta
    from pipelines.gpu_prices.schema import parse_utc
    from ci.refresh_policy import decide, prepare_save, fingerprint
    first=tmp_path/'workflow.sqlite'
    saved=tmp_path/'data_branch.sqlite'
    now=parse_utc(STAMP)
    with PriceStore(first) as store:
        store.upsert(integrated('anygpu'))
        digest=fingerprint(store,now=now)
        with store.connection:
            store.connection.executemany('INSERT OR REPLACE INTO meta VALUES (?,?)',[
                ('saved_fingerprint',digest),('last_saved_utc_day',now.date().isoformat()),
                ('deployed_fingerprint',digest),('last_deploy_at',now.isoformat())])
        assert not decide(store,now=now)['save']
        # The next hourly attempt changes only the budget, not any price.
        later=now+timedelta(hours=1)
        assert store.claim_page_fetch('anygpu',later.isoformat())
        decision=decide(store,now=later)
        assert decision['fingerprint']==digest
        assert decision['save'] and not decision['build']
        assert prepare_save(store,now=later)
        with sqlite3.connect(saved) as target: store.connection.backup(target)
    # A fresh 15-minute workflow restores the saved DB, not the previous
    # workflow's ephemeral working directory. The request must still be skipped.
    with PriceStore(saved) as store:
        assert not store.claim_page_fetch('anygpu',(later+timedelta(minutes=15)).isoformat())
        assert not decide(store,now=later+timedelta(minutes=15))['save']
