"""Offline tests against single-GET official HTML fixtures, without registry IO.

Run with --noconftest and no cacheprovider when enforcing the task's new-files
boundary: the repository conftest otherwise creates shared run/cache paths.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
from pathlib import Path
import re
import sys

import pytest

from pipelines.gpu_prices.providers.pages import Document, NotSupported, ParseError, normalize_gpu


ROOT = Path(__file__).parent / "fixtures" / "provider_pages"
MANIFEST = json.loads((ROOT / "fetch_manifest.json").read_text(encoding="utf-8"))
FETCHED_AT = "2026-11-12T13:24:09Z"
SUPPORTED = ("featurize", "nebius", "lambda_", "paperspace", "baidu_gpu")
BLOCKED = ("runpod", "digitalocean_gpu", "salad", "thunder", "massedcompute")
ALL = SUPPORTED + BLOCKED
COUNTS = {"featurize": 11, "nebius": 14, "lambda_": 22,
          "paperspace": 13, "baidu_gpu": 31}
FIELDS = set("""provider product sku gpu_model_raw gpu_model gpu_count gpu_fraction
vram_raw vcpu ram_raw price_per_hour currency price_type price_scope is_from_price
tax_basis billing_unit minimum_billing_seconds effective_from effective_to
availability region source_url tier export_allowed""".split())


def module(name):
    return importlib.import_module("pipelines.gpu_prices.providers.pages." + name)


def fixture(name):
    return (ROOT / MANIFEST["providers"][name]["fixture"]).read_text(encoding="utf-8")


def parsed(name):
    return module(name).parse(fixture(name), FETCHED_AT)


def select(name, sku, kind="on_demand"):
    found = [r for r in parsed(name) if r["sku"] == sku and r["price_type"] == kind]
    assert len(found) == 1
    return found[0]


@pytest.mark.parametrize("name", ALL)
def test_fixture_identity_size_and_single_get(name):
    info = MANIFEST["providers"][name]
    data = (ROOT / info["fixture"]).read_bytes()
    assert len(data) == info["fixture_bytes"] < 200_000
    assert hashlib.sha256(data).hexdigest() == info["fixture_sha256"]
    assert re.fullmatch(r"[0-9a-f]{64}", info["sha256"])
    assert len(info["attempts"]) == 1
    assert info["attempts"][0]["status"] == (200 if name in SUPPORTED else 403)
    assert MANIFEST["request"] == {
        "method": "GET", "User-Agent": "Python-urllib/3.12", "Accept": "text/html",
        "cookies": False, "authentication": False,
    }
    assert module(name).URLS == (info["url"],)
    assert module(name).TIER == ("B" if name in {"featurize", "nebius", "lambda_"} else "C")


@pytest.mark.parametrize("name", ALL)
def test_fixture_rows_and_contract(name):
    if name in BLOCKED:
        pytest.skip("Official plain GET returned 403; no verified price fixture")
    rows = parsed(name)
    assert len(rows) == COUNTS[name] == MANIFEST["providers"][name]["row_count"]
    for row in rows:
        assert FIELDS <= row.keys()
        assert type(row["price_per_hour"]) is float
        assert row["price_per_hour"] > 0
        assert row["gpu_model"].startswith("NVIDIA ")
        assert row["gpu_model_raw"]
        assert row["price_type"] in {"on_demand", "spot", "interruptible", "reserved"}
        assert row["price_scope"] in {"instance", "per_gpu", "gpu_component"}
        assert row["tax_basis"] in {"excluded", "included", "unknown"}
        assert row["export_allowed"] is False
        assert row["tier"] == module(name).TIER
        assert row["region"] is None
        assert row["effective_to"] is None
        assert row["observed_at"] == FETCHED_AT
        assert row["minimum_billing_seconds"] is None
        assert row["source_url"] == module(name).URLS[0]
        if row["gpu_count"] is not None:
            assert type(row["gpu_count"]) is int and row["gpu_count"] > 0
        if name != "featurize":
            assert row["availability"] == "unknown"
            assert row["availability_observed_at"] is None
        json.dumps(row, allow_nan=False)


@pytest.mark.parametrize("name,sku,kind,expected", [
    ("featurize", "RTX 4090", "on_demand", {
        "gpu_model": "NVIDIA RTX 4090 25.2GB", "vram_raw": "25.2 GB",
        "price_per_hour": 1.87, "vcpu": 14, "ram_raw": "60.1 GB", "currency": "CNY",
        "availability": "unavailable", "available_gpu_count": 0, "tax_basis": "unknown",
    }),
    ("featurize", "RTX 4090 48G", "on_demand", {
        "gpu_model": "NVIDIA RTX 4090 48GB", "vram_raw": "48.0 GB", "price_per_hour": 3.0,
    }),
    ("featurize", "RTX 5090", "on_demand", {
        "gpu_model": "NVIDIA RTX 5090 32GB", "price_per_hour": 3.0,
        "availability": "available", "available_gpu_count": 1,
    }),
    ("nebius", "NVIDIA HGX H100", "on_demand", {
        "gpu_model": "NVIDIA HGX H100", "price_per_hour": 4.5,
        "vcpu": 16, "ram_raw": "200 GB", "price_scope": "gpu_component", "is_from_price": False,
    }),
    ("nebius", "NVIDIA HGX H100", "spot", {
        "price_per_hour": 0.79, "is_from_price": True, "billing_unit": "GPU-hour",
    }),
    ("nebius", "NVIDIA L40S with Intel CPU", "interruptible", {
        "gpu_model": "NVIDIA L40S", "price_per_hour": 0.74, "vcpu": None,
        "vcpu_raw": "8-40", "ram_raw": "32-160 GB", "is_from_price": True,
    }),
    ("lambda_", "1x NVIDIA H100 SXM 80 GB", "on_demand", {
        "gpu_model": "NVIDIA H100 SXM 80GB", "price_per_hour": 4.89,
        "gpu_count": 1, "vcpu": 26, "ram_raw": "225 GiB", "price_scope": "per_gpu",
    }),
    ("lambda_", "1x NVIDIA H100 PCIe 80 GB", "on_demand", {
        "gpu_model": "NVIDIA H100 PCIe 80GB", "price_per_hour": 3.79, "gpu_count": 1,
    }),
    ("lambda_", "8x NVIDIA HGX A100 40 GB", "on_demand", {
        "gpu_model": "NVIDIA HGX A100 40GB", "price_per_hour": 1.99, "gpu_count": 8,
    }),
    ("paperspace", "V100", "on_demand", {
        "gpu_model": "NVIDIA V100 16GB", "price_per_hour": 2.3,
        "vcpu": 8, "ram_raw": "30 GB", "vram_raw": "16 GB", "price_scope": "instance",
    }),
    ("paperspace", "H100", "reserved", {
        "gpu_model": "NVIDIA HGX H100", "price_per_hour": 2.24, "is_from_price": True,
        "vcpu": 20, "ram_raw": "256 GB", "vram_raw": None, "price_conditions": "3-year commitment",
    }),
    ("paperspace", "A100-80G", "reserved", {
        "gpu_model": "NVIDIA A100 80GB", "price_per_hour": 1.15, "is_from_price": True,
        "vram_raw": "80G", "vcpu": 12, "price_conditions": "3-year commitment",
    }),
    ("paperspace", "H100", "on_demand", {
        "price_per_hour": 5.95, "is_from_price": True, "price_conditions": "special promo price",
    }),
    ("baidu_gpu", "bcc.gn5.c14m120.1A100-80g", "on_demand", {
        "gpu_model": "NVIDIA A100 NVSwitch 80GB", "gpu_count": 1, "vram_raw": "80GB",
        "price_per_hour": 30.18, "vcpu": 14, "ram_raw": "120GB", "currency": "CNY",
    }),
    ("baidu_gpu", "bcc.gn5.c112m896.8A100-40g", "on_demand", {
        "gpu_model": "NVIDIA A100 NVSwitch 40GB", "gpu_count": 8,
        "vram_raw": "320GB", "price_per_hour": 208.67,
    }),
    ("baidu_gpu", "bcc.vgn3.c2m12.1v100-32g-8C", "on_demand", {
        "gpu_model": "NVIDIA V100 32GB", "gpu_count": None, "gpu_fraction": 0.25,
        "vram_raw": "8GB", "price_per_hour": 3.41,
    }),
    ("baidu_gpu", "bcc.vgn3.c10m40.1t4-8C", "on_demand", {
        "gpu_model": "NVIDIA T4 16GB", "gpu_count": None, "gpu_fraction": 0.5,
        "vram_raw": "8GB", "price_per_hour": 3.92,
    }),
])
def test_concrete_official_values(name, sku, kind, expected):
    row = select(name, sku, kind)
    assert {key: row[key] for key in expected} == expected


def test_future_effective_date_is_not_observation_date_or_current_price():
    rows = parsed("lambda_")
    assert {r["effective_from"] for r in rows} == {"2026-11-16T00:00:00Z"}
    assert all(r["effective_from"] > FETCHED_AT for r in rows)
    assert all(r["tax_basis"] == "excluded" for r in rows)
    assert all(r["product"] == "Instances" for r in rows)
    assert all(r["price_type"] == "on_demand" for r in rows)
    assert {r["gpu_count"] for r in rows} == {1, 2, 4, 8}
    # Cluster committed prices are present in this fixture but never emitted.
    assert "2 weeks" in fixture("lambda_")
    assert not any(r["price_per_hour"] == 9.86 for r in rows)


def test_no_count_inventory_tax_region_inference():
    for name in ("featurize", "nebius", "paperspace"):
        assert all(r["gpu_count"] is None and r["gpu_fraction"] is None for r in parsed(name))
    rows = parsed("featurize")
    assert all(r["tax_basis"] == "unknown" for r in rows)
    assert all(r["availability_observed_at"] == FETCHED_AT for r in rows)
    assert all(r["sku"] != "CPU" for r in rows)
    # Physical host identifiers/IPs from hydration must not be saved as fixtures.
    assert "internalIp" not in fixture("featurize")
    assert "__NEXT_DATA__" not in fixture("featurize")


def test_missing_explicit_inventory_is_unknown_despite_rent_buttons():
    html = re.sub(r"\d+ 卡可用", "库存未声明", fixture("featurize")).replace("已售罄", "预定")
    rows = module("featurize").parse(html, FETCHED_AT)
    assert all(r["availability"] == "unknown" for r in rows)
    assert all(r["availability_observed_at"] is None for r in rows)
    assert all(r["available_gpu_count"] is None for r in rows)


def test_nebius_spot_and_flat_preemptible_are_distinct():
    rows = parsed("nebius")
    assert all(r["tax_basis"] == "excluded" for r in rows)
    assert sum(r["price_type"] == "spot" for r in rows) == 5
    assert sum(r["price_type"] == "interruptible" for r in rows) == 2
    assert not any("GB300" in r["gpu_model"] or "GB200" in r["gpu_model"] for r in rows)
    assert all(r["is_from_price"] for r in rows if r["price_type"] == "spot")
    assert all(r["vram_raw"] is None for r in rows)


def test_paperspace_hidden_monthly_and_vdi_prices_are_excluded():
    rows = parsed("paperspace")
    assert 'data-w-tab="Monthly"' in fixture("paperspace")
    assert 'instance-price-2">3.09' in fixture("paperspace")
    assert all(r["price_per_hour"] != 3.09 for r in rows)
    assert all("x2" not in r["sku"] for r in rows)
    assert all(r["product"] == "GPU compute Hourly" for r in rows)
    assert {r["sku"] for r in rows if r["price_type"] == "reserved"} == {"H100", "A100-80G"}


def test_baidu_never_uses_prepaid_and_preserves_multigpu_row_prices():
    rows = parsed("baidu_gpu")
    assert max(r["price_per_hour"] for r in rows) == 241.43
    multi = select("baidu_gpu", "bcc.gn5.c112m476.4a10")
    assert (multi["price_per_hour"], multi["gpu_count"], multi["vcpu"], multi["ram_raw"]) == (44.18, 4, 112, "476GB")
    assert all(r["price_type"] == "on_demand" and r["tax_basis"] == "unknown" for r in rows)


def test_baidu_locates_postpaid_column_by_header():
    html = fixture("baidu_gpu")
    # Swap the GN5 hourly/monthly columns, preserving their explicit headers.
    def swap_row(found):
        text = found.group(0)
        cells = re.findall(r"<t[dh]\b[^>]*>.*?</t[dh]>", text, re.S)
        cells[5], cells[6] = cells[6], cells[5]
        return "<tr>" + "".join(cells) + "</tr>"
    first = re.search(r"<table>.*?</table>", html, re.S)
    swapped = re.sub(r"<tr>.*?</tr>", swap_row, first.group(0), flags=re.S)
    changed = html[:first.start()] + swapped + html[first.end():]
    assert module("baidu_gpu").parse(changed, FETCHED_AT) == parsed("baidu_gpu")


@pytest.mark.parametrize("name,old,new", [
    ("featurize", "按量使用", "预付费"),
    ("featurize", "¥", "$"),
    ("nebius", "On-demand, GPU-hour", "On-demand, month"),
    ("nebius", "from $0.99", "price unavailable"),
    ("nebius", "NVIDIA L40S preemptible instances are excluded from spot pricing", "REMOVED"),
    ("lambda_", "November 16, 2026", "December 1, 2026"),
    ("lambda_", "PRICE/GPU/HR*", "PRICE/INSTANCE/MONTH*"),
    ("lambda_", '$7.69', 'Contact us'),
    ("lambda_", '$7.69', '€7.69'),
    ("paperspace", "3-year commitment", "1-year commitment"),
    ("paperspace", '2.30</h1>', 'quote</h1>'),
    ("paperspace", "On-demand pricing for H100 is", "REMOVED"),
    ("baidu_gpu", "后付费", "预付费"),
    ("baidu_gpu", '<td>30.18</td>', '<td></td>'),
    ("baidu_gpu", '1*Nvidia A100 Nvswitch-80g', 'Nvidia A100 Nvswitch-80g'),
    ("baidu_gpu", '1/4 * NVIDIA Tesla V100', '3/2 * NVIDIA Tesla V100'),
])
def test_structure_or_price_drift_fails_loudly(name, old, new):
    html = fixture(name)
    assert old in html
    changed = html.replace(old, new)
    with pytest.raises(ParseError):
        module(name).parse(changed, FETCHED_AT)


@pytest.mark.parametrize("name", SUPPORTED)
@pytest.mark.parametrize("bad_html", ["", "<html><h1>Pricing</h1></html>", "<div>Loading</div>"])
def test_missing_structure_does_not_return_empty_or_guessed_rows(name, bad_html):
    with pytest.raises(ParseError):
        module(name).parse(bad_html, FETCHED_AT)


@pytest.mark.parametrize("name", SUPPORTED)
def test_truncated_price_section_fails(name):
    html = fixture(name)
    doc = Document(html)
    if name == "featurize":
        scope = next(n for n in doc.root.find(tag="div")
                     if any(c.startswith("host-machine_root__") for c in n.attrs.get("class", "").split()))
    elif name == "nebius":
        scope = doc.root.find(cls="pc-highlight-table-block__root")[0]
    elif name == "lambda_":
        scope = next(n for n in doc.root.find(tag="section")
                     if any(h.text() == "Instances pricing" for h in n.find(tag="h2")))
    elif name == "paperspace":
        scope = next(n for n in doc.root.walk() if n.attrs.get("data-w-tab") == "Hourly"
                     and n.has_class("w-tab-pane"))
    else:
        scope = doc.root.find(tag="table")[0]
    with pytest.raises(ParseError):
        module(name).parse(html[:scope.end - 1], FETCHED_AT)


@pytest.mark.parametrize("name", SUPPORTED)
def test_duplicate_sections_fail_instead_of_double_counting(name):
    html = fixture(name)
    with pytest.raises(ParseError):
        module(name).parse(html + html, FETCHED_AT)


@pytest.mark.parametrize("name", SUPPORTED)
def test_invalid_observation_timestamp_fails(name):
    with pytest.raises(ParseError):
        module(name).parse(fixture(name), "2026-11-12")


@pytest.mark.parametrize("name", BLOCKED)
def test_blocked_response_is_explicitly_unsupported(name):
    assert MANIFEST["providers"][name]["outcome"] == "blocked"
    with pytest.raises(NotSupported, match="blocked"):
        module(name).parse(fixture(name), FETCHED_AT)
    # Spec examples must not be used to accept an invented HTML format.
    with pytest.raises(ParseError):
        module(name).parse("<table><tr><td>H100</td><td>$1.00/h</td></tr></table>", FETCHED_AT)


@pytest.mark.parametrize("first,second", [
    ("NVIDIA H100 PCIe", "NVIDIA H100 SXM"),
    ("NVIDIA H100 SXM", "NVIDIA H100 NVL"),
    ("NVIDIA A100 40GB", "NVIDIA A100 80GB"),
    ("RTX 4090 24G", "RTX 4090 48G"),
    ("NVIDIA B200 SXM6", "NVIDIA HGX B200"),
])
def test_normalization_never_merges_variants(first, second):
    assert normalize_gpu(first) != normalize_gpu(second)


def test_runtime_modules_only_import_stdlib_or_this_package():
    for name in ("__init__",) + ALL:
        path = Path(module("featurize").__file__).parent / (name + ".py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                assert all(alias.name.partition(".")[0] in sys.stdlib_module_names for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                assert node.module.partition(".")[0] in sys.stdlib_module_names
