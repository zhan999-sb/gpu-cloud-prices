"""Hourly Clore collection through the same persisted budget as page sources."""

from ..http import HttpClient
from ..schema import utc_now
from . import clore
from .page_collector import normalize


class CloreCollector:
    def __init__(self, *, store, client=None, clock=utc_now):
        self.store, self.client, self.clock = store, client or HttpClient(), clock
        self.pages_fetched = self.raw_items = 0
        self.complete = self.skipped = False

    def collect(self, *, limit_pages=None):
        if limit_pages is not None and (type(limit_pages) is not int or limit_pages < 1):
            raise ValueError('limit_pages must be positive')
        self.pages_fetched = self.raw_items = 0
        self.complete = self.skipped = False
        stamp = self.clock()
        if not self.store.claim_page_fetch('clore', stamp):
            self.skipped = True
            return []
        payload = self.client.get_json(clore.API)
        self.pages_fetched = 1
        self.raw_items = len(payload.get('servers', []))
        offers = [normalize(row) for row in clore.parse(payload, stamp)]
        counts, retained = {}, []
        for offer in sorted(offers, key=lambda o: (o.price_per_gpu_hour, o.sku, o.price_type)):
            key = offer.gpu_model, offer.gpu_count, offer.price_type, offer.region
            if counts.get(key, 0) < 5:
                retained.append(offer)
                counts[key] = counts.get(key, 0) + 1
        self.complete = True
        return retained
