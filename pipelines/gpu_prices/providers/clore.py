"""Clore's public marketplace, with whole-server daily prices normalized to hours.

Platform service fees may apply. Tax treatment has not been confirmed, so it is
unknown. USD-Blockchain is a stablecoin-denominated USD quote, not a fiat FX
conversion. ``price.usd`` is only a fallback and is labelled as Clore's own
conversion. The live 2026-11-12 envelope is {code: 0, servers: [...]}.

This pure adapter returns page-record dictionaries. The registered
CloreCollector uses the shared allowlisted HTTP transport and DB hourly budget.
The standalone collect(fetch=callable) helper is retained for fixture tooling.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
import json
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .common import number
from .pages import record, require
from .pages.anygpu import gpu_fields
from ..models import canonical_model
from ..clore_auth import auth_headers, CloreRequestError

API = "https://api.clore.ai/v1/marketplace"
URLS = (API,)
SOURCE_URL = "https://clore.ai/marketplace"
TIER = "A"
TIMEOUT = 120
ATTEMPTS = 3


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Clore marketplace redirects are refused")


def _retry_after(value):
    if not value:
        return 0.0
    try:
        return max(0.0, float(int(value)))
    except (ValueError, OverflowError):
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0.0


def fetch_json(url):
    """GET with optional auth header, 120s timeout; transient retries only."""
    if url != API:
        raise ValueError("Only the official public Clore marketplace is allowed")
    opener = build_opener(_NoRedirects())
    credentials = auth_headers(url)
    for attempt in range(ATTEMPTS):
        delay = 2 ** (attempt + 1)
        try:
            request = Request(url, headers={"Accept": "application/json",
                              "User-Agent": "gpu-price-index/0.1", **credentials}, method="GET")
            with opener.open(request, timeout=TIMEOUT) as response:
                data = response.read()
            return json.loads(data.decode("utf-8"))
        except HTTPError as exc:
            if exc.code != 429 and not 500 <= exc.code < 600:
                status = exc.code
                exc.close()
                raise CloreRequestError(status, bool(credentials)) from None
            delay = max(delay, _retry_after(exc.headers.get("Retry-After") if exc.headers else None))
            if attempt + 1 == ATTEMPTS:
                status = exc.code
                exc.close()
                raise CloreRequestError(status, bool(credentials)) from None
            exc.close()
        except (URLError, TimeoutError, ConnectionError, IncompleteRead):
            if attempt + 1 == ATTEMPTS:
                raise CloreRequestError(0, bool(credentials)) from None
        except (ValueError, OSError):
            raise CloreRequestError(0, bool(credentials)) from None
        except Exception:
            raise CloreRequestError(0, bool(credentials)) from None
        time.sleep(delay)
    raise AssertionError("unreachable")


def _positive(value):
    try:
        result = number(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, ArithmeticError):
        return None


def _daily_price(prices, kind):
    direct = prices.get(kind)
    if isinstance(direct, dict) and direct.get("USD-Blockchain") is not None:
        # A present but invalid direct quote must not be rescued by an unrelated
        # conversion. Bitcoin and CLORE amounts are never mistaken for dollars.
        return _positive(direct["USD-Blockchain"]), "USD-Blockchain"
    converted = prices.get("usd")
    if not isinstance(converted, dict):
        return None, None
    keys = ("on_demand_usd", "on_demand_clore", "on_demand_btc") if kind == "on_demand" else ("spot",)
    for key in keys:
        price = _positive(converted.get(key))
        if price is not None:
            return price, "price.usd." + key
    return None, None


def _server_rows(server, stamp):
    if not isinstance(server, dict) or server.get("rented") is not False:
        return []
    specs, prices = server.get("specs"), server.get("price")
    if not isinstance(specs, dict) or not isinstance(prices, dict):
        return []
    raw = specs.get("gpu")
    gpu = re.fullmatch(r"\s*([1-9]\d*)\s*[x×]\s*(\S.*?)\s*", raw) if isinstance(raw, str) else None
    if not gpu or re.search(r"mixed|unknown|no gpu|^none$|^n/?a$", gpu.group(2), re.I):
        return []
    count, model = int(gpu.group(1)), gpu.group(2)
    if not re.search(r"[A-Za-z].*\d|\d.*[A-Za-z]", model):
        return []
    partial = server.get("partial_gpu_rental")
    available = total = None
    if isinstance(partial, dict):
        available, total = partial.get("available_gpus"), partial.get("total_gpus")
        # Whole-server pricing cannot be assigned to a partially occupied server.
        if type(available) is not int or type(total) is not int or total != count or available != total:
            return []
    ident = server.get("id")
    if isinstance(ident, bool) or not isinstance(ident, (str, int)) or not str(ident).strip():
        return []
    net = specs.get("net")
    country = net.get("cc") if isinstance(net, dict) else None
    country = country.upper() if isinstance(country, str) and re.fullmatch(r"[A-Za-z]{2}", country) else None
    capacity = _positive(specs.get("gpuram"))
    vram = f"{capacity:g} GB" if capacity is not None else None
    ram = _positive(specs.get("ram"))
    fields = gpu_fields(model, vram)
    canonical = canonical_model(fields['gpu_model'], vram)
    if canonical != fields['gpu_model']:
        fields.update(gpu_model=canonical, flags=[])
    rows = []
    for kind in ("on_demand", "spot"):
        daily, basis = _daily_price(prices, kind)
        if daily is None or daily / 24 / count > 100:
            continue
        notes = ["Listed whole-server daily price divided by 24 and GPU count.",
                 "Platform service fees may apply; tax treatment unknown."]
        if basis == "USD-Blockchain":
            notes.append("Stablecoin-denominated USD price (USD-Blockchain).")
        else:
            notes.append("Clore's own USD conversion; not independently verified FX.")
        rows.append(record(
            "clore", TIER, SOURCE_URL, stamp, **fields,
            export_allowed=True, checked_at=stamp, collected_at=stamp,
            product="Clore marketplace server", sku=str(ident),
            gpu_count=count, gpu_fraction=1.0, vram_raw=vram,
            cpu_raw=specs.get("cpus"), cpu_model_raw=specs.get("cpu"),
            ram_raw=f"{ram:g} GB" if ram is not None else None,
            region=country, region_country=country, zone=None,
            price_type=kind, currency="USD", original_currency="USD",
            price_per_day=daily, original_price=daily / 24, source_billing_unit="instance-day",
            price_per_hour=daily / 24, price_per_gpu_hour=daily / 24 / count,
            price_scope="instance", billing_unit="instance-hour",
            price_basis=basis, availability="available",
            availability_observed_at=stamp, available_gpu_count=available,
            total_gpu_count=total, rent_url=SOURCE_URL, notes=notes,
        ))
    return rows


def parse(payload: dict, fetched_at: str) -> list[dict]:
    require(isinstance(payload, dict) and payload.get("code", 0) == 0
            and isinstance(payload.get("servers"), list), "Invalid Clore marketplace envelope")
    # Validate the timestamp even when every server is rented or unsupported.
    record("clore", TIER, SOURCE_URL, fetched_at)
    candidates = [row for server in payload["servers"] for row in _server_rows(server, fetched_at)]
    candidates.sort(key=lambda r: (r["price_per_gpu_hour"], r["sku"], r["price_type"]))
    result, counts, seen = [], {}, set()
    for row in candidates:
        identity = (row["sku"], row["price_type"])
        if identity in seen:
            continue
        seen.add(identity)
        # Cap the actual output model/count/type/country group, including when
        # unreviewed raw names have differing reported VRAM capacities.
        key = (row["gpu_model"], row["gpu_count"], row["price_type"], row["region"])
        if counts.get(key, 0) < 5:
            counts[key] = counts.get(key, 0) + 1
            result.append(row)
    return result


def collect(fetch=None, *, checked_at=None) -> list[dict]:
    payload = (fetch or fetch_json)(API)
    stamp = checked_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return parse(payload, stamp)
