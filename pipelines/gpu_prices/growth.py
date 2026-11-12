"""Offline SQLite growth estimate: python -m pipelines.gpu_prices.growth [--db DB].

The fixture stress case expands Clore's 28 fixture records to the full response
count recorded by p4 (2,112), treating EVERY expanded offer as a different group.
It deliberately gives no credit for the top-five-to-top-one history reduction.
An optional real DB population estimates all providers at their observed sizes.
Thirty daily snapshots are inserted into an actual SQLite database in memory;
all 96 scheduled runs/day share the bounded current tables. No files are changed.
"""

import argparse
from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path

from .providers import clore
from .providers.page_collector import PARSERS, normalize
from .store import PriceStore, history_minima, pack_rows


def estimate(offers, *, days=30):
    rows = []
    for offer in offers:
        row = offer.to_dict()
        row.update(day=offer.day, region=offer.region or '')
        rows.append(row)
    groups = {}
    for row in history_minima(rows):
        groups.setdefault(row['provider'], []).append(row)
    # Same table/page/index layout as production, including bounded current rows.
    with PriceStore(':memory:') as store:
        store.upsert(offers)
        conn = store.connection
        conn.execute('VACUUM')
        size = lambda: conn.execute('PRAGMA page_count').fetchone()[0] * conn.execute('PRAGMA page_size').fetchone()[0]
        before = size()
        compressed = {}
        for offset in range(days):
            day = (date(2026, 1, 1) + timedelta(days=offset)).isoformat()
            for provider, source in groups.items():
                sample = []
                for original in source:
                    row = dict(original)
                    row.update(day=day, collected_at=day+'T23:45:00.000000Z')
                    # Deterministic daily drift avoids an identical-price-only case.
                    for key in ('price_per_day','price_per_hour','price_per_gpu_hour','original_price'):
                        if row.get(key) is not None:
                            row[key] *= 1 + offset / 1000
                    sample.append(row)
                payload = pack_rows(sample)
                compressed[provider] = max(compressed.get(provider, 0), len(payload))
                conn.execute('INSERT INTO history_snapshots VALUES (?,?,?,?)', (provider, day, len(sample), payload))
        conn.commit()
        return {'days': days, 'scheduled_runs': days*96, 'current_records': len(rows),
                'history_points_per_day': sum(map(len, groups.values())),
                'compressed_bytes_per_day': compressed, 'database_bytes_before': before,
                'database_bytes_after': size(), 'monthly_growth_bytes': size()-before,
                'monthly_growth_mb': round((size()-before)/1_000_000, 4),
                'under_5_mb': size()-before < 5_000_000}


def fixture_offers():
    root = Path(__file__).resolve().parents[2] / 'tests' / 'fixtures'
    stamp = '2026-11-12T15:27:54Z'
    offers = []
    for name, parser in PARSERS.items():
        folder = 'new_sources' if name in {'anygpu','ai_galaxy'} else 'provider_pages'
        path = root / folder / (name+'.html')
        if not path.exists() and name == 'lambda':
            path = root / folder / 'lambda_.html'
        offers.extend(normalize(r) for r in parser.parse(path.read_text(encoding='utf-8'), stamp))
    folder = root / 'new_sources'
    sample = [normalize(r) for r in clore.parse(json.loads((folder/'clore_marketplace.json').read_text(encoding='utf-8')), stamp)]
    count = json.loads((folder/'clore_marketplace.live_summary.json').read_text(encoding='utf-8'))['record_count']
    offers.extend(replace(sample[i % len(sample)], sku='growth-'+str(i), region=(sample[i % len(sample)].region or 'unknown')+'-growth-'+str(i)) for i in range(count))
    return offers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path)
    args = parser.parse_args()
    result = {'fixture_stress': estimate(fixture_offers())}
    if args.db:
        with PriceStore(args.db, read_only=True) as store:
            # Latest full listings for each provider, including failed/stale ones:
            # do not understate storage by excluding unhealthy provider snapshots.
            rows = [dict(r) for r in store.connection.execute('SELECT * FROM offers')]
            from .schema import Offer
            result['observed_population'] = estimate([Offer(**{k: bool(r[k]) if k in {'is_from_price','export_allowed'}
                else (r[k] or None) if k=='region' else r[k] for k in Offer.__dataclass_fields__}) for r in rows])
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
