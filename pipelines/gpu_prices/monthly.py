"""Persist monthly-only quotes outside hourly offers, exports and rankings."""
import json
from .http import HttpClient
from .providers.pages import cloudclusters
from .schema import utc_now, parse_utc


def monthly_quotes(connection, built_at):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='monthly_quotes'").fetchone():
        return []
    built_at = parse_utc(built_at).isoformat(timespec='microseconds').replace('+00:00','Z')
    rows = connection.execute('SELECT payload FROM monthly_quotes WHERE collected_at=(SELECT MAX(collected_at) FROM monthly_quotes WHERE collected_at<=?) ORDER BY sku', (built_at,))
    return [json.loads(r[0]) for r in rows]


class MonthlyCollector:
    name = 'cloudclusters'

    def __init__(self, *, store, client=None, clock=utc_now):
        self.store, self.client, self.clock = store, client or HttpClient(), clock
        self.pages_fetched = self.raw_items = 0
        self.complete = self.skipped = False
        self.quotes = []

    def collect(self, *, limit_pages=None):
        if limit_pages is not None and (type(limit_pages) is not int or limit_pages < 1):
            raise ValueError('limit_pages must be positive')
        self.pages_fetched = self.raw_items = 0
        self.complete = self.skipped = False
        self.quotes = []
        stamp = self.clock()
        if not self.store.claim_page_fetch(self.name, stamp):
            self.skipped = True
            return []
        html = self.client.get_bytes(cloudclusters.URLS[0], accept='text/html').decode('utf-8')
        self.pages_fetched = 1
        self.quotes = cloudclusters.parse(html, stamp)
        self.raw_items = len(self.quotes)
        self.complete = True
        return []

    def save(self):
        if not self.complete:
            raise ValueError('Cannot save an incomplete monthly batch')
        conn = self.store.connection
        with conn:
            conn.execute('CREATE TABLE IF NOT EXISTS monthly_quotes (provider TEXT NOT NULL, sku TEXT NOT NULL, collected_at TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(provider,sku,collected_at))')
            for row in self.quotes:
                conn.execute('INSERT OR REPLACE INTO monthly_quotes VALUES (?,?,?,?)',
                    (row['provider'], row['sku'], parse_utc(row['collected_at']).isoformat(timespec='microseconds').replace('+00:00','Z'), json.dumps(row, sort_keys=True)))
            conn.execute("DELETE FROM meta WHERE key='saved_fingerprint'")
        return len(self.quotes)
