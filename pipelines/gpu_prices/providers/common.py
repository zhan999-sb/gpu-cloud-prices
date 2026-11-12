"""Shared strict, unauthenticated catalog pagination and hourly normalization."""
from decimal import Decimal
import time
from urllib.parse import urlencode

from ..http import HttpClient, validate_url
from ..schema import utc_now


def number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("missing numeric field")
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError("nonpositive numeric field")
    return float(result)


class CatalogCollector:
    def __init__(self, *, client=None, clock=utc_now, sleep=time.sleep):
        self.client = client or HttpClient()
        self.clock, self.sleep = clock, sleep
        self.pages_fetched = self.raw_items = 0
        self.complete = False

    def get(self, url):
        validate_url(url)
        if self.pages_fetched:
            self.sleep(1)
        result = self.client.get_json(url)
        self.pages_fetched += 1
        return result

    def pages(self, endpoint, key, *, size=1000, limit_pages=None, linode=False):
        page, seen, total = 1, 0, None
        while True:
            payload = self.get(endpoint + "?" + urlencode({"page": page, "page_size": size}))
            if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
                raise ValueError("catalog response shape changed")
            rows = payload[key]
            if any(not isinstance(row, dict) for row in rows):
                raise ValueError("catalog rows must be objects")
            target = payload.get("results" if linode else "total_count")
            if type(target) is not int or target < 0 or (total is not None and target != total):
                raise ValueError("missing or changing catalog total")
            total = target
            if linode and (payload.get("page") != page or type(payload.get("pages")) is not int):
                raise ValueError("invalid page envelope")
            seen += len(rows)
            self.raw_items += len(rows)
            yield rows
            if seen == total:
                return
            if not rows or seen > total:
                raise ValueError("inconsistent catalog pagination")
            if limit_pages and page >= limit_pages:
                return
            page += 1


def unique(offers):
    keys = set()
    for offer in offers:
        key = (offer.sku, offer.region, offer.price_type)
        if key in keys:
            raise ValueError("duplicate catalog offer key")
        keys.add(key)
    return offers
